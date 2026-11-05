import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { ApiError, getShipmentSummary, getStalledShipments } from './api';
import {
  deriveOperationsCapabilities,
  type AnalyticsGranularity,
  type CurrentUser,
  type PagedStalledShipment,
  type ShipmentSummary,
  type StalledShipment,
} from './types';
import { safeErrorMessage, shipmentStatusLabel } from './localization';

type OperationsDashboardProps = {
  user: CurrentUser;
  token: string;
  onUnauthorized: () => void;
};

type AppliedSummaryFilters = {
  regionId: string;
  granularity: AnalyticsGranularity;
  fromDate: string;
  toDate: string;
};

type StalledLoadResult =
  | { requestKey: string; page: PagedStalledShipment }
  | { requestKey: string; error: string };

type SummaryLoadResult =
  | { requestKey: string; summary: ShipmentSummary }
  | { requestKey: string; error: string };

const CASE_TYPE_LABELS: Record<StalledShipment['case_type'], string> = {
  PICKUP_PENDING: 'Ожидает забора',
  DELIVERY_PENDING: 'Ожидает доставки',
  PROPOSAL_PENDING: 'Ожидает решения по предложению',
  SHIPMENT_OVERDUE: 'Срок доставки превышен',
};

const UTC_DATE_FORMAT = new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'medium',
  timeZone: 'UTC',
});
const UTC_DATE_TIME_FORMAT = new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'UTC',
});

function formatCount(value: number): string {
  return new Intl.NumberFormat('ru-RU').format(value);
}

function formatUtcDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `${UTC_DATE_TIME_FORMAT.format(date)} UTC`;
}

function formatUtcDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `${UTC_DATE_FORMAT.format(date)} UTC`;
}

function formatAge(seconds: number): string {
  const age = Math.max(0, Math.floor(seconds));
  const days = Math.floor(age / 86_400);
  const hours = Math.floor((age % 86_400) / 3_600);
  const minutes = Math.floor((age % 3_600) / 60);
  const remainingSeconds = age % 60;
  const parts: string[] = [];

  if (days > 0) parts.push(`${days} дн.`);
  if (hours > 0) parts.push(`${hours} ч.`);
  if (minutes > 0) parts.push(`${minutes} мин.`);
  if (parts.length === 0 || (days === 0 && hours === 0 && minutes === 0)) {
    parts.push(`${remainingSeconds} сек.`);
  }
  return parts.join(' ');
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return 'Нет данных';
  const duration = Math.max(0, Math.round(seconds));
  const days = Math.floor(duration / 86_400);
  const hours = Math.floor((duration % 86_400) / 3_600);
  const minutes = Math.floor((duration % 3_600) / 60);
  const remainingSeconds = duration % 60;
  const parts: string[] = [];

  if (days > 0) parts.push(`${days} дн.`);
  if (hours > 0) parts.push(`${hours} ч.`);
  if (minutes > 0) parts.push(`${minutes} мин.`);
  if (parts.length === 0) parts.push(`${remainingSeconds} сек.`);
  return parts.join(' ');
}

function statusLabel(status: string): string {
  return shipmentStatusLabel(status);
}

function requestErrorMessage(error: unknown, isOnline: boolean): string {
  if (!isOnline) return 'Нет подключения к сети. Данные не загружены и не сохранены на устройстве.';
  if (error instanceof TypeError) {
    return 'Не удалось подключиться к серверу. Проверьте соединение и повторите запрос.';
  }
  return safeErrorMessage(error, 'Не удалось выполнить запрос. Проверьте соединение и повторите попытку.');
}

function utcBoundary(date: string): string {
  return new Date(`${date}T00:00:00.000Z`).toISOString();
}

