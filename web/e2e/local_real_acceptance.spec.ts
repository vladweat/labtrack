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
  await page.getByLabel('Электронная почта').fill(requiredCredential(emailName));
  await page.getByLabel('Пароль').fill(requiredCredential(passwordName));
  await page.getByRole('button', { name: 'Войти' }).click();
}

async function signOut(page: Page) {
  await page.getByRole('button', { name: 'Выйти' }).click();
  await page.getByRole('dialog')
    .getByRole('button', { name: 'Выйти и очистить данные устройства' })
    .click();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Рабочая область отправителя' })).toBeVisible();

  await page.getByLabel('Тип материала').selectOption('BLOOD');
  await page.getByRole('button', { name: 'Создать образец' }).click();
  const sampleRow = page.locator('.sender-resource-list li').filter({ hasText: 'Кровь' }).first();
  await expect(sampleRow).toBeVisible();
  const sampleCode = (await sampleRow.locator('strong').innerText()).trim();

  const expectedDelivery = new Date(Date.now() + 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 16);
  await page.getByLabel('Организация отправителя', { exact: true }).selectOption({ label: 'Local Synthetic Sender' });
  await page.getByLabel('Лаборатория-получатель').selectOption({ label: 'Центральная лаборатория' });
  await page.getByLabel('Ожидаемая дата доставки').fill(expectedDelivery);
  await page.getByRole('button', { name: 'Создать черновик отправки' }).click();
  await expect(page.getByRole('heading', { name: /^Отправка / })).toBeVisible();
  const shipmentId = (await page.locator('.sender-shipment-id').innerText()).trim();
  const shipmentCode = (await page.getByRole('heading', { name: /^Отправка / }).innerText())
    .replace(/^Отправка\s+/, '')
    .trim();
  expect(shipmentId).toMatch(/^[0-9a-f-]{36}$/i);

  await page.getByLabel('Курьер').selectOption({ label: 'Stage I Courier' });
  await page.getByRole('button', { name: 'Назначить курьера' }).click();
  await page.getByRole('button', { name: 'Добавить контейнер' }).click();
  const containerRow = page.locator('.sender-container-list li').first();
  await expect(containerRow).toBeVisible();
  const containerCode = (await containerRow.locator('strong').innerText()).trim();
  await page.getByLabel('Образец для добавления').selectOption({ label: `${sampleCode} · Кровь` });
  await page.getByRole('button', { name: 'Добавить образец в контейнер' }).click();
  await page.getByRole('button', { name: 'Опечатать отправку' }).click();
  await expect(page.getByText('Готово к забору', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Разрешить забор' }).click();
  await expect(page.getByText('Ожидает забора', { exact: true })).toBeVisible();

  await signOut(page);
  await signIn(page, 'LABTRACK_LOCAL_COURIER_EMAIL', 'LABTRACK_LOCAL_COURIER_PASSWORD');
  await expect(page.getByRole('heading', { name: 'Добро пожаловать, Stage I Courier' })).toBeVisible();
  await page.getByLabel('Идентификатор отправки').fill(shipmentId);
  await page.getByRole('button', { name: 'Загрузить назначенную отправку' }).click();
  await expect(page.getByRole('list', { name: 'Коды опечатанных контейнеров' }).getByText(containerCode)).toBeVisible();

  await page.getByLabel('Введите коды всех опечатанных контейнеров').fill(containerCode);
  await page.getByRole('button', { name: 'Поставить подтверждение забора в очередь' }).click();
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  const pickupCard = page.locator('.command-card').filter({ hasText: 'Подтверждение забора' });
  await expect(pickupCard).toContainText('Синхронизировано');
  await expect(pickupCard).toContainText('Применено на сервере');

  // Fetch a new authoritative snapshot after the Pickup command is applied.
  await page.getByRole('button', { name: 'Загрузить назначенную отправку' }).click();
  await expect(page.locator('.snapshot-panel .status-badge')).toHaveText('В пути');
  await page.getByLabel('Контейнер для доставки').selectOption({ label: containerCode });
  await page.getByRole('button', { name: 'Поставить доставку одного контейнера в очередь' }).click();
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  const deliveryCard = page.locator('.command-card').filter({
    hasText: `Доставка контейнера ${containerCode}`,
  });
  await expect(deliveryCard).toContainText('Синхронизировано');
  await expect(deliveryCard).toContainText('Применено на сервере');
  await page.getByRole('button', { name: 'Загрузить назначенную отправку' }).click();
  await expect(
    page.getByRole('list', { name: 'Коды опечатанных контейнеров' }).getByText('Ожидает доставки', { exact: true }),
  ).toBeVisible();

  await signOut(page);
  await signIn(page, 'LABTRACK_LOCAL_LAB_OPERATOR_EMAIL', 'LABTRACK_LOCAL_LAB_OPERATOR_PASSWORD');
  await expect(page.getByRole('heading', { name: 'Рабочая область лаборатории' })).toBeVisible();
  const labShipmentButton = page.getByRole('button', { name: new RegExp(shipmentCode) });
  await firstManifestResponseReady;
  try {
    await expect(labShipmentButton).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText('Загружаем манифест отправки и историю приёмки…')).toBeVisible();
    await labShipmentButton.click();
  } finally {
    releaseFirstManifestResponse();
  }
  await expect(page.getByLabel('Код контейнера при приёмке')).toBeVisible();
  await page.getByLabel('Код контейнера при приёмке').fill(containerCode);
  await page.getByRole('button', { name: 'Подтвердить приёмку' }).click();

  await page.getByRole('button', { name: `Сохранить результат для ${sampleCode}` }).click();
  await expect(page.getByText(`Результат для образца ${sampleCode} сохранён.`)).toBeVisible();
  await page.getByRole('button', { name: 'Завершить приёмку' }).click();
  await expect(page.getByTestId('lab-shipment-status')).toHaveText('Завершено');

  await signOut(page);
  await signIn(page, 'LABTRACK_LOCAL_LAB_MANAGER_EMAIL', 'LABTRACK_LOCAL_LAB_MANAGER_PASSWORD');
  await expect(page.getByRole('heading', { name: 'Рабочая область лаборатории' })).toBeVisible();
  const managerShipmentButton = page.getByRole('button', { name: new RegExp(shipmentCode) });
  await expect(managerShipmentButton).toBeVisible();
  await managerShipmentButton.click();
  await expect(page.getByRole('heading', { name: 'История приёмки' })).toBeVisible();
  await expect(page.getByText('Сеанс 1 · Закрыта')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Открыть повторно' })).toBeVisible();
  await page.getByLabel('Причина повторного открытия').fill('Synthetic manager review');
  await page.getByRole('button', { name: 'Открыть повторно' }).click();
  await expect(page.getByTestId('lab-shipment-status')).toHaveText('Приёмка');
  await expect(page.getByText('Сеанс 2 · Открыта')).toBeVisible();
  await expect(page.getByText('Причина повторного открытия: Synthetic manager review')).toBeVisible();

  await signOut(page);
  const sharedDemoNotice = page.getByRole('note', { name: 'Общее уведомление о демо' });
  await expect(sharedDemoNotice).toContainText('Учётные записи и данные демо общие и синтетические.');
  const configuredPasswords = Object.entries(process.env)
    .filter(([name, value]) => name.endsWith('_PASSWORD') && value)
    .map(([, value]) => value as string);
  const loginRendersConfiguredPassword = await page.locator('body').innerText().then((text) =>
    configuredPasswords.some((password) => text.includes(password)));
  expect(loginRendersConfiguredPassword, 'The login screen must not render configured demo passwords').toBe(false);

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
