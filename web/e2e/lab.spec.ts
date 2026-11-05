import { expect, test, type Page, type Route } from '@playwright/test';

const regionId = '66666666-6666-4666-8666-666666666660';
const labOrgId = '66666666-6666-4666-8666-666666666661';
const senderOrgId = '66666666-6666-4666-8666-666666666662';
const shipmentId = '66666666-6666-4666-8666-666666666663';
const containerId = '66666666-6666-4666-8666-666666666664';
const itemId = '66666666-6666-4666-8666-666666666665';
const userId = '66666666-6666-4666-8666-666666666666';
const handoffId = '66666666-6666-4666-8666-666666666667';
const revisionId = '66666666-6666-4666-8666-666666666668';
const sessionId = '66666666-6666-4666-8666-666666666669';
const foreignLabOrgId = '66666666-6666-4666-8666-666666666672';
const foreignShipmentId = '66666666-6666-4666-8666-666666666673';

const userProfile = {
  id: userId,
  email: 'lab@example.test',
  display_name: 'Synthetic Lab User',
};

type LabRole = 'LAB_OPERATOR' | 'LAB_MANAGER';
type MockOptions = {
  role: LabRole;
  initialStatus?: string;
  receiptStatus?: number;
  completionStatus?: number;
  reopenStatus?: number;
  includeForeignShipment?: boolean;
  totalRevisions?: number;
};

