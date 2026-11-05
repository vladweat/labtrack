import { useEffect, useRef, useState } from 'react';
import { ApiError, listInboxNotifications, markInboxNotificationRead } from './api';
import type { Notification, PagedNotification } from './types';
import { safeErrorMessage, shipmentStatusLabel } from './localization';

type InboxProps = {
  token: string;
  unreadCount: number | null;
  onUnreadCountChange: () => void;
  onUnauthorized: () => void;
  onReturnToWorkspace?: () => void;
  returnWorkspaceLabel: string;
};

type NotificationListResult =
  | { requestKey: string; page: PagedNotification }
  | { requestKey: string; error: string };

type InboxFilter = 'all' | 'unread';

const CASE_TYPE_LABELS: Record<string, string> = {
  PICKUP_PENDING: 'Ожидает забора',
  DELIVERY_PENDING: 'Ожидает доставки',
  PROPOSAL_PENDING: 'Ожидает решения по предложению',
  SHIPMENT_OVERDUE: 'Срок доставки превышен',
};

const DATE_TIME_FORMAT = new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'UTC',
});

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `${DATE_TIME_FORMAT.format(date)} UTC`;
}

function formatCount(count: number): string {
  return new Intl.NumberFormat('ru-RU').format(count);
}

function caseTypeLabel(caseType: string): string {
  return CASE_TYPE_LABELS[caseType] ?? 'Операционное событие';
}

function statusLabel(status: string): string {
  return shipmentStatusLabel(status);
}

function requestErrorMessage(error: unknown, isOnline: boolean): string {
  if (!isOnline) return 'Нет подключения к сети. Уведомления не загружены и не сохранены на устройстве.';
  if (error instanceof TypeError) {
    return 'Не удалось подключиться к серверу. Проверьте соединение и повторите запрос.';
  }
  return safeErrorMessage(error, 'Не удалось выполнить запрос. Проверьте соединение и повторите попытку.');
}

function markErrorMessage(error: unknown, isOnline: boolean): string {
  if (!isOnline) return 'Нет подключения к сети. Отметить уведомление прочитанным не удалось; оно не поставлено в очередь.';
  if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
    return 'Не удалось отметить уведомление. Запрос отклонён или запись недоступна.';
  }
  if (error instanceof TypeError) {
    return 'Не удалось связаться с сервером. Повторите попытку, чтобы безопасно проверить результат.';
  }
  return error instanceof Error
    ? `Не удалось отметить уведомление прочитанным. ${safeErrorMessage(error, 'Повторите попытку позже.')}`
    : 'Не удалось отметить уведомление прочитанным. Повторите попытку.';
}

function updateReadNotification(page: PagedNotification, updated: Notification): PagedNotification {
  return {
    ...page,
    items: page.items.map((item) => item.id === updated.id ? updated : item),
  };
}

function lastValidOffset(total: number, limit: number): number {
  if (total <= 0) return 0;
  return Math.floor((total - 1) / limit) * limit;
}

function NotificationCard({
  notification,
  busy,
  error,
  isOnline,
  onMarkRead,
}: {
  notification: Notification;
  busy: boolean;
  error: string;
  isOnline: boolean;
  onMarkRead: (notification: Notification) => void;
}) {
  const isUnread = notification.read_at === null;
  const notificationCase = notification.payload.case_type;
  const title = caseTypeLabel(notificationCase);
  const caseTimeLabel = notificationCase === 'SHIPMENT_OVERDUE' ? 'Срок истёк' : 'Время случая';

  return (
    <li className={`notification-card ${isUnread ? 'is-unread' : ''}`}>
      <div className="notification-card-heading">
        <h3>{title}</h3>
        <span className={`notification-state ${isUnread ? 'is-unread' : 'is-read'}`}>
          {isUnread ? 'Не прочитано' : 'Прочитано'}
        </span>
      </div>
      <dl className="notification-facts">
        <div>
          <dt>Отправка</dt>
          <dd><code className="mono">{notification.payload.shipment_id}</code></dd>
        </div>
        <div>
          <dt>Регион</dt>
          <dd><code className="mono">{notification.payload.region_id}</code></dd>
        </div>
        <div>
          <dt>Статус на момент уведомления</dt>
          <dd>{statusLabel(notification.payload.status)}</dd>
        </div>
        <div>
          <dt>{caseTimeLabel}</dt>
          <dd>{formatDateTime(notification.payload.last_event_at)}</dd>
        </div>
        <div>
          <dt>Получено</dt>
          <dd>{formatDateTime(notification.created_at)}</dd>
        </div>
        {notification.payload.container_id && (
          <div>
            <dt>Контейнер</dt>
            <dd><code className="mono">{notification.payload.container_id}</code></dd>
          </div>
        )}
        {notification.payload.proposal_id && (
          <div>
            <dt>Предложение</dt>
            <dd><code className="mono">{notification.payload.proposal_id}</code></dd>
          </div>
        )}
        {notification.read_at && (
          <div>
            <dt>Отмечено прочитанным</dt>
            <dd>{formatDateTime(notification.read_at)}</dd>
          </div>
        )}
      </dl>
      {error && <p className="notification-error" role="alert">{error}</p>}
      {isUnread && (
        <button
          aria-label={`Отметить уведомление «${title}» для отправки ${notification.payload.shipment_id} прочитанным`}
          className="button button-quiet notification-read-button"
          disabled={!isOnline || busy}
          onClick={() => onMarkRead(notification)}
          type="button"
        >
          {busy ? 'Сохраняем…' : 'Отметить прочитанным'}
        </button>
      )}
    </li>
  );
}