function StalledCaseCard({ item }: { item: StalledShipment }) {
  return (
    <li className="stalled-card">
      <div className="stalled-card-heading">
        <h3>{CASE_TYPE_LABELS[item.case_type]}</h3>
        <span className="status-badge">{statusLabel(item.status)}</span>
      </div>
      <dl className="stalled-facts">
        <div>
          <dt>Отправка</dt>
          <dd><code className="mono">{item.shipment_id}</code></dd>
        </div>
        <div>
          <dt>Регион</dt>
          <dd><code className="mono">{item.region_id}</code></dd>
        </div>
        <div>
          <dt>Возраст последнего события</dt>
          <dd>
            {formatAge(item.age_seconds)}
            <span className="fact-detail">Событие: {formatUtcDateTime(item.last_event_at)}</span>
          </dd>
        </div>
        {item.container_id && (
          <div>
            <dt>Контейнер</dt>
            <dd><code className="mono">{item.container_id}</code></dd>
          </div>
        )}
        {item.proposal_id && (
          <div>
            <dt>Предложение</dt>
            <dd><code className="mono">{item.proposal_id}</code></dd>
          </div>
        )}
      </dl>
    </li>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="operations-metric">
      <dt>{label}</dt>
      <dd>{typeof value === 'number' ? formatCount(value) : value}</dd>
    </div>
  );
}

