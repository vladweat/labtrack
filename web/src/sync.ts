import type { CommandState, SyncResult } from './types';
export type { CommandState, SyncResult };

type SyncResultStore = {
  saveOutcome: (result: SyncResult) => Promise<void>;
  setCommandState: (commandId: string, state: CommandState) => Promise<void>;
};

function finalOutcome(result: SyncResult): 'APPLIED' | 'REJECTED' | 'REVIEW_REQUIRED' {
  if (result.outcome === 'ALREADY_APPLIED') {
    if (!result.original_outcome) {
      throw new Error('A replay acknowledgement is missing its original outcome.');
    }
    return result.original_outcome;
  }
  if (result.outcome === 'RETRYABLE') {
    throw new Error('Retryable outcomes do not advance local command state.');
  }
  return result.outcome;
}

export async function applySyncResult(result: SyncResult, store: SyncResultStore): Promise<void> {
  await store.saveOutcome(result);
  if (result.outcome === 'RETRYABLE') return;

  const outcome = finalOutcome(result);
  const nextState: CommandState =
    outcome === 'APPLIED'
      ? 'SYNCED'
      : outcome === 'REVIEW_REQUIRED'
        ? 'REVIEW_REQUIRED'
        : 'REJECTED';
  await store.setCommandState(result.client_command_id, nextState);
}
