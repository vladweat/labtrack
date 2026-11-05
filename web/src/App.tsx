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
import { LabWorkspace } from './LabWorkspace';
import { SenderWorkspace } from './SenderWorkspace';
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
import { publishSessionChange, subscribeToSessionChanges } from './sessionSync';
import { applySyncResult } from './sync';
import {
  containerStatusLabel,
  formatDateTime,
  safeErrorMessage,
  shipmentStatusLabel,
} from './localization';
import type {
  CurrentUser,
  DeliveryCommand,
  OfflineManifest,
  OfflineCommand,
  PickupCommand,
  QueueItem,
} from './types';
import { deriveOperationsCapabilities } from './types';

type Workspace = 'courier' | 'operations' | 'sender' | 'lab' | 'inbox';

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
  return safeErrorMessage(error, 'Не удалось выполнить запрос. Проверьте соединение и повторите попытку.');
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
      <p className="eyebrow">Рабочая область курьера</p>
      <h2 id="sign-in-heading">Вход в LabTrack</h2>
      <p className="muted">
        Токен доступа хранится только в памяти. После перезагрузки войдите снова, чтобы открыть очередь команд, сохранённую на устройстве.
      </p>
      <aside className="shared-demo-notice" role="note" aria-label="Общее уведомление о демо">
        Учётные записи и данные демо общие и синтетические. Другие посетители могут видеть действия в демо и менять данные.
      </aside>
      <form className="stack-form" onSubmit={submit}>
        <label htmlFor="email">Электронная почта</label>
        <input
          autoComplete="username"
          id="email"
          name="email"
          onChange={(event) => setEmail(event.target.value)}
          required
          type="email"
          value={email}
        />
        <label htmlFor="password">Пароль</label>
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
          {busy ? 'Входим…' : 'Войти'}
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
  const title = isAccountChange ? 'Сменить учётную запись курьера?' : 'Выйти и очистить данные устройства?';
  const description = isAccountChange
    ? `Данные курьера для учётной записи ${dialog.previousUserId} будут удалены с этого устройства перед переходом к ${dialog.targetUser.display_name}. Несинхронизированных команд: ${dialog.unsyncedCount}.`
    : `Будут безвозвратно удалены несинхронизированные команды: ${dialog.unsyncedCount}, а также сохранённые офлайн-снимки этой учётной записи на устройстве.`;
  const confirmLabel =
    dialog.kind === 'logout'
      ? 'Выйти и очистить данные устройства'
      : 'Сменить учётную запись и очистить данные';

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
      <p className="eyebrow">Очередь на устройстве</p>
      <h2 id="purge-dialog-title">{title}</h2>
      <p id="purge-dialog-description">{description}</p>
      {dialog.error && <p className="inline-error" role="alert">{dialog.error}</p>}
      <p className="warning-note">
        Несинхронизированных команд ещё нет на сервере. После удаления восстановить их нельзя.
      </p>
      <div className="dialog-actions">
        <button
          autoFocus
          className="button button-quiet"
          disabled={busy}
          onClick={onCancel}
          type="button"
        >
          {isAccountChange ? 'Оставить прежнюю учётную запись' : 'Отмена'}
        </button>
        <button
          className="button button-danger"
          disabled={busy}
          onClick={() => void onConfirm()}
          type="button"
        >
          {busy ? 'Очищаем…' : confirmLabel}
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
      <p className="eyebrow">Смена учётной записи</p>
      <h2 id="switch-account-title">Войти в другую учётную запись</h2>
      <p id="switch-account-description">
        Текущий сеанс курьера и очередь команд на устройстве останутся активными, пока вы не подтвердите смену.
      </p>
      <form className="stack-form" onSubmit={submit}>
        <label htmlFor="new-account-email">Электронная почта новой учётной записи</label>
        <input
          autoComplete="username"
          autoFocus
          id="new-account-email"
          onChange={(event) => setEmail(event.target.value)}
          required
          type="email"
          value={email}
        />
        <label htmlFor="new-account-password">Пароль новой учётной записи</label>
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
            Оставить текущую учётную запись
          </button>
          <button className="button button-primary" disabled={busy} type="submit">
            {busy ? 'Проверяем учётную запись…' : 'Продолжить'}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function App() {
  const tokenRef = useRef<string | null>(null);
  const sessionUserIdRef = useRef<string | null>(null);
  const [sessionController, setSessionController] = useState(() => new AbortController());
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

  const abortSessionRequests = useCallback(() => {
    sessionController.abort();
    setSessionController(new AbortController());
  }, [sessionController]);

  const isSessionCurrent = useCallback(
    (userId: string) => sessionUserIdRef.current === userId,
    [],
  );

  const clearSession = useCallback(() => {
    sessionUserIdRef.current = null;
    abortSessionRequests();
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
    setDialog(null);
    setPageError('');
    setMessage('');
    setBusy(false);
  }, [abortSessionRequests]);

  useEffect(() => {
    const updateOnline = () => setIsOnline(navigator.onLine);
    window.addEventListener('online', updateOnline);
    window.addEventListener('offline', updateOnline);
    return () => {
      window.removeEventListener('online', updateOnline);
      window.removeEventListener('offline', updateOnline);
    };
  }, []);

  useEffect(
    () =>
      subscribeToSessionChanges(() => {
        const expectedUserId = sessionUserIdRef.current;
        if (!expectedUserId) return;
        void getActiveUserId()
          .then((activeUserId) => {
            if (
              sessionUserIdRef.current === expectedUserId &&
              activeUserId !== expectedUserId
            ) {
              clearSession();
              setPageError('Учётная запись изменилась в другой вкладке. Войдите снова.');
            }
          })
          .catch(() => {
            if (sessionUserIdRef.current === expectedUserId) {
              clearSession();
              setPageError('Не удалось подтвердить учётную запись. Войдите снова.');
            }
          });
      }),
    [clearSession],
  );

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

  async function hasActiveAccount(userId: string): Promise<boolean> {
    if (sessionUserIdRef.current !== userId) return false;
    try {
      const activeUserId = await getActiveUserId();
      return activeUserId === userId && sessionUserIdRef.current === userId;
    } catch {
      return false;
    }
  }

  async function enterSession(authenticatedUser: CurrentUser, token: string) {
    abortSessionRequests();
    sessionUserIdRef.current = authenticatedUser.id;
    tokenRef.current = token;
    setAccessToken(token);
    setUser(authenticatedUser);
    setInboxCountResult(null);
    setInboxCountRefresh((value) => value + 1);
    const capabilities = deriveOperationsCapabilities(authenticatedUser);
    const hasLabMembership = authenticatedUser.memberships.some(
      (membership) => membership.role === 'LAB_OPERATOR' || membership.role === 'LAB_MANAGER',
    );
    const canSendShipments = authenticatedUser.memberships.some(
      (membership) => membership.role === 'SENDER_OPERATOR',
    );
    setWorkspace(
      capabilities.canUseCourier
        ? 'courier'
        : hasLabMembership
          ? 'lab'
          : canSendShipments
            ? 'sender'
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
    if (!(await hasActiveAccount(authenticatedUser.id))) return;
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
      publishSessionChange(authenticatedUser.id);
      await enterSession(authenticatedUser, credentials.access_token);
    } catch (error) {
      setLoginError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  const handleDashboardUnauthorized = useCallback(() => {
    sessionUserIdRef.current = null;
    abortSessionRequests();
    tokenRef.current = null;
    setAccessToken(null);
    setUser(null);
    setInboxCountResult(null);
    setPageError('Срок действия сеанса истёк. Войдите снова; локальная очередь на устройстве сохранена.');
    setMessage('');
  }, [abortSessionRequests]);

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
            error: `Не удалось очистить данные на этом устройстве. Текущая учётная запись и очередь остаются активными. ${errorMessage(error)}`,
          });
          return;
        }
        publishSessionChange(dialog.targetUser.id);
        setDialog(null);
        await enterSession(dialog.targetUser, dialog.targetToken);
      } else {
        try {
          await purgeUserData(dialog.userId, null);
        } catch (error) {
          setDialog({
            ...dialog,
            error: `Не удалось очистить данные на этом устройстве. Вы остались в системе, очередь не изменилась. ${errorMessage(error)}`,
          });
          return;
        }
        publishSessionChange(null);
        setDialog(null);
        await clearSession();
      }
    } catch (error) {
      setPageError(`Не удалось загрузить новую учётную запись после смены. ${errorMessage(error)}`);
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
        setMessage('Вы уже вошли в эту учётную запись. Очередь на устройстве сохранена.');
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
    const userId = user.id;
    const signal = sessionController.signal;
    setBusy(true);
    setPageError('');
    setMessage('');
    try {
      if (!(await hasActiveAccount(userId))) return;
      const loadedSnapshot = await offlineManifest(shipmentId.trim(), token, signal);
      if (signal.aborted || !(await hasActiveAccount(userId))) return;
      await saveSnapshot(userId, loadedSnapshot);
      if (signal.aborted || sessionUserIdRef.current !== userId) return;
      setSnapshot(loadedSnapshot);
      setSelectedContainerId(loadedSnapshot.containers[0]?.id ?? '');
      setConfirmedCodes('');
      setLinkDelivery(false);
      setMessage('Снимок назначенной отправки сохранён на этом устройстве.');
    } catch (error) {
      if (signal.aborted || sessionUserIdRef.current !== userId) return;
      if (error instanceof ApiError && error.status === 401) {
        sessionUserIdRef.current = null;
        abortSessionRequests();
        tokenRef.current = null;
        setAccessToken(null);
        setUser(null);
        setPageError('Срок действия сеанса истёк. Войдите снова; очередь этой учётной записи сохранена на устройстве.');
      } else {
        setPageError(errorMessage(error));
      }
    } finally {
      setBusy(false);
    }
  }

  async function saveCommand(command: OfflineCommand) {
    if (!user || sessionUserIdRef.current !== user.id) return;
    const userId = user.id;
    setBusy(true);
    setPageError('');
    setMessage('');
    try {
      await addCommand(userId, command);
      if (sessionUserIdRef.current !== userId) return;
      await refreshQueue(userId);
      if (sessionUserIdRef.current !== userId) return;
      setMessage('Команда сохранена на устройстве и ожидает синхронизации. Статус передачи на сервере не изменился.');
    } catch (error) {
      if (sessionUserIdRef.current !== userId) return;
      setPageError(`Не удалось добавить команду в очередь. ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleQueuePickup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user || !snapshot) return;
    if (!codesMatch(confirmedCodes, snapshot.container_codes)) {
      setPageError('Введите все коды опечатанных контейнеров точно так, как они указаны в снимке.');
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
      setPageError('Выберите контейнер из снимка назначенной отправки.');
      return;
    }
    const pickupIsConfirmed = ['IN_TRANSIT', 'DELIVERY_PENDING', 'PARTIALLY_DELIVERED'].includes(
      snapshot.status,
    );
    if (pendingPickup && !linkDelivery) {
      setPageError('Перед сохранением свяжите доставку с ожидающим подтверждением забора.');
      return;
    }
    if (!pickupIsConfirmed && !pendingPickup) {
      setPageError('Сначала поставьте забор в очередь и синхронизируйте его либо загрузите снимок с подтверждённым забором.');
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
    if (!user || !token || sessionUserIdRef.current !== user.id) return;
    const userId = user.id;
    const signal = sessionController.signal;
    if (!navigator.onLine) {
      setMessage('Нет подключения к сети. Команды остаются на устройстве и не меняют статус передачи на сервере.');
      return;
    }

    setBusy(true);
    setPageError('');
    setMessage('Отправляем сохранённые команды на сервер…');
    try {
      const pending = (await listQueue(userId)).filter((item) => item.state === 'PENDING_SYNC');
      if (signal.aborted || !(await hasActiveAccount(userId))) return;
      if (pending.length === 0) {
        setMessage('Нет команд, ожидающих синхронизации.');
        return;
      }

      for (let offset = 0; offset < pending.length; offset += 20) {
        if (signal.aborted || !(await hasActiveAccount(userId))) return;
        const batch = pending.slice(offset, offset + 20);
        let response: { results: Awaited<ReturnType<typeof syncCommands>>['results'] };
        try {
          response = await syncCommands(
            batch.map((item) => item.record.command),
            token,
            signal,
          );
        } catch (error) {
          if (signal.aborted || sessionUserIdRef.current !== userId) return;
          if (error instanceof ApiError && error.status === 401) {
            sessionUserIdRef.current = null;
            abortSessionRequests();
            tokenRef.current = null;
            setAccessToken(null);
            setUser(null);
            setPageError('Срок действия сеанса истёк. Войдите снова; ожидающие команды сохранены для этой учётной записи.');
          } else {
            setMessage(
              'Ответ сервера не получен. Команда остаётся на устройстве; повторная отправка использует тот же идентификатор и данные.',
            );
          }
          return;
        }
        if (signal.aborted || !(await hasActiveAccount(userId))) return;

        const batchIds = new Set(batch.map((item) => item.record.command.client_command_id));
        for (const result of response.results) {
          if (!batchIds.has(result.client_command_id)) continue;
          await applySyncResult(result, {
            saveOutcome: (savedResult) => saveOutcome(userId, savedResult),
            setCommandState: (commandId, state) => setCommandState(userId, commandId, state),
          });
          if (signal.aborted || sessionUserIdRef.current !== userId) return;
          await refreshQueue(userId);
          if (signal.aborted || sessionUserIdRef.current !== userId) return;
        }
      }

      setMessage('Синхронизация завершена. Ответы сервера сохранены на этом устройстве.');
    } catch (error) {
      if (signal.aborted || sessionUserIdRef.current !== userId) return;
      setMessage(
        `Не удалось сохранить ответ сервера на устройстве. Команда ожидает синхронизации; её можно безопасно отправить повторно. ${errorMessage(error)}`,
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
  const hasSenderWorkspace = Boolean(
    user?.memberships.some((membership) => membership.role === 'SENDER_OPERATOR'),
  );
  const hasLabWorkspace = Boolean(
    user?.memberships.some((membership) =>
      membership.role === 'LAB_OPERATOR' || membership.role === 'LAB_MANAGER',
    ),
  );
  const hasOperationsWorkspace = Boolean(
    capabilities?.canViewCases || capabilities?.canViewAnalytics,
  );
  const hasMultipleWorkspaces = Boolean(
    Number(capabilities?.canUseCourier ?? false) +
      Number(hasSenderWorkspace) +
      Number(hasLabWorkspace) +
      Number(hasOperationsWorkspace) > 1,
  );
  const inboxReturnWorkspace: Workspace | null = capabilities?.canUseCourier
    ? 'courier'
    : hasLabWorkspace
      ? 'lab'
      : hasSenderWorkspace
        ? 'sender'
        : hasOperationsWorkspace
          ? 'operations'
          : null;
  const inboxReturnLabel = inboxReturnWorkspace === 'courier'
    ? 'Курьер'
    : inboxReturnWorkspace === 'lab'
      ? 'Лаборатория'
      : inboxReturnWorkspace === 'sender'
        ? 'Отправитель'
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
            user && workspace === 'lab'
              ? 'LabTrack, рабочая область лаборатории'
              : user && workspace === 'sender'
                ? 'LabTrack, рабочая область отправителя'
                : user && workspace === 'operations'
                ? 'LabTrack, операционная панель'
                : user && workspace === 'inbox'
                  ? 'LabTrack, входящие уведомления'
                  : 'LabTrack, главная страница курьера'
          }
        >
          <span className="brand-mark" aria-hidden="true">L</span>
          <span>
            LabTrack <strong>
              {user && workspace === 'lab'
                ? 'Лаборатория'
                : user && workspace === 'sender'
                  ? 'Отправитель'
                  : user && workspace === 'operations'
                    ? 'Операции'
                    : user && workspace === 'inbox'
                      ? 'Входящие'
                      : 'Курьер'}
            </strong>
          </span>
        </a>
        <div className="topbar-tools">
          <span className={`connection-pill ${isOnline ? 'is-online' : 'is-offline'}`}>
            <span className="connection-dot" aria-hidden="true" />
            {isOnline ? 'В сети' : 'Не в сети'}
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
                Сменить учётную запись
              </button>
              <button className="text-button" onClick={showLogoutConfirmation} type="button">
                Выйти
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="page-content">
        {hasMultipleWorkspaces && (
          <nav className="workspace-nav" aria-label="Рабочие области">
            {capabilities?.canUseCourier && (
              <button
                aria-pressed={workspace === 'courier'}
                className={`workspace-nav-button ${workspace === 'courier' ? 'is-current' : ''}`}
                onClick={() => setWorkspace('courier')}
                type="button"
              >
                Курьер
              </button>
            )}
            {hasLabWorkspace && (
              <button
                aria-pressed={workspace === 'lab'}
                className={`workspace-nav-button ${workspace === 'lab' ? 'is-current' : ''}`}
                onClick={() => setWorkspace('lab')}
                type="button"
              >
                Лаборатория
              </button>
            )}
            {hasSenderWorkspace && (
              <button
                aria-pressed={workspace === 'sender'}
                className={`workspace-nav-button ${workspace === 'sender' ? 'is-current' : ''}`}
                onClick={() => setWorkspace('sender')}
                type="button"
              >
                Отправитель
              </button>
            )}
            {hasOperationsWorkspace && (
              <button
                aria-pressed={workspace === 'operations'}
                className={`workspace-nav-button ${workspace === 'operations' ? 'is-current' : ''}`}
                onClick={() => setWorkspace('operations')}
                type="button"
              >
                Операции
              </button>
            )}
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
              <p className="eyebrow">Работа курьера · очередь команд на устройстве</p>
              <h1 id="page-title">Курьер офлайн</h1>
              <p className="intro-copy">
                Подтвердите забор и начните доставку отдельного контейнера по сохранённому снимку отправки. Ожидающие команды на устройстве ещё не меняют статус передачи на сервере.
              </p>
            </div>
            <div className="intro-stamp" aria-hidden="true">
              <span>LT</span>
              <small>Курьер</small>
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

            {workspace === 'lab' && hasLabWorkspace && accessToken && (
              <LabWorkspace
                key={user.id}
                isSessionCurrent={isSessionCurrent}
                onUnauthorized={handleDashboardUnauthorized}
                sessionSignal={sessionController.signal}
                token={accessToken}
                user={user}
              />
            )}

            {workspace === 'sender' && hasSenderWorkspace && accessToken && (
              <SenderWorkspace
                key={user.id}
                isSessionCurrent={isSessionCurrent}
                onUnauthorized={handleDashboardUnauthorized}
                sessionSignal={sessionController.signal}
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
                  <p className="eyebrow">Активный сеанс</p>
                  <h2 id="session-heading">Добро пожаловать, {user.display_name}</h2>
                </div>
                <p className="token-note">Токен доступа хранится в памяти</p>
              </div>
              <form className="load-form" onSubmit={handleLoadShipment}>
                <label htmlFor="shipment-id">Идентификатор отправки</label>
                <input
                  id="shipment-id"
                  onChange={(event) => setShipmentId(event.target.value)}
                  placeholder="Введите идентификатор назначенной отправки"
                  required
                  value={shipmentId}
                />
                <button className="button button-secondary" disabled={busy || !isOnline} type="submit">
                  {busy ? 'Загружаем…' : 'Загрузить назначенную отправку'}
                </button>
              </form>
            </section>

            {snapshot && (
              <div className="workspace-grid">
                <div className="workspace-main">
                  <section className="panel snapshot-panel" aria-labelledby="snapshot-heading">
                    <div className="section-heading">
                      <div>
                        <p className="eyebrow">Офлайн-снимок от сервера</p>
                        <h2 id="snapshot-heading">Назначенная отправка</h2>
                      </div>
                      <span className="status-badge">{shipmentStatusLabel(snapshot.status)}</span>
                    </div>
                    <dl className="snapshot-facts">
                      <div>
                        <dt>Отправка</dt>
                        <dd className="mono">{snapshot.shipment_id}</dd>
                      </div>
                      <div>
                        <dt>Версия манифеста</dt>
                        <dd>{snapshot.manifest_revision}</dd>
                      </div>
                      <div>
                        <dt>Снимок создан</dt>
                        <dd>{formatDateTime(snapshot.issued_at)}</dd>
                      </div>
                    </dl>
                    {snapshotIsOver24HoursOld(snapshot) && (
                      <p className="snapshot-warning" role="alert">
                        По часам устройства этому снимку больше 24 часов. Сервер решит, требуется ли проверка.
                      </p>
                    )}
                    <div className="container-section">
                      <div className="section-heading compact-heading">
                        <div>
                          <p className="eyebrow">Опечатанный манифест</p>
                          <h3>Контейнеров: {snapshot.containers.length}</h3>
                        </div>
                      </div>
                      <ul className="container-list" aria-label="Коды опечатанных контейнеров">
                        {snapshot.containers.map((container) => (
                          <li key={container.id}>
                            <span className="container-code">{container.code}</span>
                            <span className="container-state">{containerStatusLabel(container.delivery_status)}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </section>

                  <div className="actions-grid">
                    <section className="panel action-panel" aria-labelledby="pickup-heading">
                      <p className="step-number">01</p>
                      <p className="eyebrow">Вся отправка</p>
                      <h2 id="pickup-heading">Подтверждение забора</h2>
                      <p className="muted">
                        Проверьте каждый опечатанный контейнер и введите все его коды перед сохранением команды.
                      </p>
                      <form className="stack-form" onSubmit={handleQueuePickup}>
                        <label htmlFor="confirmed-codes">Введите коды всех опечатанных контейнеров</label>
                        <textarea
                          id="confirmed-codes"
                          onChange={(event) => setConfirmedCodes(event.target.value)}
                          placeholder={snapshot.container_codes.join(', ')}
                          required
                          rows={3}
                          value={confirmedCodes}
                        />
                        {pickupAlreadyQueued && (
                          <p className="muted">Забор для этого снимка уже поставлен в очередь. Загрузите новый снимок, прежде чем создавать следующую команду.</p>
                        )}
                        <button
                          className="button button-primary"
                          disabled={busy || pickupAlreadyQueued}
                          type="submit"
                        >
                          Поставить подтверждение забора в очередь
                        </button>
                      </form>
                    </section>

                    <section className="panel action-panel" aria-labelledby="delivery-heading">
                      <p className="step-number">02</p>
                      <p className="eyebrow">Один контейнер</p>
                      <h2 id="delivery-heading">Начать доставку</h2>
                      <p className="muted">
                        Выберите один опечатанный контейнер. Если подтверждение его забора уже в очереди, явно свяжите с ним доставку.
                      </p>
                      <form className="stack-form" onSubmit={handleQueueDelivery}>
                        <label htmlFor="container-to-deliver">Контейнер для доставки</label>
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
                            <span>Связать доставку с ожидающим подтверждением забора</span>
                          </label>
                        )}
                        {!canQueueDelivery && (
                          <p className="muted">Сначала поставьте забор в очередь или загрузите снимок с подтверждённым забором.</p>
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
                          Поставить доставку одного контейнера в очередь
                        </button>
                      </form>
                    </section>
                  </div>
                </div>

                <aside className="workspace-aside">
                  <CommandQueue items={queue} />
                  <section className="panel sync-panel" aria-labelledby="sync-heading">
                    <p className="eyebrow">Только по запросу</p>
                    <h2 id="sync-heading">Синхронизация</h2>
                    <p className="muted">
                      Команды отправляются только после нажатия кнопки. Если ответ потерян, повторная попытка отправит сохранённую команду без изменений.
                    </p>
                    {!isOnline && (
                      <p className="offline-note">Нет сети · ожидающие команды остаются на этом устройстве.</p>
                    )}
                    <button
                      className="button button-primary sync-button"
                      disabled={busy || !isOnline || pendingItems.length === 0}
                      onClick={handleSync}
                      type="button"
                    >
                      {busy ? 'Синхронизируем…' : 'Синхронизировать команды'}
                    </button>
                  </section>
                </aside>
              </div>
            )}

            {!snapshot && (
              <div className="workspace-grid">
                <section className="panel empty-state">
                  <div className="empty-icon" aria-hidden="true">↗</div>
                  <h2>Загрузите назначенную отправку</h2>
                  <p className="muted">
                    После загрузки на устройство сохранится снимок от сервера без медицинских данных.
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
        <span>Статус передачи на сервере остаётся основным</span>
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
