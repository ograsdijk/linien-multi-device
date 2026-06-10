import { memo, useEffect, useState, type MutableRefObject } from 'react';
import type { LockIndicatorSnapshot, PlotFrame } from '../types';
import { StatusRow } from './StatusRow';

type ThrottledStatusRowProps = {
  // Ref the parent updates on every WS plot_frame. We pull from it
  // at our own cadence; the parent never re-renders us when frames
  // arrive.
  frameRef: MutableRefObject<PlotFrame | null>;
  intervalMs: number;
  lockIndicator?: LockIndicatorSnapshot | null;
  connected?: boolean;
  lockEnabled?: boolean;
};

// Pulls the latest plot frame from the parent's ref at a fixed cadence
// (default 2 Hz). StatusRow displays numeric stats (signal_power,
// error_std, control_std) that change every frame at the backend rate
// but humans can't read faster than ~2 Hz anyway -- streaming them at
// 10 Hz would force a card re-render at the streaming rate, defeating
// the imperative-panel optimization.
//
// One setInterval per card. At 12 cards x 2 Hz = 24 callbacks/sec
// across the overview. Each callback does a single setState that only
// re-renders this small subtree, not the card or its sibling panels.
export const ThrottledStatusRow = memo(function ThrottledStatusRow({
  frameRef,
  intervalMs,
  lockIndicator,
  connected,
  lockEnabled,
}: ThrottledStatusRowProps) {
  const [frame, setFrame] = useState<PlotFrame | null>(null);

  useEffect(() => {
    let lastRef: PlotFrame | null = null;
    const tick = () => {
      const next = frameRef.current;
      if (next === lastRef) return; // skip the setState when no new frame arrived
      lastRef = next;
      setFrame(next);
    };
    // Initial pull so the first paint isn't blank.
    tick();
    const id = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(id);
  }, [frameRef, intervalMs]);

  return (
    <StatusRow
      plotFrame={frame}
      lockIndicator={lockIndicator}
      connected={connected}
      lockEnabled={lockEnabled}
    />
  );
});
