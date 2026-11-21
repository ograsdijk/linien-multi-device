import { describe, expect, it } from 'vitest';

import {
  SERIES_INDEX,
  SERIES_KEYS,
  sweepIndexToVoltage,
  visibleYExtent,
  type SeriesStats,
} from './plotShared';

describe('sweepIndexToVoltage', () => {
  it('maps an in-range sweep linearly', () => {
    expect(sweepIndexToVoltage(0, 5, 0, 0.4)).toBeCloseTo(-0.4);
    expect(sweepIndexToVoltage(2, 5, 0, 0.4)).toBeCloseTo(0);
    expect(sweepIndexToVoltage(4, 5, 0, 0.4)).toBeCloseTo(0.4);
  });

  it('clips a positive over-range sweep without rescaling earlier points', () => {
    expect(sweepIndexToVoltage(0, 5, 0.8, 0.4)).toBeCloseTo(0.4);
    expect(sweepIndexToVoltage(2, 5, 0.8, 0.4)).toBeCloseTo(0.8);
    expect(sweepIndexToVoltage(3, 5, 0.8, 0.4)).toBeCloseTo(1);
    expect(sweepIndexToVoltage(4, 5, 0.8, 0.4)).toBeCloseTo(1);
  });

  it('clips a negative over-range sweep at minus one volt', () => {
    expect(sweepIndexToVoltage(0, 5, -0.8, 0.4)).toBeCloseTo(-1);
    expect(sweepIndexToVoltage(1, 5, -0.8, 0.4)).toBeCloseTo(-1);
    expect(sweepIndexToVoltage(2, 5, -0.8, 0.4)).toBeCloseTo(-0.8);
    expect(sweepIndexToVoltage(4, 5, -0.8, 0.4)).toBeCloseTo(-0.4);
  });
});

describe('visibleYExtent', () => {
  const statsFor = (ranges: Partial<Record<(typeof SERIES_KEYS)[number], [number, number]>>) =>
    SERIES_KEYS.map((key): SeriesStats => {
      const range = ranges[key];
      return range
        ? { hasFinite: true, min: range[0], max: range[1] }
        : { hasFinite: false, min: Infinity, max: -Infinity };
    });
  const sweep = { combined_error: true, monitor_signal: true };
  const stats = statsFor({ combined_error: [-0.1, 0.1], monitor_signal: [0, 2] });
  const base = {
    stats,
    desiredVisibility: sweep,
    hasDataByKey: sweep,
    error1Visible: false,
    error2Visible: false,
  };

  it('spans every drawn series', () => {
    expect(visibleYExtent({ ...base, userShow: {} })).toEqual({ yMin: -0.1, yMax: 2 });
  });

  it('drops a series switched off in the legend', () => {
    const userShow = { [SERIES_INDEX.monitor_signal]: false };
    expect(visibleYExtent({ ...base, userShow })).toEqual({ yMin: -0.1, yMax: 0.1 });
  });

  it('includes it again when switched back on', () => {
    const userShow = { [SERIES_INDEX.monitor_signal]: true };
    expect(visibleYExtent({ ...base, userShow })).toEqual({ yMin: -0.1, yMax: 2 });
  });

  it('gives an empty extent when everything is hidden', () => {
    const userShow = {
      [SERIES_INDEX.monitor_signal]: false,
      [SERIES_INDEX.combined_error]: false,
    };
    expect(visibleYExtent({ ...base, userShow })).toEqual({ yMin: Infinity, yMax: -Infinity });
  });
});
