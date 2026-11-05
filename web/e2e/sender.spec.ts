import { expect, test, type BrowserContext, type Page, type Route } from '@playwright/test';

const regionId = '55555555-5555-4555-8555-555555555550';
const senderOrgId = '55555555-5555-4555-8555-555555555551';
const destinationOrgId = '55555555-5555-4555-8555-555555555552';
const foreignOrgId = '55555555-5555-4555-8555-555555555553';
const senderUserId = '55555555-5555-4555-8555-555555555554';
const courierId = '55555555-5555-4555-8555-555555555555';
const sampleId = '55555555-5555-4555-8555-555555555556';
const shipmentId = '55555555-5555-4555-8555-555555555557';
const containerId = '55555555-5555-4555-8555-555555555558';

const senderUser = {
  id: senderUserId,
  email: 'sender@example.test',
  display_name: 'Sender Operator',
};
const secondUser = {
  id: '55555555-5555-4555-8555-555555555561',
  email: 'sender-b@example.test',
  display_name: 'Second Sender Operator',
};

type MockOptions = {
  sampleListStatus?: number;
  createShipmentStatus?: number;
  holdSampleResponse?: boolean;
};

async function installApiMock(page: Page, options: MockOptions = {}) {
  let sampleCreated = false;
  let resolveSampleRequestStarted = () => {};
  let releaseHeldSampleResponse = () => {};
  const sampleRequestStarted = new Promise<void>((resolve) => {
    resolveSampleRequestStarted = resolve;
  });
  const heldSampleResponse = new Promise<void>((resolve) => {
    releaseHeldSampleResponse = resolve;
  });
  let shipmentStatus = 'DRAFT';
  let assignedCourierId: string | null = null;
  let containerCreated = false;
  let sampleAssigned = false;
  const mutationRequests: Array<{
    path: string;
    method: string;
    body: unknown;
    idempotencyKey: string | null;
  }> = [];

  const shipment = () => ({
    id: shipmentId,
    code: 'SHP-LOCAL-001',
    region_id: regionId,
    sender_org_id: senderOrgId,
    destination_org_id: destinationOrgId,
    courier_user_id: assignedCourierId,
    status: shipmentStatus,
    version: shipmentStatus === 'DRAFT' ? 1 : 3,
    manifest_revision: containerCreated ? 2 : 1,
    expected_delivery_at: new Date(Date.now() + 86_400_000).toISOString(),
    sealed_at: shipmentStatus === 'DRAFT' ? null : new Date().toISOString(),
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    completed_at: null,
    closed_at: null,
    closed_by: null,
    closure_reason: null,
  });

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const body = request.postDataJSON() as Record<string, unknown> | null;
    const authorization = request.headers().authorization;

    if (path === '/api/v1/auth/token' && method === 'POST') {
      await route.fulfill({
        json: { access_token: `test-jwt-${senderUserId}`, token_type: 'bearer', expires_in: 900 },
      });
      return;
    }
    if (path === '/api/v1/auth/me' && method === 'GET') {
      expect(authorization).toBe(`Bearer test-jwt-${senderUserId}`);
      await route.fulfill({
        json: {
          ...senderUser,
          memberships: [{ role: 'SENDER_OPERATOR', organization_id: senderOrgId, region_id: regionId }],
        },
      });
      return;
    }
    if (path === '/api/v1/notifications/unread-count') {
      await route.fulfill({ json: { count: 0 } });
      return;
    }
    if (path === '/api/v1/operations/stalled-shipments') {
      await route.fulfill({ json: { items: [], total: 0, limit: 50, offset: 0 } });
      return;
    }
    if (path === '/api/v1/organizations' && method === 'GET') {
      await route.fulfill({
        json: {
          items: [
            { id: senderOrgId, region_id: regionId, code: 'SENDER-01', name: 'North Sender', kind: 'SENDER', is_active: true },
            { id: destinationOrgId, region_id: regionId, code: 'LAB-01', name: 'Central Lab', kind: 'LABORATORY', is_active: true },
            { id: foreignOrgId, region_id: regionId, code: 'SENDER-02', name: 'Other Sender', kind: 'SENDER', is_active: true },
          ],
          total: 3,
          limit: 50,
          offset: 0,
        },
      });
      return;
    }
    if (path === `/api/v1/organizations/${senderOrgId}/couriers` && method === 'GET') {
      await route.fulfill({ json: { items: [{ id: courierId, display_name: 'Courier A' }], total: 1, limit: 50, offset: 0 } });
      return;
    }
    if (path === '/api/v1/samples' && method === 'GET') {
      if (options.sampleListStatus) {
        await route.fulfill({ status: options.sampleListStatus, json: { detail: 'Resource not found' } });
        return;
      }
      const items = sampleCreated
        ? [{ id: sampleId, code: 'SMP-LOCAL-001', origin_organization_id: senderOrgId, material_type: 'BLOOD', collected_at: null, created_at: new Date().toISOString() }]
        : [];
      await route.fulfill({ json: { items, total: items.length, limit: 50, offset: 0 } });
      return;
    }
    if (path === '/api/v1/samples' && method === 'POST') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      if (options.holdSampleResponse) {
        resolveSampleRequestStarted();
        await heldSampleResponse;
      }
      sampleCreated = true;
      await route.fulfill({
        status: 201,
        json: { id: sampleId, code: 'SMP-LOCAL-001', origin_organization_id: senderOrgId, material_type: body?.material_type ?? 'BLOOD', collected_at: null, created_at: new Date().toISOString() },
      });
      return;
    }
    if (path === '/api/v1/shipments' && method === 'POST') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      if (options.createShipmentStatus) {
        await route.fulfill({ status: options.createShipmentStatus, json: { detail: 'Sender and configured destination laboratory must share a region' } });
        return;
      }
      await route.fulfill({ status: 201, json: shipment() });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/courier` && method === 'PATCH') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      assignedCourierId = String(body?.courier_user_id);
      await route.fulfill({ json: shipment() });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/containers` && method === 'POST') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      containerCreated = true;
      await route.fulfill({
        status: 201,
        json: { id: containerId, code: 'CNT-LOCAL-001', shipment_id: shipmentId, created_at: new Date().toISOString(), delivery_status: 'PREPARED', received_at: null, received_by: null, state_version: 1, receipt_evidence: null },
      });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/containers/${containerId}/items` && method === 'POST') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      sampleAssigned = true;
      await route.fulfill({
        status: 201,
        json: { id: '55555555-5555-4555-8555-555555555559', container_id: containerId, sample_id: sampleId, receipt_result: null, receipt_recorded_at: null, receipt_reason_code: null, receipt_comment: null },
      });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/seal` && method === 'POST') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      if (!containerCreated || !sampleAssigned || !assignedCourierId) {
        await route.fulfill({ status: 422, json: { detail: 'Shipment is incomplete' } });
        return;
      }
      shipmentStatus = 'READY_FOR_PICKUP';
      await route.fulfill({ json: shipment() });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/handoffs/pickup/confirm-release` && method === 'POST') {
      mutationRequests.push({ path, method, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      shipmentStatus = 'PICKUP_PENDING';
      await route.fulfill({
        json: {
          shipment_id: shipmentId,
          shipment_status: shipmentStatus,
          version: 4,
          handoff: { id: '55555555-5555-4555-8555-555555555560', kind: 'PICKUP', attempt_no: 1, status: 'PENDING', initiated_by: senderUserId, initiated_at: new Date().toISOString(), confirmed_by: null, confirmed_at: null, withdrawn_at: null, withdrawn_by: null, withdrawal_reason: null, container_id: null, superseded_by_incident_id: null, superseded_at: null },
          container_id: null,
          container_status: null,
        },
      });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: `Unexpected API request: ${method} ${path}` } });
  });

  return { mutationRequests, sampleRequestStarted, releaseHeldSampleResponse };
}

async function installSecondTabApiMock(page: Page) {
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/token' && request.method() === 'POST') {
      const body = request.postDataJSON() as { email: string };
      const user = body.email === secondUser.email ? secondUser : senderUser;
      await route.fulfill({ json: { access_token: `test-jwt-${user.id}`, token_type: 'bearer', expires_in: 900 } });
      return;
    }
    if (url.pathname === '/api/v1/auth/me') {
      const token = request.headers().authorization?.replace(/^Bearer /, '') ?? '';
      const user = token === `test-jwt-${secondUser.id}` ? secondUser : senderUser;
      await route.fulfill({ json: { ...user, memberships: [{ role: 'SENDER_OPERATOR', organization_id: senderOrgId, region_id: regionId }] } });
      return;
    }
    if (url.pathname === '/api/v1/notifications/unread-count') {
      await route.fulfill({ json: { count: 0 } });
      return;
    }
    if (url.pathname === '/api/v1/organizations') {
      await route.fulfill({ json: { items: [
        { id: senderOrgId, region_id: regionId, code: 'SENDER-01', name: 'North Sender', kind: 'SENDER', is_active: true },
        { id: destinationOrgId, region_id: regionId, code: 'LAB-01', name: 'Central Lab', kind: 'LABORATORY', is_active: true },
      ], total: 2, limit: 50, offset: 0 } });
      return;
    }
    if (url.pathname === `/api/v1/organizations/${senderOrgId}/couriers`) {
      await route.fulfill({ json: { items: [], total: 0, limit: 50, offset: 0 } });
      return;
    }
    if (url.pathname === '/api/v1/samples') {
      await route.fulfill({ json: { items: [], total: 0, limit: 50, offset: 0 } });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: 'Unexpected API request' } });
  });
}

async function signInAsSender(page: Page) {
  await page.getByLabel('Электронная почта').fill(senderUser.email);
  await page.getByLabel('Пароль').fill('test-password');
  await page.getByRole('button', { name: 'Войти' }).click();
}

test('Sender can prepare and release a Shipment using server returned state', async ({ page }) => {
  const api = await installApiMock(page);
  await page.goto('/');
  await signInAsSender(page);

  const senderTab = page.getByRole('button', { name: 'Отправитель' });
  await expect(senderTab).toBeVisible();
  await senderTab.click();
  await expect(page.getByRole('heading', { name: 'Рабочая область отправителя' })).toBeVisible();

  await page.getByLabel('Тип материала').selectOption('BLOOD');
  await page.getByRole('button', { name: 'Создать образец' }).click();
  await expect(page.getByText('SMP-LOCAL-001', { exact: true })).toBeVisible();

  const expectedDelivery = new Date(Date.now() + 86_400_000).toISOString().slice(0, 16);
  await page.getByLabel('Организация отправителя', { exact: true }).selectOption(senderOrgId);
  await page.getByLabel('Лаборатория-получатель').selectOption(destinationOrgId);
  await page.getByLabel('Ожидаемая дата доставки').fill(expectedDelivery);
  await page.getByRole('button', { name: 'Создать черновик отправки' }).click();
  await expect(page.getByRole('heading', { name: 'Отправка SHP-LOCAL-001' })).toBeVisible();

  await page.getByLabel('Курьер').selectOption(courierId);
  await page.getByRole('button', { name: 'Назначить курьера' }).click();
  await page.getByRole('button', { name: 'Добавить контейнер' }).click();
  await expect(page.getByRole('list', { name: 'Контейнеры отправки' }).getByText('CNT-LOCAL-001', { exact: true })).toBeVisible();
  await page.getByLabel('Образец для добавления').selectOption(sampleId);
  await page.getByRole('button', { name: 'Добавить образец в контейнер' }).click();
  await page.getByRole('button', { name: 'Опечатать отправку' }).click();
  await expect(page.getByText('Готово к забору', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Разрешить забор' }).click();
  await expect(page.getByText('Ожидает забора', { exact: true })).toBeVisible();
  expect(api.mutationRequests.map((request) => `${request.method} ${request.path}`)).toEqual([
    'POST /api/v1/samples',
    'POST /api/v1/shipments',
    `PATCH /api/v1/shipments/${shipmentId}/courier`,
    `POST /api/v1/shipments/${shipmentId}/containers`,
    `POST /api/v1/shipments/${shipmentId}/containers/${containerId}/items`,
    `POST /api/v1/shipments/${shipmentId}/seal`,
    `POST /api/v1/shipments/${shipmentId}/handoffs/pickup/confirm-release`,
  ]);
  expect(api.mutationRequests.every((request) => request.idempotencyKey?.length)).toBe(true);
  expect(api.mutationRequests.at(-1)?.body).toEqual({ container_codes: ['CNT-LOCAL-001'] });
});

test('Sender lists do not offer another organization and render a masked 404', async ({ page }) => {
  await installApiMock(page, { sampleListStatus: 404 });
  await page.goto('/');
  await signInAsSender(page);
  await page.getByRole('button', { name: 'Отправитель' }).click();

  const senderOptions = page.getByLabel('Организация отправителя', { exact: true }).locator('option');
  await expect(senderOptions).toHaveCount(1);
  await expect(senderOptions.getByText('Other Sender')).toHaveCount(0);
  await expect(page.getByRole('alert')).toContainText('Запись не найдена или недоступна.');
  await expect(page.getByText('SMP-FOREIGN-001')).toHaveCount(0);
});

test('Server validation error does not create a local draft Shipment', async ({ page }) => {
  await installApiMock(page, { createShipmentStatus: 422 });
  await page.goto('/');
  await signInAsSender(page);
  await page.getByRole('button', { name: 'Отправитель' }).click();
  await page.getByLabel('Организация отправителя', { exact: true }).selectOption(senderOrgId);
  await page.getByLabel('Лаборатория-получатель').selectOption(destinationOrgId);
  await page.getByLabel('Ожидаемая дата доставки').fill(new Date(Date.now() + 86_400_000).toISOString().slice(0, 16));
  await page.getByRole('button', { name: 'Создать черновик отправки' }).click();
  await expect(page.getByRole('alert')).toContainText('Организация отправителя и лаборатория-получатель должны находиться в одном регионе.');
  await expect(page.getByText('SHP-LOCAL-001')).toHaveCount(0);
});

test('aborts an in-flight Sender mutation when another tab switches accounts', async ({ browser }) => {
  const context: BrowserContext = await browser.newContext();
  const firstTab = await context.newPage();
  const secondTab = await context.newPage();
  const firstApi = await installApiMock(firstTab, { holdSampleResponse: true });
  await installSecondTabApiMock(secondTab);
  let samplePostFailed = false;
  firstTab.on('requestfailed', (request) => {
    if (new URL(request.url()).pathname === '/api/v1/samples' && request.method() === 'POST') {
      samplePostFailed = true;
    }
  });

  await firstTab.goto('/');
  await signInAsSender(firstTab);
  await firstTab.getByLabel('Тип материала').selectOption('BLOOD');
  await firstTab.getByRole('button', { name: 'Создать образец' }).click();
  await firstApi.sampleRequestStarted;

  await secondTab.goto('/');
  await signInAsSender(secondTab);
  await secondTab.getByRole('button', { name: 'Сменить учётную запись' }).click();
  const switchDialog = secondTab.getByRole('dialog');
  await switchDialog.getByLabel('Электронная почта новой учётной записи').fill(secondUser.email);
  await switchDialog.getByLabel('Пароль новой учётной записи').fill('test-password');
  await switchDialog.getByRole('button', { name: 'Продолжить' }).click();
  await secondTab.getByRole('dialog')
    .getByRole('button', { name: 'Сменить учётную запись и очистить данные' })
    .click();

  await expect(firstTab.getByRole('alert')).toContainText('Учётная запись изменилась в другой вкладке. Войдите снова.');
  await expect.poll(() => samplePostFailed, { timeout: 2_000 }).toBe(true);
  firstApi.releaseHeldSampleResponse();
  await expect(firstTab.getByText('SMP-LOCAL-001', { exact: true })).toHaveCount(0);
  await context.close();
});
