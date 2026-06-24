import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, type PsdStartOptions } from '../../api';
import { openPsdStream } from '../../ws';
import type { PsdMeasurement, PsdStreamMessage } from '../../types';

const MAX_CURVES = 200;

// Distinct, reasonably colour-blind-friendly palette; assigned stably per uuid
// in arrival order (mirrors the upstream RandomColorChoser intent, but
// deterministic so re-renders never re-colour a curve).
const PALETTE = [
  '#1f77b4',
  '#ff7f0e',
  '#2ca02c',
  '#d62728',
  '#9467bd',
  '#8c564b',
  '#e377c2',
  '#17becf',
  '#bcbd22',
  '#393b79',
  '#637939',
  '#8c6d31',
  '#843c39',
  '#7b4173',
];

export type PsdCurveEntry = PsdMeasurement & {
  color: string;
  visible: boolean;
};

const isMeasurement = (entry: unknown): entry is PsdMeasurement => {
  if (!entry || typeof entry !== 'object') return false;
  const m = entry as Partial<PsdMeasurement>;
  return typeof m.uuid === 'string' && Array.isArray(m.curve);
};

export const usePsdController = () => {
  const [measurements, setMeasurements] = useState<PsdMeasurement[]>([]);
  const [visibility, setVisibility] = useState<Record<string, boolean>>({});
  const [colors, setColors] = useState<Record<string, string>>({});
  const [wsConnected, setWsConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // Once a uuid has a complete measurement, a late partial (which can race in
  // after completion) must not clobber it. Ports the upstream complete-uids
  // guard in psd_window.py.
  const completeUidsRef = useRef<Set<string>>(new Set());

  const handleEntry = useCallback((entry: PsdMeasurement) => {
    const uuid = entry.uuid;
    if (!uuid) return;
    if (!entry.complete && completeUidsRef.current.has(uuid)) return;
    if (entry.complete) completeUidsRef.current.add(uuid);

    setMeasurements((prev) => {
      const idx = prev.findIndex((m) => m.uuid === uuid);
      if (idx >= 0) {
        const next = prev.slice();
        next[idx] = entry;
        return next;
      }
      const next = [...prev, entry];
      if (next.length > MAX_CURVES) next.splice(0, next.length - MAX_CURVES);
      return next;
    });
    setVisibility((prev) => (uuid in prev ? prev : { ...prev, [uuid]: true }));
    setColors((prev) => {
      if (uuid in prev) return prev;
      const color = PALETTE[Object.keys(prev).length % PALETTE.length];
      return { ...prev, [uuid]: color };
    });
  }, []);

  const loadTail = useCallback(async () => {
    try {
      const payload = await api.getPsdTail(MAX_CURVES);
      for (const entry of payload.entries) {
        if (isMeasurement(entry)) handleEntry(entry);
      }
    } catch {
      // Best-effort seed; the live stream will fill in subsequent measurements.
    }
  }, [handleEntry]);

  // Stable ref so the WS effect runs once (mount) rather than reopening the
  // socket whenever handleEntry's identity changes.
  const handleEntryRef = useRef(handleEntry);
  useEffect(() => {
    handleEntryRef.current = handleEntry;
  }, [handleEntry]);

  useEffect(() => {
    loadTail().catch(() => null);
  }, [loadTail]);

  useEffect(() => {
    const INITIAL_RECONNECT_DELAY_MS = 1000;
    const MAX_RECONNECT_DELAY_MS = 5000;
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let reconnectDelay = INITIAL_RECONNECT_DELAY_MS;

    const connect = () => {
      if (disposed) return;
      socket = openPsdStream((message: PsdStreamMessage) => {
        if (!message || message.type !== 'psd' || !message.entry) return;
        if (isMeasurement(message.entry)) handleEntryRef.current(message.entry);
      });
      const current = socket;
      current.onopen = () => {
        if (disposed || current !== socket) return;
        reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
        setWsConnected(true);
      };
      const handleDown = () => {
        if (current !== socket) return;
        setWsConnected(false);
        if (disposed || reconnectTimer !== null) return;
        const delay = reconnectDelay;
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null;
          connect();
        }, delay);
      };
      current.onclose = handleDown;
      current.onerror = handleDown;
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (socket) {
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
        socket = null;
      }
    };
    // Mount-once: live state is read through refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startPsd = useCallback(
    async (deviceKeys: string[], opts?: PsdStartOptions) => {
      if (deviceKeys.length === 0) return;
      setBusy(true);
      setNotice(null);
      try {
        const result = await api.startPsdAcquisitionMany(deviceKeys, opts);
        const skipped = Object.entries(result.skipped ?? {});
        if (skipped.length > 0) {
          setNotice(
            `Skipped ${skipped.length} device(s): ` +
              skipped.map(([key, reason]) => `${key} (${reason})`).join(', ')
          );
        }
      } catch (err) {
        setNotice(err instanceof Error ? err.message : 'Failed to start PSD.');
      } finally {
        setBusy(false);
      }
    },
    []
  );

  const stopPsd = useCallback(async (deviceKeys: string[]) => {
    if (deviceKeys.length === 0) return;
    setBusy(true);
    try {
      await api.stopPsdAcquisitionMany(deviceKeys);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Failed to stop PSD.');
    } finally {
      setBusy(false);
    }
  }, []);

  const toggleVisible = useCallback((uuid: string) => {
    setVisibility((prev) => ({ ...prev, [uuid]: !(prev[uuid] ?? true) }));
  }, []);

  const deleteCurve = useCallback((uuid: string) => {
    completeUidsRef.current.delete(uuid);
    setMeasurements((prev) => prev.filter((m) => m.uuid !== uuid));
    setVisibility((prev) => {
      const next = { ...prev };
      delete next[uuid];
      return next;
    });
    setColors((prev) => {
      const next = { ...prev };
      delete next[uuid];
      return next;
    });
  }, []);

  const clearAll = useCallback(async () => {
    completeUidsRef.current = new Set();
    setMeasurements([]);
    setVisibility({});
    setColors({});
    try {
      await api.clearPsd();
    } catch {
      // Local clear already applied; server history clear is best-effort.
    }
  }, []);

  const curves = useMemo<PsdCurveEntry[]>(
    () =>
      measurements.map((m) => ({
        ...m,
        color: colors[m.uuid] ?? PALETTE[0],
        visible: visibility[m.uuid] ?? true,
      })),
    [measurements, colors, visibility]
  );

  return {
    curves,
    wsConnected,
    busy,
    notice,
    setNotice,
    startPsd,
    stopPsd,
    toggleVisible,
    deleteCurve,
    clearAll,
  };
};
