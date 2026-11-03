import { createServer, type Server } from 'node:http';
import { expect, test, type Page, type Request, type Route } from '@playwright/test';

type AcceptanceDiagnostics = {
  apiFailures: string[];
  apiActivity: string[];
  failedRequests: string[];
  externalRequests: string[];
  blockedOffOriginManifestRequests: string[];
  offOriginManifestResponses: number[];
  manifestFetches: string[];
  redirectProbeResponses: number[];
  pageErrors: string[];
  pendingApiFailureDetails: Promise<void>[];
  releaseHeldManifestResponse: () => void;
  closeRedirectProbeServers: () => Promise<void>;
  requestStartedAt: Map<Request, number>;
};

const diagnosticsByPage = new WeakMap<Page, AcceptanceDiagnostics>();

async function fetchWithoutFollowingRedirects(route: Route) {
  return route.fetch({ maxRedirects: 0 });
}

function listenLoopback(server: Server): Promise<number> {
  return new Promise((resolve, reject) => {
    const onError = (error: Error) => reject(error);
    server.once('error', onError);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', onError);
      const address = server.address();
      if (!address || typeof address === 'string') {
        reject(new Error('Redirect probe server did not bind a TCP port.'));
        return;
      }
      resolve(address.port);
    });
  });
}

function closeServer(server: Server): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) reject(error);
      else resolve();
    });
    server.closeAllConnections();
  });
}

