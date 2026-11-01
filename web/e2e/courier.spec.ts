import { expect, test, type Page, type Route } from '@playwright/test';

const shipmentId = '11111111-1111-4111-8111-111111111111';
const snapshotId = '22222222-2222-4222-8222-222222222222';
const containerIds = [
  '33333333-3333-4333-8333-333333333331',
  '33333333-3333-4333-8333-333333333332',
];
const codes = ['BX-101', 'BX-102'];
const primaryUser = {
  id: '44444444-4444-4444-8444-444444444441',
  email: 'courier-a@example.test',
  display_name: 'Courier A',
};
const secondUser = {
  id: '44444444-4444-4444-8444-444444444442',
  email: 'courier-b@example.test',
  display_name: 'Courier B',
};

type MockOptions = {
  users?: Array<typeof primaryUser>;
  oldSnapshot?: boolean;
  reviewOnSync?: boolean;
  dropFirstSyncResponse?: boolean;
};

function manifest(oldSnapshot = false) {
  const issuedAt = new Date(Date.now() - (oldSnapshot ? 25 : 1) * 60 * 60 * 1000).toISOString();
  return {
    shipment_id: shipmentId,
    manifest_revision: 7,
    status: 'READY_FOR_PICKUP',
    container_codes: codes,
    cached_at: issuedAt,
    containers: codes.map((code, index) => ({
      id: containerIds[index],
      code,
      delivery_status: 'PREPARED',
      state_version: 1,
    })),
    snapshot_id: snapshotId,
    issued_at: issuedAt,
    pickup_state_version: 4,
  };
}

async function installApiMock(page: Page, options: MockOptions = {}) {
  const users = options.users ?? [primaryUser, secondUser];
  const usersByEmail = new Map(users.map((user) => [user.email, user]));
  const usersByToken = new Map(users.map((user) => [`test-jwt-${user.id}`, user]));
  const syncRequests: Array<Array<Record<string, unknown>>> = [];
  const syncAuthTokens: string[] = [];
  const applied = new Map<string, Record<string, unknown>>();
  const authMeRequests: Array<{ token: string; cacheControl: string | null }> = [];
  let serverStatus = 'READY_FOR_PICKUP';
  let dropped = false;

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === '/api/v1/auth/token' && request.method() === 'POST') {
      const body = request.postDataJSON() as { email: string };
      const user = usersByEmail.get(body.email);
      if (!user) {
        await route.fulfill({ status: 401, json: { detail: 'Invalid email or password' } });
        return;
      }
      await route.fulfill({
        json: { access_token: `test-jwt-${user.id}`, token_type: 'bearer', expires_in: 900 },
      });
      return;
    }

    if (url.pathname === '/api/v1/auth/me' && request.method() === 'GET') {
      const token = request.headers().authorization?.replace(/^Bearer /, '') ?? '';
      authMeRequests.push({
        token,
        cacheControl: request.headers()['cache-control'] ?? null,
      });
      const user = usersByToken.get(token);
      if (!user) {
        await route.fulfill({ status: 401, json: { detail: 'Not authenticated' } });
        return;
      }
      await route.fulfill({
        json: {
          ...user,
          memberships: [
            {
              role: 'COURIER',
              organization_id: null,
              region_id: '55555555-5555-4555-8555-555555555555',
            },
          ],
        },
      });
      return;
    }

    if (url.pathname === `/api/v1/courier/shipments/${shipmentId}/offline-manifest`) {
      await route.fulfill({ json: manifest(options.oldSnapshot) });
      return;
    }

    if (url.pathname === '/api/v1/courier/sync' && request.method() === 'POST') {
      const body = request.postDataJSON() as { commands: Array<Record<string, unknown>> };
      const commands = structuredClone(body.commands);
      syncRequests.push(commands);
      syncAuthTokens.push(request.headers().authorization ?? '');

      if (options.dropFirstSyncResponse && !dropped) {
        dropped = true;
        for (const command of commands) {
          const commandId = String(command.client_command_id);
          const result = makeResult(command, 'APPLIED', 'IN_TRANSIT');
          applied.set(commandId, result);
          serverStatus = 'IN_TRANSIT';
        }
        await route.abort('failed');
        return;
      }

      const results = commands.map((command) => {
        if (options.reviewOnSync) {
          return makeResult(command, 'REVIEW_REQUIRED', serverStatus, {
            code: 'SNAPSHOT_EXPIRED',
            message: 'Snapshot is older than 24 hours; review is required before applying.',
          });
        }

        const existing = applied.get(String(command.client_command_id));
        if (existing) {
          return {
            ...existing,
            outcome: 'ALREADY_APPLIED',
            original_outcome: 'APPLIED',
          };
        }

        serverStatus =
          command.action === 'COURIER_CONFIRM_PICKUP' ? 'IN_TRANSIT' : 'DELIVERY_PENDING';
        const result = makeResult(command, 'APPLIED', serverStatus);
        applied.set(String(command.client_command_id), result);
        return result;
      });

      await route.fulfill({ json: { results } });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: 'Unexpected API request' } });
  });

  return {
    syncRequests,
    syncAuthTokens,
    authMeRequests,
    get serverStatus() {
      return serverStatus;
    },
  };
}

