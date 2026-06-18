import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import { openLogsStream } from '../../ws';
import type { Device, LogsStreamMessage, UiLogEntry } from '../../types';
import type { UiToast } from '../../components/ToastStack';
import { sanitizeUiLogEntries } from '../runtime/messageGuards';

const MAX_LOG_ROWS = 5000;
const TOAST_COOLDOWN_MS = 20_000;
const TOAST_DURATION_MS = 6000;

// Stored alongside the user-visible fields on each in-memory log row so we
// can do free-text filtering with a single substring check instead of
// rebuilding a `JSON.stringify(...)`-laden haystack per row per keystroke.
type LogRow = UiLogEntry & { _haystack: string };

const buildHaystack = (entry: UiLogEntry, deviceLabel: string): string => {
  let details = '';
  if (entry.details) {
    try {
      details = JSON.stringify(entry.details);
    } catch {
      details = '';
    }
  }
  return `${entry.message || ''} ${entry.code || ''} ${details} ${deviceLabel}`.toLowerCase();
};

const attachHaystack = (entry: UiLogEntry, deviceLabel: string): LogRow => ({
  ...entry,
  _haystack: buildHaystack(entry, deviceLabel),
});

const levelBucket = (entry: UiLogEntry): 'info' | 'warning' | 'error' => {
  const levelName = String(entry.level_name || '').toLowerCase();
  if (levelName === 'critical' || levelName === 'error') return 'error';
  if (levelName === 'warning') return 'warning';
  if (typeof entry.level === 'number') {
    if (entry.level >= 40) return 'error';
    if (entry.level >= 30) return 'warning';
  }
  return 'info';
};

const toastFromLogEntry = (
  entry: UiLogEntry,
  deviceLabel?: string | null
): Omit<UiToast, 'id'> | null => {
  const code = String(entry.code || '').trim().toLowerCase();
  const resolvedLabel = (deviceLabel || '').trim() || String(entry.device_key || '').trim();
  const deviceSuffix = resolvedLabel ? ` (${resolvedLabel})` : '';
  const message = entry.message || 'Operation updated.';
  if (code === 'lock_lost') {
    return { level: 'error', title: `Lock Lost${deviceSuffix}`, message };
  }
  if (code === 'lock_acquired') {
    return { level: 'info', title: `Lock Acquired${deviceSuffix}`, message };
  }
  if (code === 'auto_relock_action_failed') {
    return { level: 'error', title: `Auto-Relock Failed${deviceSuffix}`, message };
  }
  if (code === 'auto_relock_action_success') {
    return { level: 'info', title: `Auto-Relock Complete${deviceSuffix}`, message };
  }
  if (
    code === 'auto_relock_toggle_failed' ||
    code === 'disable_lock_failed' ||
    code === 'auto_lock_scan_failed'
  ) {
    return { level: 'error', title: `Error${deviceSuffix}`, message };
  }
  if (code === 'connection_diagnosis') {
    const category = String(entry.details?.category || '').trim();
    // A crash with the lock still held is reassuring-but-actionable (warning);
    // a reboot / unreachable board means the lock is gone (error).
    const level: UiToast['level'] =
      category === 'server_crashed' || category === 'recovering' ? 'warning' : 'error';
    return { level, title: `Connection${deviceSuffix}`, message };
  }
  const details = entry.details;
  if (
    levelBucket(entry) === 'error' &&
    details &&
    typeof details === 'object' &&
    details.origin === 'ui'
  ) {
    return { level: 'error', title: `Error${deviceSuffix}`, message };
  }
  return null;
};