function requiredCredential(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing local acceptance credential variable: ${name}`);
  return value;
}

async function signIn(page: Page, emailName: string, passwordName: string) {
  await page.getByLabel('Email').fill(requiredCredential(emailName));
  await page.getByLabel('Password').fill(requiredCredential(passwordName));
  await page.getByRole('button', { name: 'Sign in' }).click();
}

async function signOut(page: Page) {
  await page.getByRole('button', { name: 'Log out' }).click();
  await page.getByRole('dialog')
    .getByRole('button', { name: 'Log out and clear local data' })
    .click();
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
}

function redactGeneratedCredentials(message: string): string {
  const credentials = Object.entries(process.env)
    .filter(([name, value]) => value && (name.endsWith('_EMAIL') || name.endsWith('_PASSWORD')))
    .map(([, value]) => value as string)
    .sort((left, right) => right.length - left.length);
  return credentials.reduce((redacted, credential) => redacted.replaceAll(credential, '[redacted]'), message);
}

test.afterEach(async ({ page }, testInfo) => {
  const diagnostics = diagnosticsByPage.get(page);
  if (!diagnostics) return;
  diagnostics.releaseHeldManifestResponse();
  await diagnostics.closeRedirectProbeServers();
  if (testInfo.status === testInfo.expectedStatus) return;

  await Promise.all(diagnostics.pendingApiFailureDetails);
  const visibleAlerts = await page.getByRole('alert').allTextContents().catch(() => []);
  const messages = [
    ...diagnostics.apiFailures,
    ...diagnostics.failedRequests,
    ...diagnostics.externalRequests,
    ...diagnostics.pageErrors,
    ...visibleAlerts.map((message) => `UI alert: ${message.trim()}`),
  ].filter(Boolean).map(redactGeneratedCredentials);
  messages.push(...diagnostics.apiActivity.map(redactGeneratedCredentials));
  const uiText = await page.locator('main').innerText().catch(() => 'Unavailable');
  messages.push(
    redactGeneratedCredentials(`UI text at failure: ${uiText.replaceAll(/\s+/g, ' ').slice(0, 2500)}`),
  );
  if (messages.length > 0) {
    console.error(`Local acceptance diagnostics:\n${messages.join('\n')}`);
  }
});

test('real Sender → Courier → Lab workflow reaches server completion', async ({ page }) => {
  test.setTimeout(90_000);
  const diagnostics: AcceptanceDiagnostics = {
    apiFailures: [],
    apiActivity: [],
    failedRequests: [],
    externalRequests: [],
    blockedOffOriginManifestRequests: [],
    offOriginManifestResponses: [],
    manifestFetches: [],
    redirectProbeResponses: [],
    pageErrors: [],
    pendingApiFailureDetails: [],
    releaseHeldManifestResponse: () => {},
    closeRedirectProbeServers: async () => {},
    requestStartedAt: new Map(),
  };
  diagnosticsByPage.set(page, diagnostics);
  const expectedOrigin = `http://127.0.0.1:${process.env.LABTRACK_ACCEPTANCE_WEB_PORT ?? '18417'}`;
  const apiProxyTarget = process.env.API_PROXY_TARGET;
  if (!apiProxyTarget) throw new Error('Missing local API proxy target for off-origin blocking probe.');
  const offOriginManifestProbeUrl = new URL(
    '/api/v1/shipments/00000000-0000-0000-0000-000000000000/manifest',
    apiProxyTarget,
  );
  if (offOriginManifestProbeUrl.origin === expectedOrigin) {
    throw new Error('Off-origin manifest probe must use a distinct loopback origin.');
  }
  const offOriginManifestRequest =
    `GET ${offOriginManifestProbeUrl.origin}${offOriginManifestProbeUrl.pathname}`;

  let redirectTargetRequests = 0;
  let redirectTargetUrl = new URL('http://127.0.0.1:1/followed');
  const redirectTargetServer = createServer((_request, response) => {
    redirectTargetRequests += 1;
    response.end('redirect target reached');
  });
  const redirectSourceServer = createServer((_request, response) => {
    response.writeHead(302, { Location: redirectTargetUrl.href });
    response.end();
  });
  diagnostics.closeRedirectProbeServers = async () => {
    await Promise.all([closeServer(redirectSourceServer), closeServer(redirectTargetServer)]);
  };
  const redirectTargetPort = await listenLoopback(redirectTargetServer);
  redirectTargetUrl = new URL(`http://127.0.0.1:${redirectTargetPort}/followed`);
  const redirectSourcePort = await listenLoopback(redirectSourceServer);
  const redirectProbeUrl = `http://127.0.0.1:${redirectSourcePort}/redirect-probe`;
  const redirectTargetRequest = `GET ${redirectTargetUrl.origin}${redirectTargetUrl.pathname}`;

  // Keep browser traffic on Vite. Local API requests continue unchanged through
  // the configured proxy; off-origin requests are blocked before they can leave.
  await page.route('**/*', async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.origin !== expectedOrigin) {
      diagnostics.externalRequests.push(
        `${route.request().method()} ${requestUrl.origin}${requestUrl.pathname}`,
      );
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });
  let notifyFirstManifestResponseReady = () => {};
  let releaseFirstManifestResponse = () => {};
  const firstManifestResponseReady = new Promise<void>((resolve) => {
    notifyFirstManifestResponseReady = resolve;
  });
  const releaseManifestResponse = new Promise<void>((resolve) => {
    releaseFirstManifestResponse = resolve;
  });
  diagnostics.releaseHeldManifestResponse = releaseFirstManifestResponse;
  let firstManifestResponseHeld = false;
  await page.route('**/api/v1/shipments/*/manifest', async (route) => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    if (requestUrl.origin !== expectedOrigin) {
      const description = `${request.method()} ${requestUrl.origin}${requestUrl.pathname}`;
      diagnostics.externalRequests.push(description);
      diagnostics.blockedOffOriginManifestRequests.push(description);
      await route.abort('blockedbyclient');
      return;
    }
    diagnostics.manifestFetches.push(
      `${request.method()} ${requestUrl.origin}${requestUrl.pathname}`,
    );
    const response = await fetchWithoutFollowingRedirects(route);
    if (response.status() >= 300 && response.status() < 400) {
      diagnostics.apiFailures.push(
        `${request.method()} ${requestUrl.pathname} returned unexpected redirect ${response.status()}`,
      );
      await route.abort('blockedbyclient');
      return;
    }
    if (requestUrl.origin === expectedOrigin && !firstManifestResponseHeld) {
      firstManifestResponseHeld = true;
      notifyFirstManifestResponseReady();
      await releaseManifestResponse;
    }
    await route.fulfill({ response });
  });
  await page.route(redirectProbeUrl, async (route) => {
    const response = await fetchWithoutFollowingRedirects(route);
    diagnostics.redirectProbeResponses.push(response.status());
    if (response.status() >= 300 && response.status() < 400) {
      await route.abort('blockedbyclient');
      return;
    }
    await route.fulfill({ response });
  });
  page.on('pageerror', (error) => diagnostics.pageErrors.push(redactGeneratedCredentials(error.message)));
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/v1')) {
      diagnostics.requestStartedAt.set(request, Date.now());
      diagnostics.apiActivity.push(`→ ${request.method()} ${url.pathname}`);
    }
  });
  page.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText ?? 'unknown network failure';
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/v1')) {
      diagnostics.requestStartedAt.delete(request);
      diagnostics.apiActivity.push(`× ${request.method()} ${url.pathname}: ${failure}`);
      if (
        request.url() !== offOriginManifestProbeUrl.href
        && !['net::ERR_ABORTED', 'NS_BINDING_ABORTED'].includes(failure)
      ) {
        diagnostics.failedRequests.push(`${request.method()} ${url.pathname} failed: ${failure}`);
      }
    }
  });
  page.on('response', (response) => {
    if (response.url() === offOriginManifestProbeUrl.href) {
      diagnostics.offOriginManifestResponses.push(response.status());
    }
    const responseUrl = new URL(response.url());
    if (responseUrl.pathname.startsWith('/api/v1')) {
      const request = response.request();
      const startedAt = diagnostics.requestStartedAt.get(request);
      diagnostics.requestStartedAt.delete(request);
      const elapsed = startedAt === undefined ? '' : ` in ${Date.now() - startedAt}ms`;
      diagnostics.apiActivity.push(
        `← ${response.status()} ${request.method()} ${responseUrl.pathname}${elapsed}`,
      );
      if (response.status() >= 400) {
        diagnostics.pendingApiFailureDetails.push((async () => {
          const body = await response.text().catch(() => 'Response body unavailable');
          diagnostics.apiFailures.push(
            redactGeneratedCredentials(
              `${request.method()} ${responseUrl.pathname} returned ${response.status()}: ${body}`,
            ),
          );
        })());
      }
    }
  });

  await page.goto('/');
  const offOriginProbeResult = await page.evaluate(async (url) => {
    try {
      const response = await fetch(url);
      return `response:${response.status}`;
    } catch {
      return 'blocked';
    }
  }, offOriginManifestProbeUrl.href);
  expect(offOriginProbeResult).toBe('blocked');
  expect(diagnostics.blockedOffOriginManifestRequests).toEqual([offOriginManifestRequest]);
  expect(diagnostics.manifestFetches).toEqual([]);
  expect(diagnostics.offOriginManifestResponses).toEqual([]);

  await page.evaluate((url) => {
    const probeFrame = document.createElement('iframe');
    probeFrame.title = 'local redirect guard probe';
    probeFrame.src = url;
    document.body.append(probeFrame);
  }, redirectProbeUrl);
  await expect.poll(() => diagnostics.redirectProbeResponses).toEqual([302]);
  expect(diagnostics.redirectProbeResponses).toEqual([302]);
  expect(redirectTargetRequests).toBe(0);

  await signIn(page, 'LABTRACK_LOCAL_SENDER_EMAIL', 'LABTRACK_LOCAL_SENDER_PASSWORD');
  await expect(page.getByRole('heading', { name: 'Sender workspace' })).toBeVisible();

  await page.getByLabel('Material type').selectOption('BLOOD');
  await page.getByRole('button', { name: 'Create sample' }).click();
  const sampleRow = page.locator('.sender-resource-list li').filter({ hasText: 'BLOOD' }).first();
  await expect(sampleRow).toBeVisible();
  const sampleCode = (await sampleRow.locator('strong').innerText()).trim();

  const expectedDelivery = new Date(Date.now() + 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 16);
  await page.getByLabel('Sender organization').selectOption({ label: 'Local Synthetic Sender' });
  await page.getByLabel('Destination laboratory').selectOption({ label: 'Центральная лаборатория' });
  await page.getByLabel('Expected delivery').fill(expectedDelivery);
  await page.getByRole('button', { name: 'Create draft Shipment' }).click();
  await expect(page.getByRole('heading', { name: /^Shipment / })).toBeVisible();
  const shipmentId = (await page.locator('.sender-shipment-id').innerText()).trim();
  const shipmentCode = (await page.getByRole('heading', { name: /^Shipment / }).innerText())
    .replace(/^Shipment\s+/, '')
    .trim();
  expect(shipmentId).toMatch(/^[0-9a-f-]{36}$/i);

  await page.getByLabel('Courier').selectOption({ label: 'Stage I Courier' });
  await page.getByRole('button', { name: 'Assign Courier' }).click();
  await page.getByRole('button', { name: 'Add container' }).click();
  const containerRow = page.locator('.sender-container-list li').first();
  await expect(containerRow).toBeVisible();
  const containerCode = (await containerRow.locator('strong').innerText()).trim();
  await page.getByLabel('Sample to add').selectOption({ label: `${sampleCode} · BLOOD` });
  await page.getByRole('button', { name: 'Add sample to container' }).click();
  await page.getByRole('button', { name: 'Seal Shipment' }).click();
  await expect(page.getByText('Готово к забору', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Release Pickup' }).click();
  await expect(page.getByText('Ожидает забора', { exact: true })).toBeVisible();

  await signOut(page);
  await signIn(page, 'LABTRACK_LOCAL_COURIER_EMAIL', 'LABTRACK_LOCAL_COURIER_PASSWORD');
  await expect(page.getByRole('heading', { name: 'Welcome, Stage I Courier' })).toBeVisible();
  await page.getByLabel('Shipment ID').fill(shipmentId);
  await page.getByRole('button', { name: 'Load assigned Shipment' }).click();
  await expect(page.getByRole('list', { name: 'Sealed container codes' }).getByText(containerCode)).toBeVisible();

  await page.getByLabel('Confirm all sealed container codes').fill(containerCode);
  await page.getByRole('button', { name: 'Queue Pickup confirmation' }).click();
  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  const pickupCard = page.locator('.command-card').filter({ hasText: 'Pickup confirmation' });
  await expect(pickupCard).toContainText('SYNCED');
  await expect(pickupCard).toContainText('Server applied');

  // Fetch a new authoritative snapshot after the Pickup command is applied.
  await page.getByRole('button', { name: 'Load assigned Shipment' }).click();
  await expect(page.locator('.snapshot-panel .status-badge')).toHaveText('IN TRANSIT');
  await page.getByLabel('Container to deliver').selectOption({ label: containerCode });
  await page.getByRole('button', { name: 'Queue one-container Delivery' }).click();
  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  const deliveryCard = page.locator('.command-card').filter({
    hasText: `Delivery initiation for ${containerCode}`,
  });
  await expect(deliveryCard).toContainText('SYNCED');
  await expect(deliveryCard).toContainText('Server applied');
  await page.getByRole('button', { name: 'Load assigned Shipment' }).click();
  await expect(
    page.getByRole('list', { name: 'Sealed container codes' }).getByText('DELIVERY PENDING', { exact: true }),
  ).toBeVisible();

  await signOut(page);
  await signIn(page, 'LABTRACK_LOCAL_LAB_OPERATOR_EMAIL', 'LABTRACK_LOCAL_LAB_OPERATOR_PASSWORD');
  await expect(page.getByRole('heading', { name: 'Lab workspace' })).toBeVisible();
  const labShipmentButton = page.getByRole('button', { name: new RegExp(shipmentCode) });
  await firstManifestResponseReady;
  try {
    await expect(labShipmentButton).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText('Loading Shipment manifest and receiving history…')).toBeVisible();
    await labShipmentButton.click();
  } finally {
    releaseFirstManifestResponse();
  }
  await expect(page.getByLabel('Receipt container code')).toBeVisible();
  await page.getByLabel('Receipt container code').fill(containerCode);
  await page.getByRole('button', { name: 'Confirm receipt' }).click();

  await page.getByRole('button', { name: `Record result for ${sampleCode}` }).click();
  await expect(page.getByText(`Result saved for ${sampleCode}.`)).toBeVisible();
  await page.getByRole('button', { name: 'Complete receiving' }).click();
  await expect(page.getByTestId('lab-shipment-status')).toHaveText('COMPLETED');
  await Promise.all(diagnostics.pendingApiFailureDetails);
  expect(diagnostics.apiFailures, 'Unexpected real API responses').toEqual([]);
  expect(diagnostics.failedRequests, 'Failed real API requests').toEqual([]);
  expect(
    diagnostics.externalRequests.filter((request) => request !== offOriginManifestRequest
      && request !== redirectTargetRequest),
    'Unexpected browser requests outside the local Vite origin',
  ).toEqual([]);
  expect(diagnostics.externalRequests.filter((request) => request === offOriginManifestRequest)).toHaveLength(1);
  expect(diagnostics.externalRequests.filter((request) => request === redirectTargetRequest).length).toBeLessThanOrEqual(1);
  expect(diagnostics.blockedOffOriginManifestRequests).toEqual([offOriginManifestRequest]);
  expect(diagnostics.offOriginManifestResponses).toEqual([]);
  expect(
    diagnostics.manifestFetches.every((request) => request.startsWith(`GET ${expectedOrigin}/`)),
  ).toBe(true);
  expect(diagnostics.pageErrors).toEqual([]);
});