export function NotificationInbox({
  token,
  unreadCount,
  onUnreadCountChange,
  onUnauthorized,
  onReturnToWorkspace,
  returnWorkspaceLabel,
}: InboxProps) {
  const [isOnline, setIsOnline] = useState(navigator.onLine);
  const [filter, setFilter] = useState<InboxFilter>('all');
  const [offset, setOffset] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [listResult, setListResult] = useState<NotificationListResult | null>(null);
  const [refreshingAfterRead, setRefreshingAfterRead] = useState(false);
  const [markingIds, setMarkingIds] = useState<Set<string>>(() => new Set());
  const [markErrors, setMarkErrors] = useState<Record<string, string>>({});
  const [announcement, setAnnouncement] = useState('');
  const idempotencyKeys = useRef(new Map<string, string>());

  const requestKey = JSON.stringify([token, filter, offset, refreshVersion, isOnline]);
  const matchingResult = listResult?.requestKey === requestKey ? listResult : null;
  const page = matchingResult && 'page' in matchingResult ? matchingResult.page : null;
  const listError = !isOnline
    ? requestErrorMessage(null, false)
    : matchingResult && 'error' in matchingResult
      ? matchingResult.error
      : '';
  const loading = Boolean(isOnline && !matchingResult);

  useEffect(() => {
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  useEffect(() => {
    if (!isOnline) return;
    const controller = new AbortController();
    void listInboxNotifications(token, {
      limit: 20,
      offset,
      unreadOnly: filter === 'unread',
      signal: controller.signal,
    })
      .then((data) => {
        if (controller.signal.aborted) return;
        const validOffset = lastValidOffset(data.total, data.limit);
        if (offset > validOffset) {
          setOffset(validOffset);
          return;
        }
        setListResult({ requestKey, page: data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          setListResult({
            requestKey,
            error: 'Срок действия сеанса истёк. Войдите снова; локальная очередь на устройстве сохранена.',
          });
          onUnauthorized();
        } else {
          setListResult({ requestKey, error: requestErrorMessage(error, isOnline) });
        }
      });

    return () => controller.abort();
  }, [filter, isOnline, offset, onUnauthorized, refreshVersion, requestKey, token]);

  async function refreshListAfterRead(currentRequestKey: string) {
    setRefreshingAfterRead(true);
    try {
      const data = await listInboxNotifications(token, {
        limit: 20,
        offset,
        unreadOnly: filter === 'unread',
      });
      const validOffset = lastValidOffset(data.total, data.limit);
      if (offset > validOffset) {
        setOffset(validOffset);
        return;
      }
      setListResult({ requestKey: currentRequestKey, page: data });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setListResult({
          requestKey: currentRequestKey,
          error: 'Срок действия сеанса истёк. Войдите снова; локальная очередь на устройстве сохранена.',
        });
        onUnauthorized();
      } else {
        setListResult({
          requestKey: currentRequestKey,
          error: requestErrorMessage(error, navigator.onLine),
        });
      }
    } finally {
      setRefreshingAfterRead(false);
    }
  }

  async function markRead(notification: Notification) {
    if (!isOnline || notification.read_at || markingIds.has(notification.id)) return;

    const idempotencyKey = idempotencyKeys.current.get(notification.id) ?? crypto.randomUUID();
    idempotencyKeys.current.set(notification.id, idempotencyKey);
    setMarkingIds((current) => new Set(current).add(notification.id));
    setMarkErrors((current) => {
      const next = { ...current };
      delete next[notification.id];
      return next;
    });

    try {
      const updated = await markInboxNotificationRead(token, notification.id, idempotencyKey);
      idempotencyKeys.current.delete(notification.id);
      setListResult((current) => {
        if (!current || current.requestKey !== requestKey || !('page' in current)) return current;
        const updatedPage = updateReadNotification(current.page, updated);
        if (filter === 'unread') {
          return {
            requestKey,
            page: {
              ...updatedPage,
              items: updatedPage.items.filter((item) => item.id !== updated.id),
              total: Math.max(0, updatedPage.total - 1),
            },
          };
        }
        return { requestKey, page: updatedPage };
      });
      setAnnouncement('Уведомление отмечено прочитанным.');
      await refreshListAfterRead(requestKey);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setMarkErrors((current) => ({
          ...current,
          [notification.id]: 'Срок действия сеанса истёк. Войдите снова.',
        }));
        onUnauthorized();
      } else {
        setMarkErrors((current) => ({
          ...current,
          [notification.id]: markErrorMessage(error, navigator.onLine),
        }));
      }
    } finally {
      onUnreadCountChange();
      setMarkingIds((current) => {
        const next = new Set(current);
        next.delete(notification.id);
        return next;
      });
    }
  }

  function refresh() {
    setAnnouncement('');
    setRefreshVersion((version) => version + 1);
    onUnreadCountChange();
  }

  function changeFilter(nextFilter: InboxFilter) {
    setFilter(nextFilter);
    setOffset(0);
    setAnnouncement('');
  }

  return (
    <section className="panel inbox-panel" aria-labelledby="inbox-heading">
      <div className="section-heading inbox-heading">
        <div>
          <p className="eyebrow">Личные уведомления</p>
          <h1 id="inbox-heading">Входящие</h1>
          <p className="inbox-unread-count">
            Непрочитанных: {unreadCount === null ? '—' : formatCount(unreadCount)}
          </p>
        </div>
        <div className="inbox-actions">
          {onReturnToWorkspace && returnWorkspaceLabel && (
            <button
              className="button button-quiet"
              onClick={onReturnToWorkspace}
              type="button"
            >
              Вернуться в {returnWorkspaceLabel}
            </button>
          )}
          <button
            className="button button-quiet"
            disabled={!isOnline || loading || refreshingAfterRead}
            onClick={refresh}
            type="button"
          >
            Обновить список и счётчик
          </button>
        </div>
      </div>

      {!isOnline && (
        <p className="offline-note inbox-offline" role="status">
          Нет подключения к сети. Список не загружается; отметка о прочтении офлайн не ставится и не будет отправлена позже.
        </p>
      )}

      <div className="inbox-filter">
        <label htmlFor="inbox-read-filter">Показывать</label>
        <select
          id="inbox-read-filter"
          disabled={markingIds.size > 0}
          onChange={(event) => changeFilter(event.target.value as InboxFilter)}
          value={filter}
        >
          <option value="all">Все уведомления</option>
          <option value="unread">Только непрочитанные</option>
        </select>
      </div>

      {announcement && <p className="inbox-announcement" role="status" aria-live="polite">{announcement}</p>}
      {loading && <p className="inbox-state" role="status" aria-live="polite">Загружаем уведомления…</p>}
      {refreshingAfterRead && (
        <p className="inbox-state" role="status" aria-live="polite">Обновляем список…</p>
      )}
      {listError && (
        <div className="inbox-list-error">
          <p className="notification-error" role="alert">{listError}</p>
          <button
            className="button button-quiet"
            disabled={!isOnline || loading || refreshingAfterRead}
            onClick={refresh}
            type="button"
          >
            Повторить загрузку
          </button>
        </div>
      )}
      {!loading && page && page.items.length === 0 && (
        <p className="inbox-empty" role="status">
          {filter === 'unread' ? 'Непрочитанных уведомлений нет.' : 'Уведомлений пока нет.'}
        </p>
      )}
      {!loading && page && page.items.length > 0 && (
        <>
          <p className="inbox-total" role="status" aria-live="polite">
            Всего уведомлений в выборке: {formatCount(page.total)}
          </p>
          <ol className="notification-list" aria-label="Уведомления получателя">
            {page.items.map((notification) => (
              <NotificationCard
                busy={markingIds.has(notification.id)}
                error={markErrors[notification.id] ?? ''}
                isOnline={isOnline}
                key={notification.id}
                notification={notification}
                onMarkRead={markRead}
              />
            ))}
          </ol>
        </>
      )}
      {!loading && page && page.total > 0 && (
        <nav className="inbox-pagination" aria-label="Страницы входящих уведомлений">
          <button
            className="button button-quiet"
            disabled={!isOnline || markingIds.size > 0 || page.offset <= 0}
            onClick={() => setOffset(Math.max(0, page.offset - page.limit))}
            type="button"
          >
            Предыдущая
          </button>
          <span>
            {page.items.length === 0 ? 'На этой странице нет уведомлений' : 'Показано'}{' '}
            {page.items.length > 0 && formatCount(page.offset + 1)}
            {page.items.length > 0 && '–'}
            {page.items.length > 0 && formatCount(Math.min(page.offset + page.items.length, page.total))}
            {' '}из {formatCount(page.total)}
          </span>
          <button
            className="button button-quiet"
            disabled={!isOnline || markingIds.size > 0 || page.offset + page.limit >= page.total}
            onClick={() => setOffset(page.offset + page.limit)}
            type="button"
          >
            Следующая
          </button>
        </nav>
      )}
    </section>
  );
}
