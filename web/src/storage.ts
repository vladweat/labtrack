import type {
  CommandState,
  OfflineCommand,
  OfflineManifest,
  QueueItem,
  StoredCommand,
  StoredCommandState,
  StoredOutcome,
  StoredSnapshot,
  SyncResult,
} from './types';

const DATABASE_NAME = 'labtrack-courier-v1';
const DATABASE_VERSION = 1;
const STORE_NAMES = ['snapshots', 'commands', 'command-states', 'outcomes', 'meta'] as const;
const ACTIVE_USER_KEY = 'active-user';

type MetaRecord = { key: string; userId: string | null };

let databasePromise: Promise<IDBDatabase> | undefined;

function openDatabase(): Promise<IDBDatabase> {
  if (databasePromise) return databasePromise;

  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      for (const storeName of ['snapshots', 'commands', 'command-states', 'outcomes']) {
        if (!database.objectStoreNames.contains(storeName)) {
          const store = database.createObjectStore(storeName, { keyPath: 'id' });
          store.createIndex('userId', 'userId', { unique: false });
        }
      }
      if (!database.objectStoreNames.contains('meta')) {
        database.createObjectStore('meta', { keyPath: 'key' });
      }
    };
    request.onerror = () => reject(request.error ?? new Error('Could not open local courier storage.'));
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
  });

  return databasePromise;
}

function requestValue<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Local storage request failed.'));
  });
}

function transactionCompletion(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error ?? new Error('Local storage transaction aborted.'));
    transaction.onerror = () => reject(transaction.error ?? new Error('Local storage transaction failed.'));
  });
}

async function runTransaction<T>(
  stores: readonly string[],
  mode: IDBTransactionMode,
  operation: (transaction: IDBTransaction) => Promise<T> | T,
): Promise<T> {
  const database = await openDatabase();
  const transaction = database.transaction([...stores], mode);
  const completion = transactionCompletion(transaction);
  try {
    const result = await operation(transaction);
    await completion;
    return result;
  } catch (error) {
    try {
      transaction.abort();
    } catch {
      // The transaction may already have completed while an operation error surfaced.
    }
    await completion.catch(() => undefined);
    throw error;
  }
}

function recordId(userId: string, commandId: string): string {
  return `${userId}:${commandId}`;
}

function scheduleUserDeletion(transaction: IDBTransaction, storeName: string, userId: string) {
  const store = transaction.objectStore(storeName);
  const request = store.index('userId').getAllKeys(IDBKeyRange.only(userId));
  request.onsuccess = () => {
    for (const key of request.result) store.delete(key);
  };
}

export async function getActiveUserId(): Promise<string | null> {
  return runTransaction(['meta'], 'readonly', async (transaction) => {
    const record = await requestValue<MetaRecord | undefined>(
      transaction.objectStore('meta').get(ACTIVE_USER_KEY),
    );
    return record?.userId ?? null;
  });
}

export async function setActiveUserId(userId: string | null): Promise<void> {
  await runTransaction(['meta'], 'readwrite', (transaction) => {
    transaction.objectStore('meta').put({ key: ACTIVE_USER_KEY, userId } satisfies MetaRecord);
  });
}

export async function purgeUserData(userId: string, nextUserId: string | null = null): Promise<void> {
  await runTransaction([...STORE_NAMES], 'readwrite', (transaction) => {
    for (const storeName of ['snapshots', 'commands', 'command-states', 'outcomes']) {
      scheduleUserDeletion(transaction, storeName, userId);
    }
    const meta = transaction.objectStore('meta');
    const request = meta.get(ACTIVE_USER_KEY) as IDBRequest<MetaRecord | undefined>;
    request.onsuccess = () => {
      if (request.result?.userId === userId) {
        meta.put({ key: ACTIVE_USER_KEY, userId: nextUserId } satisfies MetaRecord);
      }
    };
  });
}

export async function saveSnapshot(userId: string, snapshot: OfflineManifest): Promise<void> {
  const record: StoredSnapshot = {
    id: recordId(userId, snapshot.shipment_id),
    userId,
    snapshot,
    savedAt: new Date().toISOString(),
  };
  await runTransaction(['snapshots'], 'readwrite', (transaction) => {
    transaction.objectStore('snapshots').put(record);
  });
}

export async function listSnapshots(userId: string): Promise<StoredSnapshot[]> {
  return runTransaction(['snapshots'], 'readonly', (transaction) =>
    requestValue<StoredSnapshot[]>(
      transaction.objectStore('snapshots').index('userId').getAll(IDBKeyRange.only(userId)),
    ),
  ).then((records) => records.sort((left, right) => right.savedAt.localeCompare(left.savedAt)));
}

export async function addCommand(userId: string, command: OfflineCommand): Promise<void> {
  const id = recordId(userId, command.client_command_id);
  const record: StoredCommand = { id, userId, command, queuedAt: new Date().toISOString() };
  const state: StoredCommandState = {
    id,
    userId,
    commandId: command.client_command_id,
    state: 'PENDING_SYNC',
    updatedAt: new Date().toISOString(),
  };
  await runTransaction(['commands', 'command-states'], 'readwrite', (transaction) => {
    transaction.objectStore('commands').add(record);
    transaction.objectStore('command-states').add(state);
  });
}

export async function saveOutcome(userId: string, result: SyncResult): Promise<void> {
  const id = recordId(userId, result.client_command_id);
  const record: StoredOutcome = {
    id,
    userId,
    commandId: result.client_command_id,
    result,
    savedAt: new Date().toISOString(),
  };
  await runTransaction(['outcomes'], 'readwrite', (transaction) => {
    transaction.objectStore('outcomes').put(record);
  });
}

export async function setCommandState(
  userId: string,
  commandId: string,
  state: CommandState,
): Promise<void> {
  const id = recordId(userId, commandId);
  await runTransaction(['command-states'], 'readwrite', async (transaction) => {
    const store = transaction.objectStore('command-states');
    const current = await requestValue<StoredCommandState | undefined>(store.get(id));
    if (!current) throw new Error('The queued command status is missing from local storage.');
    store.put({ ...current, state, updatedAt: new Date().toISOString() });
  });
}

export async function listQueue(userId: string): Promise<QueueItem[]> {
  const records = await runTransaction(
    ['commands', 'command-states', 'outcomes'],
    'readonly',
    async (transaction) => {
      const commands = transaction
        .objectStore('commands')
        .index('userId')
        .getAll(IDBKeyRange.only(userId)) as IDBRequest<StoredCommand[]>;
      const states = transaction
        .objectStore('command-states')
        .index('userId')
        .getAll(IDBKeyRange.only(userId)) as IDBRequest<StoredCommandState[]>;
      const outcomes = transaction
        .objectStore('outcomes')
        .index('userId')
        .getAll(IDBKeyRange.only(userId)) as IDBRequest<StoredOutcome[]>;
      const [commandRecords, stateRecords, outcomeRecords] = await Promise.all([
        requestValue(commands),
        requestValue(states),
        requestValue(outcomes),
      ]);
      return { commandRecords, stateRecords, outcomeRecords };
    },
  );

  const states = new Map(records.stateRecords.map((record) => [record.id, record.state]));
  const outcomes = new Map(records.outcomeRecords.map((record) => [record.id, record.result]));
  return records.commandRecords
    .map((record) => ({
      record,
      state: states.get(record.id) ?? 'PENDING_SYNC',
      outcome: outcomes.get(record.id),
    }))
    .sort((left, right) => left.record.queuedAt.localeCompare(right.record.queuedAt));
}

