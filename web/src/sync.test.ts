import { describe, expect, it } from 'vitest';
import { applySyncResult } from './sync';

const appliedResult = {
  client_command_id: 'command-1',
  shipment_id: 'shipment-1',
  outcome: 'APPLIED',
  original_outcome: null,
  server_status: 'IN_TRANSIT',
  recorded_at: '2026-11-02T10:00:00Z',
  error: null,
  container_id: null,
  client_performed_at: '2026-11-02T09:00:00Z',
} as const;

describe('applySyncResult', () => {
  it('persists the server acknowledgement before advancing local command state', async () => {
    const calls: string[] = [];

    await applySyncResult(appliedResult, {
      saveOutcome: async () => {
        calls.push('outcome-persisted');
      },
      setCommandState: async (_commandId, state) => {
        calls.push(`state:${state}`);
      },
    });

    expect(calls).toEqual(['outcome-persisted', 'state:SYNCED']);
  });

  it('retains a retryable command as pending after saving the retry response', async () => {
    const calls: string[] = [];

    await applySyncResult(
      { ...appliedResult, outcome: 'RETRYABLE', server_status: null, recorded_at: null },
      {
        saveOutcome: async () => {
          calls.push('outcome-persisted');
        },
        setCommandState: async (_commandId, state) => {
          calls.push(`state:${state}`);
        },
      },
    );

    expect(calls).toEqual(['outcome-persisted']);
  });
});
