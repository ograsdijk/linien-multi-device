import { useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { Button, Group, NumberInput, RangeSlider, Text } from '@mantine/core';

const SWEEP_MIN = -1;
const SWEEP_MAX = 1;
const SWEEP_STEP = 0.001;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const roundToStep = (value: number) =>
  Number((Math.round(value / SWEEP_STEP) * SWEEP_STEP).toFixed(6));

type SweepControlsProps = {
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
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
  const rangeRef = useRef(range);
  const draggingRef = useRef(false);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    rangeRef.current = range;
  }, [range]);

  useEffect(() => {
    if (!draggingRef.current) {
      setRange([min, max]);
    }
  }, [min, max]);

  const commitRange = (nextRange?: [number, number]) => {
    draggingRef.current = false;
    const resolved = nextRange ?? rangeRef.current;
    rangeRef.current = resolved;
    const [lo, hi] = resolved;
    const newCenter = (lo + hi) / 2;
    const newAmplitude = Math.abs(hi - lo) / 2;
    onSetParam('sweep_center', newCenter, false);
    onSetParam('sweep_amplitude', newAmplitude, true);
  };

  const commitRangeShift = (nextRange?: [number, number]) => {
    draggingRef.current = false;
    const resolved = nextRange ?? rangeRef.current;
    rangeRef.current = resolved;
    const [lo, hi] = resolved;
    const newCenter = (lo + hi) / 2;
    onSetParam('sweep_center', newCenter, true);
  };

  const handleRangeChange = (value: [number, number]) => {
    draggingRef.current = true;
    setRange((prev) => {
      const [lo, hi] = value ?? prev;
      const nextLo = clamp(lo, SWEEP_MIN, hi - SWEEP_STEP);
      const nextHi = clamp(hi, nextLo + SWEEP_STEP, SWEEP_MAX);
      return [nextLo, nextHi];
    });
  };

  const handleRangeBarPointerDownCapture = (
    event: ReactPointerEvent<HTMLDivElement>
  ) => {
    if (event.pointerType === 'mouse' && event.button !== 0) {
      return;
    }
    const target = event.target as HTMLElement;
    if (target.closest('.sweep-range-thumb')) {
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
      rangeRef.current = nextRange;
      setRange(nextRange);
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
    };
  }, []);

  return (
    <div className="panel" style={{ padding: 12 }}>
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

      <div style={{ marginBottom: 12 }} onPointerDownCapture={handleRangeBarPointerDownCapture}>
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
          step={0.01}
          onChange={(value) => onSetParam('sweep_center', Number(value), true)}
        />
        <NumberInput
          label="Amplitude"
          value={amplitude}
          min={0}
          max={1}
          step={0.01}
          onChange={(value) => onSetParam('sweep_amplitude', Number(value), true)}
        />
      </Group>
    </div>
  );
}
