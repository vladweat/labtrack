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
  holdManifestResponse?: boolean;
  holdSyncResponse?: boolean;
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
  let resolveManifestRequestStarted = () => {};
  let releaseHeldManifest: (() => void) | null = null;
  let resolveSyncRequestStarted = () => {};
  let releaseHeldSync: (() => void) | null = null;
  const manifestRequestStarted = new Promise<void>((resolve) => {
    resolveManifestRequestStarted = resolve;
  });
  const syncRequestStarted = new Promise<void>((resolve) => {
    resolveSyncRequestStarted = resolve;
  });

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
      if (options.holdManifestResponse) {
        resolveManifestRequestStarted();
        await new Promise<void>((resolve) => {
          releaseHeldManifest = resolve;
        });
      }
      await route.fulfill({ json: manifest(options.oldSnapshot) });
      return;
    }

    if (url.pathname === '/api/v1/courier/sync' && request.method() === 'POST') {
      const body = request.postDataJSON() as { commands: Array<Record<string, unknown>> };
      const commands = structuredClone(body.commands);
      syncRequests.push(commands);
      syncAuthTokens.push(request.headers().authorization ?? '');
      if (options.holdSyncResponse) {
        resolveSyncRequestStarted();
        await new Promise<void>((resolve) => {
          releaseHeldSync = resolve;
        });
      }

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
    manifestRequestStarted,
    syncRequestStarted,
    releaseManifestResponse() {
      releaseHeldManifest?.();
    },
    releaseSyncResponse() {
      releaseHeldSync?.();
    },
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
  await page.getByLabel('Электронная почта').fill(email);
  await page.getByLabel('Пароль').fill('test-password');
  await page.getByRole('button', { name: 'Войти' }).click();
}

async function loadShipment(page: Page) {
  await page.getByLabel('Идентификатор отправки').fill(shipmentId);
  await page.getByRole('button', { name: 'Загрузить назначенную отправку' }).click();
  await expect(
    page.getByRole('list', { name: 'Коды опечатанных контейнеров' }).getByText('BX-101', { exact: true }),
  ).toBeVisible();
}

async function queuePickup(page: Page) {
  await page.getByLabel('Введите коды всех опечатанных контейнеров').fill(codes.join(', '));
  await page.getByRole('button', { name: 'Поставить подтверждение забора в очередь' }).click();
  await expect(page.getByText(/Ожидает синхронизации/).first()).toBeVisible();
}

async function switchAccount(page: Page) {
  await page.getByRole('button', { name: 'Сменить учётную запись' }).click();
  const accountLogin = page.getByRole('dialog');
  await accountLogin.getByLabel('Электронная почта новой учётной записи').fill(secondUser.email);
  await accountLogin.getByLabel('Пароль новой учётной записи').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Продолжить' }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Сменить учётную запись и очистить данные' })
    .click();
  await expect(page.getByRole('heading', { name: 'Добро пожаловать, Courier B' })).toBeVisible();
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

async function changeActiveUserMarker(page: Page, userId: string | null) {
  await page.evaluate(async (nextUserId) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('labtrack-courier-v1');
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });

    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(['meta'], 'readwrite');
      transaction.objectStore('meta').put({ key: 'active-user', userId: nextUserId });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    database.close();
  }, userId);
}

test('loads the cached PWA shell offline and never caches API responses', async ({
  page,
  context,
}) => {
  const mock = await installApiMock(page);
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Курьер офлайн' })).toBeVisible();
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
  expect(webManifest.name).toContain('курьер');
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
  await expect(page.getByRole('heading', { name: 'Курьер офлайн' })).toBeVisible();
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
  await expect(page.getByRole('status')).toContainText(/Статус передачи на сервере не изменился/);
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

  await expect(page.getByText(/Ожидает синхронизации/).first()).toBeVisible();
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
  await page.getByLabel('Контейнер для доставки').selectOption(containerIds[0]);
  await page.getByLabel('Связать доставку с ожидающим подтверждением забора').check();
  await page.getByRole('button', { name: 'Поставить доставку одного контейнера в очередь' }).click();
  await expect(page.getByText(/Ожидает синхронизации/).nth(1)).toBeVisible();
  await page.evaluate(() => {
    const writeOrder = (window as Window & { __idbWriteOrder?: string[] }).__idbWriteOrder;
    if (writeOrder) writeOrder.length = 0;
  });
  await context.setOffline(false);

  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByText(/Применено на сервере/).first()).toBeVisible();
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
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByText(/Ответ сервера не получен|повторная отправка использует тот же идентификатор/i)).toBeVisible();
  await expect(page.getByText(/Ожидает синхронизации/).first()).toBeVisible();
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByText(/Применено на сервере/).first()).toBeVisible();

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
  await expect(page.getByText(/больше 24 часов/)).toBeVisible();
  await queuePickup(page);
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByText(/Требуется проверка.*Снимок старше 24 часов/)).toBeVisible();
});

