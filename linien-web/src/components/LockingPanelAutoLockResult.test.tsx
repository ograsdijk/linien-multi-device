import { MantineProvider } from '@mantine/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { AutoLockScanResult } from '../types';
import { LockingPanel } from './LockingPanel';

const result: AutoLockScanResult = {
  target_index: 1024,
  target_voltage: 0.412,
  target_slope_rising: true,
  score: 0.87,
  left_excursion: 0.12,
  right_excursion: 0.13,
  pair_excursion: 0.25,
  symmetry: 0.92,
  approach: {
    enabled: true,
    accepted: true,
    target_voltage: 0.412,
    commanded_voltage: 0.43,
    start_voltage: 0.0,
    center_move_v: 0.43,
    center_correction_v: 0.018,
    center_offset_v: 0.0004,
    capture_tolerance_v: 0.01,
    rejection_bound_v: 0.08,
    attempts: [
      {
        attempt: 1,
        from_below: true,
        direct: true,
        set_points: 1,
        commanded_voltage: 0.412,
        offset_v: 0.018,
        accepted: false,
        detail: '',
      },
    ],
  },
};

const renderPanel = (onAutoLockFromScan: () => Promise<AutoLockScanResult>) =>
  render(
    <MantineProvider>
      <LockingPanel
        deviceKey="dev-1"
        params={{}}
        onSetParam={vi.fn()}
        onStartLock={vi.fn()}
        onStartAutolockSelection={vi.fn()}
        onAbortAutolockSelection={vi.fn()}
        onAutoLockFromScan={onAutoLockFromScan}
        onStopLock={vi.fn()}
        lockMode="autolock_scan"
      />
    </MantineProvider>
  );

describe('LockingPanel auto-lock summary', () => {
  it('drops the previous run summary when the next attempt fails', async () => {
    let succeed = true;
    const run = vi.fn(async () => {
      if (succeed) return result;
      throw new Error('Auto-lock aborted: never confirmed.');
    });
    renderPanel(run);

    fireEvent.click(screen.getByRole('button', { name: /Auto-lock from scan/i }));
    await waitFor(() => expect(screen.getByText(/moved \+0.4300 V/)).toBeTruthy());

    // A failed attempt restores the center, so the move it describes no longer
    // stands -- leaving it up contradicts the failure toast beside it.
    succeed = false;
    fireEvent.click(screen.getByRole('button', { name: /Auto-lock from scan/i }));

    await waitFor(() => expect(screen.queryByText(/moved \+0.4300 V/)).toBeNull());
  });
});
