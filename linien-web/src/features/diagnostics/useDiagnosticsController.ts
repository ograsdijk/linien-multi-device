import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api';
import type { BoardEvent, DiagnosticsBundle } from '../../types';

type UseDiagnosticsControllerArgs = {
  /** The device the modal is open for, or null when it is closed. */
  deviceKey: string | null;
  appendUiErrorLog: (
    source: string,
    code: string,
    message: string,
    deviceKey?: string
  ) => void;
};

const toErrorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message ? error.message : fallback;

/**
 * State for the per-device diagnostics modal.
 *
 * The timeline loads as soon as the modal opens -- it is a cache read on the
 * gateway, so it costs nothing and is usually the first thing worth looking at.
 * Collecting the bundle is a dozen SSH commands and stays explicit.
 */
export const useDiagnosticsController = ({
  deviceKey,
  appendUiErrorLog,
}: UseDiagnosticsControllerArgs) => {
  const [events, setEvents] = useState<BoardEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [bundle, setBundle] = useState<DiagnosticsBundle | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadEvents = useCallback(async () => {
    if (!deviceKey) return;
    setEventsLoading(true);
    try {
      const result = await api.getBoardEvents(deviceKey);
      setEvents(result.events ?? []);
    } catch (err) {
      setError(toErrorMessage(err, 'Could not load the board timeline.'));
    } finally {
      setEventsLoading(false);
    }
  }, [deviceKey]);

  // Reset on every open, so a modal opened for a second board never shows the
  // first board's bundle while its own is still being collected.
  useEffect(() => {
    setBundle(null);
    setError(null);
    setEvents([]);
    if (deviceKey) void loadEvents();
  }, [deviceKey, loadEvents]);

  const collect = useCallback(async () => {
    if (!deviceKey) return;
    setCollecting(true);
    setError(null);
    try {
      const result = await api.collectDiagnostics(deviceKey);
      setBundle(result);
      // A bundle that came back `ok: false` still carries whatever was read
      // before the board stopped answering, so it is shown rather than
      // discarded -- with the reason alongside it.
      if (!result.ok && result.error) setError(result.error);
    } catch (err) {
      const message = toErrorMessage(err, 'Could not collect diagnostics.');
      setError(message);
      appendUiErrorLog('board_diagnostics', 'diagnostics_collect_failed', message, deviceKey);
    } finally {
      setCollecting(false);
    }
  }, [appendUiErrorLog, deviceKey]);

  const enablePersistentLog = useCallback(async () => {
    if (!deviceKey) return;
    setEnabling(true);
    setError(null);
    try {
      await api.enablePersistentLog(deviceKey);
      // Re-collect so the panel stops offering an action that has been taken,
      // and reload the timeline, which now has one more entry.
      await Promise.all([collect(), loadEvents()]);
    } catch (err) {
      const message = toErrorMessage(err, 'Could not enable persistent logging.');
      setError(message);
      appendUiErrorLog('board_diagnostics', 'persistent_log_failed', message, deviceKey);
    } finally {
      setEnabling(false);
    }
  }, [appendUiErrorLog, collect, deviceKey, loadEvents]);

  return {
    events,
    eventsLoading,
    bundle,
    collecting,
    enabling,
    error,
    collect,
    enablePersistentLog,
    reloadEvents: loadEvents,
  };
};