test('warns before logout and clears the user’s local courier data', async ({ page }) => {
  await installApiMock(page);
  await page.goto('/');
  await signIn(page);
  await loadShipment(page);
  await queuePickup(page);

  await page.getByRole('button', { name: 'Выйти' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/безвозвратно удалены несинхронизированные команды/i);
  await dialog.getByRole('button', { name: 'Выйти и очистить данные устройства' }).click();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();

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
  await expect(dialog).toContainText(/Данные курьера .* будут удалены с этого устройства/);
  await dialog.getByRole('button', { name: 'Сменить учётную запись и очистить данные' }).click();
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

  await page.getByRole('button', { name: 'Сменить учётную запись' }).click();
  const accountLogin = page.getByRole('dialog');
  await expect(accountLogin).toContainText(/Текущий сеанс курьера и очередь команд на устройстве останутся активными/);
  await accountLogin.getByLabel('Электронная почта новой учётной записи').fill('unknown@example.test');
  await accountLogin.getByLabel('Пароль новой учётной записи').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Продолжить' }).click();
  await expect(accountLogin.getByRole('alert')).toContainText('Неверный адрес электронной почты или пароль.');
  await expect(page.getByRole('heading', { name: 'Добро пожаловать, Courier A' })).toBeVisible();
  await accountLogin.getByLabel('Электронная почта новой учётной записи').fill(secondUser.email);
  await accountLogin.getByLabel('Пароль новой учётной записи').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Продолжить' }).click();

  const confirmation = page.getByRole('dialog');
  await expect(confirmation).toContainText(/Данные курьера .* будут удалены с этого устройства/);
  await expect(page.getByRole('heading', { name: 'Добро пожаловать, Courier A' })).toBeVisible();
  const syncButton = page.getByRole('button', { name: 'Синхронизировать команды' });
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
  await expect(page.getByRole('heading', { name: 'Добро пожаловать, Courier A' })).toBeVisible();

  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByText(/Применено на сервере/).first()).toBeVisible();
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
  await page.getByRole('button', { name: 'Сменить учётную запись' }).click();
  const accountLogin = page.getByRole('dialog');
  await accountLogin.getByLabel('Электронная почта новой учётной записи').fill(secondUser.email);
  await accountLogin.getByLabel('Пароль новой учётной записи').fill('test-password');
  await accountLogin.getByRole('button', { name: 'Продолжить' }).click();

  await page.evaluate(() => {
    (window as Window & { __failNextPurge?: boolean }).__failNextPurge = true;
  });
  const confirmation = page.getByRole('dialog');
  await confirmation.getByRole('button', { name: 'Сменить учётную запись и очистить данные' }).click();
  await expect(confirmation).toContainText(/Не удалось очистить данные на этом устройстве\. Текущая учётная запись и очередь остаются активными/);
  await expect(page.getByRole('heading', { name: 'Добро пожаловать, Courier A' })).toBeVisible();
  await expect(page.getByText(/Ожидает синхронизации/).first()).toBeVisible();
  expect((await inspectDatabase(page)).commands).toHaveLength(1);

  await confirmation.getByRole('button', { name: 'Оставить прежнюю учётную запись' }).click();
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByText(/Применено на сервере/).first()).toBeVisible();
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
    await expect(secondPage.getByText(/На этом устройстве нет команд в очереди/)).toBeVisible();
    expect((await inspectDatabase(firstPage)).commands).toHaveLength(1);
    expect((await inspectDatabase(secondPage)).commands).toHaveLength(0);
  } finally {
    await firstPage.context().close();
    await secondPage.context().close();
  }
});

