import { useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { Button, Group, NumberInput, RangeSlider, Text } from '@mantine/core';
import { toClampedNumberOr } from '../utils/numberInput';

const SWEEP_MIN = -1;
const SWEEP_MAX = 1;
const SWEEP_STEP = 0.001;
const DRAG_UPDATE_INTERVAL_MS = 100;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const roundToStep = (value: number) =>
  Number((Math.round(value / SWEEP_STEP) * SWEEP_STEP).toFixed(6));
const rangesEqual = (a: [number, number], b: [number, number]) =>
  Math.abs(a[0] - b[0]) < SWEEP_STEP / 2 && Math.abs(a[1] - b[1]) < SWEEP_STEP / 2;

type SweepControlsProps = {
  params: Record<string, any>;
  onSetParam: (
    name: string,
    value: any,
    writeRegisters?: boolean,
    options?: { optimistic?: boolean }
  ) => void;
};

export function SweepControls({ params, onSetParam }: SweepControlsProps) {
  const centerRaw = params.sweep_center;
  const centerParsed = centerRaw == null ? NaN : Number(centerRaw);
  const center = Number.isFinite(centerParsed) ? centerParsed : 0;
  const amplitudeRaw = params.sweep_amplitude;
  const amplitudeParsed = amplitudeRaw == null ? NaN : Number(amplitudeRaw);
  const amplitude = Number.isFinite(amplitudeParsed) ? Math.max(0, amplitudeParsed) : 1;
  const paused = Boolean(params.sweep_pause);
  const min = Math.max(SWEEP_MIN, center - amplitude);
  const max = Math.min(SWEEP_MAX, center + amplitude);

  const [range, setRange] = useState<[number, number]>([min, max]);
  const [isDragging, setIsDragging] = useState(false);
  const [dragMode, setDragMode] = useState<'thumb' | 'bar' | null>(null);
  const rangeRef = useRef(range);
  const displayedRangeRef = useRef(range);
  const draggingRef = useRef(false);
  const dragModeRef = useRef<'thumb' | 'bar' | null>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);
  const dragVisualRafRef = useRef<number | null>(null);
  const pendingVisualRangeRef = useRef<[number, number] | null>(null);
  const dragPublishTimerRef = useRef<number | null>(null);
  const pendingDragRangeRef = useRef<[number, number] | null>(null);
  const lastSentCenterRef = useRef<number | null>(null);
  const lastSentAmplitudeRef = useRef<number | null>(null);

  useEffect(() => {
    rangeRef.current = range;
    displayedRangeRef.current = range;
  }, [range]);

  const clearDragVisualState = () => {
    if (dragVisualRafRef.current !== null) {
      window.cancelAnimationFrame(dragVisualRafRef.current);
      dragVisualRafRef.current = null;
    }
    pendingVisualRangeRef.current = null;
  };

  const queueVisualRange = (nextRange: [number, number]) => {
    rangeRef.current = nextRange;
    pendingVisualRangeRef.current = nextRange;
    if (dragVisualRafRef.current !== null) {
      return;
    }
    dragVisualRafRef.current = window.requestAnimationFrame(() => {
      dragVisualRafRef.current = null;
      const pending = pendingVisualRangeRef.current;
      pendingVisualRangeRef.current = null;
      if (!pending) {
        return;
      }
      if (rangesEqual(displayedRangeRef.current, pending)) {
        return;
      }
      setRange(pending);
    });
  };

  const clearDragPublishState = () => {
    if (dragPublishTimerRef.current !== null) {
      window.clearTimeout(dragPublishTimerRef.current);
      dragPublishTimerRef.current = null;
    }
    pendingDragRangeRef.current = null;
    lastSentCenterRef.current = null;
    lastSentAmplitudeRef.current = null;
  };

  const publishDragRange = (
    nextRange: [number, number],
    mode: 'thumb' | 'bar',
    force: boolean
  ) => {
    const [lo, hi] = nextRange;
    const newCenter = roundToStep((lo + hi) / 2);
    const newAmplitude = roundToStep(Math.abs(hi - lo) / 2);

    if (mode === 'bar') {
      if (
        !force &&
        lastSentCenterRef.current != null &&
        Math.abs(lastSentCenterRef.current - newCenter) < SWEEP_STEP / 2
      ) {
        return;
      }
      onSetParam('sweep_center', newCenter, true, force ? undefined : { optimistic: false });
      lastSentCenterRef.current = newCenter;
      return;
    }

    const centerChanged =
      lastSentCenterRef.current == null ||
      Math.abs(lastSentCenterRef.current - newCenter) >= SWEEP_STEP / 2;
    const ampChanged =
      lastSentAmplitudeRef.current == null ||
      Math.abs(lastSentAmplitudeRef.current - newAmplitude) >= SWEEP_STEP / 2;
    if (!force && !centerChanged && !ampChanged) {
      return;
    }
    onSetParam('sweep_center', newCenter, false, force ? undefined : { optimistic: false });
    onSetParam('sweep_amplitude', newAmplitude, true, force ? undefined : { optimistic: false });
    lastSentCenterRef.current = newCenter;
    lastSentAmplitudeRef.current = newAmplitude;
  };

  const scheduleDragPublish = (nextRange: [number, number]) => {
    pendingDragRangeRef.current = nextRange;
    if (dragPublishTimerRef.current !== null) {
      return;
    }
    dragPublishTimerRef.current = window.setTimeout(() => {
      dragPublishTimerRef.current = null;
      if (!draggingRef.current) {
        pendingDragRangeRef.current = null;
        return;
      }
      const pending = pendingDragRangeRef.current;
      pendingDragRangeRef.current = null;
      if (!pending) {
        return;
      }
      const mode = dragModeRef.current;
      if (mode == null) {
        return;
      }
      publishDragRange(pending, mode, false);
      if (pendingDragRangeRef.current != null) {
        scheduleDragPublish(pendingDragRangeRef.current);
      }
    }, DRAG_UPDATE_INTERVAL_MS);
  };

  useEffect(() => {
    if (!draggingRef.current) {
      setRange([min, max]);
    }
  }, [min, max]);

  const commitRange = (nextRange?: [number, number]) => {
    clearDragVisualState();
    clearDragPublishState();
    draggingRef.current = false;
    setIsDragging(false);
    setDragMode(null);
    dragModeRef.current = null;
    const resolved = nextRange ?? rangeRef.current;
    rangeRef.current = resolved;
    setRange(resolved);
    const [lo, hi] = resolved;
    publishDragRange([lo, hi], 'thumb', true);
  };

  const commitRangeShift = (nextRange?: [number, number]) => {
    clearDragVisualState();
    clearDragPublishState();
    draggingRef.current = false;
    setIsDragging(false);
    setDragMode(null);
    dragModeRef.current = null;
    const resolved = nextRange ?? rangeRef.current;
    rangeRef.current = resolved;
    setRange(resolved);
    publishDragRange(resolved, 'bar', true);
  };

  const handleRangeChange = (value: [number, number]) => {
    draggingRef.current = true;
    setIsDragging(true);
    if (dragModeRef.current == null) {
      setDragMode('thumb');
      dragModeRef.current = 'thumb';
    }
    const [prevLo, prevHi] = rangeRef.current;
    const [rawLo, rawHi] = value ?? [prevLo, prevHi];
    const nextLo = clamp(rawLo, SWEEP_MIN, rawHi - SWEEP_STEP);
    const nextHi = clamp(rawHi, nextLo + SWEEP_STEP, SWEEP_MAX);
    const nextRange: [number, number] = [nextLo, nextHi];
    queueVisualRange(nextRange);
    scheduleDragPublish(nextRange);
  };

  const handleRangeBarPointerDownCapture = (
    event: ReactPointerEvent<HTMLDivElement>
  ) => {
    if (event.pointerType === 'mouse' && event.button !== 0) {
      return;
    }
    const target = event.target as HTMLElement;
    if (target.closest('.sweep-range-thumb')) {
      setIsDragging(true);
      setDragMode('thumb');
      return;
    }
    if (!target.closest('.sweep-range-bar')) {
      return;
    }
    const track = event.currentTarget.querySelector(
      '.sweep-range-track-container'
    ) as HTMLElement | null;
    if (!track) {
      return;
    }
    const startRange = rangeRef.current;
    const width = startRange[1] - startRange[0];
    if (width <= SWEEP_STEP) {
      return;
    }
    const trackWidth = track.getBoundingClientRect().width;
    if (!Number.isFinite(trackWidth) || trackWidth <= 1) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    draggingRef.current = true;
    setIsDragging(true);
    setDragMode('bar');
    dragModeRef.current = 'bar';

    const startClientX = event.clientX;
    const pointerId = event.pointerId;

    const cleanup = () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('pointercancel', onPointerCancel);
      dragCleanupRef.current = null;
    };

    const updateFromClientX = (clientX: number) => {
      const deltaX = clientX - startClientX;
      const valuePerPx = (SWEEP_MAX - SWEEP_MIN) / trackWidth;
      const maxStart = SWEEP_MAX - width;
      const nextStart = clamp(startRange[0] + deltaX * valuePerPx, SWEEP_MIN, maxStart);
      const snappedStart = roundToStep(nextStart);
      const snappedEnd = roundToStep(snappedStart + width);
      const nextRange: [number, number] = [
        clamp(snappedStart, SWEEP_MIN, SWEEP_MAX - SWEEP_STEP),
        clamp(snappedEnd, SWEEP_MIN + SWEEP_STEP, SWEEP_MAX),
      ];
      queueVisualRange(nextRange);
      scheduleDragPublish(nextRange);
    };

    const onPointerMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) {
        return;
      }
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };

    const onPointerUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) {
        return;
      }
      upEvent.preventDefault();
      cleanup();
      commitRangeShift();
    };

    const onPointerCancel = (cancelEvent: PointerEvent) => {
      if (cancelEvent.pointerId !== pointerId) {
        return;
      }
      cancelEvent.preventDefault();
      cleanup();
      draggingRef.current = false;
      setIsDragging(false);
      setDragMode(null);
      dragModeRef.current = null;
      clearDragVisualState();
      clearDragPublishState();
      setRange([min, max]);
      rangeRef.current = [min, max];
    };

    dragCleanupRef.current = cleanup;
    window.addEventListener('pointermove', onPointerMove, { passive: false });
    window.addEventListener('pointerup', onPointerUp, { passive: false });
    window.addEventListener('pointercancel', onPointerCancel, { passive: false });
  };

  useEffect(() => {
    return () => {
      dragCleanupRef.current?.();
      clearDragVisualState();
      clearDragPublishState();
    };
  }, []);

  const sliderShellClassName = [
    'sweep-range-shell',
    isDragging ? 'sweep-range-shell--dragging' : '',
    dragMode === 'bar' ? 'sweep-range-shell--bar' : '',
    dragMode === 'thumb' ? 'sweep-range-shell--thumb' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className="panel sweep-controls" style={{ padding: 12 }}>
      <Group justify="space-between" align="center" mb={8}>
        <Text fw={600}>Sweep</Text>
        <Button
          size="xs"
          color={paused ? 'green' : 'orange'}
          variant="light"
          onClick={() => onSetParam('sweep_pause', !paused, true)}
        >
          {paused ? 'Start' : 'Pause'}
        </Button>
      </Group>

      <div
        className={sliderShellClassName}
        style={{ marginBottom: 12 }}
        onPointerDownCapture={handleRangeBarPointerDownCapture}
      >
        <RangeSlider
          classNames={{
            trackContainer: 'sweep-range-track-container',
            bar: 'sweep-range-bar',
            thumb: 'sweep-range-thumb',
          }}
          min={SWEEP_MIN}
          max={SWEEP_MAX}
          step={SWEEP_STEP}
          minRange={SWEEP_STEP}
          value={range}
          onChange={(value) => handleRangeChange(value as [number, number])}
          onChangeEnd={(value) => commitRange(value as [number, number])}
          label={(value) => value.toFixed(3)}
          color="orange"
        />
      </div>

      <Group grow>
        <NumberInput
          label="Center"
          value={center}
          min={SWEEP_MIN}
          max={SWEEP_MAX}
          decimalScale={4}
          step={0.01}
          onChange={(value) =>
            onSetParam('sweep_center', toClampedNumberOr(value, center, SWEEP_MIN, SWEEP_MAX), true)
          }
        />
        <NumberInput
          label="Amplitude"
          value={amplitude}
          min={0}
          max={1}
          decimalScale={4}
          step={0.01}
          onChange={(value) =>
            onSetParam('sweep_amplitude', toClampedNumberOr(value, amplitude, 0, 1), true)
          }
        />
      </Group>
    </div>
  );
}