function AnalyticsResults({ summary }: { summary: ShipmentSummary }) {
  const statuses = Object.entries(summary.by_status).sort(([left], [right]) =>
    statusLabel(left).localeCompare(statusLabel(right), 'ru'),
  );

  return (
    <div className="analytics-results">
      <p className="analytics-scope">
        Регион: <code className="mono">{summary.region_id}</code>
        <span>
          Период UTC: [{summary.from ? formatUtcDateTime(summary.from) : 'не задано'};{' '}
          {summary.to ? formatUtcDateTime(summary.to) : 'не задано'})
        </span>
      </p>
      <section className="analytics-subsection" aria-labelledby="analytics-metrics-heading">
        <h3 id="analytics-metrics-heading">Сводные показатели</h3>
        <dl className="operations-metrics">
          <Metric label="Отправок в выборке" value={summary.total} />
          <Metric label="Завершено с проблемами" value={summary.completed_with_issues} />
          <Metric label="Просрочено, в работе" value={summary.overdue_active} />
          <Metric label="Закрыто без доставки" value={summary.closed_undelivered} />
          <Metric label="Доставлено частично" value={summary.partially_delivered} />
          <Metric
            label="Завершено с проблемами и частичной доставкой"
            value={summary.partially_completed_with_issues}
          />
          <Metric label="Образцов отсутствует" value={summary.missing_samples} />
          <Metric label="Образцов отклонено" value={summary.rejected_samples} />
          <Metric label="Контейнеров отсутствует" value={summary.container_missing} />
          <Metric label="Контейнеров отклонено" value={summary.container_rejected} />
          <Metric
            label="Среднее время полной доставки"
            value={formatDuration(summary.average_full_shipment_delivery_seconds)}
          />
          <Metric
            label="Среднее время доставки принятого контейнера при частичной доставке"
            value={formatDuration(summary.average_received_container_delivery_seconds)}
          />
        </dl>
      </section>

      <section className="analytics-subsection" aria-labelledby="analytics-status-heading">
        <h3 id="analytics-status-heading">Отправки по статусам</h3>
        {statuses.length === 0 ? (
          <p className="muted">Нет данных по статусам за выбранный период.</p>
        ) : (
          <div className="operations-table-wrap" tabIndex={0} aria-label="Таблица статусов с горизонтальной прокруткой">
            <table className="operations-table compact-table">
              <thead>
                <tr><th scope="col">Статус</th><th scope="col">Количество отправок</th></tr>
              </thead>
              <tbody>
                {statuses.map(([status, count]) => (
                  <tr key={status}>
                    <th scope="row">{statusLabel(status)}</th>
                    <td>{formatCount(count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="analytics-subsection" aria-labelledby="analytics-period-heading">
        <div className="analytics-section-heading">
          <h3 id="analytics-period-heading">Динамика по периодам</h3>
          <p className="muted">Границы в UTC; неделя начинается в понедельник.</p>
        </div>
        {summary.period_buckets.length === 0 ? (
          <p className="muted">Нет периодов для отображения.</p>
        ) : (
          <div className="operations-table-wrap" tabIndex={0} aria-label="Таблица периодов с горизонтальной прокруткой">
            <table className="operations-table">
              <thead>
                <tr>
                  <th scope="col">Период UTC</th>
                  <th scope="col">Отправок</th>
                  <th scope="col">Завершено</th>
                  <th scope="col">Доставлено частично</th>
                  <th scope="col">Проблемы с образцами</th>
                  <th scope="col">Проблемы с контейнерами</th>
                </tr>
              </thead>
              <tbody>
                {summary.period_buckets.map((bucket) => (
                  <tr key={`${bucket.period_start}:${bucket.period_end}`}>
                    <th scope="row">
                      {formatUtcDate(bucket.period_start)} – {formatUtcDate(bucket.period_end)}
                    </th>
                    <td>{formatCount(bucket.total_shipments)}</td>
                    <td>{formatCount(bucket.completed_shipments)}</td>
                    <td>{formatCount(bucket.partially_delivered_shipments)}</td>
                    <td>{formatCount(bucket.sample_issues)}</td>
                    <td>{formatCount(bucket.container_issues)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="analytics-subsection" aria-labelledby="analytics-facility-heading">
        <h3 id="analytics-facility-heading">Показатели по организациям</h3>
        {summary.by_facility.length === 0 ? (
          <p className="muted">Нет данных по организациям за выбранный период.</p>
        ) : (
          <div className="operations-table-wrap" tabIndex={0} aria-label="Таблица организаций с горизонтальной прокруткой">
            <table className="operations-table">
              <thead>
                <tr>
                  <th scope="col">Организация</th>
                  <th scope="col">Отправок</th>
                  <th scope="col">Завершено</th>
                  <th scope="col">Завершено с проблемами</th>
                </tr>
              </thead>
              <tbody>
                {summary.by_facility.map((facility) => (
                  <tr key={facility.organization_id}>
                    <th scope="row"><code className="mono">{facility.organization_id}</code></th>
                    <td>{formatCount(facility.shipment_count)}</td>
                    <td>{formatCount(facility.completed_count)}</td>
                    <td>{formatCount(facility.with_issues_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

export function OperationsDashboard({
  user,
  token,
  onUnauthorized,
}: OperationsDashboardProps) {
  const capabilities = useMemo(() => deriveOperationsCapabilities(user), [user]);
  const [isOnline, setIsOnline] = useState(navigator.onLine);
  const [caseRegionId, setCaseRegionId] = useState('');
  const [caseOffset, setCaseOffset] = useState(0);
  const [caseRefresh, setCaseRefresh] = useState(0);
  const [stalledResult, setStalledResult] = useState<StalledLoadResult | null>(null);

  const initialAnalyticsRegion =
    capabilities.analyticsRegionIds.length === 1 ? capabilities.analyticsRegionIds[0] : '';
  const [analyticsRegionId, setAnalyticsRegionId] = useState(initialAnalyticsRegion);
  const [analyticsGranularity, setAnalyticsGranularity] = useState<AnalyticsGranularity>('day');
  const [analyticsFromDate, setAnalyticsFromDate] = useState('');
  const [analyticsToDate, setAnalyticsToDate] = useState('');
  const [analyticsValidationError, setAnalyticsValidationError] = useState('');
  const [appliedAnalyticsFilters, setAppliedAnalyticsFilters] = useState<AppliedSummaryFilters | null>(
    initialAnalyticsRegion
      ? {
          regionId: initialAnalyticsRegion,
          granularity: 'day',
          fromDate: '',
          toDate: '',
        }
      : null,
  );
  const [analyticsRefresh, setAnalyticsRefresh] = useState(0);
  const [summaryResult, setSummaryResult] = useState<SummaryLoadResult | null>(null);

  const caseRequestKey = JSON.stringify([
    token,
    caseRegionId,
    caseOffset,
    caseRefresh,
    isOnline,
  ]);
  const matchingStalledResult = stalledResult?.requestKey === caseRequestKey ? stalledResult : null;
  const stalledPage = matchingStalledResult && 'page' in matchingStalledResult
    ? matchingStalledResult.page
    : null;
  const stalledError = !isOnline
    ? requestErrorMessage(null, false)
    : matchingStalledResult && 'error' in matchingStalledResult
      ? matchingStalledResult.error
      : '';
  const stalledLoading = Boolean(
    capabilities.canViewCases && isOnline && !matchingStalledResult,
  );

  const summaryRequestKey = appliedAnalyticsFilters
    ? JSON.stringify([token, appliedAnalyticsFilters, analyticsRefresh, isOnline])
    : '';
  const matchingSummaryResult = summaryResult?.requestKey === summaryRequestKey ? summaryResult : null;
  const summary = matchingSummaryResult && 'summary' in matchingSummaryResult
    ? matchingSummaryResult.summary
    : null;
  const summaryError = !isOnline && capabilities.canViewAnalytics
    ? requestErrorMessage(null, false)
    : matchingSummaryResult && 'error' in matchingSummaryResult
      ? matchingSummaryResult.error
      : '';
  const summaryLoading = Boolean(
    capabilities.canViewAnalytics && appliedAnalyticsFilters && isOnline && !matchingSummaryResult,
  );

  useEffect(() => {
    const handleOnline = () => {
      setIsOnline(true);
      setCaseRefresh((value) => value + 1);
      setAnalyticsRefresh((value) => value + 1);
    };
    const handleOffline = () => {
      setIsOnline(false);
      setCaseRefresh((value) => value + 1);
      setAnalyticsRefresh((value) => value + 1);
    };

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  useEffect(() => {
    if (!capabilities.canViewCases || !isOnline) return;
    const controller = new AbortController();
    void getStalledShipments(token, {
      limit: 20,
      offset: caseOffset,
      ...(caseRegionId ? { regionId: caseRegionId } : {}),
      signal: controller.signal,
    })
      .then((page) => {
        if (!controller.signal.aborted) setStalledResult({ requestKey: caseRequestKey, page });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          setStalledResult({
            requestKey: caseRequestKey,
            error: 'Срок действия сеанса истёк. Войдите снова. Данные очереди на устройстве сохранены.',
          });
          onUnauthorized();
        } else {
          setStalledResult({ requestKey: caseRequestKey, error: requestErrorMessage(error, isOnline) });
        }
      });

    return () => controller.abort();
  }, [
    caseOffset,
    caseRefresh,
    caseRegionId,
    capabilities.canViewCases,
    caseRequestKey,
    isOnline,
    onUnauthorized,
    token,
  ]);

  useEffect(() => {
    if (!capabilities.canViewAnalytics || !appliedAnalyticsFilters || !isOnline) return;
    const controller = new AbortController();
    void getShipmentSummary(token, {
      granularity: appliedAnalyticsFilters.granularity,
      regionId: appliedAnalyticsFilters.regionId,
      ...(appliedAnalyticsFilters.fromDate
        ? { from: utcBoundary(appliedAnalyticsFilters.fromDate) }
        : {}),
      ...(appliedAnalyticsFilters.toDate
        ? { to: utcBoundary(appliedAnalyticsFilters.toDate) }
        : {}),
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted) {
          setSummaryResult({ requestKey: summaryRequestKey, summary: data });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          setSummaryResult({
            requestKey: summaryRequestKey,
            error: 'Срок действия сеанса истёк. Войдите снова. Данные очереди на устройстве сохранены.',
          });
          onUnauthorized();
        } else {
          setSummaryResult({
            requestKey: summaryRequestKey,
            error: requestErrorMessage(error, isOnline),
          });
        }
      });

    return () => controller.abort();
  }, [
    analyticsRefresh,
    appliedAnalyticsFilters,
    capabilities.canViewAnalytics,
    summaryRequestKey,
    isOnline,
    onUnauthorized,
    token,
  ]);

  function applyAnalyticsFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAnalyticsValidationError('');

    if (!capabilities.analyticsRegionIds.includes(analyticsRegionId)) {
      setAnalyticsValidationError('Выберите регион, доступный для аналитики.');
      return;
    }
    if (analyticsFromDate && analyticsToDate && analyticsFromDate >= analyticsToDate) {
      setAnalyticsValidationError('Дата «до» должна быть позже даты «с».');
      return;
    }

    setAppliedAnalyticsFilters({
      regionId: analyticsRegionId,
      granularity: analyticsGranularity,
      fromDate: analyticsFromDate,
      toDate: analyticsToDate,
    });
  }

  const analyticsFiltersChanged = Boolean(
    appliedAnalyticsFilters &&
      (appliedAnalyticsFilters.regionId !== analyticsRegionId ||
        appliedAnalyticsFilters.granularity !== analyticsGranularity ||
        appliedAnalyticsFilters.fromDate !== analyticsFromDate ||
        appliedAnalyticsFilters.toDate !== analyticsToDate),
  );

  return (
    <div className="operations-dashboard">
      <section className="operations-intro" aria-labelledby="operations-title">
        <div>
          <p className="eyebrow">Региональная работа</p>
          <h1 id="operations-title">Операционная панель</h1>
          <p className="intro-copy">
            Просматривайте задержанные случаи и сводные показатели в пределах выданного доступа.
            Аналитика содержит только агрегированные данные.
          </p>
        </div>
        <div className="operations-stamp" aria-hidden="true">LT</div>
      </section>

      {!isOnline && (
        <p className="offline-note operations-offline" role="status">
          Нет подключения к сети. Данные панели не загружаются и не сохраняются на устройстве.
        </p>
      )}

      {capabilities.canViewCases && (
        <section className="panel operations-panel" aria-labelledby="stalled-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Текущие случаи</p>
              <h2 id="stalled-heading">Задержанные отправки</h2>
            </div>
            <button
              className="button button-quiet"
              disabled={stalledLoading || !isOnline}
              onClick={() => setCaseRefresh((value) => value + 1)}
              type="button"
            >
              Обновить
            </button>
          </div>

          <div className="operations-filters">
            <label htmlFor="case-region">Регион случаев</label>
            <select
              id="case-region"
              onChange={(event) => {
                setCaseRegionId(event.target.value);
                setCaseOffset(0);
              }}
              value={caseRegionId}
            >
              <option value="">Все доступные регионы</option>
              {capabilities.caseRegionIds.map((regionId) => (
                <option key={regionId} value={regionId}>{regionId}</option>
              ))}
            </select>
            <p className="filter-help">
              Без фильтра отображаются случаи во всех регионах, разрешённых сервером.
            </p>
          </div>

          {stalledLoading && (
            <p className="operations-state" role="status" aria-live="polite">Загружаем случаи…</p>
          )}
          {stalledError && <p className="operations-error" role="alert">{stalledError}</p>}
          {!stalledLoading && stalledPage && stalledPage.items.length === 0 && (
            <p className="operations-empty" role="status">Задержанных случаев не найдено.</p>
          )}
          {!stalledLoading && stalledPage && stalledPage.items.length > 0 && (
            <>
              <p className="operations-result-count" role="status" aria-live="polite">
                Всего случаев: {formatCount(stalledPage.total)}
              </p>
              <ol className="stalled-list" aria-label="Задержанные случаи">
                {stalledPage.items.map((item) => (
                  <StalledCaseCard
                    item={item}
                    key={`${item.case_type}:${item.shipment_id}:${item.container_id ?? '-'}:${item.proposal_id ?? '-'}`}
                  />
                ))}
              </ol>
              <nav className="operations-pagination" aria-label="Страницы задержанных случаев">
                <button
                  className="button button-quiet"
                  disabled={stalledLoading || stalledPage.offset <= 0}
                  onClick={() => setCaseOffset(Math.max(0, stalledPage.offset - stalledPage.limit))}
                  type="button"
                >
                  Предыдущая
                </button>
                <span>
                  Показано {formatCount(stalledPage.offset + 1)}–
                  {formatCount(Math.min(stalledPage.offset + stalledPage.items.length, stalledPage.total))}
                  {' '}из {formatCount(stalledPage.total)}
                </span>
                <button
                  className="button button-quiet"
                  disabled={
                    stalledLoading || stalledPage.offset + stalledPage.limit >= stalledPage.total
                  }
                  onClick={() => setCaseOffset(stalledPage.offset + stalledPage.limit)}
                  type="button"
                >
                  Следующая
                </button>
              </nav>
            </>
          )}
        </section>
      )}

      {capabilities.canViewAnalytics && (
        <section className="panel operations-panel" aria-labelledby="analytics-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Агрегированные данные</p>
              <h2 id="analytics-heading">Аналитика отправок</h2>
            </div>
          </div>

          <form className="analytics-filters" onSubmit={applyAnalyticsFilters}>
            <div className="analytics-filter-grid">
              <div className="filter-control">
                <label htmlFor="analytics-region">Регион аналитики</label>
                <select
                  disabled={capabilities.analyticsRegionIds.length === 0}
                  id="analytics-region"
                  onChange={(event) => setAnalyticsRegionId(event.target.value)}
                  value={analyticsRegionId}
                >
                  {capabilities.analyticsRegionIds.length !== 1 && (
                    <option value="">Выберите регион</option>
                  )}
                  {capabilities.analyticsRegionIds.map((regionId) => (
                    <option key={regionId} value={regionId}>{regionId}</option>
                  ))}
                </select>
              </div>
              <div className="filter-control">
                <label htmlFor="analytics-granularity">Группировка</label>
                <select
                  id="analytics-granularity"
                  onChange={(event) => setAnalyticsGranularity(event.target.value as AnalyticsGranularity)}
                  value={analyticsGranularity}
                >
                  <option value="day">По дням</option>
                  <option value="week">По неделям</option>
                </select>
              </div>
              <div className="filter-control">
                <label htmlFor="analytics-from">С даты (UTC, включительно)</label>
                <input
                  id="analytics-from"
                  onChange={(event) => setAnalyticsFromDate(event.target.value)}
                  type="date"
                  value={analyticsFromDate}
                />
              </div>
              <div className="filter-control">
                <label htmlFor="analytics-to">До даты (UTC, не включая)</label>
                <input
                  id="analytics-to"
                  onChange={(event) => setAnalyticsToDate(event.target.value)}
                  type="date"
                  value={analyticsToDate}
                />
              </div>
            </div>
            <p className="filter-help">
              Интервал задаётся по границам календарных дат UTC: начало включено, дата «до» не включена.
              Недели начинаются в понедельник UTC. Если даты не заданы, отображаются все доступные периоды.
            </p>
            {capabilities.analyticsRegionIds.length > 1 && !analyticsRegionId && (
              <p className="filter-help">Для просмотра сводки выберите один из доступных регионов.</p>
            )}
            {capabilities.analyticsRegionIds.length === 0 && (
              <p className="operations-error" role="alert">
                Для этой учётной записи не передан доступный регион аналитики.
              </p>
            )}
            {analyticsValidationError && (
              <p className="operations-error" role="alert">{analyticsValidationError}</p>
            )}
            <button
              className="button button-secondary"
              disabled={capabilities.analyticsRegionIds.length === 0}
              type="submit"
            >
              Применить фильтры
            </button>
          </form>

          {analyticsFiltersChanged && summary && (
            <p className="filter-pending" role="status">
              Показана сводка по ранее применённым фильтрам. Нажмите «Применить фильтры», чтобы обновить её.
            </p>
          )}
          {summaryLoading && (
            <p className="operations-state" role="status" aria-live="polite">Загружаем сводку…</p>
          )}
          {summaryError && (
            <div className="operations-error-group">
              <p className="operations-error" role="alert">{summaryError}</p>
              <button
                className="button button-quiet"
                disabled={!isOnline || summaryLoading}
                onClick={() => setAnalyticsRefresh((value) => value + 1)}
                type="button"
              >
                Повторить запрос
              </button>
            </div>
          )}
          {!summaryLoading && summary && <AnalyticsResults summary={summary} />}
          {!summaryLoading && !summaryError && !summary && !appliedAnalyticsFilters &&
            capabilities.analyticsRegionIds.length > 1 && (
              <p className="operations-empty" role="status">
                Выберите регион и примените фильтры, чтобы загрузить сводку.
              </p>
            )}
        </section>
      )}
    </div>
  );
}