function makeResult(
  command: Record<string, unknown>,
  outcome: string,
  serverStatus: string,
  error: { code: string; message: string } | null = null,
) {
  return {
    client_command_id: command.client_command_id,
    shipment_id: command.shipment_id,
    container_id: command.container_id ?? null,
    client_performed_at: command.client_performed_at,
    outcome,
    original_outcome: null,
    server_status: serverStatus,
    recorded_at: outcome === 'RETRYABLE' ? null : new Date().toISOString(),
    error,
  };
}

async function signIn(page: Page, email = primaryUser.email) {
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('test-password');
  await page.getByRole('button', { name: 'Sign in' }).click();
}

async function loadShipment(page: Page) {
  await page.getByLabel('Shipment ID').fill(shipmentId);
  await page.getByRole('button', { name: 'Load assigned Shipment' }).click();
  await expect(
    page.getByRole('list', { name: 'Sealed container codes' }).getByText('BX-101', { exact: true }),
  ).toBeVisible();
}

async function queuePickup(page: Page) {
  await page.getByLabel('Confirm all sealed container codes').fill(codes.join(', '));
  await page.getByRole('button', { name: 'Queue Pickup confirmation' }).click();
  await expect(page.getByText(/Local pending/).first()).toBeVisible();
}

async function inspectDatabase(page: Page) {
  return page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('labtrack-courier-v1');
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });

    try {
      const names = [...db.objectStoreNames];
      const transaction = db.transaction(names, 'readonly');
      const records = await Promise.all(
        names.map(
          (name) =>
            new Promise<[string, unknown[]]>((resolve, reject) => {
              const request = transaction.objectStore(name).getAll();
              request.onerror = () => reject(request.error);
              request.onsuccess = () => resolve([name, request.result as unknown[]]);
            }),
        ),
      );
      return Object.fromEntries(records);
    } finally {
      db.close();
    }
  });
}

test('loads the cached PWA shell offline and never caches API responses', async ({
  page,
  context,
}) => {
  const mock = await installApiMock(page);
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Courier Offline' })).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => navigator.serviceWorker.getRegistration().then(Boolean)))
    .toBe(true);
  const manifestUrl = await page.locator('link[rel="manifest"]').getAttribute('href');
  expect(manifestUrl).toBeTruthy();
  const webManifest = await page.evaluate(async (url) => {
    const response = await fetch(url!);
    return response.json() as Promise<{
      display: string;
      name: string;
      start_url: string;
      icons: Array<{ sizes: string; type: string }>;
    }>;
  }, manifestUrl);
  expect(webManifest.display).toBe('standalone');
  expect(webManifest.name).toContain('Courier');
  expect(webManifest.start_url).toBe('/');
  expect(webManifest.icons.map((icon) => icon.sizes)).toEqual(
    expect.arrayContaining(['192x192', '512x512']),
  );

  await page.evaluate(() =>
    fetch('/api/v1/auth/me', {
      cache: 'no-store',
      headers: { 'Cache-Control': 'no-store' },
    }),
  );
  expect(mock.authMeRequests).toHaveLength(1);
  expect(mock.authMeRequests[0].cacheControl).toBe('no-store');
  const cachedUrls = await page.evaluate(async () => {
    const cacheNames = await caches.keys();
    const requests = await Promise.all(
      cacheNames.map(async (name) => (await caches.open(name)).keys()),
    );
    return requests.flat().map((request) => request.url);
  });
  expect(cachedUrls.some((url) => url.includes('/api/v1/'))).toBe(false);

  await page.unroute('**/api/v1/**');
  await context.setOffline(true);
  const apiRequestFailed = await page.evaluate(async () => {
    try {
      await fetch('/api/v1/auth/me', { cache: 'no-store' });
      return false;
    } catch {
      return true;
    }
  });
  expect(apiRequestFailed).toBe(true);
  expect(mock.authMeRequests).toHaveLength(1);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Courier Offline' })).toBeVisible();
});

