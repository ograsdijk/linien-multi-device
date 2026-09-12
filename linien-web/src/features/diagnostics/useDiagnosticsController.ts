import { useCallback, useEffect, useRef, useState } from 'react';
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
  // Which board the visible state belongs to. A collect is a dozen SSH
  // commands, so the operator can easily close the modal and open another
  // board before it resolves -- and rendering board A's dmesg and journal
  // under board B's title would be worse than showing nothing at all.
  // Clearing state on open is not enough: the in-flight promise still lands.
  const currentKeyRef = useRef<string | null>(deviceKey);
  currentKeyRef.current = deviceKey;

  const loadEvents = useCallback(async () => {
    if (!deviceKey) return;
    setEventsLoading(true);
    try {
      const result = await api.getBoardEvents(deviceKey);
      if (currentKeyRef.current !== deviceKey) return;
      setEvents(result.events ?? []);
    } catch (err) {
      if (currentKeyRef.current !== deviceKey) return;
      setError(toErrorMessage(err, 'Could not load the board timeline.'));
    } finally {
      if (currentKeyRef.current === deviceKey) setEventsLoading(false);
    }
  }, [deviceKey]);

  // Reset on every open. Combined with the guard above, a modal opened for a
  // second board never shows the first board's bundle.
  //
  // The busy flags are reset here too, not just in the abandoned request's
  // `finally`: that branch deliberately does nothing once the key has moved
  // on, so without this the new board's buttons would spin forever waiting on
  // a request that belongs to the previous one.
  useEffect(() => {
    setBundle(null);
    setError(null);
    setEvents([]);
    setCollecting(false);
    setEnabling(false);
    if (deviceKey) void loadEvents();
  }, [deviceKey, loadEvents]);

  const collect = useCallback(async () => {
    if (!deviceKey) return;
    setCollecting(true);
    setError(null);
    try {
      const result = await api.collectDiagnostics(deviceKey);
      if (currentKeyRef.current !== deviceKey) return;
      setBundle(result);
      // A bundle that came back `ok: false` still carries whatever was read
      // before the board stopped answering, so it is shown rather than
      // discarded -- with the reason alongside it.
      if (!result.ok && result.error) setError(result.error);
    } catch (err) {
      const message = toErrorMessage(err, 'Could not collect diagnostics.');
      // Logged whichever board is in front of the operator now: the failure
      // happened, and the log entry carries the key it happened to.
      appendUiErrorLog('board_diagnostics', 'diagnostics_collect_failed', message, deviceKey);
      if (currentKeyRef.current !== deviceKey) return;
      setError(message);
    } finally {
      if (currentKeyRef.current === deviceKey) setCollecting(false);
    }
  }, [appendUiErrorLog, deviceKey]);

  const enablePersistentLog = useCallback(async () => {
    if (!deviceKey) return;
    setEnabling(true);
    setError(null);
    try {
      await api.enablePersistentLog(deviceKey);
      if (currentKeyRef.current !== deviceKey) return;
      // Re-collect so the panel stops offering an action that has been taken,
      // and reload the timeline, which now has one more entry.
      await Promise.all([collect(), loadEvents()]);
    } catch (err) {
      const message = toErrorMessage(err, 'Could not enable persistent logging.');
      appendUiErrorLog('board_diagnostics', 'persistent_log_failed', message, deviceKey);
      if (currentKeyRef.current !== deviceKey) return;
      setError(message);
    } finally {
      if (currentKeyRef.current === deviceKey) setEnabling(false);
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
