import type { QueueItem } from './types';

type CommandQueueProps = {
  items: QueueItem[];
};

function commandName(item: QueueItem): string {
  return item.record.command.action === 'COURIER_CONFIRM_PICKUP'
    ? 'Pickup confirmation'
    : `Delivery initiation for ${item.record.command.container_code}`;
}

function commandStateText(item: QueueItem): string {
  if (item.state === 'PENDING_SYNC') {
    if (item.outcome?.outcome === 'RETRYABLE') {
      return `Retry needed. ${item.outcome.error?.message ?? 'The server asked for a retry.'} The same command ID and payload will be resent.`;
    }
    return 'Local pending. Server custody is unchanged until synchronization is confirmed.';
  }
  if (item.state === 'SYNCED') {
    const recordedAt = item.outcome?.recorded_at
      ? ` at ${new Date(item.outcome.recorded_at).toLocaleString()}`
      : '';
    return `Server applied${recordedAt}.`;
  }
  if (item.state === 'REVIEW_REQUIRED') {
    return 'Review required. This snapshot is older than 24 hours; no custody change was applied.';
  }
  return `Not applied by the server. ${item.outcome?.error?.message ?? 'Review the Shipment before trying again.'}`;
}

export function CommandQueue({ items }: CommandQueueProps) {
  return (
    <section className="panel queue-panel" aria-labelledby="queue-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">This device only</p>
          <h2 id="queue-heading">Command queue</h2>
        </div>
        <span className="count-pill" aria-label={`${items.length} saved commands`}>
          {items.length}
        </span>
      </div>
      {items.length === 0 ? (
        <p className="muted">No commands are queued on this device.</p>
      ) : (
        <ol className="command-list">
          {items.map((item) => (
            <li className="command-card" key={item.record.id}>
              <div className="command-card-heading">
                <h3>{commandName(item)}</h3>
                <span className={`state-badge state-${item.state.toLowerCase()}`}>
                  {item.state.replaceAll('_', ' ')}
                </span>
              </div>
              <p>{commandStateText(item)}</p>
              {item.record.command.action === 'COURIER_INITIATE_CONTAINER_DELIVERY' &&
                item.record.command.depends_on_command_id && (
                  <p className="dependency-note">
                    Explicitly linked to Pickup {item.record.command.depends_on_command_id.slice(0, 8)}.
                  </p>
                )}
              <p className="command-id">Command ID: {item.record.command.client_command_id}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
