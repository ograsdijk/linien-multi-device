import { useCallback, useEffect, useRef, useState } from 'react';
import type { PlotFrame, StreamMessage } from '../types';

type UsePlotFrameBufferOptions = {
  deviceKey: string;
  initialFrame?: PlotFrame | null;
  onSummaryUpdate?: (message: StreamMessage) => void;
  summaryIntervalMs?: number;
};

type UsePlotFrameBufferResult = {
  plotFrame: PlotFrame | null;
  handlePlotFrameMessage: (msg: StreamMessage) => boolean;
};

export function usePlotFrameBuffer({
  deviceKey,
  initialFrame = null,
  onSummaryUpdate,
  summaryIntervalMs = 1000,
}: UsePlotFrameBufferOptions): UsePlotFrameBufferResult {
  const [localPlotFrame, setLocalPlotFrame] = useState<PlotFrame | null>(initialFrame);
  const pendingPlotFrameRef = useRef<PlotFrame | null>(null);
  const plotRafRef = useRef<number | null>(null);
  const lastSummaryUpdateRef = useRef(0);
  const onSummaryUpdateRef = useRef(onSummaryUpdate);

  useEffect(() => {
    onSummaryUpdateRef.current = onSummaryUpdate;
  }, [onSummaryUpdate]);

  useEffect(() => {
    setLocalPlotFrame(initialFrame ?? null);
    pendingPlotFrameRef.current = null;
    lastSummaryUpdateRef.current = 0;
    if (plotRafRef.current !== null) {
      window.cancelAnimationFrame(plotRafRef.current);
      plotRafRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceKey]);

  useEffect(() => {
    return () => {
      if (plotRafRef.current !== null) {
        window.cancelAnimationFrame(plotRafRef.current);
        plotRafRef.current = null;
      }
    };
  }, []);

  const handlePlotFrameMessage = useCallback(
    (msg: StreamMessage): boolean => {
      if (msg.type !== 'plot_frame') {
        return false;
      }
      pendingPlotFrameRef.current = msg;
      if (plotRafRef.current === null) {
        plotRafRef.current = window.requestAnimationFrame(() => {
          plotRafRef.current = null;
          const nextFrame = pendingPlotFrameRef.current;
          pendingPlotFrameRef.current = null;
          if (nextFrame) {
            setLocalPlotFrame(nextFrame);
          }
        });
      }
      const now = performance.now();
      if (now - lastSummaryUpdateRef.current >= summaryIntervalMs) {
        lastSummaryUpdateRef.current = now;
        onSummaryUpdateRef.current?.(msg);
      }
      return true;
    },
    [summaryIntervalMs]
  );

  return {
    plotFrame: localPlotFrame ?? initialFrame ?? null,
    handlePlotFrameMessage,
  };
}
