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

export type Organization = {
  id: string;
  region_id: string;
  code: string;
  name: string;
  kind: string;
  is_active: boolean;
};

export type PagedOrganization = {
  items: Organization[];
  total: number;
  limit: number;
  offset: number;
};

export type OrganizationCourier = {
  id: string;
  display_name: string;
};

export type PagedOrganizationCourier = {
  items: OrganizationCourier[];
  total: number;
  limit: number;
  offset: number;
};

export type Sample = {
  id: string;
  code: string;
  origin_organization_id: string;
  material_type: 'BLOOD' | 'URINE' | 'OTHER';
  collected_at: string | null;
  created_at: string;
};

export type PagedSample = {
  items: Sample[];
  total: number;
  limit: number;
  offset: number;
};

export type CreateSampleRequest = {
  origin_organization_id: string;
  material_type: Sample['material_type'];
  collected_at?: string | null;
};

export type ShipmentStatus =
  | 'DRAFT'
  | 'READY_FOR_PICKUP'
  | 'PICKUP_PENDING'
  | 'IN_TRANSIT'
  | 'DELIVERY_PENDING'
  | 'DELIVERY_BLOCKED'
  | 'PARTIALLY_DELIVERED'
  | 'RECEIVING'
  | 'COMPLETED'
  | 'COMPLETED_WITH_ISSUES'
  | 'CANCELLED'
  | 'CLOSED_UNDELIVERED';

export type Shipment = {
  id: string;
  code: string;
  region_id: string;
  sender_org_id: string;
  destination_org_id: string;
  courier_user_id: string | null;
  status: ShipmentStatus;
  version: number;
  manifest_revision: number;
  expected_delivery_at: string | null;
  sealed_at: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  closed_at: string | null;
  closed_by: string | null;
  closure_reason: string | null;
};

export type TransportContainer = {
  id: string;
  code: string;
  shipment_id: string;
  created_at: string;
  delivery_status: string;
  received_at: string | null;
  received_by: string | null;
  state_version: number;
  receipt_evidence: Record<string, unknown> | null;
};

export type Handoff = {
  id: string;
  kind: 'PICKUP' | 'DELIVERY';
  attempt_no: number;
  status: 'PENDING' | 'CONFIRMED' | 'WITHDRAWN' | 'SUPERSEDED';
  initiated_by: string;
  initiated_at: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  withdrawn_at: string | null;
  withdrawn_by: string | null;
  withdrawal_reason: string | null;
  container_id: string | null;
  superseded_by_incident_id: string | null;
  superseded_at: string | null;
};

export type HandoffActionResult = {
  shipment_id: string;
  shipment_status: ShipmentStatus;
  version: number;
  handoff: Handoff;
  container_id: string | null;
  container_status: string | null;
};

export type ShipmentCaseType =
  | 'PICKUP_PENDING'
  | 'DELIVERY_PENDING'
  | 'PROPOSAL_PENDING'
  | 'SHIPMENT_OVERDUE';

export type StalledShipment = {
  shipment_id: string;
  status: string;
  region_id: string;
  last_event_at: string;
  age_seconds: number;
  case_type: ShipmentCaseType;
  container_id: string | null;
  proposal_id: string | null;
};

export type PagedStalledShipment = {
  items: StalledShipment[];
  total: number;
  limit: number;
  offset: number;
};

export type AnalyticsGranularity = 'day' | 'week';

export type AnalyticsPeriodBucket = {
  period_start: string;
  period_end: string;
  total_shipments: number;
  completed_shipments: number;
  partially_delivered_shipments: number;
  sample_issues: number;
  container_issues: number;
};

export type AnalyticsFacility = {
  organization_id: string;
  shipment_count: number;
  completed_count: number;
  with_issues_count: number;
};

export type ShipmentSummary = {
  region_id: string;
  from: string | null;
  to: string | null;
  total: number;
  by_status: Record<string, number>;
  completed_with_issues: number;
  missing_samples: number;
  rejected_samples: number;
  overdue_active: number;
  closed_undelivered: number;
  container_missing: number;
  container_rejected: number;
  partially_delivered: number;
  partially_completed_with_issues: number;
  average_full_shipment_delivery_seconds: number | null;
  average_received_container_delivery_seconds: number | null;
  period_buckets: AnalyticsPeriodBucket[];
  by_facility: AnalyticsFacility[];
};

export type NotificationPayload = {
  case_type: ShipmentCaseType;
  shipment_id: string;
  region_id: string;
  status: string;
  last_event_at: string;
  container_id?: string | null;
  proposal_id?: string | null;
};

export type Notification = {
  id: string;
  event_id: string;
  type: string;
  payload: NotificationPayload;
  recipient_user_id: string;
  created_at: string;
  read_at: string | null;
};

export type PagedNotification = {
  items: Notification[];
  total: number;
  limit: number;
  offset: number;
};

export type NotificationUnreadCount = {
  count: number;
};

export type OperationsCapabilities = {
  canUseCourier: boolean;
  canViewCases: boolean;
  canViewAnalytics: boolean;
  caseRegionIds: string[];
  analyticsRegionIds: string[];
};

const CASE_ACCESS_ROLES = new Set([
  'SENDER_OPERATOR',
  'COURIER',
  'LAB_OPERATOR',
  'LAB_MANAGER',
  'REGIONAL_OPERATIONS',
]);

const ANALYTICS_ACCESS_ROLES = new Set(['REGIONAL_ANALYST', 'REGIONAL_OPERATIONS']);

export function deriveOperationsCapabilities(user: CurrentUser): OperationsCapabilities {
  const memberships = user.memberships ?? [];
  const caseMemberships = memberships.filter((membership) => CASE_ACCESS_ROLES.has(membership.role));
  const analyticsMemberships = memberships.filter((membership) =>
    ANALYTICS_ACCESS_ROLES.has(membership.role),
  );
  const distinctRegions = (items: CourierMembership[]) =>
    [...new Set(items.map((membership) => membership.region_id).filter(Boolean))].sort();

  return {
    canUseCourier: memberships.some((membership) => membership.role === 'COURIER'),
    canViewCases: caseMemberships.length > 0,
    canViewAnalytics: analyticsMemberships.length > 0,
    caseRegionIds: distinctRegions(caseMemberships),
    analyticsRegionIds: distinctRegions(analyticsMemberships),
  };
}

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