test('queues Pickup locally while offline without changing server custody', async ({
  page,
  context,
}) => {
  const mock = await installApiMock(page);
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);

  await context.setOffline(true);
  await queuePickup(page);

  expect(mock.serverStatus).toBe('READY_FOR_PICKUP');
  expect(mock.syncRequests).toHaveLength(0);
  await expect(page.getByRole('status')).toContainText(/Server custody is unchanged until synchronization/);
  const records = (await inspectDatabase(page))['commands'] as Array<{
    command: { action: string };
  }>;
  expect(records).toHaveLength(1);
  expect(records[0].command.action).toBe('COURIER_CONFIRM_PICKUP');
});

test('retains the same user queue across reload and reauthentication without storing the token', async ({
  page,
  context,
}) => {
  const mock = await installApiMock(page);
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);
  expect(mock.authMeRequests[0].cacheControl).toBe('no-store');
  await context.setOffline(true);
  await queuePickup(page);
  await context.setOffline(false);

  const beforeReload = await inspectDatabase(page);
  const savedCommand = (beforeReload.commands as Array<{ command: unknown }>)[0].command;
  await page.reload();
  await signIn(page);

  await expect(page.getByText(/Local pending/).first()).toBeVisible();
  const afterReload = await inspectDatabase(page);
  expect((afterReload.commands as Array<{ command: unknown }>)[0].command).toEqual(savedCommand);
  expect(JSON.stringify(afterReload)).not.toContain(`test-jwt-${primaryUser.id}`);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  const cachedResponses = await page.evaluate(async () => {
    const names = await caches.keys();
    const responses = await Promise.all(
      names.flatMap(async (name) => {
        const cache = await caches.open(name);
        const requests = await cache.keys();
        return requests.map(async (request) => (await cache.match(request))?.clone().text());
      }),
    );
    return (await Promise.all(responses.flat())).filter(Boolean).join('\n');
  });
  expect(cachedResponses).not.toContain(`test-jwt-${primaryUser.id}`);
});

test('links Delivery to the queued Pickup and saves each outcome before showing it as applied', async ({
  page,
  context,
}) => {
  await page.addInitScript(() => {
    const writeOrder: string[] = [];
    (window as Window & { __idbWriteOrder?: string[] }).__idbWriteOrder = writeOrder;
    const originalTransaction = IDBDatabase.prototype.transaction;
    IDBDatabase.prototype.transaction = function (...args) {
      const transaction = originalTransaction.apply(this, args);
      const stores = Array.isArray(args[0]) ? args[0] : [args[0]];
      if (args[1] === 'readwrite' && stores.includes('outcomes')) {
        transaction.addEventListener('complete', () => writeOrder.push('outcome-persisted'));
      }
      if (args[1] === 'readwrite' && stores.includes('command-states')) {
        transaction.addEventListener('complete', () => writeOrder.push('queue-state-advanced'));
      }
      return transaction;
    };
  });
  const mock = await installApiMock(page);
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);
  await context.setOffline(true);
  await queuePickup(page);
  await page.getByLabel('Container to deliver').selectOption(containerIds[0]);
  await page.getByLabel('Link this delivery to my queued Pickup').check();
  await page.getByRole('button', { name: 'Queue one-container Delivery' }).click();
  await expect(page.getByText(/Local pending/).nth(1)).toBeVisible();
  await page.evaluate(() => {
    const writeOrder = (window as Window & { __idbWriteOrder?: string[] }).__idbWriteOrder;
    if (writeOrder) writeOrder.length = 0;
  });
  await context.setOffline(false);

  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  await expect(page.getByText(/Server applied/).first()).toBeVisible();
  expect(mock.syncRequests).toHaveLength(1);
  const [pickup, delivery] = mock.syncRequests[0];
  expect(pickup.action).toBe('COURIER_CONFIRM_PICKUP');
  expect(delivery.action).toBe('COURIER_INITIATE_CONTAINER_DELIVERY');
  expect(delivery.depends_on_command_id).toBe(pickup.client_command_id);

  const records = await inspectDatabase(page);
  expect(records.commands).toHaveLength(2);
  expect(records.outcomes).toHaveLength(2);
  expect(
    (records['command-states'] as Array<{ state: string }>).every(
      (record) => record.state === 'SYNCED',
    ),
  ).toBe(true);
  expect(
    (records.outcomes as Array<{ result: { outcome: string } }>).every(
      (record) => record.result.outcome === 'APPLIED',
    ),
  ).toBe(true);
  expect(
    await page.evaluate(() => (window as Window & { __idbWriteOrder?: string[] }).__idbWriteOrder),
  ).toEqual([
    'outcome-persisted',
    'queue-state-advanced',
    'outcome-persisted',
    'queue-state-advanced',
  ]);
});

