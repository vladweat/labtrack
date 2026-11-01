export type AccessTokenResponse = {
  access_token: string;
  token_type: 'bearer';
  expires_in: number;
};

export type CourierMembership = {
  role: string;
  organization_id: string | null;
  region_id: string;
};

export type CurrentUser = {
  id: string;
  email: string;
  display_name: string;
  memberships: CourierMembership[];
};

export type OfflineContainer = {
  id: string;
  code: string;
  delivery_status: string;
  state_version: number;
};

export type OfflineManifest = {
  shipment_id: string;
  manifest_revision: number;
  status: string;
  container_codes: string[];
  cached_at: string;
  containers: OfflineContainer[];
  snapshot_id: string;
  issued_at: string;
  pickup_state_version: number;
};

type OfflineCommandBase = Readonly<{
  client_command_id: string;
  shipment_id: string;
  snapshot_id: string;
  expected_manifest_version: number;
  expected_target_state_version: number;
  client_performed_at: string;
}>;

export type PickupCommand = OfflineCommandBase &
  Readonly<{
    action: 'COURIER_CONFIRM_PICKUP';
    container_codes: readonly string[];
  }>;

export type DeliveryCommand = OfflineCommandBase &
  Readonly<{
    action: 'COURIER_INITIATE_CONTAINER_DELIVERY';
    container_id: string;
    container_code: string;
    depends_on_command_id?: string;
  }>;

export type OfflineCommand = PickupCommand | DeliveryCommand;

export type ServerOutcome =
  | 'APPLIED'
  | 'ALREADY_APPLIED'
  | 'REJECTED'
  | 'RETRYABLE'
  | 'REVIEW_REQUIRED';

export type SyncResult = {
  client_command_id: string;
  shipment_id: string;
  container_id: string | null;
  client_performed_at: string;
  outcome: ServerOutcome;
  original_outcome: 'APPLIED' | 'REJECTED' | 'REVIEW_REQUIRED' | null;
  server_status: string | null;
  recorded_at: string | null;
  error: { code: string; message: string } | null;
};

export type CommandState = 'PENDING_SYNC' | 'SYNCED' | 'REJECTED' | 'REVIEW_REQUIRED';

export type StoredSnapshot = {
  id: string;
  userId: string;
  snapshot: OfflineManifest;
  savedAt: string;
};

export type StoredCommand = {
  id: string;
  userId: string;
  command: OfflineCommand;
  queuedAt: string;
};

export type StoredCommandState = {
  id: string;
  userId: string;
  commandId: string;
  state: CommandState;
  updatedAt: string;
};

export type StoredOutcome = {
  id: string;
  userId: string;
  commandId: string;
  result: SyncResult;
  savedAt: string;
};

export type QueueItem = {
  record: StoredCommand;
  state: CommandState;
  outcome?: SyncResult;
};
