import type {
  AccessTokenResponse,
  CurrentUser,
  OfflineCommand,
  OfflineManifest,
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
  options: { token?: string; method?: 'GET' | 'POST'; body?: unknown } = {},
): Promise<T> {
  const headers = new Headers({ Accept: 'application/json', 'Cache-Control': 'no-store' });
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');
  if (options.token) headers.set('Authorization', `Bearer ${options.token}`);

  const response = await fetch(`${apiBase}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: 'no-store',
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | { detail?: string; message?: string }
      | null;
    throw new ApiError(
      payload?.detail ?? payload?.message ?? `Request failed with ${response.status}.`,
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

export function offlineManifest(shipmentId: string, token: string): Promise<OfflineManifest> {
  return request(`/courier/shipments/${encodeURIComponent(shipmentId)}/offline-manifest`, { token });
}

export function syncCommands(
  commands: readonly OfflineCommand[],
  token: string,
): Promise<{ results: SyncResult[] }> {
  return request('/courier/sync', {
    token,
    method: 'POST',
    body: { commands },
  });
}