test('replays the exact command ID and payload after a lost sync response', async ({ page }) => {
  const mock = await installApiMock(page, { dropFirstSyncResponse: true });
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);
  await queuePickup(page);

  const beforeSync = (await inspectDatabase(page)).commands as Array<{
    command: Record<string, unknown>;
  }>;
  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  await expect(page.getByText(/response was not received|retry sends the same command/i)).toBeVisible();
  await expect(page.getByText(/Local pending/).first()).toBeVisible();
  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  await expect(page.getByText(/Server applied/).first()).toBeVisible();

  expect(mock.syncRequests).toHaveLength(2);
  expect(mock.syncRequests[1]).toEqual(mock.syncRequests[0]);
  expect(mock.syncRequests[0][0]).toEqual(beforeSync[0].command);
  const records = await inspectDatabase(page);
  expect((records.outcomes as Array<{ result: { outcome: string } }>)[0].result.outcome).toBe(
    'ALREADY_APPLIED',
  );
});

test('explains server review when the snapshot is older than 24 hours', async ({ page }) => {
  await installApiMock(page, { oldSnapshot: true, reviewOnSync: true });
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);
  await expect(page.getByText(/more than 24 hours old/i)).toBeVisible();
  await queuePickup(page);
  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  await expect(page.getByText(/Review required.*older than 24 hours/i)).toBeVisible();
});

test('warns before logout and clears the user’s local courier data', async ({ page }) => {
  await installApiMock(page);
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);
  await queuePickup(page);

  await page.getByRole('button', { name: 'Log out' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/unsynced local command(?:s)?.*permanently deleted/i);
  await dialog.getByRole('button', { name: 'Log out and clear local data' }).click();
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();

  const records = await inspectDatabase(page);
  expect(records.commands).toHaveLength(0);
  expect(records.snapshots).toHaveLength(0);
  expect(JSON.stringify(records)).not.toContain(primaryUser.id);
});

test('warns and purges the previous account queue before same-device account change', async ({
  page,
}) => {
  await installApiMock(page);
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);
  await queuePickup(page);
  await page.reload();

  await signIn(page, secondUser.email);
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/saved courier data.*will be cleared/i);
  await dialog.getByRole('button', { name: 'Switch account and clear local data' }).click();
  await expect(page.getByText('Courier B', { exact: true })).toBeVisible();

  const records = await inspectDatabase(page);
  expect(records.commands).toHaveLength(0);
  expect(records.snapshots).toHaveLength(0);
});

