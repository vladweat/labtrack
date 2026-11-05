import type { QueueItem } from './types';
import { commandStateLabel, formatDateTime, localizeCommandError } from './localization';

type CommandQueueProps = {
  items: QueueItem[];
};

function commandName(item: QueueItem): string {
  return item.record.command.action === 'COURIER_CONFIRM_PICKUP'
    ? 'Подтверждение забора'
    : `Доставка контейнера ${item.record.command.container_code}`;
}

function commandStateText(item: QueueItem): string {
  if (item.state === 'PENDING_SYNC') {
    if (item.outcome?.outcome === 'RETRYABLE') {
      return `Требуется повторная попытка. ${localizeCommandError(item.outcome.error?.code, item.outcome.error?.message)} При повторе отправятся те же данные команды.`;
    }
    return 'Ожидает синхронизации. Статус передачи на сервере не изменится до подтверждения.';
  }
  if (item.state === 'SYNCED') {
    const recordedAt = item.outcome?.recorded_at
      ? ` · ${formatDateTime(item.outcome.recorded_at)}`
      : '';
    return `Применено на сервере${recordedAt}.`;
  }
  if (item.state === 'REVIEW_REQUIRED') {
    return 'Требуется проверка. Снимок старше 24 часов; данные передачи не изменены.';
  }
  return `Сервер не применил команду. ${localizeCommandError(item.outcome?.error?.code, item.outcome?.error?.message)}`;
}

export function CommandQueue({ items }: CommandQueueProps) {
  return (
    <section className="panel queue-panel" aria-labelledby="queue-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Только на этом устройстве</p>
          <h2 id="queue-heading">Очередь команд</h2>
        </div>
        <span className="count-pill" aria-label={`Сохранено команд: ${items.length}`}>
          {items.length}
        </span>
      </div>
      {items.length === 0 ? (
        <p className="muted">На этом устройстве нет команд в очереди.</p>
      ) : (
        <ol className="command-list">
          {items.map((item) => (
            <li className="command-card" key={item.record.id}>
              <div className="command-card-heading">
                <h3>{commandName(item)}</h3>
                <span className={`state-badge state-${item.state.toLowerCase()}`}>
                  {commandStateLabel(item.state)}
                </span>
              </div>
              <p>{commandStateText(item)}</p>
              {item.record.command.action === 'COURIER_INITIATE_CONTAINER_DELIVERY' &&
                item.record.command.depends_on_command_id && (
                  <p className="dependency-note">
                    Связано с подтверждением забора {item.record.command.depends_on_command_id.slice(0, 8)}.
                  </p>
                )}
              <p className="command-id">Идентификатор команды: {item.record.command.client_command_id}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
