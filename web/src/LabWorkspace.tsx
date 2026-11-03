import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  ApiError,
  completeReceiving,
  confirmLabContainerReceipt,
  getReceivingHistory,
  getShipmentManifest,
  listIssueReasons,
  listShipments,
  recordSampleResult,
  reopenReceiving,
} from './api';
import type {
  CurrentUser,
  IssueReason,
  ReceiptResult,
  ReceivingHistory,
  Shipment,
  ShipmentManifest,
  ShipmentManifestItem,
} from './types';

type LabWorkspaceProps = {
  user: CurrentUser;
  token: string;
  sessionSignal: AbortSignal;
  isSessionCurrent: (userId: string) => boolean;
  onUnauthorized: () => void;
};

type ResultDraft = {
  result: ReceiptResult;
  reasonCode: string;
  comment: string;
};

const STATUS_LABELS: Record<string, string> = {
  DELIVERY_PENDING: 'Ожидает приёмки',
  PARTIALLY_DELIVERED: 'Частично доставлено',
  RECEIVING: 'Приёмка',
  COMPLETED: 'Завершено',
  COMPLETED_WITH_ISSUES: 'Завершено с замечаниями',
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status.replaceAll('_', ' ');
}

function messageForError(error: unknown): string {
  return error instanceof Error ? error.message : 'Не удалось выполнить запрос. Повторите попытку.';
}

function idempotencyKey(): string {
  return crypto.randomUUID();
}

