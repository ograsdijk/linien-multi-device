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
    await waitFor(() => expect(screen.getByText(/score=0.870/)).toBeTruthy());

    // A failed attempt restores the geometry, so the run it describes no longer
    // stands -- leaving it up contradicts the failure toast beside it.
    succeed = false;
    fireEvent.click(screen.getByRole('button', { name: /Auto-lock from scan/i }));

    await waitFor(() => expect(screen.queryByText(/score=0.870/)).toBeNull());
  });
});