async function installLabApiMock(page: Page, options: MockOptions) {
  let shipmentStatus = options.initialStatus ?? 'DELIVERY_PENDING';
  let containerStatus = shipmentStatus === 'DELIVERY_PENDING' ? 'DELIVERY_PENDING' : 'RECEIVED';
  let result: 'ACCEPTED' | 'MISSING' | 'REJECTED' | null =
    shipmentStatus.startsWith('COMPLETED') ? 'MISSING' : null;
  let reasonCode: string | null = result ? 'SAMPLE_MISSING' : null;
  let comment: string | null = result ? 'Synthetic package note' : null;
  let receivingSessionStatus = shipmentStatus.startsWith('COMPLETED') ? 'CLOSED' : 'OPEN';
  let sessionSequence = 1;
  const revisions: Array<Record<string, unknown>> = shipmentStatus.startsWith('COMPLETED')
    ? [{
        id: revisionId,
        receiving_session_id: sessionId,
        container_item_id: itemId,
        old_result: null,
        new_result: 'MISSING',
        old_reason_code: null,
        new_reason_code: 'SAMPLE_MISSING',
        old_comment: null,
        new_comment: 'Synthetic package note',
        changed_by: userId,
        changed_at: new Date().toISOString(),
      }]
    : [];
  const mutations: Array<{ method: string; path: string; body: unknown; idempotencyKey: string | null }> = [];
  const historyRequests: Array<{ limit: number; offset: number }> = [];

  function addSyntheticRevisions(count: number): void {
    const additions = Array.from({ length: count }, (_, index) => ({
      id: `inserted-revision-${index + 1}`,
      receiving_session_id: sessionId,
      container_item_id: itemId,
      old_result: null,
      new_result: 'MISSING',
      old_reason_code: null,
      new_reason_code: 'SAMPLE_MISSING',
      old_comment: null,
      new_comment: `Inserted marker ${index + 1}`,
      changed_by: userId,
      changed_at: new Date(Date.now() + index * 1000).toISOString(),
    }));
    revisions.unshift(...additions);
  }

  const shipment = () => ({
    id: shipmentId,
    code: 'SHP-LAB-001',
    region_id: regionId,
    sender_org_id: senderOrgId,
    destination_org_id: labOrgId,
    courier_user_id: userId,
    status: shipmentStatus,
    version: 4,
    manifest_revision: 2,
    expected_delivery_at: new Date(Date.now() + 86_400_000).toISOString(),
    sealed_at: new Date().toISOString(),
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    completed_at: shipmentStatus.startsWith('COMPLETED') ? new Date().toISOString() : null,
    closed_at: null,
    closed_by: null,
    closure_reason: null,
  });

  const sessions = () => {
    const firstSession = {
      id: sessionId,
      shipment_id: shipmentId,
      sequence: 1,
      status: sessionSequence > 1 ? 'CLOSED' : receivingSessionStatus,
      opened_by: userId,
      opened_at: new Date().toISOString(),
      reopen_reason: null,
      closed_by: sessionSequence > 1 || receivingSessionStatus === 'CLOSED' ? userId : null,
      closed_at: sessionSequence > 1 || receivingSessionStatus === 'CLOSED' ? new Date().toISOString() : null,
    };
    if (sessionSequence === 1) return [firstSession];
    return [firstSession, {
      ...firstSession,
      id: '66666666-6666-4666-8666-666666666674',
      sequence: 2,
      status: 'OPEN',
      reopen_reason: 'Synthetic manager reopen',
      closed_by: null,
      closed_at: null,
    }];
  };

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const body = request.postDataJSON() as Record<string, unknown> | null;

    if (path === '/api/v1/auth/token' && method === 'POST') {
      await route.fulfill({
        json: { access_token: `test-jwt-${userId}`, token_type: 'bearer', expires_in: 900 },
      });
      return;
    }
    if (path === '/api/v1/auth/me' && method === 'GET') {
      await route.fulfill({
        json: {
          ...userProfile,
          memberships: [{ role: options.role, organization_id: labOrgId, region_id: regionId }],
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
    if (path === '/api/v1/shipments' && method === 'GET') {
      const items = [shipment()];
      if (options.includeForeignShipment) {
        items.push({ ...shipment(), id: foreignShipmentId, code: 'SHP-FOREIGN-001', destination_org_id: foreignLabOrgId });
      }
      await route.fulfill({ json: { items, total: items.length, limit: 50, offset: 0 } });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/manifest` && method === 'GET') {
      await route.fulfill({
        json: {
          shipment_id: shipmentId,
          status: shipmentStatus,
          sealed_at: new Date().toISOString(),
          manifest_revision: 2,
          containers: [{
            id: containerId,
            code: 'CNT-LAB-001',
            delivery_status: containerStatus,
            receipt_evidence_method: containerStatus === 'RECEIVED' ? 'NORMAL' : null,
            items: [{
              id: itemId,
              sample_id: '66666666-6666-4666-8666-666666666670',
              sample_code: 'SMP-LAB-001',
              material_type: 'BLOOD',
              receipt_result: result,
              receipt_reason_code: reasonCode,
              receipt_comment: comment,
            }],
          }],
        },
      });
      return;
    }
    if (path === '/api/v1/issue-reasons' && method === 'GET') {
      await route.fulfill({
        json: {
          items: [
            { id: 'reason-sample-missing', code: 'SAMPLE_MISSING', scope: 'SAMPLE', kind: 'MISSING', label: 'Sample missing', is_active: true },
            { id: 'reason-sample-rejected', code: 'SAMPLE_REJECTED', scope: 'SAMPLE', kind: 'REJECTED', label: 'Sample rejected', is_active: true },
            { id: 'reason-container-missing', code: 'CONTAINER_MISSING', scope: 'CONTAINER', kind: 'MISSING', label: 'Container missing', is_active: true },
            { id: 'reason-inactive', code: 'INACTIVE_SAMPLE_MISSING', scope: 'SAMPLE', kind: 'MISSING', label: 'Inactive reason', is_active: false },
          ],
          total: 4,
          limit: 50,
          offset: 0,
        },
      });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/receiving/history` && method === 'GET') {
      const limit = Number(url.searchParams.get('limit') ?? 100);
      const offset = Number(url.searchParams.get('offset') ?? 0);
      historyRequests.push({ limit, offset });
      const olderRevisionCount = options.totalRevisions ?? 0;
      const total = olderRevisionCount + revisions.length;
      const olderRevisions = options.totalRevisions === undefined
        ? []
        : Array.from({ length: olderRevisionCount }, (_, index) => ({
            id: `history-revision-${index + 1}`,
            receiving_session_id: sessionId,
            container_item_id: itemId,
            old_result: null,
            new_result: 'MISSING',
            old_reason_code: null,
            new_reason_code: 'SAMPLE_MISSING',
            old_comment: null,
            new_comment: `History marker ${index + 1}`,
            changed_by: userId,
            changed_at: new Date(Date.now() - index * 1000).toISOString(),
          }));
      const allRevisions = [...revisions, ...olderRevisions];
      await route.fulfill({
        json: {
          sessions: sessions(),
          revisions: allRevisions.slice(offset, offset + limit),
          total_revisions: total,
          limit,
          offset,
        },
      });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/containers/${containerId}/handoffs/delivery/confirm-receipt` && method === 'POST') {
      mutations.push({ method, path, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      if (options.receiptStatus) {
        await route.fulfill({ status: options.receiptStatus, json: { detail: 'Container code does not match the sealed manifest' } });
        return;
      }
      if (body?.container_code !== 'CNT-LAB-001') {
        await route.fulfill({ status: 409, json: { detail: 'Container code does not match the sealed manifest' } });
        return;
      }
      containerStatus = 'RECEIVED';
      shipmentStatus = 'RECEIVING';
      receivingSessionStatus = 'OPEN';
      await route.fulfill({
        json: {
          shipment_id: shipmentId,
          shipment_status: shipmentStatus,
          version: 5,
          handoff: {
            id: handoffId,
            kind: 'DELIVERY',
            attempt_no: 1,
            status: 'CONFIRMED',
            initiated_by: '66666666-6666-4666-8666-666666666671',
            initiated_at: new Date().toISOString(),
            confirmed_by: userId,
            confirmed_at: new Date().toISOString(),
            withdrawn_at: null,
            withdrawn_by: null,
            withdrawal_reason: null,
            container_id: containerId,
            superseded_by_incident_id: null,
            superseded_at: null,
          },
          container_id: containerId,
          container_status: containerStatus,
        },
      });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/receiving/items/${itemId}` && method === 'PATCH') {
      mutations.push({ method, path, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      result = body?.result as typeof result;
      reasonCode = typeof body?.reason_code === 'string' ? body.reason_code : null;
      comment = typeof body?.comment === 'string' ? body.comment : null;
      revisions.unshift({
        id: `revision-${revisions.length + 1}`,
        receiving_session_id: sessionId,
        container_item_id: itemId,
        old_result: null,
        new_result: result,
        old_reason_code: null,
        new_reason_code: reasonCode,
        old_comment: null,
        new_comment: comment,
        changed_by: userId,
        changed_at: new Date().toISOString(),
      });
      await route.fulfill({ json: {
        id: itemId,
        container_id: containerId,
        sample_id: '66666666-6666-4666-8666-666666666670',
        receipt_result: result,
        receipt_recorded_at: new Date().toISOString(),
        receipt_reason_code: reasonCode,
        receipt_comment: comment,
      } });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/receiving/complete` && method === 'POST') {
      mutations.push({ method, path, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      if (options.completionStatus) {
        await route.fulfill({ status: options.completionStatus, json: { detail: 'Every item in a received container must have a result' } });
        return;
      }
      if (!result) {
        await route.fulfill({ status: 409, json: { detail: 'Every item in a received container must have a result' } });
        return;
      }
      shipmentStatus = result === 'ACCEPTED' ? 'COMPLETED' : 'COMPLETED_WITH_ISSUES';
      receivingSessionStatus = 'CLOSED';
      await route.fulfill({ json: shipment() });
      return;
    }
    if (path === `/api/v1/shipments/${shipmentId}/receiving/reopen` && method === 'POST') {
      mutations.push({ method, path, body, idempotencyKey: request.headers()['idempotency-key'] ?? null });
      if (options.reopenStatus) {
        await route.fulfill({ status: options.reopenStatus, json: { detail: 'Receiving session must be closed' } });
        return;
      }
      if (typeof body?.reason !== 'string' || !body.reason.trim()) {
        await route.fulfill({ status: 422, json: { detail: 'reason must not be blank' } });
        return;
      }
      shipmentStatus = 'RECEIVING';
      sessionSequence += 1;
      receivingSessionStatus = 'OPEN';
      await route.fulfill({ json: shipment() });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: `Unexpected API request: ${method} ${path}` } });
  });

  return { addSyntheticRevisions, mutations, historyRequests };
}

async function signIn(page: Page) {
  await page.getByLabel('Электронная почта').fill(userProfile.email);
  await page.getByLabel('Пароль').fill('test-password');
  await page.getByRole('button', { name: 'Войти' }).click();
}

test('Lab Operator receives, records a result, reviews history, and completes receiving', async ({ page }) => {
  const api = await installLabApiMock(page, { role: 'LAB_OPERATOR' });
  await page.goto('/');
  await signIn(page);

  const labTab = page.getByRole('button', { name: 'Лаборатория' });
  await expect(labTab).toBeVisible();
  await labTab.click();
  await expect(page.getByRole('heading', { name: 'Рабочая область лаборатории' })).toBeVisible();
  await expect(page.getByText('SHP-LAB-001', { exact: true })).toBeVisible();

  await page.getByLabel('Код контейнера при приёмке').fill('CNT-LAB-001');
  await page.getByRole('button', { name: 'Подтвердить приёмку' }).click();
  await expect(page.locator('.lab-container-card .status-badge').first()).toHaveText('Принят');

  await page.getByRole('button', { name: 'Завершить приёмку' }).click();
  await expect(page.getByRole('alert')).toContainText('Укажите результат для каждого образца в принятом контейнере.');
  await expect(page.getByTestId('lab-shipment-status')).toHaveText('Приёмка');

  const resultSelect = page.getByLabel('Результат для образца SMP-LAB-001');
  await resultSelect.selectOption('MISSING');
  const reasonSelect = page.getByLabel('Причина для образца SMP-LAB-001');
  await expect(reasonSelect.locator('option:not([value=""])')).toHaveCount(1);
  await reasonSelect.selectOption('SAMPLE_MISSING');
  await page.getByLabel('Комментарий к образцу SMP-LAB-001').fill('Synthetic package note');
  await page.getByRole('button', { name: 'Сохранить результат для SMP-LAB-001' }).click();
  await expect(page.getByText('Результат: Отсутствует · Образец отсутствует', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'История приёмки' })).toBeVisible();
  await expect(page.getByText(`Образец: ${itemId}`, { exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Завершить приёмку' }).click();
  await expect(page.getByTestId('lab-shipment-status')).toHaveText('Завершено с замечаниями');
  await expect(page.getByRole('button', { name: 'Открыть повторно' })).toHaveCount(0);
  expect(api.mutations.map((entry) => `${entry.method} ${entry.path}`)).toEqual([
    `POST /api/v1/shipments/${shipmentId}/containers/${containerId}/handoffs/delivery/confirm-receipt`,
    `POST /api/v1/shipments/${shipmentId}/receiving/complete`,
    `PATCH /api/v1/shipments/${shipmentId}/receiving/items/${itemId}`,
    `POST /api/v1/shipments/${shipmentId}/receiving/complete`,
  ]);
  expect(api.mutations.every((entry) => entry.idempotencyKey?.length)).toBe(true);
  expect(api.mutations[2]?.body).toEqual({ result: 'MISSING', reason_code: 'SAMPLE_MISSING', comment: 'Synthetic package note' });
});

test('Lab Manager can receive and reopen with a reason but cannot edit results or complete', async ({ page }) => {
  const api = await installLabApiMock(page, { role: 'LAB_MANAGER', initialStatus: 'COMPLETED_WITH_ISSUES' });
  await page.goto('/');
  await signIn(page);
  await page.getByRole('button', { name: 'Лаборатория' }).click();

  await expect(page.getByRole('button', { name: 'Открыть повторно' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Завершить приёмку' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Сохранить результат/ })).toHaveCount(0);
  await page.getByLabel('Причина повторного открытия').fill('Retest after instrument review');
  await page.getByRole('button', { name: 'Открыть повторно' }).click();
  await expect(page.getByTestId('lab-shipment-status')).toHaveText('Приёмка');
  expect(api.mutations.at(-1)?.path).toBe(`/api/v1/shipments/${shipmentId}/receiving/reopen`);
  expect(api.mutations.at(-1)?.body).toEqual({ reason: 'Retest after instrument review' });
  expect(api.mutations.at(-1)?.idempotencyKey?.length).toBeTruthy();
  await expect(page.getByText('Сеанс 2 · Открыта')).toBeVisible();
  await expect(page.getByText('Причина повторного открытия: Synthetic manager reopen')).toBeVisible();
});

test('Lab Manager can confirm receipt without receiving result controls', async ({ page }) => {
  const api = await installLabApiMock(page, { role: 'LAB_MANAGER' });
  await page.goto('/');
  await signIn(page);
  await page.getByRole('button', { name: 'Лаборатория' }).click();

  await expect(page.getByLabel('Код контейнера при приёмке')).toBeVisible();
  await expect(page.getByRole('button', { name: /Сохранить результат/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Завершить приёмку' })).toHaveCount(0);
  await page.getByLabel('Код контейнера при приёмке').fill('CNT-LAB-001');
  await page.getByRole('button', { name: 'Подтвердить приёмку' }).click();
  await expect(page.locator('.lab-container-card .status-badge').first()).toHaveText('Принят');
  expect(api.mutations).toHaveLength(1);
  expect(api.mutations[0]?.idempotencyKey?.length).toBeTruthy();
});


test('Lab workspace omits Shipments for unrelated destination organizations', async ({ page }) => {
  await installLabApiMock(page, { role: 'LAB_OPERATOR', includeForeignShipment: true });
  await page.goto('/');
  await signIn(page);
  await page.getByRole('button', { name: 'Лаборатория' }).click();

  await expect(page.getByRole('button', { name: /SHP-LAB-001/ })).toBeVisible();
  await expect(page.getByText('SHP-FOREIGN-001')).toHaveCount(0);
});


test('Lab workspace loads older receiving revisions by offset and refreshes the current page', async ({ page }) => {
  const api = await installLabApiMock(page, {
    role: 'LAB_OPERATOR',
    initialStatus: 'RECEIVING',
    totalRevisions: 205,
  });
  await page.goto('/');
  await signIn(page);
  await page.getByRole('button', { name: 'Лаборатория' }).click();

  await expect(page.getByText('Показано записей: 100 из 205')).toBeVisible();
  await expect(page.getByText('Комментарий: History marker 101', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Загрузить более ранние изменения' }).click();
  await expect(page.getByText('Показано записей: 200 из 205')).toBeVisible();
  await expect(page.getByText('Комментарий: History marker 101', { exact: true })).toBeVisible();
  expect(api.historyRequests).toContainEqual({ limit: 100, offset: 100 });

  await page.getByRole('button', { name: 'Сохранить результат для SMP-LAB-001' }).click();
  await expect(page.getByText('Показано записей: 201 из 206')).toBeVisible();
  await expect(page.getByText('Комментарий: History marker 101', { exact: true })).toBeVisible();
  await expect(page.getByText('Не указан → Принят', { exact: true })).toBeVisible();
  expect(api.historyRequests.at(-1)).toEqual({ limit: 100, offset: 0 });
});


test('Lab workspace resumes sequential history offsets after more than 100 revisions arrive', async ({ page }) => {
  const api = await installLabApiMock(page, {
    role: 'LAB_OPERATOR',
    initialStatus: 'RECEIVING',
    totalRevisions: 205,
  });
  await page.goto('/');
  await signIn(page);
  await page.getByRole('button', { name: 'Лаборатория' }).click();

  await expect(page.getByText('Показано записей: 100 из 205')).toBeVisible();
  await page.getByRole('button', { name: 'Загрузить более ранние изменения' }).click();
  await expect(page.getByText('Показано записей: 200 из 205')).toBeVisible();
  expect(api.historyRequests.map(({ offset }) => offset)).toEqual([0, 100]);

  api.addSyntheticRevisions(151);
  await page.getByLabel('Результат для образца SMP-LAB-001').selectOption('MISSING');
  await page.getByLabel('Причина для образца SMP-LAB-001').selectOption('SAMPLE_MISSING');
  await page.getByLabel('Комментарий к образцу SMP-LAB-001').fill('Post-refresh marker');
  await page.getByRole('button', { name: 'Сохранить результат для SMP-LAB-001' }).click();
  await expect(page.getByText('Показано записей: 300 из 357')).toBeVisible();
  await expect(page.getByText('Комментарий: Post-refresh marker', { exact: true })).toBeVisible();

  const loadOlderButton = page.getByRole('button', { name: 'Загрузить более ранние изменения' });
  await loadOlderButton.click();
  await expect(loadOlderButton).toBeEnabled();
  await expect.poll(() => api.historyRequests.map(({ offset }) => offset))
    .toEqual([0, 100, 0, 100]);
  await expect(page.getByText('Показано записей: 352 из 357')).toBeVisible();

  await loadOlderButton.click();
  await expect(loadOlderButton).toBeEnabled();
  await expect.poll(() => api.historyRequests.map(({ offset }) => offset))
    .toEqual([0, 100, 0, 100, 200]);

  await loadOlderButton.click();
  await expect(page.getByText('Показано записей: 357 из 357')).toBeVisible();
  await expect(loadOlderButton).toHaveCount(0);
  expect(api.historyRequests.map(({ offset }) => offset)).toEqual([0, 100, 0, 100, 200, 300]);

  const renderedRevisions = await page
    .locator('section[aria-labelledby="lab-history-heading"] ol.lab-history-list')
    .nth(1)
    .locator('li')
    .allTextContents();
  const renderedComments = renderedRevisions
    .map((revision) => revision.match(/Комментарий: (.+?)Изменил/)?.[1])
    .filter((comment): comment is string => Boolean(comment));
  const expectedComments = [
    'Post-refresh marker',
    ...Array.from({ length: 151 }, (_, index) => `Inserted marker ${index + 1}`),
    ...Array.from({ length: 205 }, (_, index) => `History marker ${index + 1}`),
  ];
  expect(new Set(renderedComments)).toEqual(new Set(expectedComments));
});
