import { useCallback, useRef, useState, type MutableRefObject, type RefObject } from 'react';
import type { LockIndicatorSnapshot, PlotFrame, StreamMessage } from '../../types';
import { useDeviceStream } from '../../hooks/useDeviceStream';
import type { OverviewPlotPanelHandle } from '../../components/OverviewPlotPanel';
import { markStreamFrame } from './streamFreshness';

// Compare two lock-indicator snapshots by the fields the UI actually reads.
// Avoids forcing a re-render when the backend sends a fresh indicator object
// every frame whose content is identical.
const indicatorEqual = (
  a: LockIndicatorSnapshot | null,
  b: LockIndicatorSnapshot | null,
): boolean => {
  if (a === b) return true;
  if (!a || !b) return false;
  if (a.state !== b.state) return false;
  if (a.reasons === b.reasons) return true;
  if (!a.reasons || !b.reasons) return false;
  if (a.reasons.length !== b.reasons.length) return false;
  for (let i = 0; i < a.reasons.length; i++) {
    if (a.reasons[i] !== b.reasons[i]) return false;
  }
  return true;
};

type PlotStreamOptions = {
  deviceKey: string;
  enabled: boolean;
  maxFps?: number;
  onStateUpdate?: (deviceKey: string, message: StreamMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

type PlotStream = {
  /** Attach to an OverviewPlotPanel; frames are pushed to uPlot through it. */
  panelRef: RefObject<OverviewPlotPanelHandle>;
  /** Latest frame, for ThrottledStatusRow to pull at its own cadence. */
  latestFrameRef: MutableRefObject<PlotFrame | null>;
  lockIndicator: LockIndicatorSnapshot | null;
};

/**
 * Wires a device's websocket stream to an OverviewPlotPanel without
 * re-rendering the caller per frame: plot frames go straight to uPlot through
 * an imperative handle, the newest one is stashed in a ref, and only genuine
 * lock-indicator transitions reach React state.
 */
export function usePlotStream({
  deviceKey,
  enabled,
  maxFps,
  onStateUpdate,
  onOpen,
  onClose,
}: PlotStreamOptions): PlotStream {
  const panelRef = useRef<OverviewPlotPanelHandle>(null);
  const latestFrameRef = useRef<PlotFrame | null>(null);
  const [lockIndicator, setLockIndicator] = useState<LockIndicatorSnapshot | null>(null);

  // Per-consumer frame throttle by wall clock. Kept in a ref so a changing
  // maxFps does not recreate the onMessage closure (which would tear down the
  // WS subscription).
  const lastPlotRef = useRef(0);
  const plotThrottleMsRef = useRef(maxFps && maxFps > 0 ? 1000 / maxFps : 0);
  plotThrottleMsRef.current = maxFps && maxFps > 0 ? 1000 / maxFps : 0;

  const onMessage = useCallback(
    (msg: StreamMessage) => {
      if (msg.type === 'plot_frame') {
        // Proof the stream is alive; the status backstop poll consults this.
        markStreamFrame(deviceKey);
        const throttleMs = plotThrottleMsRef.current;
        if (throttleMs > 0) {
          const now = performance.now();
          if (now - lastPlotRef.current < throttleMs) return;
          lastPlotRef.current = now;
        }
        panelRef.current?.applyFrame(msg);
        latestFrameRef.current = msg;
        const nextIndicator = msg.lock_indicator ?? null;
        setLockIndicator((prev) =>
          indicatorEqual(prev, nextIndicator) ? prev : nextIndicator,
        );
        onStateUpdate?.(deviceKey, msg);
        return;
      }
      onStateUpdate?.(deviceKey, msg);
    },
    [deviceKey, onStateUpdate],
  );

  useDeviceStream(deviceKey, enabled, onMessage, {
    maxFps,
    detail: 'summary',
    onOpen,
    onClose,
  });

  return { panelRef, latestFrameRef, lockIndicator };
}