for (const useStorageFallback of [false, true]) {
  test(
    `invalidates a sibling tab after account switch${useStorageFallback ? ' using storage fallback' : ''}`,
    async ({ browser }) => {
      const context = await browser.newContext();
      if (useStorageFallback) {
        await context.addInitScript(() => {
          Object.defineProperty(window, 'BroadcastChannel', { configurable: true, value: undefined });
        });
      }
      const firstPage = await context.newPage();
      const secondPage = await context.newPage();
      const firstMock = await installApiMock(firstPage);
      const secondMock = await installApiMock(secondPage);
      try {
        await firstPage.goto('http://127.0.0.1:4173/');
        await secondPage.goto('http://127.0.0.1:4173/');
        await signIn(firstPage, primaryUser.email);
        await signIn(secondPage, primaryUser.email);
        await loadShipment(secondPage);
        await queuePickup(secondPage);

        await switchAccount(firstPage);

        await expect(secondPage.getByRole('button', { name: 'Войти' })).toBeVisible();
        await expect(secondPage.getByRole('heading', { name: 'Добро пожаловать, Courier A' })).toHaveCount(0);
        await expect(
          secondPage.getByRole('button', { name: 'Синхронизировать команды' }),
        ).toHaveCount(0);
        await expect(
          secondPage.getByRole('button', { name: 'Поставить подтверждение забора в очередь' }),
        ).toHaveCount(0);
        expect(firstMock.syncRequests).toHaveLength(0);
        expect(secondMock.syncRequests).toHaveLength(0);
        const records = await inspectDatabase(secondPage);
        expect(records.commands).toHaveLength(0);
        expect(records.snapshots).toHaveLength(0);
      } finally {
        await context.close();
      }
    },
  );
}

test('rejects a queued command after the shared active account changed', async ({ page }) => {
  await installApiMock(page);
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);

  // Model another tab switching the shared IndexedDB account before this stale tab writes.
  await changeActiveUserMarker(page, secondUser.id);
  await page.getByLabel('Введите коды всех опечатанных контейнеров').fill(codes.join(', '));
  await page.getByRole('button', { name: 'Поставить подтверждение забора в очередь' }).click();

  expect((await inspectDatabase(page)).commands).toHaveLength(0);
  await expect(page.locator('.page-alert')).toContainText('Не удалось добавить команду в очередь.');
});

test('does not persist a Shipment snapshot returned after the active account changed', async ({ page }) => {
  const mock = await installApiMock(page, { holdManifestResponse: true });
  await page.goto('/');
  await signIn(page, primaryUser.email);

  await page.getByLabel('Идентификатор отправки').fill(shipmentId);
  const loadButton = page.locator('.load-form button');
  const loadRequest = loadButton.click();
  await mock.manifestRequestStarted;
  await expect(loadButton).toBeDisabled();
  await changeActiveUserMarker(page, secondUser.id);
  mock.releaseManifestResponse();
  await loadRequest;
  await expect(loadButton).toBeEnabled();

  expect((await inspectDatabase(page)).snapshots).toHaveLength(0);
});

test('does not persist sync outcomes returned after the active account changed', async ({ page }) => {
  const mock = await installApiMock(page, { holdSyncResponse: true });
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);
  await queuePickup(page);

  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await mock.syncRequestStarted;
  await changeActiveUserMarker(page, secondUser.id);
  mock.releaseSyncResponse();
  await expect(page.getByRole('button', { name: 'Синхронизировать команды' })).toBeEnabled();

  const records = await inspectDatabase(page);
  expect(mock.syncRequests).toHaveLength(1);
  expect(records.outcomes).toHaveLength(0);
  expect(records['command-states']).toHaveLength(1);
  expect(records['command-states'][0]).toMatchObject({ state: 'PENDING_SYNC' });
});

test('does not start synchronization when the shared active account no longer matches the tab', async ({
  page,
}) => {
  const mock = await installApiMock(page);
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);
  await queuePickup(page);

  await changeActiveUserMarker(page, secondUser.id);
  await page.getByRole('button', { name: 'Синхронизировать команды' }).click();
  await expect(page.getByRole('button', { name: 'Синхронизировать команды' })).toBeEnabled();

  expect(mock.syncRequests).toHaveLength(0);
  const records = await inspectDatabase(page);
  expect(records.outcomes).toHaveLength(0);
  expect(records['command-states'][0]).toMatchObject({ state: 'PENDING_SYNC' });
});

test('does not purge a previous account after another tab already changed the active account', async ({
  page,
}) => {
  await installApiMock(page);
  await page.goto('/');
  await signIn(page, primaryUser.email);
  await loadShipment(page);
  await queuePickup(page);
  await page.getByRole('button', { name: 'Сменить учётную запись' }).click();
  await page.getByLabel('Электронная почта новой учётной записи').fill(secondUser.email);
  await page.getByLabel('Пароль новой учётной записи').fill('test-password');
  await page.getByRole('button', { name: 'Продолжить' }).click();

  await changeActiveUserMarker(page, secondUser.id);
  const confirmation = page.getByRole('dialog');
  await confirmation.getByRole('button', { name: 'Сменить учётную запись и очистить данные' }).click();

  await expect(confirmation).toContainText(/Не удалось очистить данные на этом устройстве\. Текущая учётная запись и очередь остаются активными/);
  expect((await inspectDatabase(page)).commands).toHaveLength(1);
});
