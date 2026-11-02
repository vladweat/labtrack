import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import {
  ApiError,
  currentUser,
  getInboxUnreadCount,
  login,
  offlineManifest,
  syncCommands,
} from './api';
import { CommandQueue } from './CommandQueue';
import { NotificationInbox } from './NotificationInbox';
import { OperationsDashboard } from './OperationsDashboard';
import {
  addCommand,
  getActiveUserId,
  listQueue,
  listSnapshots,
  purgeUserData,
  saveOutcome,
  saveSnapshot,
  setActiveUserId,
  setCommandState,
} from './storage';
import { applySyncResult } from './sync';
import type {
  CurrentUser,
  DeliveryCommand,
  OfflineManifest,
  OfflineCommand,
  PickupCommand,
  QueueItem,
} from './types';
import { deriveOperationsCapabilities } from './types';

type Workspace = 'courier' | 'operations' | 'inbox';

type InboxCountResult =
  | { requestKey: string; count: number }
  | { requestKey: string; error: string };

type PurgeDialog =
  | { kind: 'switch-login' }
  | { kind: 'logout'; userId: string; unsyncedCount: number; error?: string }
  | {
      kind: 'account-change';
      previousUserId: string;
      targetUser: CurrentUser;
      targetToken: string;
      unsyncedCount: number;
      error?: string;
    };

function createCommandId(): string {
  return crypto.randomUUID();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The request could not be completed.';
}

function inboxCountFailure(error: unknown, isOnline: boolean): string {
  if (!isOnline) return 'Нет подключения к сети. Счётчик непрочитанных не обновлён.';
  if (error instanceof TypeError) {
    return 'Не удалось подключиться к серверу для обновления счётчика.';
  }
  return `Не удалось обновить счётчик непрочитанных уведомлений. ${errorMessage(error)}`;
}

function sortedCodes(value: string): string[] {
  return value
    .split(/[\s,;]+/)
    .map((code) => code.trim())
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right));
}

function codesMatch(supplied: string, expected: string[]): boolean {
  const actual = sortedCodes(supplied);
  return actual.length === expected.length && actual.every((code, index) => code === [...expected].sort()[index]);
}

function snapshotIsOver24HoursOld(snapshot: OfflineManifest): boolean {
  return Date.now() - Date.parse(snapshot.issued_at) > 24 * 60 * 60 * 1000;
}

function LoginForm({
  busy,
  error,
  onSubmit,
}: {
  busy: boolean;
  error: string;
  onSubmit: (email: string, password: string) => Promise<void>;
}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSubmit(email.trim(), password);
  }

  return (
    <section className="login-card panel" aria-labelledby="sign-in-heading">
      <div className="login-mark" aria-hidden="true">L</div>
      <p className="eyebrow">Courier workspace</p>
      <h2 id="sign-in-heading">Sign in to continue</h2>
      <p className="muted">
        Your access token stays in memory. After a reload, sign in again to open your saved device queue.
      </p>
      <form className="stack-form" onSubmit={submit}>
        <label htmlFor="email">Email</label>
        <input
          autoComplete="username"
          id="email"
          name="email"
          onChange={(event) => setEmail(event.target.value)}
          required
          type="email"
          value={email}
        />
        <label htmlFor="password">Password</label>
        <input
          autoComplete="current-password"
          id="password"
          name="password"
          onChange={(event) => setPassword(event.target.value)}
          required
          type="password"
          value={password}
        />
        {error && <p role="alert" className="inline-error">{error}</p>}
        <button className="button button-primary" disabled={busy} type="submit">
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </section>
  );
}

