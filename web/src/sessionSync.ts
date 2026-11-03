export type SessionChangeMessage = {
  eventId: string;
  activeUserId: string | null;
};

const CHANNEL_NAME = 'labtrack-session-v1';
const STORAGE_EVENT_KEY = 'labtrack-session-change-v1';

let channel: BroadcastChannel | null = null;
let subscribers = 0;
let channelUnavailable = false;

function getChannel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === 'undefined' || channelUnavailable) return null;
  try {
    channel ??= new BroadcastChannel(CHANNEL_NAME);
    return channel;
  } catch {
    channelUnavailable = true;
    return null;
  }
}

function parseMessage(value: unknown): SessionChangeMessage | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as Partial<SessionChangeMessage>;
  if (typeof candidate.eventId !== 'string') return null;
  if (candidate.activeUserId !== null && typeof candidate.activeUserId !== 'string') return null;
  return {
    eventId: candidate.eventId,
    activeUserId: candidate.activeUserId,
  };
}

export function publishSessionChange(activeUserId: string | null): void {
  const message: SessionChangeMessage = {
    eventId: crypto.randomUUID(),
    activeUserId,
  };
  const sharedChannel = getChannel();
  if (sharedChannel) {
    sharedChannel.postMessage(message);
    return;
  }

  try {
    window.localStorage.setItem(STORAGE_EVENT_KEY, JSON.stringify(message));
  } catch {
    // The storage fallback is best-effort when browser storage is unavailable.
  }
}

export function subscribeToSessionChanges(
  onChange: (message: SessionChangeMessage) => void,
): () => void {
  const sharedChannel = getChannel();
  let lastEventId: string | null = null;
  const receive = (value: unknown) => {
    const message = parseMessage(value);
    if (!message || message.eventId === lastEventId) return;
    lastEventId = message.eventId;
    onChange(message);
  };
  const receiveBroadcast = (event: MessageEvent<unknown>) => receive(event.data);
  const receiveStorage = (event: StorageEvent) => {
    if (event.key !== STORAGE_EVENT_KEY || !event.newValue) return;
    try {
      receive(JSON.parse(event.newValue) as unknown);
    } catch {
      // Ignore storage events that do not contain a session message.
    }
  };

  if (sharedChannel) {
    subscribers += 1;
    sharedChannel.addEventListener('message', receiveBroadcast);
  } else {
    window.addEventListener('storage', receiveStorage);
  }

  return () => {
    if (!sharedChannel) {
      window.removeEventListener('storage', receiveStorage);
      return;
    }
    sharedChannel.removeEventListener('message', receiveBroadcast);
    subscribers -= 1;
    if (subscribers === 0) {
      sharedChannel.close();
      channel = null;
    }
  };
}
