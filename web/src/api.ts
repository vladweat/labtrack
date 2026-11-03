import type {
  AccessTokenResponse,
  AnalyticsGranularity,
  CurrentUser,
  CreateSampleRequest,
  HandoffActionResult,
  Notification,
  PagedOrganization,
  PagedOrganizationCourier,
  PagedSample,
  Sample,
  OfflineCommand,
  OfflineManifest,
  NotificationUnreadCount,
  PagedNotification,
  PagedStalledShipment,
  Shipment,
  TransportContainer,
  ShipmentSummary,
  SyncResult,
} from './types';

const apiBase = (import.meta.env.VITE_API_BASE ?? '/api/v1').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  path: string,
  options: {
    token?: string;
    method?: 'GET' | 'POST' | 'PATCH';
    body?: unknown;
    signal?: AbortSignal;
    idempotencyKey?: string;
  } = {},
): Promise<T> {
  const headers = new Headers({ Accept: 'application/json', 'Cache-Control': 'no-store' });
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');
  if (options.token) headers.set('Authorization', `Bearer ${options.token}`);
  if (options.idempotencyKey) headers.set('Idempotency-Key', options.idempotencyKey);

  const response = await fetch(`${apiBase}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: 'no-store',
    signal: options.signal,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | {
          detail?: string;
          message?: string;
          error?: { detail?: string; message?: string };
        }
      | null;
    throw new ApiError(
      payload?.detail ??
        payload?.message ??
        payload?.error?.detail ??
        payload?.error?.message ??
        `Request failed with ${response.status}.`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

export function login(email: string, password: string): Promise<AccessTokenResponse> {
  return request('/auth/token', { method: 'POST', body: { email, password } });
}

export function currentUser(token: string): Promise<CurrentUser> {
  return request('/auth/me', { token });
}

export function listOrganizations(
  token: string,
  options: { regionId?: string; kind?: string; signal?: AbortSignal } = {},
): Promise<PagedOrganization> {
  const query = new URLSearchParams({ limit: '100', offset: '0' });
  if (options.regionId) query.set('region_id', options.regionId);
  if (options.kind) query.set('kind', options.kind);
  return request(`/organizations?${query.toString()}`, { token, signal: options.signal });
}

export function listOrganizationCouriers(
  token: string,
  organizationId: string,
  signal?: AbortSignal,
): Promise<PagedOrganizationCourier> {
  return request(`/organizations/${encodeURIComponent(organizationId)}/couriers`, {
    token,
    signal,
  });
}

export function listSamples(
  token: string,
  originOrganizationId: string,
  signal?: AbortSignal,
): Promise<PagedSample> {
  const query = new URLSearchParams({
    origin_organization_id: originOrganizationId,
    limit: '100',
    offset: '0',
  });
  return request(`/samples?${query.toString()}`, { token, signal });
}

export function createSample(
  token: string,
  body: CreateSampleRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<Sample> {
  return request('/samples', { token, method: 'POST', body, idempotencyKey, signal });
}

export function createShipment(
  token: string,
  body: {
    sender_org_id: string;
    destination_org_id: string;
    expected_delivery_at: string | null;
  },
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<Shipment> {
  return request('/shipments', { token, method: 'POST', body, idempotencyKey, signal });
}

export function assignShipmentCourier(
  token: string,
  shipmentId: string,
  courierUserId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<Shipment> {
  return request(`/shipments/${encodeURIComponent(shipmentId)}/courier`, {
    token,
    method: 'PATCH',
    body: { courier_user_id: courierUserId },
    idempotencyKey,
    signal,
  });
}

export function addShipmentContainer(
  token: string,
  shipmentId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TransportContainer> {
  return request(`/shipments/${encodeURIComponent(shipmentId)}/containers`, {
    token,
    method: 'POST',
    idempotencyKey,
    signal,
  });
}

export function addSampleToContainer(
  token: string,
  shipmentId: string,
  containerId: string,
  sampleId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<{
  id: string;
  container_id: string;
  sample_id: string;
  receipt_result: 'ACCEPTED' | 'MISSING' | 'REJECTED' | null;
  receipt_recorded_at: string | null;
  receipt_reason_code: string | null;
  receipt_comment: string | null;
}> {
  return request(
    `/shipments/${encodeURIComponent(shipmentId)}/containers/${encodeURIComponent(containerId)}/items`,
    { token, method: 'POST', body: { sample_id: sampleId }, idempotencyKey, signal },
  );
}

export function sealShipment(
  token: string,
  shipmentId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<Shipment> {
  return request(`/shipments/${encodeURIComponent(shipmentId)}/seal`, {
    token,
    method: 'POST',
    idempotencyKey,
    signal,
  });
}

export function releaseShipmentPickup(
  token: string,
  shipmentId: string,
  containerCodes: readonly string[],
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<HandoffActionResult> {
  return request(`/shipments/${encodeURIComponent(shipmentId)}/handoffs/pickup/confirm-release`, {
    token,
    method: 'POST',
    body: { container_codes: containerCodes },
    idempotencyKey,
    signal,
  });
}

export function offlineManifest(
  shipmentId: string,
  token: string,
  signal?: AbortSignal,
): Promise<OfflineManifest> {
  return request(`/courier/shipments/${encodeURIComponent(shipmentId)}/offline-manifest`, {
    token,
    signal,
  });
}

export function syncCommands(
  commands: readonly OfflineCommand[],
  token: string,
  signal?: AbortSignal,
): Promise<{ results: SyncResult[] }> {
  return request('/courier/sync', {
    token,
    method: 'POST',
    body: { commands },
    signal,
  });
}

export function getStalledShipments(
  token: string,
  options: {
    limit: number;
    offset: number;
    regionId?: string;
    signal?: AbortSignal;
  },
): Promise<PagedStalledShipment> {
  const query = new URLSearchParams({
    limit: String(options.limit),
    offset: String(options.offset),
  });
  if (options.regionId) query.set('region_id', options.regionId);

  return request(`/operations/stalled-shipments?${query.toString()}`, {
    token,
    signal: options.signal,
  });
}

export function getShipmentSummary(
  token: string,
  options: {
    granularity: AnalyticsGranularity;
    from?: string;
    to?: string;
    regionId: string;
    signal?: AbortSignal;
  },
): Promise<ShipmentSummary> {
  const query = new URLSearchParams({
    granularity: options.granularity,
    region_id: options.regionId,
  });
  if (options.from) query.set('from', options.from);
  if (options.to) query.set('to', options.to);

  return request(`/analytics/shipments/summary?${query.toString()}`, {
    token,
    signal: options.signal,
  });
}

export function listInboxNotifications(
  token: string,
  options: {
    limit: number;
    offset: number;
    unreadOnly?: boolean;
    signal?: AbortSignal;
  },
): Promise<PagedNotification> {
  const query = new URLSearchParams({
    limit: String(options.limit),
    offset: String(options.offset),
  });
  if (options.unreadOnly !== undefined) {
    query.set('unread_only', String(options.unreadOnly));
  }

  return request(`/notifications?${query.toString()}`, {
    token,
    signal: options.signal,
  });
}

export function getInboxUnreadCount(
  token: string,
  signal?: AbortSignal,
): Promise<NotificationUnreadCount> {
  return request('/notifications/unread-count', { token, signal });
}

export function markInboxNotificationRead(
  token: string,
  notificationId: string,
  idempotencyKey: string,
): Promise<Notification> {
  return request(`/notifications/${encodeURIComponent(notificationId)}/read`, {
    token,
    method: 'POST',
    idempotencyKey,
  });
}