function formatDate(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function LabWorkspace({
  user,
  token,
  sessionSignal,
  isSessionCurrent,
  onUnauthorized,
}: LabWorkspaceProps) {
  const destinationOrganizations = useMemo(
    () => new Set(
      user.memberships
        .filter((membership) =>
          (membership.role === 'LAB_OPERATOR' || membership.role === 'LAB_MANAGER') &&
          membership.organization_id,
        )
        .map((membership) => membership.organization_id as string),
    ),
    [user.memberships],
  );
  const [shipments, setShipments] = useState<Shipment[]>([]);
  const [selectedShipmentId, setSelectedShipmentId] = useState('');
  const [manifest, setManifest] = useState<ShipmentManifest | null>(null);
  const [history, setHistory] = useState<ReceivingHistory | null>(null);
  const [historyNextOffset, setHistoryNextOffset] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [detailsShipmentId, setDetailsShipmentId] = useState('');
  const [reasons, setReasons] = useState<IssueReason[]>([]);
  const [receiptCodes, setReceiptCodes] = useState<Record<string, string>>({});
  const [resultDrafts, setResultDrafts] = useState<Record<string, ResultDraft>>({});
  const [reopenReason, setReopenReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const operationKeys = useRef(new Map<string, string>());
  const loadSequence = useRef(0);

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

  const requestIsCurrent = useCallback((userId: string, signal: AbortSignal): boolean =>
    !signal.aborted && isSessionCurrent(userId), [isSessionCurrent]);

  const handleError = useCallback((cause: unknown, userId: string, signal: AbortSignal) => {
    if (!requestIsCurrent(userId, signal)) return;
    setError(messageForError(cause));
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized();
  }, [onUnauthorized, requestIsCurrent]);

  const selectedShipment = shipments.find((shipment) => shipment.id === selectedShipmentId) ?? null;
  const canOperateSelected = Boolean(
    selectedShipment && user.memberships.some((membership) =>
      membership.role === 'LAB_OPERATOR' &&
      membership.organization_id === selectedShipment.destination_org_id,
    ),
  );
  const canManageSelected = Boolean(
    selectedShipment && user.memberships.some((membership) =>
      membership.role === 'LAB_MANAGER' &&
      membership.organization_id === selectedShipment.destination_org_id,
    ),
  );

  useEffect(() => {
    const controller = new AbortController();
    const userId = user.id;
    const abortFromSession = () => controller.abort();
    if (sessionSignal.aborted) controller.abort();
    else sessionSignal.addEventListener('abort', abortFromSession, { once: true });
    void Promise.all([
      listShipments(token, controller.signal),
      listIssueReasons(token, controller.signal),
    ])
      .then(([shipmentResponse, reasonResponse]) => {
        if (!requestIsCurrent(userId, controller.signal)) return;
        const scopedShipments = shipmentResponse.items.filter((shipment) =>
          destinationOrganizations.has(shipment.destination_org_id),
        );
        setShipments(scopedShipments);
        setSelectedShipmentId((current) =>
          current && scopedShipments.some((shipment) => shipment.id === current)
            ? current
            : scopedShipments[0]?.id ?? '',
        );
        setReasons(reasonResponse.items.filter((reason) =>
          reason.is_active && reason.scope === 'SAMPLE',
        ));
      })
      .catch((cause: unknown) => {
        handleError(cause, userId, controller.signal);
      })
      .finally(() => {
        if (requestIsCurrent(userId, controller.signal)) setLoading(false);
      });
    return () => {
      sessionSignal.removeEventListener('abort', abortFromSession);
      controller.abort();
    };
  }, [destinationOrganizations, handleError, requestIsCurrent, sessionSignal, token, user.id]);

  useEffect(() => {
    const sequence = ++loadSequence.current;
    if (!selectedShipmentId) return;
    const controller = new AbortController();
    const abortFromSession = () => controller.abort();
    if (sessionSignal.aborted) controller.abort();
    else sessionSignal.addEventListener('abort', abortFromSession, { once: true });
    const userId = user.id;
    void Promise.all([
      getShipmentManifest(token, selectedShipmentId, controller.signal),
      getReceivingHistory(token, selectedShipmentId, controller.signal),
    ])
      .then(([nextManifest, nextHistory]) => {
        if (
          sequence !== loadSequence.current ||
          !requestIsCurrent(userId, controller.signal)
        ) return;
        setManifest(nextManifest);
        setHistory(nextHistory);
        setHistoryNextOffset(nextHistory.offset + nextHistory.revisions.length);
        setDetailsShipmentId(selectedShipmentId);
        setReceiptCodes({});
        setResultDrafts({});
      })
      .catch((cause: unknown) => {
        if (sequence === loadSequence.current) handleError(cause, userId, controller.signal);
      });
    return () => {
      sessionSignal.removeEventListener('abort', abortFromSession);
      controller.abort();
    };
  }, [handleError, requestIsCurrent, selectedShipmentId, sessionSignal, token, user.id]);

  const refreshSelected = useCallback(async (
    shipmentId: string,
    userId: string,
    signal: AbortSignal,
  ) => {
    const [shipmentResponse, nextManifest, nextHistory] = await Promise.all([
      listShipments(token, signal),
      getShipmentManifest(token, shipmentId, signal),
      getReceivingHistory(token, shipmentId, signal),
    ]);
    if (!requestIsCurrent(userId, signal)) return;
    setShipments(shipmentResponse.items.filter((shipment) =>
      destinationOrganizations.has(shipment.destination_org_id),
    ));
    if (selectedShipmentId === shipmentId) {
      setManifest(nextManifest);
      setHistoryNextOffset(nextHistory.offset + nextHistory.revisions.length);
      setHistory((current) => {
        const currentIds = new Set(nextHistory.revisions.map((revision) => revision.id));
        const previouslyLoaded = detailsShipmentId === shipmentId
          ? current?.revisions.filter((revision) => !currentIds.has(revision.id)) ?? []
          : [];
        return {
          ...nextHistory,
          revisions: [...nextHistory.revisions, ...previouslyLoaded],
          offset: 0,
        };
      });
      setDetailsShipmentId(shipmentId);
      setReceiptCodes({});
      setResultDrafts({});
    }
  }, [destinationOrganizations, detailsShipmentId, requestIsCurrent, selectedShipmentId, token]);

  async function loadOlderRevisionsHandler() {
    if (!selectedShipment || !history || history.revisions.length >= history.total_revisions || historyLoading) return;
    const shipmentId = selectedShipment.id;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const selectionSequence = loadSequence.current;
    const offset = historyNextOffset;
    setHistoryLoading(true);
    setError('');
    try {
      const page = await getReceivingHistory(token, shipmentId, signal, { limit: 100, offset });
      if (
        !requestIsCurrent(userId, signal) ||
        selectionSequence !== loadSequence.current ||
        detailsShipmentId !== shipmentId
      ) return;
      setHistory((current) => {
        if (!current || detailsShipmentId !== shipmentId) return current;
        const existingIds = new Set(current.revisions.map((revision) => revision.id));
        const additions = page.revisions.filter((revision) => !existingIds.has(revision.id));
        return {
          ...page,
          revisions: [...current.revisions, ...additions],
          offset: 0,
        };
      });
      setHistoryNextOffset(page.offset + page.revisions.length);
    } catch (cause) {
      handleError(cause, userId, signal);
    } finally {
      if (requestIsCurrent(userId, signal) && selectionSequence === loadSequence.current) {
        setHistoryLoading(false);
      }
    }
  }

  function selectShipment(shipmentId: string): void {
    loadSequence.current += 1;
    setHistoryLoading(false);
    setSelectedShipmentId(shipmentId);
  }

  async function confirmReceiptHandler(containerId: string) {
    if (!selectedShipment) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const code = (receiptCodes[containerId] ?? '').trim();
    if (!code) return;
    const operation = `receipt:${selectedShipment.id}:${containerId}:${code}`;
    setBusy(operation);
    setError('');
    setMessage('');
    try {
      const result = await confirmLabContainerReceipt(
        token,
        selectedShipment.id,
        containerId,
        code,
        keyFor(operation),
        signal,
      );
      if (!requestIsCurrent(userId, signal)) return;
      await refreshSelected(result.shipment_id, userId, signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setMessage(`Receipt confirmed by the server. Current Shipment status: ${statusLabel(result.shipment_status)}.`);
    } catch (cause) {
      handleError(cause, userId, signal);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy('');
    }
  }

  async function recordResultHandler(item: ShipmentManifestItem, draft: ResultDraft) {
    if (!selectedShipment || !canOperateSelected || manifest?.status !== 'RECEIVING') return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const body = draft.result === 'ACCEPTED'
      ? { result: 'ACCEPTED' as const }
      : {
          result: draft.result,
          reason_code: draft.reasonCode,
          comment: draft.comment.trim() || null,
        };
    if (body.result !== 'ACCEPTED' && !body.reason_code) {
      setError('Choose an active reason for this result.');
      return;
    }
    const operation = `result:${selectedShipment.id}:${item.id}:${JSON.stringify(body)}`;
    setBusy(operation);
    setError('');
    setMessage('');
    try {
      await recordSampleResult(
        token,
        selectedShipment.id,
        item.id,
        body,
        keyFor(operation),
        signal,
      );
      if (!requestIsCurrent(userId, signal)) return;
      await refreshSelected(selectedShipment.id, userId, signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setMessage(`Result saved for ${item.sample_code}.`);
    } catch (cause) {
      handleError(cause, userId, signal);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy('');
    }
  }

  async function completeHandler() {
    if (!selectedShipment || !canOperateSelected) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const operation = `complete:${selectedShipment.id}:${selectedShipment.version}`;
    setBusy(operation);
    setError('');
    setMessage('');
    try {
      const updated = await completeReceiving(
        token,
        selectedShipment.id,
        keyFor(operation),
        signal,
      );
      if (!requestIsCurrent(userId, signal)) return;
      await refreshSelected(updated.id, userId, signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setMessage(`Receiving completed by the server. Current Shipment status: ${statusLabel(updated.status)}.`);
    } catch (cause) {
      handleError(cause, userId, signal);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy('');
    }
  }

  async function reopenHandler(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedShipment || !canManageSelected) return;
    const userId = user.id;
    const signal = sessionSignal;
    if (!requestIsCurrent(userId, signal)) return;
    const reason = reopenReason.trim();
    if (!reason) {
      setError('A reason is required to reopen receiving.');
      return;
    }
    const operation = `reopen:${selectedShipment.id}:${reason}`;
    setBusy(operation);
    setError('');
    setMessage('');
    try {
      const updated = await reopenReceiving(
        token,
        selectedShipment.id,
        reason,
        keyFor(operation),
        signal,
      );
      if (!requestIsCurrent(userId, signal)) return;
      await refreshSelected(updated.id, userId, signal);
      if (!requestIsCurrent(userId, signal)) return;
      completeOperation(operation);
      setReopenReason('');
      setMessage(`Receiving reopened by the server. Current Shipment status: ${statusLabel(updated.status)}.`);
    } catch (cause) {
      handleError(cause, userId, signal);
    } finally {
      if (requestIsCurrent(userId, signal)) setBusy('');
    }
  }

  function updateResultDraft(item: ShipmentManifestItem, patch: Partial<ResultDraft>) {
    setResultDrafts((current) => {
      const currentDraft = current[item.id] ?? {
        result: item.receipt_result ?? 'ACCEPTED',
        reasonCode: item.receipt_reason_code ?? '',
        comment: item.receipt_comment ?? '',
      };
      const next = { ...currentDraft, ...patch };
      if (patch.result && patch.result !== currentDraft.result) {
        next.reasonCode = '';
        next.comment = '';
      }
      return { ...current, [item.id]: next };
    });
  }

  function draftFor(item: ShipmentManifestItem): ResultDraft {
    return resultDrafts[item.id] ?? {
      result: item.receipt_result ?? 'ACCEPTED',
      reasonCode: item.receipt_reason_code ?? '',
      comment: item.receipt_comment ?? '',
    };
  }

  return (
    <div className="lab-workspace">
      <section className="operations-intro" aria-labelledby="lab-workspace-heading">
        <div>
          <p className="eyebrow">Destination laboratory</p>
          <h1 id="lab-workspace-heading">Lab workspace</h1>
          <p className="muted">Confirm each Delivery using the sealed container code, then record sample results in the open receiving session.</p>
        </div>
        <div className="operations-stamp" aria-hidden="true">L</div>
      </section>

      {error && <p className="page-alert" role="alert">{error}</p>}
      {message && <p className="page-message" role="status" aria-live="polite">{message}</p>}
      {loading && <p role="status">Loading destination Shipments…</p>}

      {!loading && shipments.length === 0 && (
        <section className="panel lab-panel">
          <p className="muted">No Shipments are assigned to this Laboratory.</p>
        </section>
      )}

      {!loading && shipments.length > 0 && (
        <div className="lab-grid">
          <section className="panel lab-panel" aria-labelledby="lab-shipments-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Destination organization</p>
                <h2 id="lab-shipments-heading">Shipments</h2>
              </div>
            </div>
            <ul className="lab-shipment-list" aria-label="Destination Shipments">
              {shipments.map((shipment) => (
                <li key={shipment.id}>
                  <button
                    aria-pressed={shipment.id === selectedShipmentId}
                    className={`lab-shipment-select ${shipment.id === selectedShipmentId ? 'is-selected' : ''}`}
                    disabled={Boolean(busy)}
                    onClick={() => selectShipment(shipment.id)}
                    type="button"
                  >
                    <strong>{shipment.code}</strong>
                    <span>{statusLabel(shipment.status)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <div className="lab-detail-column">
            {selectedShipment && detailsShipmentId !== selectedShipment.id && (
              <p role="status">Loading Shipment manifest and receiving history…</p>
            )}
            {selectedShipment && detailsShipmentId === selectedShipment.id && manifest && (
              <section className="panel lab-panel" aria-labelledby="lab-detail-heading">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">Server record</p>
                    <h2 id="lab-detail-heading">Shipment {selectedShipment.code}</h2>
                  </div>
                  <span className="status-badge" data-testid="lab-shipment-status">{selectedShipment.status}</span>
                </div>
                {manifest ? (
                  <>
                    <p className="muted">Manifest revision {manifest.manifest_revision}</p>
                    <div className="lab-container-list">
                      {manifest.containers.map((container) => (
                        <section className="lab-container-card" key={container.id} aria-labelledby={`container-${container.id}`}>
                          <div className="section-heading">
                            <div>
                              <p className="eyebrow">Container</p>
                              <h3 id={`container-${container.id}`}>{container.code}</h3>
                            </div>
                            <span className="status-badge">{container.delivery_status}</span>
                          </div>
                          {container.delivery_status === 'DELIVERY_PENDING' && (canOperateSelected || canManageSelected) && (
                            <div className="stack-form lab-receipt-form">
                              <label htmlFor={`receipt-code-${container.id}`}>Receipt container code</label>
                              <input
                                id={`receipt-code-${container.id}`}
                                autoComplete="off"
                                onChange={(event) => setReceiptCodes((current) => ({
                                  ...current,
                                  [container.id]: event.target.value,
                                }))}
                                value={receiptCodes[container.id] ?? ''}
                              />
                              <button
                                className="button button-secondary"
                                disabled={Boolean(busy) || !(receiptCodes[container.id] ?? '').trim()}
                                onClick={() => void confirmReceiptHandler(container.id)}
                                type="button"
                              >
                                Confirm receipt
                              </button>
                            </div>
                          )}
                          <ul className="lab-item-list" aria-label={`Samples in ${container.code}`}>
                            {container.items.map((item) => {
                              const draft = draftFor(item);
                              const matchingReasons = reasons.filter((reason) => reason.kind === draft.result);
                              return (
                                <li className="lab-item-card" key={item.id}>
                                  <div className="lab-item-summary">
                                    <strong>{item.sample_code}</strong>
                                    <span>{item.material_type}</span>
                                  </div>
                                  <p className="muted">
                                    Result: {item.receipt_result ?? 'Not recorded'}
                                    {item.receipt_reason_code ? ` · ${item.receipt_reason_code}` : ''}
                                  </p>
                                  {canOperateSelected &&
                                    container.delivery_status === 'RECEIVED' &&
                                    manifest.status === 'RECEIVING' && (
                                      <div className="stack-form lab-result-form">
                                        <label htmlFor={`result-${item.id}`}>Result for {item.sample_code}</label>
                                        <select
                                          id={`result-${item.id}`}
                                          onChange={(event) => updateResultDraft(item, {
                                            result: event.target.value as ReceiptResult,
                                          })}
                                          value={draft.result}
                                        >
                                          <option value="ACCEPTED">Accepted</option>
                                          <option value="MISSING">Missing</option>
                                          <option value="REJECTED">Rejected</option>
                                        </select>
                                        {draft.result !== 'ACCEPTED' && (
                                          <>
                                            <label htmlFor={`reason-${item.id}`}>Reason for {item.sample_code}</label>
                                            <select
                                              id={`reason-${item.id}`}
                                              onChange={(event) => updateResultDraft(item, { reasonCode: event.target.value })}
                                              value={draft.reasonCode}
                                              required
                                            >
                                              <option value="">Select an active Sample reason</option>
                                              {matchingReasons.map((reason) => (
                                                <option key={reason.id} value={reason.code}>{reason.label}</option>
                                              ))}
                                            </select>
                                            <label htmlFor={`comment-${item.id}`}>Comment for {item.sample_code}</label>
                                            <textarea
                                              id={`comment-${item.id}`}
                                              maxLength={500}
                                              onChange={(event) => updateResultDraft(item, { comment: event.target.value })}
                                              value={draft.comment}
                                            />
                                          </>
                                        )}
                                        <button
                                          className="button button-secondary"
                                          disabled={Boolean(busy) || (draft.result !== 'ACCEPTED' && !draft.reasonCode)}
                                          onClick={() => void recordResultHandler(item, draft)}
                                          type="button"
                                        >
                                          Record result for {item.sample_code}
                                        </button>
                                      </div>
                                    )}
                                </li>
                              );
                            })}
                          </ul>
                        </section>
                      ))}
                    </div>

                    {canOperateSelected && manifest.status === 'RECEIVING' && (
                      <button
                        className="button button-primary lab-complete-button"
                        disabled={Boolean(busy)}
                        onClick={() => void completeHandler()}
                        type="button"
                      >
                        Complete receiving
                      </button>
                    )}

                    {canManageSelected &&
                      ['COMPLETED', 'COMPLETED_WITH_ISSUES'].includes(selectedShipment.status) && (
                        <form className="stack-form lab-reopen-form" onSubmit={reopenHandler}>
                          <h3>Reopen receiving</h3>
                          <label htmlFor="reopen-reason">Reopen reason</label>
                          <textarea
                            id="reopen-reason"
                            maxLength={500}
                            onChange={(event) => setReopenReason(event.target.value)}
                            required
                            value={reopenReason}
                          />
                          <button className="button button-secondary" disabled={Boolean(busy) || !reopenReason.trim()} type="submit">
                            Reopen receiving
                          </button>
                        </form>
                      )}
                  </>
                ) : (
                  <p role="status">Loading Shipment manifest…</p>
                )}
              </section>
            )}

            {selectedShipment && detailsShipmentId === selectedShipment.id && history && (
              <section className="panel lab-panel" aria-labelledby="lab-history-heading">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">Receiving audit trail</p>
                    <h2 id="lab-history-heading">Receiving history</h2>
                  </div>
                </div>
                <h3>Sessions</h3>
                {history.sessions.length === 0 ? (
                  <p className="muted">No receiving session has been opened yet.</p>
                ) : (
                  <ol className="lab-history-list">
                    {history.sessions.map((session) => (
                      <li key={session.id}>
                        <strong>Session {session.sequence} · {session.status}</strong>
                        <span>Opened {formatDate(session.opened_at)}</span>
                        {session.closed_at && <span>Closed {formatDate(session.closed_at)}</span>}
                        {session.reopen_reason && <span>Reopen reason: {session.reopen_reason}</span>}
                      </li>
                    ))}
                  </ol>
                )}
                <h3>Result revisions</h3>
                <p className="muted">Showing {history.revisions.length} of {history.total_revisions} revisions</p>
                {history.revisions.length === 0 ? (
                  <p className="muted">No result revisions have been recorded.</p>
                ) : (
                  <ol className="lab-history-list">
                    {history.revisions.map((revision) => (
                      <li key={revision.id}>
                        <strong>{revision.old_result ?? '—'} → {revision.new_result}</strong>
                        <span>Sample item {revision.container_item_id}</span>
                        {revision.new_reason_code && <span>Reason: {revision.new_reason_code}</span>}
                        {revision.new_comment && <span>Comment: {revision.new_comment}</span>}
                        <span>Changed by {revision.changed_by}</span>
                        <span>Changed {formatDate(revision.changed_at)}</span>
                      </li>
                    ))}
                  </ol>
                )}
                {history.revisions.length < history.total_revisions && (
                  <button
                    className="button button-secondary"
                    disabled={historyLoading || Boolean(busy)}
                    onClick={() => void loadOlderRevisionsHandler()}
                    type="button"
                  >
                    {historyLoading ? 'Loading revisions…' : 'Load older revisions'}
                  </button>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