test('canceling account change keeps the old session and blocks background sync activation', async ({
  page,
  context,
}) => {
  const mock = await installApiMock(page);
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);
  await context.setOffline(true);
  await queuePickup(page);
  await context.setOffline(false);

  await page.getByRole('button', { name: 'Switch account' }).click();
  const accountLogin = page.getByRole('dialog');
  await expect(accountLogin).toContainText(/current Courier session and device queue stay active/i);
  await accountLogin.getByLabel('New account email').fill('unknown@example.test');
  await accountLogin.getByLabel('New account password').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Continue to account' }).click();
  await expect(accountLogin.getByRole('alert')).toContainText('Invalid email or password');
  await expect(page.getByRole('heading', { name: 'Welcome, Courier A' })).toBeVisible();
  await accountLogin.getByLabel('New account email').fill(secondUser.email);
  await accountLogin.getByLabel('New account password').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Continue to account' }).click();

  const confirmation = page.getByRole('dialog');
  await expect(confirmation).toContainText(/saved courier data.*will be cleared/i);
  await expect(page.getByRole('heading', { name: 'Welcome, Courier A' })).toBeVisible();
  const syncButton = page.getByRole('button', { name: 'Sync pending commands' });
  await expect(syncButton).toBeEnabled();

  let mouseWasBlocked = false;
  try {
    await syncButton.click({ timeout: 500 });
  } catch {
    mouseWasBlocked = true;
  }
  expect(mouseWasBlocked).toBe(true);
  expect(mock.syncRequests).toHaveLength(0);

  const backgroundButtonReceivedFocus = await syncButton.evaluate((button) => {
    button.focus();
    return document.activeElement === button;
  });
  expect(backgroundButtonReceivedFocus).toBe(false);
  await page.keyboard.press('Enter');
  await expect(confirmation).not.toBeVisible();
  await expect(page.getByRole('heading', { name: 'Welcome, Courier A' })).toBeVisible();

  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  await expect(page.getByText(/Server applied/).first()).toBeVisible();
  expect(mock.syncRequests).toHaveLength(1);
  expect(mock.syncAuthTokens).toEqual([`Bearer test-jwt-${primaryUser.id}`]);
  expect(mock.syncAuthTokens).not.toContain(`Bearer test-jwt-${secondUser.id}`);
});

test('a failed local purge leaves the previous session and queue active', async ({ page }) => {
  await page.addInitScript(() => {
    const windowWithPurgeFailure = window as Window & { __failNextPurge?: boolean };
    windowWithPurgeFailure.__failNextPurge = false;
    const originalTransaction = IDBDatabase.prototype.transaction;
    IDBDatabase.prototype.transaction = function (...args) {
      const transaction = originalTransaction.apply(this, args);
      const stores = Array.isArray(args[0]) ? args[0] : [args[0]];
      if (
        windowWithPurgeFailure.__failNextPurge &&
        args[1] === 'readwrite' &&
        stores.includes('meta') &&
        stores.includes('commands')
      ) {
        windowWithPurgeFailure.__failNextPurge = false;
        transaction.abort();
      }
      return transaction;
    };
  });
  const mock = await installApiMock(page);
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);
  await queuePickup(page);
  await page.getByRole('button', { name: 'Switch account' }).click();
  const accountLogin = page.getByRole('dialog');
  await accountLogin.getByLabel('New account email').fill(secondUser.email);
  await accountLogin.getByLabel('New account password').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Continue to account' }).click();

  await page.evaluate(() => {
    (window as Window & { __failNextPurge?: boolean }).__failNextPurge = true;
  });
  const confirmation = page.getByRole('dialog');
  await confirmation.getByRole('button', { name: 'Switch account and clear local data' }).click();
  await expect(confirmation).toContainText(/could not clear.*current account and queue are still active/i);
  await expect(page.getByRole('heading', { name: 'Welcome, Courier A' })).toBeVisible();
  await expect(page.getByText(/Local pending/).first()).toBeVisible();
  expect((await inspectDatabase(page)).commands).toHaveLength(1);

  await confirmation.getByRole('button', { name: 'Keep previous account' }).click();
  await page.getByRole('button', { name: 'Sync pending commands' }).click();
  await expect(page.getByText(/Server applied/).first()).toBeVisible();
  expect(mock.syncAuthTokens).toEqual([`Bearer test-jwt-${primaryUser.id}`]);
});

test('keeps queues separate between browser installations on one device', async ({ browser }) => {
  const firstPage = await browser.newPage();
  const secondPage = await browser.newPage();
  try {
    await installApiMock(firstPage);
    await installApiMock(secondPage);
    await firstPage.goto('http://127.0.0.1:4173/');
    await secondPage.goto('http://127.0.0.1:4173/');
    await signIn(firstPage);
    await loadShipment(firstPage);
    await queuePickup(firstPage);

    await signIn(secondPage);
    await expect(secondPage.getByText(/No commands are queued on this device/)).toBeVisible();
    expect((await inspectDatabase(firstPage)).commands).toHaveLength(1);
    expect((await inspectDatabase(secondPage)).commands).toHaveLength(0);
  } finally {
    await firstPage.context().close();
    await secondPage.context().close();
  }
});