export const useLogsController = (devices: Device[]) => {
  const [logsOpen, setLogsOpen] = useState(false);
  const [logsWsConnected, setLogsWsConnected] = useState(false);
  const [logsLoading, setLogsLoading] = useState(false);
  const [logsErrorLatched, setLogsErrorLatched] = useState(false);
  const [logRows, setLogRows] = useState<LogRow[]>([]);
  const [logLevelFilter, setLogLevelFilter] = useState('all');
  const [logSourceFilter, setLogSourceFilter] = useState('all');
  const [logDeviceFilter, setLogDeviceFilter] = useState('all');
  const [logSearchText, setLogSearchText] = useState('');
  const [logAutoScroll, setLogAutoScroll] = useState(true);
  const [toasts, setToasts] = useState<UiToast[]>([]);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const logSeenInModalRef = useRef(false);
  const toastCooldownRef = useRef<Record<string, number>>({});
  // O(1) dedup ref aligned with logRows. Membership is checked here rather
  // than rebuilding a Set from `prev.map(...)` on every append (which is
  // O(n) per entry and dominates with a large log buffer under bursty
  // logging).
  const seenLogIdsRef = useRef<Set<string>>(new Set());
  // Latch ref so a log burst that includes a new error can flip the
  // `logsErrorLatched` state once via an effect, instead of doing
  // `setLogsErrorLatched` from inside `setLogRows`'s updater (an
  // anti-pattern that breaks under StrictMode/concurrent rendering).
  const pendingErrorLatchRef = useRef(false);
  const [errorLatchTick, setErrorLatchTick] = useState(0);

  const deviceNameByKey = useMemo(
    () =>
      new Map(
        devices.map((device) => [device.key, (device.name || '').trim() || device.key])
      ),
    [devices]
  );

  const resolveDeviceLabel = useCallback(
    (deviceKey?: string | null) => {
      if (!deviceKey) return '';
      return deviceNameByKey.get(deviceKey) || deviceKey;
    },
    [deviceNameByKey]
  );

  const dismissToast = useCallback((toastId: string) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== toastId));
  }, []);

  const pushToast = useCallback(
    (toast: Omit<UiToast, 'id'>) => {
      const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      setToasts((prev) => [...prev, { ...toast, id }].slice(-5));
      window.setTimeout(() => {
        dismissToast(id);
      }, TOAST_DURATION_MS);
    },
    [dismissToast]
  );

  const appendLogEntries = useCallback(
    (entries: UiLogEntry[]) => {
      if (entries.length === 0) return;
      const nowMs = Date.now();
      const seenIds = seenLogIdsRef.current;
      const accepted: LogRow[] = [];
      let sawError = false;
      for (const entry of entries) {
        if (!entry || typeof entry.id !== 'string' || seenIds.has(entry.id)) {
          continue;
        }
        seenIds.add(entry.id);
        const deviceLabel = resolveDeviceLabel(entry.device_key ?? undefined);
        accepted.push(attachHaystack(entry, deviceLabel));
        if (levelBucket(entry) === 'error') {
          sawError = true;
        }
        const toast = toastFromLogEntry(entry, deviceLabel);
        if (!toast) {
          continue;
        }
        const fingerprint = `${entry.code || ''}|${entry.device_key || ''}|${toast.level}|${entry.message || ''}`;
        const lastShown = toastCooldownRef.current[fingerprint] ?? 0;
        if (nowMs - lastShown > TOAST_COOLDOWN_MS) {
          toastCooldownRef.current[fingerprint] = nowMs;
          pushToast(toast);
        }
      }
      if (accepted.length === 0) {
        return;
      }
      if (sawError) {
        pendingErrorLatchRef.current = true;
        setErrorLatchTick((tick) => tick + 1);
      }
      setLogRows((prev) => {
        let next: LogRow[];
        if (prev.length + accepted.length <= MAX_LOG_ROWS) {
          next = prev.concat(accepted);
        } else {
          // Need to evict; also clean the dedup set so it doesn't grow
          // without bound across long sessions.
          const combined = prev.concat(accepted);
          const startIdx = combined.length - MAX_LOG_ROWS;
          for (let i = 0; i < startIdx; i++) {
            const dropped = combined[i];
            if (dropped) seenIds.delete(dropped.id);
          }
          next = combined.slice(startIdx);
        }
        return next;
      });
    },
    [pushToast, resolveDeviceLabel]
  );

  // Flip the error-latch flag once per burst, after `setLogRows` has been
  // queued. Reading `pendingErrorLatchRef` outside any state updater keeps
  // the side effect out of the reducer and avoids StrictMode double-invoke
  // warnings.
  useEffect(() => {
    if (!pendingErrorLatchRef.current) return;
    pendingErrorLatchRef.current = false;
    setLogsErrorLatched(true);
  }, [errorLatchTick]);

  // Rebuild haystacks if the device label map shifts (rename or new
  // device). Without this, free-text search could miss the new label on
  // pre-existing rows.
  useEffect(() => {
    setLogRows((prev) => {
      if (prev.length === 0) return prev;
      let mutated = false;
      const next: LogRow[] = new Array(prev.length);
      for (let i = 0; i < prev.length; i++) {
        const row = prev[i];
        const label = resolveDeviceLabel(row.device_key ?? undefined);
        const fresh = buildHaystack(row, label);
        if (fresh === row._haystack) {
          next[i] = row;
        } else {
          next[i] = { ...row, _haystack: fresh };
          mutated = true;
        }
      }
      return mutated ? next : prev;
    });
  }, [resolveDeviceLabel]);

  const appendUiErrorLog = useCallback(
    (source: string, code: string, message: string, deviceKey?: string) => {
      appendLogEntries([
        {
          id: `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          ts: Date.now() / 1000,
          level: 40,
          level_name: 'error',
          device_key: deviceKey ?? null,
          source,
          code,
          message,
          details: { origin: 'ui' },
        },
      ]);
    },
    [appendLogEntries]
  );

  const loadLogsTail = useCallback(async () => {
    setLogsLoading(true);
    try {
      const payload = await api.getLogsTail(1000);
      const entries = sanitizeUiLogEntries(payload.entries);
      const trimmed = entries.slice(Math.max(0, entries.length - MAX_LOG_ROWS));
      // Rebuild the dedup set from scratch since we just replaced the
      // entire backing array.
      const nextSeen = new Set<string>();
      const rows: LogRow[] = [];
      for (const entry of trimmed) {
        if (!entry || typeof entry.id !== 'string' || nextSeen.has(entry.id)) {
          continue;
        }
        nextSeen.add(entry.id);
        const deviceLabel = resolveDeviceLabel(entry.device_key ?? undefined);
        rows.push(attachHaystack(entry, deviceLabel));
      }
      seenLogIdsRef.current = nextSeen;
      setLogRows(rows);
    } catch {
      // Best effort refresh.
    } finally {
      setLogsLoading(false);
    }
  }, [resolveDeviceLabel]);

  const clearLogs = useCallback(async () => {
    try {
      await api.clearLogs();
      seenLogIdsRef.current = new Set();
      setLogRows([]);
    } catch {
      // Best effort clear.
    }
  }, []);

  const copyLogMessage = useCallback((value: string) => {
    if (!value) return;
    navigator.clipboard.writeText(value).catch(() => null);
  }, []);

  const copyLogJson = useCallback((entry: UiLogEntry) => {
    navigator.clipboard.writeText(JSON.stringify(entry, null, 2)).catch(() => null);
  }, []);

  useEffect(() => {
    const socket = openLogsStream((message: LogsStreamMessage) => {
      if (!message || message.type !== 'log' || !message.entry) return;
      appendLogEntries([message.entry]);
    });
    socket.onopen = () => setLogsWsConnected(true);
    socket.onclose = () => setLogsWsConnected(false);
    socket.onerror = () => setLogsWsConnected(false);
    return () => {
      socket.close();
    };
  }, [appendLogEntries]);

  useEffect(() => {
    if (!logsOpen) return;
    logSeenInModalRef.current = true;
    loadLogsTail().catch(() => null);
  }, [logsOpen, loadLogsTail]);

  useEffect(() => {
    if (logsOpen) return;
    if (!logSeenInModalRef.current) return;
    setLogsErrorLatched(false);
    logSeenInModalRef.current = false;
  }, [logsOpen]);

  useEffect(() => {
    if (!logsOpen || !logAutoScroll) return;
    const host = logScrollRef.current;
    if (!host) return;
    host.scrollTop = host.scrollHeight;
  }, [logsOpen, logAutoScroll, logRows]);

  const filteredLogRows = useMemo(() => {
    const needle = logSearchText.trim().toLowerCase();
    return logRows.filter((entry) => {
      const bucket = levelBucket(entry);
      if (logLevelFilter !== 'all' && bucket !== logLevelFilter) return false;
      const sourceValue = String(entry.source || '');
      if (logSourceFilter !== 'all' && sourceValue !== logSourceFilter) return false;
      const deviceValue = String(entry.device_key || '');
      if (logDeviceFilter !== 'all' && deviceValue !== logDeviceFilter) return false;
      if (!needle) return true;
      return entry._haystack.includes(needle);
    });
  }, [
    logRows,
    logLevelFilter,
    logSourceFilter,
    logDeviceFilter,
    logSearchText,
  ]);

  return {
    logsOpen,
    setLogsOpen,
    logsWsConnected,
    logsLoading,
    logsErrorLatched,
    logRows,
    filteredLogRows,
    logLevelFilter,
    setLogLevelFilter,
    logSourceFilter,
    setLogSourceFilter,
    logDeviceFilter,
    setLogDeviceFilter,
    logSearchText,
    setLogSearchText,
    logAutoScroll,
    setLogAutoScroll,
    toasts,
    dismissToast,
    pushToast,
    loadLogsTail,
    clearLogs,
    copyLogMessage,
    copyLogJson,
    logScrollRef,
    appendUiErrorLog,
  };
};
