import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import { NumberInput } from '@mantine/core';
import type { NumberInputProps } from '@mantine/core';
import { toFiniteNumber } from '../utils/numberInput';

type DeferredNumberInputProps = Omit<
  NumberInputProps,
  'defaultValue' | 'onChange' | 'value'
> & {
  value: number;
  onCommit: (value: number) => void;
  formatValue?: (value: number) => number | string;
  parseCommit?: (value: number) => number | null;
};

const clamp = (value: number, min?: number, max?: number) => {
  const lower = min ?? Number.NEGATIVE_INFINITY;
  const upper = max ?? Number.POSITIVE_INFINITY;
  return Math.min(upper, Math.max(lower, value));
};

export function DeferredNumberInput({
  value,
  onCommit,
  formatValue,
  parseCommit,
  min,
  max,
  onBlur,
  onFocus,
  onKeyDown,
  onPointerDownCapture,
  clampBehavior = 'none',
  ...props
}: DeferredNumberInputProps) {
  // Store the formatter in a ref so callers that pass an unmemoized
  // formatValue closure (very common) do not retrigger the
  // sync-from-prop effect every render, which would clobber the user's
  // in-progress edit.
  const formatValueRef = useRef(formatValue);
  useEffect(() => {
    formatValueRef.current = formatValue;
  }, [formatValue]);

  const format = (nextValue: number) =>
    formatValueRef.current?.(nextValue) ?? nextValue;
  const [draft, setDraft] = useState<number | string>(() => format(value));
  const focusedRef = useRef(false);
  const editedRef = useRef(false);
  const stepCommitRef = useRef(false);
  const stepCommitTimerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!focusedRef.current) {
      setDraft(format(value));
      editedRef.current = false;
    }
    // Intentionally only depend on `value`. `formatValue` is read via ref
    // so external value updates are still picked up without firing on
    // every render of the parent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const revertDraft = () => {
    setDraft(format(value));
    editedRef.current = false;
  };

  const resolveCommitValue = (raw: unknown): number | null => {
    const parsed = toFiniteNumber(raw);
    if (parsed == null) {
      return null;
    }
    const clamped = clamp(parsed, min, max);
    return parseCommit ? parseCommit(clamped) : clamped;
  };

  const commitDraft = (raw: unknown) => {
    const next = resolveCommitValue(raw);
    if (next == null) {
      revertDraft();
      return;
    }
    setDraft(format(next));
    editedRef.current = false;
    onCommit(next);
  };

  const markStepCommit = () => {
    stepCommitRef.current = true;
    if (stepCommitTimerRef.current !== null) {
      window.clearTimeout(stepCommitTimerRef.current);
    }
    stepCommitTimerRef.current = window.setTimeout(() => {
      stepCommitRef.current = false;
      stepCommitTimerRef.current = null;
    }, 0);
  };

  const handleChange = (next: number | string) => {
    setDraft(next);
    if (stepCommitRef.current) {
      stepCommitRef.current = false;
      if (stepCommitTimerRef.current !== null) {
        window.clearTimeout(stepCommitTimerRef.current);
        stepCommitTimerRef.current = null;
      }
      commitDraft(next);
      return;
    }
    editedRef.current = true;
  };

  const handleBlur = (event: React.FocusEvent<HTMLInputElement>) => {
    focusedRef.current = false;
    if (editedRef.current) {
      commitDraft(draft);
    } else {
      revertDraft();
    }
    onBlur?.(event);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      commitDraft(draft);
    }
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      markStepCommit();
    }
    onKeyDown?.(event);
  };

  const handlePointerDownCapture = (event: PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button[aria-hidden="true"]')) {
      markStepCommit();
    }
    onPointerDownCapture?.(event as unknown as PointerEvent<HTMLInputElement>);
  };

  useEffect(() => {
    return () => {
      if (stepCommitTimerRef.current !== null) {
        window.clearTimeout(stepCommitTimerRef.current);
      }
    };
  }, []);

  return (
    <div onPointerDownCapture={handlePointerDownCapture}>
      <NumberInput
        {...props}
        min={min}
        max={max}
        value={draft}
        clampBehavior={clampBehavior}
        onChange={handleChange}
        onFocus={(event) => {
          focusedRef.current = true;
          onFocus?.(event);
        }}
        onBlur={handleBlur}
        onKeyDown={handleKeyDown}
      />
    </div>
  );
}
