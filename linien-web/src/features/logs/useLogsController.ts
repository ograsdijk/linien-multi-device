import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import { openLogsStream } from '../../ws';
import type { Device, LogsStreamMessage, UiLogEntry } from '../../types';
import type { UiToast } from '../../components/ToastStack';
import { sanitizeUiLogEntries } from '../runtime/messageGuards';

const MAX_LOG_ROWS = 5000;
const TOAST_COOLDOWN_MS = 20_000;
const TOAST_DURATION_MS = 6000;

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
  const [logRows, setLogRows] = useState<UiLogEntry[]>([]);
  const [logLevelFilter, setLogLevelFilter] = useState('all');
  const [logSourceFilter, setLogSourceFilter] = useState('all');
  const [logDeviceFilter, setLogDeviceFilter] = useState('all');
  const [logSearchText, setLogSearchText] = useState('');
  const [logAutoScroll, setLogAutoScroll] = useState(true);
  const [toasts, setToasts] = useState<UiToast[]>([]);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const logSeenInModalRef = useRef(false);
  const toastCooldownRef = useRef<Record<string, number>>({});

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
      setLogRows((prev) => {
        const seen = new Set(prev.map((entry) => entry.id));
        const next = [...prev];
        for (const entry of entries) {
          if (!entry || typeof entry.id !== 'string' || seen.has(entry.id)) {
            continue;
          }
          seen.add(entry.id);
          next.push(entry);
          if (levelBucket(entry) === 'error') {
            setLogsErrorLatched(true);
          }
          const toast = toastFromLogEntry(
            entry,
            resolveDeviceLabel(entry.device_key ?? undefined)
          );
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
        if (next.length > MAX_LOG_ROWS) {
          return next.slice(next.length - MAX_LOG_ROWS);
        }
        return next;
      });
    },
    [pushToast, resolveDeviceLabel]
  );

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
      setLogRows(entries.slice(Math.max(0, entries.length - MAX_LOG_ROWS)));
    } catch {
      // Best effort refresh.
    } finally {
      setLogsLoading(false);
    }
  }, []);

  const clearLogs = useCallback(async () => {
    try {
      await api.clearLogs();
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
      const deviceName = entry.device_key
        ? (deviceNameByKey.get(entry.device_key) ?? '')
        : '';
      const haystack = `${entry.message || ''} ${entry.code || ''} ${JSON.stringify(
        entry.details || {}
      )} ${deviceName}`.toLowerCase();
      return haystack.includes(needle);
    });
  }, [
    logRows,
    logLevelFilter,
    logSourceFilter,
    logDeviceFilter,
    logSearchText,
    deviceNameByKey,
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
    loadLogsTail,
    clearLogs,
    copyLogMessage,
    copyLogJson,
    logScrollRef,
    appendUiErrorLog,
  };
};
