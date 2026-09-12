import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { DiagnosticsBundle } from '../../types';
import { useDiagnosticsController } from './useDiagnosticsController';

const bundle = (overrides: Partial<DiagnosticsBundle> = {}): DiagnosticsBundle => ({
  ok: true,
  error: null,
  collected_at: 1_700_000_000,
  sections: [
    { name: 'kernel', title: 'Kernel', command: 'dmesg', output: 'boot', error: null },
  ],
  persistent_journal: true,
  ...overrides,
});

const setup = (deviceKey: string | null = 'dev-1') => {
  const appendUiErrorLog = vi.fn();
  const hook = renderHook(() =>
    useDiagnosticsController({ deviceKey, appendUiErrorLog })
  );
  return { hook, appendUiErrorLog };
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getBoardEvents').mockResolvedValue({ events: [] });
});

it('loads the timeline as soon as the modal opens', async () => {
  vi.spyOn(api, 'getBoardEvents').mockResolvedValue({
    events: [
      {
        ts: 1,
        device_key: 'dev-1',
        kind: 'reboot_detected',
        detail: 'The board restarted.',
      },
    ],
  });
  const { hook } = setup();

  await waitFor(() => expect(hook.result.current.events).toHaveLength(1));
  expect(api.getBoardEvents).toHaveBeenCalledWith('dev-1');
});

it('does not touch the gateway while the modal is closed', async () => {
  setup(null);

  expect(api.getBoardEvents).not.toHaveBeenCalled();
});

it('collects the bundle only when asked', async () => {
  const collect = vi.spyOn(api, 'collectDiagnostics').mockResolvedValue(bundle());
  const { hook } = setup();
  await waitFor(() => expect(api.getBoardEvents).toHaveBeenCalled());

  expect(collect).not.toHaveBeenCalled();

  await act(async () => {
    await hook.result.current.collect();
  });

  expect(collect).toHaveBeenCalledWith('dev-1');
  expect(hook.result.current.bundle?.sections[0].output).toBe('boot');
});

it('keeps a partial bundle from a board that stopped answering', async () => {
  // The sections collected before the board went away are the whole point.
  vi.spyOn(api, 'collectDiagnostics').mockResolvedValue(
    bundle({ ok: false, error: 'no route to host', persistent_journal: null })
  );
  const { hook } = setup();

  await act(async () => {
    await hook.result.current.collect();
  });

  expect(hook.result.current.bundle?.sections).toHaveLength(1);
  expect(hook.result.current.error).toBe('no route to host');
});

it('reports a failed collect as an error and a log entry', async () => {
  vi.spyOn(api, 'collectDiagnostics').mockRejectedValue(new Error('gateway down'));
  const { hook, appendUiErrorLog } = setup();

  await act(async () => {
    await hook.result.current.collect();
  });

  expect(hook.result.current.error).toBe('gateway down');
  expect(appendUiErrorLog).toHaveBeenCalledWith(
    'board_diagnostics',
    'diagnostics_collect_failed',
    'gateway down',
    'dev-1'
  );
});

it('re-collects after enabling persistent logs', async () => {
  // Otherwise the panel keeps offering an action that has already been taken.
  vi.spyOn(api, 'enablePersistentLog').mockResolvedValue({
    ok: true,
    persistent_journal: true,
  });
  const collect = vi.spyOn(api, 'collectDiagnostics').mockResolvedValue(bundle());
  const { hook } = setup();

  await act(async () => {
    await hook.result.current.enablePersistentLog();
  });

  expect(collect).toHaveBeenCalledWith('dev-1');
  expect(hook.result.current.bundle?.persistent_journal).toBe(true);
});

it('reports a failure to enable persistent logs', async () => {
  vi.spyOn(api, 'enablePersistentLog').mockRejectedValue(
    new Error('journald would not restart')
  );
  const { hook, appendUiErrorLog } = setup();

  await act(async () => {
    await hook.result.current.enablePersistentLog();
  });

  expect(hook.result.current.error).toBe('journald would not restart');
  expect(appendUiErrorLog).toHaveBeenCalledWith(
    'board_diagnostics',
    'persistent_log_failed',
    'journald would not restart',
    'dev-1'
  );
});

it('clears a previous board’s bundle when reopened for another', async () => {
  vi.spyOn(api, 'collectDiagnostics').mockResolvedValue(bundle());
  const appendUiErrorLog = vi.fn();
  const hook = renderHook(
    ({ deviceKey }) => useDiagnosticsController({ deviceKey, appendUiErrorLog }),
    { initialProps: { deviceKey: 'dev-1' as string | null } }
  );

  await act(async () => {
    await hook.result.current.collect();
  });
  expect(hook.result.current.bundle).not.toBeNull();

  act(() => {
    hook.rerender({ deviceKey: 'dev-2' });
  });

  expect(hook.result.current.bundle).toBeNull();
});
