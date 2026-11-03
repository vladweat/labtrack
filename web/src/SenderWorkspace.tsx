import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  addSampleToContainer,
  addShipmentContainer,
  ApiError,
  assignShipmentCourier,
  createSample,
  createShipment,
  listOrganizationCouriers,
  listOrganizations,
  listSamples,
  releaseShipmentPickup,
  sealShipment,
} from './api';
import type {
  CurrentUser,
  Organization,
  OrganizationCourier,
  Sample,
  Shipment,
  TransportContainer,
} from './types';

type SenderWorkspaceProps = {
  user: CurrentUser;
  token: string;
  sessionSignal: AbortSignal;
  isSessionCurrent: (userId: string) => boolean;
  onUnauthorized: () => void;
};

type ContainerDraft = TransportContainer & { sampleCodes: string[]; sampleIds: string[] };

const SHIPMENT_STATUS_LABELS: Record<string, string> = {
  DRAFT: 'Черновик',
  READY_FOR_PICKUP: 'Готово к забору',
  PICKUP_PENDING: 'Ожидает забора',
};

function messageForError(error: unknown): string {
  return error instanceof Error ? error.message : 'Не удалось выполнить запрос. Повторите попытку.';
}

function statusLabel(status: string): string {
  return SHIPMENT_STATUS_LABELS[status] ?? status.replaceAll('_', ' ');
}

function idempotencyKey(): string {
  return crypto.randomUUID();
}