function PurgeConfirmation({
  dialog,
  busy,
  onCancel,
  onConfirm,
}: {
  dialog: Exclude<PurgeDialog, { kind: 'switch-login' }>;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const isAccountChange = dialog.kind === 'account-change';
  const title = isAccountChange ? 'Switch courier account?' : 'Log out and clear device data?';
  const description = isAccountChange
    ? `Saved courier data for account ${dialog.previousUserId} will be cleared from this device before switching to ${dialog.targetUser.display_name}. This includes ${dialog.unsyncedCount} unsynced local command${dialog.unsyncedCount === 1 ? '' : 's'}.`
    : `${dialog.unsyncedCount} unsynced local command${dialog.unsyncedCount === 1 ? '' : 's'} will be permanently deleted, along with this account’s saved offline snapshots on this device.`;
  const confirmLabel =
    dialog.kind === 'logout'
      ? 'Log out and clear local data'
      : 'Switch account and clear local data';

  useEffect(() => {
    const element = dialogRef.current;
    if (element && !element.open) element.showModal();
    return () => {
      if (element?.open) element.close();
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      aria-describedby="purge-dialog-description"
      aria-labelledby="purge-dialog-title"
      className="confirmation-dialog panel"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
    >
      <p className="eyebrow">Device-local queue</p>
      <h2 id="purge-dialog-title">{title}</h2>
      <p id="purge-dialog-description">{description}</p>
      {dialog.error && <p className="inline-error" role="alert">{dialog.error}</p>}
      <p className="warning-note">
        Unsynced commands are not on the server and cannot be recovered after they are deleted.
      </p>
      <div className="dialog-actions">
        <button
          autoFocus
          className="button button-quiet"
          disabled={busy}
          onClick={onCancel}
          type="button"
        >
          {isAccountChange ? 'Keep previous account' : 'Cancel'}
        </button>
        <button
          className="button button-danger"
          disabled={busy}
          onClick={() => void onConfirm()}
          type="button"
        >
          {busy ? 'Clearing…' : confirmLabel}
        </button>
        </div>
    </dialog>
  );
}

function SwitchAccountPrompt({
  busy,
  error,
  onCancel,
  onSubmit,
}: {
  busy: boolean;
  error: string;
  onCancel: () => void;
  onSubmit: (email: string, password: string) => Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  useEffect(() => {
    const element = dialogRef.current;
    if (element && !element.open) element.showModal();
    return () => {
      if (element?.open) element.close();
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSubmit(email.trim(), password);
  }

  return (
    <dialog
      ref={dialogRef}
      aria-describedby="switch-account-description"
      aria-labelledby="switch-account-title"
      className="confirmation-dialog panel"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
    >
      <p className="eyebrow">Account change</p>
      <h2 id="switch-account-title">Sign in to another account</h2>
      <p id="switch-account-description">
        Your current Courier session and device queue stay active until you confirm a switch.
      </p>
      <form className="stack-form" onSubmit={submit}>
        <label htmlFor="new-account-email">New account email</label>
        <input
          autoComplete="username"
          autoFocus
          id="new-account-email"
          onChange={(event) => setEmail(event.target.value)}
          required
          type="email"
          value={email}
        />
        <label htmlFor="new-account-password">New account password</label>
        <input
          autoComplete="current-password"
          id="new-account-password"
          onChange={(event) => setPassword(event.target.value)}
          required
          type="password"
          value={password}
        />
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="dialog-actions">
          <button className="button button-quiet" disabled={busy} onClick={onCancel} type="button">
            Keep current account
          </button>
          <button className="button button-primary" disabled={busy} type="submit">
            {busy ? 'Checking account…' : 'Continue to account'}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function App() {
  const tokenRef = useRef<string | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [workspace, setWorkspace] = useState<Workspace>('courier');
  const [snapshot, setSnapshot] = useState<OfflineManifest | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [shipmentId, setShipmentId] = useState('');
  const [confirmedCodes, setConfirmedCodes] = useState('');
  const [selectedContainerId, setSelectedContainerId] = useState('');
  const [linkDelivery, setLinkDelivery] = useState(false);
  const [isOnline, setIsOnline] = useState(navigator.onLine);
  const [busy, setBusy] = useState(false);
  const [loginError, setLoginError] = useState('');
  const [switchLoginError, setSwitchLoginError] = useState('');
  const [pageError, setPageError] = useState('');
  const [message, setMessage] = useState('');
  const [dialog, setDialog] = useState<PurgeDialog | null>(null);
  const [inboxCountRefresh, setInboxCountRefresh] = useState(0);
  const [inboxCountResult, setInboxCountResult] = useState<InboxCountResult | null>(null);

  useEffect(() => {
    const updateOnline = () => setIsOnline(navigator.onLine);
    window.addEventListener('online', updateOnline);
    window.addEventListener('offline', updateOnline);
    return () => {
      window.removeEventListener('online', updateOnline);
      window.removeEventListener('offline', updateOnline);
    };
  }, []);

  const pendingItems = queue.filter((item) => item.state === 'PENDING_SYNC');
  const pendingPickup = queue.find(
    (item) =>
      item.state === 'PENDING_SYNC' &&
      item.record.command.action === 'COURIER_CONFIRM_PICKUP' &&
      item.record.command.snapshot_id === snapshot?.snapshot_id,
  );

  async function refreshQueue(userId: string) {
    setQueue(await listQueue(userId));
  }

  async function enterSession(authenticatedUser: CurrentUser, token: string) {
    tokenRef.current = token;
    setAccessToken(token);
    setUser(authenticatedUser);
    setInboxCountResult(null);
    setInboxCountRefresh((value) => value + 1);
    const capabilities = deriveOperationsCapabilities(authenticatedUser);
    setWorkspace(
      capabilities.canUseCourier
        ? 'courier'
        : capabilities.canViewCases || capabilities.canViewAnalytics
          ? 'operations'
          : 'inbox',
    );
    setLoginError('');
    setPageError('');
    setMessage('');
    const [snapshots, items] = await Promise.all([
      listSnapshots(authenticatedUser.id),
      listQueue(authenticatedUser.id),
    ]);
    const latestSnapshot = snapshots[0]?.snapshot ?? null;
    setSnapshot(latestSnapshot);
    setShipmentId(latestSnapshot?.shipment_id ?? '');
    setQueue(items);
    setSelectedContainerId(latestSnapshot?.containers[0]?.id ?? '');
  }

  async function handleLogin(email: string, password: string) {
    setBusy(true);
    setLoginError('');
    try {
      const credentials = await login(email, password);
      const authenticatedUser = await currentUser(credentials.access_token);
      const activeUserId = await getActiveUserId();
      if (activeUserId && activeUserId !== authenticatedUser.id) {
        const oldQueue = await listQueue(activeUserId);
        setDialog({
          kind: 'account-change',
          previousUserId: activeUserId,
          targetUser: authenticatedUser,
          targetToken: credentials.access_token,
          unsyncedCount: oldQueue.filter((item) => item.state === 'PENDING_SYNC').length,
        });
        return;
      }
      await setActiveUserId(authenticatedUser.id);
      await enterSession(authenticatedUser, credentials.access_token);
    } catch (error) {
      setLoginError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function clearSession() {
    tokenRef.current = null;
    setAccessToken(null);
    setUser(null);
    setInboxCountResult(null);
    setSnapshot(null);
    setQueue([]);
    setShipmentId('');
    setConfirmedCodes('');
    setSelectedContainerId('');
    setLinkDelivery(false);
    setPageError('');
    setMessage('');
  }

  const handleDashboardUnauthorized = useCallback(() => {
    tokenRef.current = null;
    setAccessToken(null);
    setUser(null);
    setInboxCountResult(null);
    setPageError('Срок действия сеанса истёк. Войдите снова; локальная очередь на устройстве сохранена.');
    setMessage('');
  }, []);

  const inboxCountRequestKey = user && accessToken
    ? JSON.stringify([user.id, accessToken, inboxCountRefresh, isOnline])
    : '';
  const matchingInboxCount = inboxCountResult?.requestKey === inboxCountRequestKey
    ? inboxCountResult
    : null;
  const unreadCount = matchingInboxCount && 'count' in matchingInboxCount
    ? matchingInboxCount.count
    : null;
  const inboxCountLoading = Boolean(user && accessToken && isOnline && !matchingInboxCount);
  const activeUserId = user?.id;
  const inboxCountError = !user
    ? ''
    : !isOnline
      ? inboxCountFailure(null, false)
      : matchingInboxCount && 'error' in matchingInboxCount
        ? matchingInboxCount.error
        : '';

  useEffect(() => {
    if (!activeUserId || !accessToken || !isOnline) return;
    const controller = new AbortController();
    void getInboxUnreadCount(accessToken, controller.signal)
      .then((response) => {
        if (!controller.signal.aborted) {
          setInboxCountResult({ requestKey: inboxCountRequestKey, count: response.count });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          setInboxCountResult({
            requestKey: inboxCountRequestKey,
            error: 'Срок действия сеанса истёк. Войдите снова.',
          });
          handleDashboardUnauthorized();
        } else {
          setInboxCountResult({
            requestKey: inboxCountRequestKey,
            error: inboxCountFailure(error, isOnline),
          });
        }
      });

    return () => controller.abort();
  }, [
    accessToken,
    handleDashboardUnauthorized,
    inboxCountRequestKey,
    isOnline,
    activeUserId,
  ]);

  function openInbox() {
    setWorkspace('inbox');
    setInboxCountRefresh((value) => value + 1);
  }

  function refreshInboxCount() {
    setInboxCountRefresh((value) => value + 1);
  }

  function cancelPurge() {
    setDialog(null);
  }

  async function confirmPurge() {
    if (!dialog || dialog.kind === 'switch-login') return;
    setBusy(true);
    try {
      if (dialog.kind === 'account-change') {
        try {
          await purgeUserData(dialog.previousUserId, dialog.targetUser.id);
        } catch (error) {
          setDialog({
            ...dialog,
            error: `Could not clear this device’s saved data. Your current account and queue are still active. ${errorMessage(error)}`,
          });
          return;
        }
        setDialog(null);
        await enterSession(dialog.targetUser, dialog.targetToken);
      } else {
        try {
          await purgeUserData(dialog.userId, null);
        } catch (error) {
          setDialog({
            ...dialog,
            error: `Could not clear this device’s saved data. You are still signed in and your queue is unchanged. ${errorMessage(error)}`,
          });
          return;
        }
        setDialog(null);
        await clearSession();
      }
    } catch (error) {
      setPageError(`The new account could not be loaded after the data switch. ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleSwitchLogin(email: string, password: string) {
    if (!user) return;
    setBusy(true);
    setSwitchLoginError('');
    try {
      const credentials = await login(email, password);
      const targetUser = await currentUser(credentials.access_token);
      if (targetUser.id === user.id) {
        setDialog(null);
        setMessage('You are already using this account. Your local queue remains active.');
        return;
      }

      const oldQueue = await listQueue(user.id);
      setDialog({
        kind: 'account-change',
        previousUserId: user.id,
        targetUser,
        targetToken: credentials.access_token,
        unsyncedCount: oldQueue.filter((item) => item.state === 'PENDING_SYNC').length,
      });
    } catch (error) {
      setSwitchLoginError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleLoadShipment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = tokenRef.current;
    if (!user || !token) return;
    setBusy(true);
    setPageError('');
    setMessage('');
    try {
      const loadedSnapshot = await offlineManifest(shipmentId.trim(), token);
      await saveSnapshot(user.id, loadedSnapshot);
      setSnapshot(loadedSnapshot);
      setSelectedContainerId(loadedSnapshot.containers[0]?.id ?? '');
      setConfirmedCodes('');
      setLinkDelivery(false);
      setMessage('The assigned Shipment snapshot is saved on this device.');
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        tokenRef.current = null;
        setAccessToken(null);
        setUser(null);
        setPageError('Your sign-in expired. Sign in again to continue; this account’s local queue is retained.');
      } else {
        setPageError(errorMessage(error));
      }
    } finally {
      setBusy(false);
    }
  }

  async function saveCommand(command: OfflineCommand) {
    if (!user) return;
    setBusy(true);
    setPageError('');
    setMessage('');
    try {
      await addCommand(user.id, command);
      await refreshQueue(user.id);
      setMessage('Saved on this device as local pending. Server custody is unchanged until synchronization.');
    } catch (error) {
      setPageError(`The command was not queued. ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleQueuePickup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user || !snapshot) return;
    if (!codesMatch(confirmedCodes, snapshot.container_codes)) {
      setPageError('Enter the complete sealed container code set exactly as shown in the snapshot.');
      return;
    }
    const command: PickupCommand = Object.freeze({
      action: 'COURIER_CONFIRM_PICKUP',
      client_command_id: createCommandId(),
      shipment_id: snapshot.shipment_id,
      snapshot_id: snapshot.snapshot_id,
      expected_manifest_version: snapshot.manifest_revision,
      expected_target_state_version: snapshot.pickup_state_version,
      client_performed_at: new Date().toISOString(),
      container_codes: Object.freeze([...snapshot.container_codes]),
    });
    await saveCommand(command);
    setConfirmedCodes('');
  }

  async function handleQueueDelivery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user || !snapshot) return;
    const selectedContainer = snapshot.containers.find((container) => container.id === selectedContainerId);
    if (!selectedContainer) {
      setPageError('Choose a container from the assigned Shipment snapshot.');
      return;
    }
    const pickupIsConfirmed = ['IN_TRANSIT', 'DELIVERY_PENDING', 'PARTIALLY_DELIVERED'].includes(
      snapshot.status,
    );
    if (pendingPickup && !linkDelivery) {
      setPageError('Link this Delivery explicitly to the queued Pickup before saving it.');
      return;
    }
    if (!pickupIsConfirmed && !pendingPickup) {
      setPageError('Queue and sync a Pickup first, or load a snapshot showing confirmed Pickup.');
      return;
    }
    const command: DeliveryCommand = Object.freeze({
      action: 'COURIER_INITIATE_CONTAINER_DELIVERY',
      client_command_id: createCommandId(),
      shipment_id: snapshot.shipment_id,
      snapshot_id: snapshot.snapshot_id,
      expected_manifest_version: snapshot.manifest_revision,
      expected_target_state_version: selectedContainer.state_version,
      client_performed_at: new Date().toISOString(),
      container_id: selectedContainer.id,
      container_code: selectedContainer.code,
      ...(linkDelivery && pendingPickup
        ? { depends_on_command_id: pendingPickup.record.command.client_command_id }
        : {}),
    });
    await saveCommand(command);
    setLinkDelivery(false);
  }

  async function handleSync() {
    const token = tokenRef.current;
    if (!user || !token) return;
    if (!navigator.onLine) {
      setMessage('You are offline. Commands remain local pending and have not changed server custody.');
      return;
    }

    setBusy(true);
    setPageError('');
    setMessage('Sending saved commands to the server…');
    try {
      const pending = (await listQueue(user.id)).filter((item) => item.state === 'PENDING_SYNC');
      if (pending.length === 0) {
        setMessage('There are no pending commands to synchronize.');
        return;
      }

      for (let offset = 0; offset < pending.length; offset += 20) {
        const batch = pending.slice(offset, offset + 20);
        let response: { results: Awaited<ReturnType<typeof syncCommands>>['results'] };
        try {
          response = await syncCommands(
            batch.map((item) => item.record.command),
            token,
          );
        } catch (error) {
          if (error instanceof ApiError && error.status === 401) {
            tokenRef.current = null;
            setAccessToken(null);
            setUser(null);
            setPageError('Your sign-in expired. Sign in again; pending commands remain saved for this account.');
          } else {
            setMessage(
              'The sync response was not received. Your command is still local pending; retry sends the same command ID and payload.',
            );
          }
          return;
        }

        const batchIds = new Set(batch.map((item) => item.record.command.client_command_id));
        for (const result of response.results) {
          if (!batchIds.has(result.client_command_id)) continue;
          await applySyncResult(result, {
            saveOutcome: (savedResult) => saveOutcome(user.id, savedResult),
            setCommandState: (commandId, state) => setCommandState(user.id, commandId, state),
          });
          await refreshQueue(user.id);
        }
      }

      setMessage('Synchronization finished. Server outcomes are saved on this device.');
    } catch (error) {
      setMessage(
        `A server response could not be saved locally. The command remains pending and can be replayed safely. ${errorMessage(error)}`,
      );
    } finally {
      setBusy(false);
    }
  }

  function showLogoutConfirmation() {
    if (!user) return;
    setDialog({
      kind: 'logout',
      userId: user.id,
      unsyncedCount: pendingItems.length,
    });
  }

  function showSwitchConfirmation() {
    if (!user) return;
    setSwitchLoginError('');
    setDialog({ kind: 'switch-login' });
  }

  const pickupAlreadyQueued = queue.some(
    (item) =>
      item.record.command.action === 'COURIER_CONFIRM_PICKUP' &&
      item.record.command.snapshot_id === snapshot?.snapshot_id,
  );
  const pickupIsConfirmed = snapshot
    ? ['IN_TRANSIT', 'DELIVERY_PENDING', 'PARTIALLY_DELIVERED'].includes(snapshot.status)
    : false;
  const canQueueDelivery = Boolean(snapshot && (pendingPickup || pickupIsConfirmed));
  const capabilities = user ? deriveOperationsCapabilities(user) : null;
  const hasOperationsWorkspace = Boolean(
    capabilities?.canViewCases || capabilities?.canViewAnalytics,
  );
  const hasMultipleWorkspaces = Boolean(capabilities?.canUseCourier && hasOperationsWorkspace);
  const inboxReturnWorkspace: Workspace | null = capabilities?.canUseCourier
    ? 'courier'
    : hasOperationsWorkspace
      ? 'operations'
      : null;
  const inboxReturnLabel = inboxReturnWorkspace === 'courier'
    ? 'Курьер'
    : inboxReturnWorkspace === 'operations'
      ? 'Операции'
      : '';

  function returnToAvailableWorkspace() {
    if (inboxReturnWorkspace) setWorkspace(inboxReturnWorkspace);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a
          className="brand"
          href="/"
          aria-label={
            user && workspace === 'operations'
              ? 'LabTrack, операционная панель'
              : user && workspace === 'inbox'
                ? 'LabTrack, входящие уведомления'
                : 'LabTrack Courier home'
          }
        >
          <span className="brand-mark" aria-hidden="true">L</span>
          <span>
            LabTrack <strong>
              {user && workspace === 'operations'
                ? 'Операции'
                : user && workspace === 'inbox'
                  ? 'Входящие'
                  : 'Courier'}
            </strong>
          </span>
        </a>
        <div className="topbar-tools">
          <span className={`connection-pill ${isOnline ? 'is-online' : 'is-offline'}`}>
            <span className="connection-dot" aria-hidden="true" />
            {isOnline ? 'Online' : 'Offline'}
          </span>
          {user && (
            <div className="account-tools">
              <span className="user-name">{user.display_name}</span>
              <button
                aria-label={
                  inboxCountLoading
                    ? 'Входящие, загружается число непрочитанных'
                    : inboxCountError
                      ? 'Входящие, счётчик непрочитанных недоступен'
                      : `Входящие, непрочитанных: ${unreadCount ?? 0}`
                }
                aria-pressed={workspace === 'inbox'}
                className={`text-button inbox-nav-trigger ${workspace === 'inbox' ? 'is-current' : ''}`}
                onClick={openInbox}
                type="button"
              >
                <span>Входящие</span>
                <span aria-hidden="true" className="inbox-count-badge">
                  {inboxCountLoading ? '…' : inboxCountError ? '!' : unreadCount ?? 0}
                </span>
              </button>
              <button className="text-button" onClick={showSwitchConfirmation} type="button">
                Switch account
              </button>
              <button className="text-button" onClick={showLogoutConfirmation} type="button">
                Log out
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="page-content">
        {hasMultipleWorkspaces && (
          <nav className="workspace-nav" aria-label="Рабочие области">
            <button
              aria-pressed={workspace === 'courier'}
              className={`workspace-nav-button ${workspace === 'courier' ? 'is-current' : ''}`}
              onClick={() => setWorkspace('courier')}
              type="button"
            >
              Курьер
            </button>
            <button
              aria-pressed={workspace === 'operations'}
              className={`workspace-nav-button ${workspace === 'operations' ? 'is-current' : ''}`}
              onClick={() => setWorkspace('operations')}
              type="button"
            >
              Операции
            </button>
          </nav>
        )}

        {user && inboxCountLoading && (
          <p className="inbox-count-message" role="status" aria-live="polite">
            Загружаем число непрочитанных уведомлений…
          </p>
        )}
        {user && inboxCountError && (
          <p className="inbox-count-message" role="alert">
            {inboxCountError}{' '}
            <button className="text-button" onClick={refreshInboxCount} type="button">
              Повторить
            </button>
          </p>
        )}

        {(!user || workspace === 'courier') && (
          <section className="page-intro" aria-labelledby="page-title">
            <div>
              <p className="eyebrow">Field operations · device-local queue</p>
              <h1 id="page-title">Courier Offline</h1>
              <p className="intro-copy">
                Confirm Pickup and start a single-container Delivery with a saved Shipment snapshot. Local pending actions are not server custody events.
              </p>
            </div>
            <div className="intro-stamp" aria-hidden="true">
              <span>LT</span>
              <small>Courier</small>
            </div>
          </section>
        )}

        {pageError && (!user || workspace === 'courier') && (
          <p className="page-alert" role="alert">{pageError}</p>
        )}
        {message && user && workspace === 'courier' && (
          <p className="page-message" role="status" aria-live="polite">{message}</p>
        )}

        {!user ? (
          <LoginForm busy={busy} error={loginError} onSubmit={handleLogin} />
        ) : (
          <>
            {workspace === 'operations' && hasOperationsWorkspace && accessToken && (
              <OperationsDashboard
                key={user.id}
                onUnauthorized={handleDashboardUnauthorized}
                token={accessToken}
                user={user}
              />
            )}

            {workspace === 'inbox' && accessToken && (
              <NotificationInbox
                key={user.id}
                onUnauthorized={handleDashboardUnauthorized}
                onUnreadCountChange={refreshInboxCount}
                onReturnToWorkspace={inboxReturnWorkspace ? returnToAvailableWorkspace : undefined}
                returnWorkspaceLabel={inboxReturnLabel}
                token={accessToken}
                unreadCount={unreadCount}
              />
            )}

            {workspace === 'courier' && capabilities?.canUseCourier && (
              <>
            <section className="panel session-panel" aria-labelledby="session-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Authenticated session</p>
                  <h2 id="session-heading">Welcome, {user.display_name}</h2>
                </div>
                <p className="token-note">Access token held in memory</p>
              </div>
              <form className="load-form" onSubmit={handleLoadShipment}>
                <label htmlFor="shipment-id">Shipment ID</label>
                <input
                  id="shipment-id"
                  onChange={(event) => setShipmentId(event.target.value)}
                  placeholder="Paste an assigned Shipment ID"
                  required
                  value={shipmentId}
                />
                <button className="button button-secondary" disabled={busy || !isOnline} type="submit">
                  {busy ? 'Loading…' : 'Load assigned Shipment'}
                </button>
              </form>
            </section>

            {snapshot && (
              <div className="workspace-grid">
                <div className="workspace-main">
                  <section className="panel snapshot-panel" aria-labelledby="snapshot-heading">
                    <div className="section-heading">
                      <div>
                        <p className="eyebrow">Server-issued offline snapshot</p>
                        <h2 id="snapshot-heading">Assigned Shipment</h2>
                      </div>
                      <span className="status-badge">{snapshot.status.replaceAll('_', ' ')}</span>
                    </div>
                    <dl className="snapshot-facts">
                      <div>
                        <dt>Shipment</dt>
                        <dd className="mono">{snapshot.shipment_id}</dd>
                      </div>
                      <div>
                        <dt>Manifest revision</dt>
                        <dd>{snapshot.manifest_revision}</dd>
                      </div>
                      <div>
                        <dt>Snapshot issued</dt>
                        <dd>{new Date(snapshot.issued_at).toLocaleString()}</dd>
                      </div>
                    </dl>
                    {snapshotIsOver24HoursOld(snapshot) && (
                      <p className="snapshot-warning" role="alert">
                        This snapshot is more than 24 hours old by this device’s clock. The server decides whether review is required.
                      </p>
                    )}
                    <div className="container-section">
                      <div className="section-heading compact-heading">
                        <div>
                          <p className="eyebrow">Sealed manifest</p>
                          <h3>{snapshot.containers.length} containers</h3>
                        </div>
                      </div>
                      <ul className="container-list" aria-label="Sealed container codes">
                        {snapshot.containers.map((container) => (
                          <li key={container.id}>
                            <span className="container-code">{container.code}</span>
                            <span className="container-state">{container.delivery_status.replaceAll('_', ' ')}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </section>

                  <div className="actions-grid">
                    <section className="panel action-panel" aria-labelledby="pickup-heading">
                      <p className="step-number">01</p>
                      <p className="eyebrow">Whole Shipment</p>
                      <h2 id="pickup-heading">Confirm Pickup</h2>
                      <p className="muted">
                        Check each sealed container and enter the complete code set before saving this command.
                      </p>
                      <form className="stack-form" onSubmit={handleQueuePickup}>
                        <label htmlFor="confirmed-codes">Confirm all sealed container codes</label>
                        <textarea
                          id="confirmed-codes"
                          onChange={(event) => setConfirmedCodes(event.target.value)}
                          placeholder={snapshot.container_codes.join(', ')}
                          required
                          rows={3}
                          value={confirmedCodes}
                        />
                        {pickupAlreadyQueued && (
                          <p className="muted">Pickup is already queued for this snapshot. Load a new snapshot before starting another command.</p>
                        )}
                        <button
                          className="button button-primary"
                          disabled={busy || pickupAlreadyQueued}
                          type="submit"
                        >
                          Queue Pickup confirmation
                        </button>
                      </form>
                    </section>

                    <section className="panel action-panel" aria-labelledby="delivery-heading">
                      <p className="step-number">02</p>
                      <p className="eyebrow">One container</p>
                      <h2 id="delivery-heading">Start Delivery</h2>
                      <p className="muted">
                        Choose one sealed container. If its Pickup is queued here, link this action explicitly.
                      </p>
                      <form className="stack-form" onSubmit={handleQueueDelivery}>
                        <label htmlFor="container-to-deliver">Container to deliver</label>
                        <select
                          id="container-to-deliver"
                          onChange={(event) => setSelectedContainerId(event.target.value)}
                          value={selectedContainerId}
                        >
                          {snapshot.containers.map((container) => (
                            <option key={container.id} value={container.id}>
                              {container.code}
                            </option>
                          ))}
                        </select>
                        {pendingPickup && (
                          <label className="checkbox-row" htmlFor="link-delivery">
                            <input
                              checked={linkDelivery}
                              id="link-delivery"
                              onChange={(event) => setLinkDelivery(event.target.checked)}
                              type="checkbox"
                            />
                            <span>Link this delivery to my queued Pickup</span>
                          </label>
                        )}
                        {!canQueueDelivery && (
                          <p className="muted">Queue a Pickup first, or load a snapshot with confirmed Pickup.</p>
                        )}
                        <button
                          className="button button-secondary"
                          disabled={
                            busy ||
                            !canQueueDelivery ||
                            Boolean(pendingPickup && !linkDelivery)
                          }
                          type="submit"
                        >
                          Queue one-container Delivery
                        </button>
                      </form>
                    </section>
                  </div>
                </div>

                <aside className="workspace-aside">
                  <CommandQueue items={queue} />
                  <section className="panel sync-panel" aria-labelledby="sync-heading">
                    <p className="eyebrow">Foreground only</p>
                    <h2 id="sync-heading">Synchronize</h2>
                    <p className="muted">
                      Commands sync only when you choose this action. If a reply is lost, retry sends the saved command unchanged.
                    </p>
                    {!isOnline && (
                      <p className="offline-note">Offline · pending commands stay on this device.</p>
                    )}
                    <button
                      className="button button-primary sync-button"
                      disabled={busy || !isOnline || pendingItems.length === 0}
                      onClick={handleSync}
                      type="button"
                    >
                      {busy ? 'Synchronizing…' : 'Sync pending commands'}
                    </button>
                  </section>
                </aside>
              </div>
            )}

            {!snapshot && (
              <div className="workspace-grid">
                <section className="panel empty-state">
                  <div className="empty-icon" aria-hidden="true">↗</div>
                  <h2>Load an assigned Shipment</h2>
                  <p className="muted">
                    A successful load saves a server-issued, PHI-free snapshot for this account on this device.
                  </p>
                </section>
                <CommandQueue items={queue} />
              </div>
            )}
              </>
            )}

            {!capabilities?.canUseCourier && !hasOperationsWorkspace && workspace !== 'inbox' && (
              <section className="panel empty-state access-empty-state">
                <h2>Нет доступных рабочих областей</h2>
                <p className="muted">
                  Для этой учётной записи не назначена роль курьера или доступ к операционной панели.
                </p>
              </section>
            )}
          </>
        )}
      </main>

      <footer className="site-footer">
        <span>
          LabTrack {user && workspace === 'operations'
            ? 'Операции'
            : user && workspace === 'inbox'
              ? 'Входящие'
              : 'Courier'}
        </span>
        <span>Server custody remains authoritative</span>
      </footer>

      {dialog?.kind === 'switch-login' && (
        <SwitchAccountPrompt
          busy={busy}
          error={switchLoginError}
          onCancel={cancelPurge}
          onSubmit={handleSwitchLogin}
        />
      )}
      {dialog && dialog.kind !== 'switch-login' && (
        <PurgeConfirmation
          busy={busy}
          dialog={dialog}
          onCancel={cancelPurge}
          onConfirm={confirmPurge}
        />
      )}
    </div>
  );
}

export default App;