export function SenderWorkspace({
  user,
  token,
  sessionSignal,
  isSessionCurrent,
  onUnauthorized,
}: SenderWorkspaceProps) {
  const senderOrganizationIds = useMemo(
    () => [...new Set(
      user.memberships
        .filter((membership) => membership.role === 'SENDER_OPERATOR' && membership.organization_id)
        .map((membership) => membership.organization_id as string),
    )],
    [user.memberships],
  );
  const [senderOrganizations, setSenderOrganizations] = useState<Organization[]>([]);
  const [destinationOrganizations, setDestinationOrganizations] = useState<Organization[]>([]);
  const [senderOrganizationId, setSenderOrganizationId] = useState('');
  const [destinationOrganizationId, setDestinationOrganizationId] = useState('');
  const [courierResult, setCourierResult] = useState<{
    organizationId: string;
    items: OrganizationCourier[];
  }>({ organizationId: '', items: [] });
  const [courierId, setCourierId] = useState('');
  const [sampleResult, setSampleResult] = useState<{ organizationId: string; items: Sample[] }>({
    organizationId: '',
    items: [],
  });
  const [materialType, setMaterialType] = useState<Sample['material_type']>('BLOOD');
  const [expectedDeliveryAt, setExpectedDeliveryAt] = useState('');
  const [shipment, setShipment] = useState<Shipment | null>(null);
  const [containers, setContainers] = useState<ContainerDraft[]>([]);
  const [selectedContainerId, setSelectedContainerId] = useState('');
  const [sampleToAdd, setSampleToAdd] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const operationKeys = useRef(new Map<string, string>());

  function keyFor(operation: string): string {
    const existing = operationKeys.current.get(operation);
    if (existing) return existing;
    const key = idempotencyKey();
    operationKeys.current.set(operation, key);
    return key;
  }

  function completeOperation(operation: string): void {
    operationKeys.current.delete(operation);
  }

  const handleError = useCallback((cause: unknown): void => {
    setError(messageForError(cause));
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized();
  }, [onUnauthorized]);

  const couriers = courierResult.organizationId === senderOrganizationId ? courierResult.items : [];
  const samples = sampleResult.organizationId === senderOrganizationId ? sampleResult.items : [];

  useEffect(() => {
    const controller = new AbortController();
    void listOrganizations(token, { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        const allowedSenders = response.items.filter((organization) =>
          senderOrganizationIds.includes(organization.id),
        );
        const laboratories = response.items.filter((organization) =>
          organization.kind === 'LABORATORY',
        );
        const firstSender = allowedSenders[0];
        const firstSenderLaboratories = laboratories.filter(
          (organization) => organization.region_id === firstSender?.region_id,
        );
        setSenderOrganizations(allowedSenders);
        setDestinationOrganizations(laboratories);
        setSenderOrganizationId((current) =>
          current && allowedSenders.some((organization) => organization.id === current)
            ? current
            : allowedSenders[0]?.id ?? '',
        );
        setDestinationOrganizationId((current) =>
          current && firstSenderLaboratories.some((organization) => organization.id === current)
            ? current
            : firstSenderLaboratories[0]?.id ?? '',
        );
        if (allowedSenders.length === 0) {
          setError('No active Sender organization is assigned to this account.');
        }
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) handleError(cause);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [handleError, senderOrganizationIds, token]);

  useEffect(() => {
    if (!senderOrganizationId) {
      return;
    }
    const controller = new AbortController();
    void Promise.all([
      listOrganizationCouriers(token, senderOrganizationId, controller.signal),
      listSamples(token, senderOrganizationId, controller.signal),
    ])
      .then(([courierResponse, sampleResponse]) => {
        if (controller.signal.aborted) return;
        setCourierResult({ organizationId: senderOrganizationId, items: courierResponse.items });
        setCourierId(courierResponse.items[0]?.id ?? '');
        setSampleResult({ organizationId: senderOrganizationId, items: sampleResponse.items });
        setSampleToAdd((current) =>
          current && sampleResponse.items.some((sample) => sample.id === current)
            ? current
            : sampleResponse.items[0]?.id ?? '',
        );
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) handleError(cause);
      });
    return () => controller.abort();
  }, [handleError, senderOrganizationId, token]);

  const assignedSampleIds = useMemo(
    () => new Set(containers.flatMap((container) => container.sampleIds)),
    [containers],
  );
  const availableSamples = samples.filter((sample) => !assignedSampleIds.has(sample.id));
  const selectedContainer = containers.find((container) => container.id === selectedContainerId) ?? null;
  const selectedSenderRegionId = senderOrganizations.find(
    (organization) => organization.id === senderOrganizationId,
  )?.region_id;
  const availableDestinationOrganizations = destinationOrganizations.filter(
    (organization) => organization.region_id === selectedSenderRegionId,
  );

  function handleSenderOrganizationChange(organizationId: string): void {
    setSenderOrganizationId(organizationId);
    setCourierId('');
    setSampleToAdd('');
    const regionId = senderOrganizations.find((organization) => organization.id === organizationId)?.region_id;
    setDestinationOrganizationId((current) =>
      current && destinationOrganizations.some(
        (organization) => organization.id === current && organization.region_id === regionId,
      )
        ? current
        : destinationOrganizations.find((organization) => organization.region_id === regionId)?.id ?? '',
    );
  }

  function requestIsCurrent(userId: string, signal: AbortSignal): boolean {
    return !signal.aborted && isSessionCurrent(userId);
  }

  async function createSampleHandler(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!senderOrganizationId) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const operation = `sample:${senderOrganizationId}:${materialType}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const sample = await createSample(token, {
        origin_organization_id: senderOrganizationId,
        material_type: materialType,
      }, keyFor(operation), signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setSampleResult((current) => ({
        organizationId: senderOrganizationId,
        items: [
          sample,
          ...(current.organizationId === senderOrganizationId ? current.items : []).filter(
            (item) => item.id !== sample.id,
          ),
        ],
      }));
      setSampleToAdd(sample.id);
      setMessage(`Sample ${sample.code} was created by the server.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  async function createShipmentHandler(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!senderOrganizationId || !destinationOrganizationId) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const operation = `shipment:${senderOrganizationId}:${destinationOrganizationId}:${expectedDeliveryAt}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const created = await createShipment(token, {
        sender_org_id: senderOrganizationId,
        destination_org_id: destinationOrganizationId,
        expected_delivery_at: expectedDeliveryAt ? new Date(expectedDeliveryAt).toISOString() : null,
      }, keyFor(operation), signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setShipment(created);
      setContainers([]);
      setMessage(`Shipment ${created.code} was created. Current server status: ${statusLabel(created.status)}.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  async function assignCourierHandler(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!shipment || !courierId) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const operation = `courier:${shipment.id}:${courierId}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const updated = await assignShipmentCourier(token, shipment.id, courierId, keyFor(operation), signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setShipment(updated);
      setMessage(`Courier assigned. Current server status: ${statusLabel(updated.status)}.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  async function addContainerHandler() {
    if (!shipment) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const operation = `container:${shipment.id}:${containers.length}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const created = await addShipmentContainer(token, shipment.id, keyFor(operation), signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      const container: ContainerDraft = { ...created, sampleCodes: [], sampleIds: [] };
      setContainers((current) => [...current, container]);
      setSelectedContainerId(container.id);
      setMessage(`Container ${container.code} was added by the server.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  async function addSampleHandler(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!shipment || !selectedContainer || !sampleToAdd) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const sample = samples.find((item) => item.id === sampleToAdd);
    if (!sample) return;
    const operation = `sample-item:${shipment.id}:${selectedContainer.id}:${sample.id}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await addSampleToContainer(
        token,
        shipment.id,
        selectedContainer.id,
        sample.id,
        keyFor(operation),
        signal,
      );
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setContainers((current) => current.map((container) =>
        container.id === selectedContainer.id
          ? {
              ...container,
              sampleIds: [...container.sampleIds, sample.id],
              sampleCodes: [...container.sampleCodes, sample.code],
            }
          : container,
      ));
      setSampleToAdd(availableSamples.find((item) => item.id !== sample.id)?.id ?? '');
      setMessage(`Sample ${sample.code} was added to ${selectedContainer.code}.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  async function sealShipmentHandler() {
    if (!shipment) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const operation = `seal:${shipment.id}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const updated = await sealShipment(token, shipment.id, keyFor(operation), signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setShipment(updated);
      setMessage(`Shipment sealed. Current server status: ${statusLabel(updated.status)}.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  async function releasePickupHandler() {
    if (!shipment || containers.length === 0) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const codes = containers.map((container) => container.code);
    const operation = `release:${shipment.id}:${codes.join(',')}`;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await releaseShipmentPickup(token, shipment.id, codes, keyFor(operation), signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setShipment((current) => current
        ? { ...current, status: result.shipment_status, version: result.version, updated_at: result.handoff.initiated_at }
        : current,
      );
      setMessage(`Pickup release recorded. Current server status: ${statusLabel(result.shipment_status)}.`);
    } catch (cause) {
      if (requestIsCurrent(userId, signal)) handleError(cause);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy(false);
    }
  }

  return (
    <div className="sender-workspace">
      <section className="operations-intro" aria-labelledby="sender-workspace-heading">
        <div>
          <p className="eyebrow">Shipment preparation</p>
          <h1 id="sender-workspace-heading">Sender workspace</h1>
          <p className="muted">Create samples and prepare a Shipment. The server validates each action and owns every status transition.</p>
        </div>
        <div className="operations-stamp" aria-hidden="true">S</div>
      </section>

      {error && <p className="page-alert" role="alert">{error}</p>}
      {message && <p className="page-message" role="status" aria-live="polite">{message}</p>}
      {loading && <p role="status">Loading Sender organizations…</p>}

      {!loading && senderOrganizations.length === 0 && (
        <section className="panel sender-panel">
          <p className="muted">This account has no active Sender organization available.</p>
        </section>
      )}

      {senderOrganizations.length > 0 && (
        <div className="sender-grid">
          <section className="panel sender-panel" aria-labelledby="sender-samples-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Origin organization</p>
                <h2 id="sender-samples-heading">Samples</h2>
              </div>
            </div>
            <label htmlFor="sample-origin-organization">Sample origin organization</label>
            <select
              id="sample-origin-organization"
              onChange={(event) => handleSenderOrganizationChange(event.target.value)}
              value={senderOrganizationId}
            >
              {senderOrganizations.map((organization) => (
                <option key={organization.id} value={organization.id}>{organization.name}</option>
              ))}
            </select>
            <form className="stack-form" onSubmit={createSampleHandler}>
              <label htmlFor="sample-material-type">Material type</label>
              <select
                id="sample-material-type"
                onChange={(event) => setMaterialType(event.target.value as Sample['material_type'])}
                value={materialType}
              >
                <option value="BLOOD">Blood</option>
                <option value="URINE">Urine</option>
                <option value="OTHER">Other</option>
              </select>
              <button className="button button-primary" disabled={busy || loading} type="submit">
                {busy ? 'Saving…' : 'Create sample'}
              </button>
            </form>

            <h3 className="sender-subheading">Available samples</h3>
            {samples.length === 0 ? (
              <p className="muted">No samples are available for this organization.</p>
            ) : (
              <ul className="sender-resource-list" aria-label="Available samples">
                {samples.map((sample) => (
                  <li key={sample.id}>
                    <span><strong>{sample.code}</strong><small>{sample.material_type}</small></span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="sender-workflow">
            {!shipment ? (
              <section className="panel sender-panel" aria-labelledby="sender-create-shipment-heading">
                <p className="eyebrow">New draft</p>
                <h2 id="sender-create-shipment-heading">Create draft Shipment</h2>
                <form className="stack-form" onSubmit={createShipmentHandler}>
                  <label htmlFor="shipment-sender-organization">Sender organization</label>
                  <select
                    id="shipment-sender-organization"
                    onChange={(event) => handleSenderOrganizationChange(event.target.value)}
                    value={senderOrganizationId}
                  >
                    {senderOrganizations.map((organization) => (
                      <option key={organization.id} value={organization.id}>{organization.name}</option>
                    ))}
                  </select>
                  <label htmlFor="destination-laboratory">Destination laboratory</label>
                  <select
                    id="destination-laboratory"
                    onChange={(event) => setDestinationOrganizationId(event.target.value)}
                    value={destinationOrganizationId}
                    required
                  >
                    <option value="" disabled>Select a laboratory</option>
                    {availableDestinationOrganizations.map((organization) => (
                      <option key={organization.id} value={organization.id}>{organization.name}</option>
                    ))}
                  </select>
                  <p className="muted">The server confirms whether this is the configured destination laboratory.</p>
                  <label htmlFor="expected-delivery">Expected delivery</label>
                  <input
                    id="expected-delivery"
                    onChange={(event) => setExpectedDeliveryAt(event.target.value)}
                    required
                    type="datetime-local"
                    value={expectedDeliveryAt}
                  />
                  <button className="button button-primary" disabled={busy || !destinationOrganizationId} type="submit">
                    {busy ? 'Creating…' : 'Create draft Shipment'}
                  </button>
                </form>
              </section>
            ) : (
              <section className="panel sender-panel sender-shipment" aria-labelledby="sender-shipment-heading">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">Server record</p>
                    <h2 id="sender-shipment-heading">Shipment {shipment.code}</h2>
                  </div>
                  <span className="status-badge">{statusLabel(shipment.status)}</span>
                </div>
                <p className="sender-shipment-id mono">{shipment.id}</p>

                {shipment.status === 'DRAFT' && (
                  <>
                    <form className="stack-form sender-inline-form" onSubmit={assignCourierHandler}>
                      <label htmlFor="sender-courier">Courier</label>
                      <select id="sender-courier" onChange={(event) => setCourierId(event.target.value)} value={courierId} required>
                        {couriers.length === 0 ? <option value="">No active Couriers available</option> : null}
                        {couriers.map((courier) => <option key={courier.id} value={courier.id}>{courier.display_name}</option>)}
                      </select>
                      <button className="button button-secondary" disabled={busy || !courierId} type="submit">Assign Courier</button>
                    </form>

                    <div className="sender-container-heading">
                      <h3>Containers</h3>
                      <button className="button button-secondary" disabled={busy} onClick={addContainerHandler} type="button">Add container</button>
                    </div>
                    {containers.length === 0 ? (
                      <p className="muted">Add a container, then assign at least one sample to every container.</p>
                    ) : (
                      <ul className="sender-resource-list sender-container-list" aria-label="Shipment containers">
                        {containers.map((container) => (
                          <li className={container.id === selectedContainerId ? 'is-selected' : ''} key={container.id}>
                            <button
                              aria-pressed={container.id === selectedContainerId}
                              className="sender-container-select"
                              onClick={() => setSelectedContainerId(container.id)}
                              type="button"
                            >
                              <strong>{container.code}</strong>
                              <span>{container.sampleCodes.length} samples</span>
                            </button>
                            {container.sampleCodes.length > 0 && (
                              <small>{container.sampleCodes.join(', ')}</small>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}

                    {selectedContainer && availableSamples.length > 0 && (
                      <form className="stack-form sender-inline-form" onSubmit={addSampleHandler}>
                        <label htmlFor="sample-to-add">Sample to add</label>
                        <select id="sample-to-add" onChange={(event) => setSampleToAdd(event.target.value)} value={sampleToAdd}>
                          {availableSamples.map((sample) => <option key={sample.id} value={sample.id}>{sample.code} · {sample.material_type}</option>)}
                        </select>
                        <button className="button button-secondary" disabled={busy} type="submit">Add sample to container</button>
                      </form>
                    )}

                    <button className="button button-primary sender-action-button" disabled={busy} onClick={sealShipmentHandler} type="button">Seal Shipment</button>
                  </>
                )}

                {shipment.status === 'READY_FOR_PICKUP' && (
                  <section className="sender-release" aria-labelledby="sender-release-heading">
                    <p className="eyebrow">Complete manifest confirmation</p>
                    <h3 id="sender-release-heading">Release Pickup</h3>
                    <p className="muted">The server will verify the assigned Courier and complete sealed container code set.</p>
                    <ul className="sender-code-list" aria-label="Sealed container codes">
                      {containers.map((container) => <li key={container.id}><code>{container.code}</code></li>)}
                    </ul>
                    <button className="button button-primary" disabled={busy} onClick={releasePickupHandler} type="button">Release Pickup</button>
                  </section>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
