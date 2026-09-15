import React from 'react';
import { MantineProvider } from '@mantine/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api';
import type { LockApproachProbeResult, LockApproachSettings } from '../types';
import { LockApproachPanel } from './LockApproachPanel';

const settings = (overrides: Partial<LockApproachSettings> = {}): LockApproachSettings => ({
  enabled: false,
  capture_fraction: 0.5,
  max_correction_span: 4,
  max_direct_jump_v: 2,
  approach_offset_v: 0.05,
  ramp_step_v: 0.005,
  ramp_step_delay_ms: 20,
  settle_ms: 300,
  approach_from_below: true,
  max_approach_iterations: 2,
  ...overrides,
});

const renderPanel = (halfRangeSweepV?: number) =>
  render(
    <MantineProvider>
      <LockApproachPanel deviceKey="dev-1" halfRangeSweepV={halfRangeSweepV} />
    </MantineProvider>
  );

describe('LockApproachPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('hides the motion settings until the guarded move is switched on', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    renderPanel();

    await waitFor(() =>
      expect(screen.getByLabelText(/Guarded center move/i)).toBeTruthy()
    );
    expect(screen.queryByText(/Approach offset/i)).toBeNull();
  });

  it('shows the acceptance window in volts, since it is never typed in', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(
      settings({ enabled: true, capture_fraction: 0.5 })
    );
    renderPanel(0.08);

    // 0.5 x 0.08 V calibrated half-width.
    await waitFor(() => expect(screen.getByText(/= 0.0400 V/)).toBeTruthy());
  });

  it('reports the measured verdict and every sample', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    const probe: LockApproachProbeResult = {
      target_voltage: 0.2,
      start_voltage: -0.3,
      capture_tolerance_v: 0.01,
      verdict: 'backlash',
      detail: 'the offset flips sign with direction',
      samples: [
        { from_below: true, settle_ms: 50, offset_v: 0.03, detail: '' },
        { from_below: false, settle_ms: 50, offset_v: -0.03, detail: '' },
      ],
    };
    vi.spyOn(api, 'measureLockApproach').mockResolvedValue(probe);
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));

    await waitFor(() => expect(screen.getByText(/flips sign with direction/)).toBeTruthy());
    expect(screen.getByText(/below @50ms: \+0.0300 V/)).toBeTruthy();
    expect(screen.getByText(/above @50ms: -0.0300 V/)).toBeTruthy();
  });

  it('surfaces a failed measurement instead of leaving a stale verdict', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    vi.spyOn(api, 'measureLockApproach').mockRejectedValue(
      new Error('Device is already locked. Start sweep first.')
    );
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));

    await waitFor(() =>
      expect(screen.getByText(/Device is already locked/)).toBeTruthy()
    );
  });

  it('samples with no detection are shown as such, not as zero offset', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    vi.spyOn(api, 'measureLockApproach').mockResolvedValue({
      target_voltage: 0.2,
      start_voltage: -0.3,
      capture_tolerance_v: 0.01,
      verdict: 'inconclusive',
      detail: 'not enough successful measurements to tell',
      samples: [{ from_below: true, settle_ms: 50, offset_v: null, detail: 'no signal' }],
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));

    await waitFor(() => expect(screen.getByText(/below @50ms: no detection/)).toBeTruthy());
  });
});

describe('LockApproachPanel persistence', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('does not overwrite stored settings with defaults when the load fails', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockRejectedValue(new Error('offline'));
    const save = vi.spyOn(api, 'updateLockApproachSettings');
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText(/Could not load approach settings/i)).toBeTruthy()
    );
    // The editable controls are withheld entirely, so nothing can be saved.
    expect(screen.queryByLabelText(/Guarded center move/i)).toBeNull();
    expect(save).not.toHaveBeenCalled();
  });

  it('recovers the controls when a retry succeeds', async () => {
    const get = vi
      .spyOn(api, 'getLockApproachSettings')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(settings({ enabled: true }));
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: /Retry/i }));

    await waitFor(() => expect(screen.getByLabelText(/Guarded center move/i)).toBeTruthy());
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('flushes a pending edit on unmount instead of dropping it', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    const save = vi.spyOn(api, 'updateLockApproachSettings').mockResolvedValue(
      settings({ enabled: true })
    );
    const view = renderPanel();

    fireEvent.click(await screen.findByLabelText(/Guarded center move/i));
    // Unmount inside the 250 ms debounce window.
    view.unmount();

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save.mock.calls[0][1].enabled).toBe(true);
  });
});

describe('LockApproachPanel device identity', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('flushes a pending edit to the device it was made on, not the current one', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    const save = vi
      .spyOn(api, 'updateLockApproachSettings')
      .mockResolvedValue(settings({ enabled: true }));
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-a" />
      </MantineProvider>
    );

    fireEvent.click(await screen.findByLabelText(/Guarded center move/i));
    // Switch to another device and unmount inside the 250 ms debounce window.
    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-b" />
      </MantineProvider>
    );
    view.unmount();

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save.mock.calls[0][0]).toBe('dev-a');
  });
});

describe('LockApproachPanel stream updates', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('does not load until the tab it lives in is actually shown', async () => {
    const get = vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active={false} />
      </MantineProvider>
    );

    expect(get).not.toHaveBeenCalled();

    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active />
      </MantineProvider>
    );
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
  });

  it('picks up another client’s saved change from the stream', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings({ enabled: true }));
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" halfRangeSweepV={0.08} />
      </MantineProvider>
    );

    await waitFor(() => expect(screen.getByText(/= 0.0400 V/)).toBeTruthy());

    view.rerender(
      <MantineProvider>
        <LockApproachPanel
          deviceKey="dev-1"
          halfRangeSweepV={0.08}
          settingsFromStream={settings({ enabled: true, capture_fraction: 0.25 })}
        />
      </MantineProvider>
    );

    await waitFor(() => expect(screen.getByText(/= 0.0200 V/)).toBeTruthy());
  });

  it('a broadcast never clobbers an edit that has not been saved yet', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings({ enabled: true }));
    vi.spyOn(api, 'updateLockApproachSettings').mockResolvedValue(settings());
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" halfRangeSweepV={0.08} />
      </MantineProvider>
    );

    await waitFor(() => expect(screen.getByText(/= 0.0400 V/)).toBeTruthy());
    // Start an edit, leaving a save pending in the debounce window.
    fireEvent.click(screen.getByLabelText(/Approach from below/i));

    view.rerender(
      <MantineProvider>
        <LockApproachPanel
          deviceKey="dev-1"
          halfRangeSweepV={0.08}
          settingsFromStream={settings({ enabled: true, capture_fraction: 0.25 })}
        />
      </MantineProvider>
    );

    // Still showing the local value, not the broadcast one.
    expect(screen.getByText(/= 0.0400 V/)).toBeTruthy();
  });
});

describe('LockApproachPanel tab switching', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('does not refetch and flash the controls when the tab is revisited', async () => {
    const get = vi
      .spyOn(api, 'getLockApproachSettings')
      .mockResolvedValue(settings({ enabled: true }));
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active />
      </MantineProvider>
    );
    await waitFor(() => expect(screen.getByLabelText(/Guarded center move/i)).toBeTruthy());

    const away = (
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active={false} />
      </MantineProvider>
    );
    view.rerender(away);
    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active />
      </MantineProvider>
    );

    expect(get).toHaveBeenCalledTimes(1);
    // Controls stayed up rather than dropping back to a loading state.
    expect(screen.getByLabelText(/Guarded center move/i)).toBeTruthy();
  });

  it('retries after a failed load when the tab is revisited', async () => {
    const get = vi
      .spyOn(api, 'getLockApproachSettings')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(settings());
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active />
      </MantineProvider>
    );
    await waitFor(() =>
      expect(screen.getByText(/Could not load approach settings/i)).toBeTruthy()
    );

    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active={false} />
      </MantineProvider>
    );
    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-1" active />
      </MantineProvider>
    );

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });
});

describe('LockApproachPanel acceptance window', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the applied window when it is narrower than the configured one', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(
      settings({ enabled: true, capture_fraction: 0.5 })
    );
    vi.spyOn(api, 'measureLockApproach').mockResolvedValue({
      target_voltage: 0.2,
      start_voltage: -0.3,
      // The neighbour guard tightened 0.5 x 0.02 = 0.0100 down to 0.0030.
      capture_tolerance_v: 0.003,
      verdict: 'backlash',
      detail: 'flips sign',
      samples: [{ from_below: true, settle_ms: 50, offset_v: 0.005, detail: '' }],
    });
    renderPanel(0.02);

    await waitFor(() => expect(screen.getByText(/= 0.0100 V acceptance window/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Measure hysteresis/i }));

    // The configured figure alone would overstate the real threshold 3x.
    await waitFor(() =>
      expect(screen.getByText(/= 0.0100 V, applied as 0.0030 V/)).toBeTruthy()
    );
    expect(screen.getByText(/narrowed to 0.0030 V/)).toBeTruthy();
  });

  it('says nothing extra when the configured window is what gets applied', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(
      settings({ enabled: true, capture_fraction: 0.5 })
    );
    vi.spyOn(api, 'measureLockApproach').mockResolvedValue({
      target_voltage: 0.2,
      start_voltage: -0.3,
      capture_tolerance_v: 0.01,
      verdict: 'negligible',
      detail: 'both directions land inside',
      samples: [{ from_below: true, settle_ms: 50, offset_v: 0.001, detail: '' }],
    });
    renderPanel(0.02);

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));

    await waitFor(() => expect(screen.getByText(/both directions land inside/)).toBeTruthy());
    expect(screen.queryByText(/applied as/)).toBeNull();
    expect(screen.queryByText(/narrowed to/)).toBeNull();
  });
});

describe('LockApproachPanel state consistency', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('issues exactly one save per edit', async () => {
    // Rendered under StrictMode, as the app is. React 18.2 does not in fact
    // re-invoke an event-queued updater here -- measured, not assumed -- so
    // this is a guard on the debounce rather than a regression test. The
    // scheduling still lives outside the updater, because purity is what makes
    // that guarantee independent of React's re-invocation rules.
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    const save = vi.spyOn(api, 'updateLockApproachSettings').mockResolvedValue(settings());
    render(
      <React.StrictMode>
        <MantineProvider>
          <LockApproachPanel deviceKey="dev-1" />
        </MantineProvider>
      </React.StrictMode>
    );

    fireEvent.click(await screen.findByLabelText(/Guarded center move/i));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(save).toHaveBeenCalledTimes(1);
  });

  it('a slow response for another device cannot land under the current one', async () => {
    let resolveA: (value: LockApproachSettings) => void = () => {};
    vi.spyOn(api, 'getLockApproachSettings').mockImplementation((key: string) => {
      if (key === 'dev-a') {
        return new Promise<LockApproachSettings>((resolve) => {
          resolveA = resolve;
        });
      }
      return Promise.resolve(settings({ enabled: true, capture_fraction: 0.25 }));
    });
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-a" halfRangeSweepV={0.08} />
      </MantineProvider>
    );

    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-b" halfRangeSweepV={0.08} />
      </MantineProvider>
    );
    await waitFor(() => expect(screen.getByText(/= 0.0200 V/)).toBeTruthy());

    // dev-a's request now resolves, after dev-b's already landed.
    resolveA(settings({ enabled: true, capture_fraction: 0.5 }));
    await new Promise((resolve) => setTimeout(resolve, 50));

    // Still dev-b's value, not the stale one.
    expect(screen.getByText(/= 0.0200 V/)).toBeTruthy();
  });

  it('withdraws the applied-window figure once the window settings change', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(
      settings({ enabled: true, capture_fraction: 0.5 })
    );
    vi.spyOn(api, 'updateLockApproachSettings').mockResolvedValue(settings());
    vi.spyOn(api, 'measureLockApproach').mockResolvedValue({
      target_voltage: 0.2,
      start_voltage: -0.3,
      capture_tolerance_v: 0.003,
      verdict: 'backlash',
      detail: 'flips sign',
      samples: [{ from_below: true, settle_ms: 50, offset_v: 0.005, detail: '' }],
    });
    renderPanel(0.02);

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));
    await waitFor(() => expect(screen.getByText(/applied as 0.0030 V/)).toBeTruthy());

    // Changing capture_fraction makes the measured window describe a config
    // that is no longer in force.
    fireEvent.click(screen.getByLabelText(/Approach from below/i));
    expect(screen.getByText(/applied as 0.0030 V/)).toBeTruthy(); // unrelated setting

    const captureInput = screen.getByLabelText(/Capture fraction/i);
    fireEvent.change(captureInput, { target: { value: '0.25' } });
    fireEvent.blur(captureInput);

    await waitFor(() => expect(screen.queryByText(/applied as/)).toBeNull());
    // The verdict itself describes the actuator and survives.
    expect(screen.getByText(/flips sign/)).toBeTruthy();
  });
});

describe('LockApproachPanel measurement identity', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('a verdict measured on one device is not shown under another', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(
      settings({ enabled: true })
    );
    vi.spyOn(api, 'measureLockApproach').mockResolvedValue({
      target_voltage: 0.2,
      start_voltage: -0.3,
      capture_tolerance_v: 0.01,
      verdict: 'backlash',
      detail: 'the offset flips sign with direction',
      samples: [{ from_below: true, settle_ms: 50, offset_v: 0.03, detail: '' }],
    });
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-a" />
      </MantineProvider>
    );

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));
    await waitFor(() => expect(screen.getByText(/flips sign with direction/)).toBeTruthy());

    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-b" />
      </MantineProvider>
    );

    // Wait until dev-b has loaded and the controls are up again -- otherwise
    // the loading state hides everything and the assertion proves nothing.
    await waitFor(() =>
      expect(screen.getByLabelText(/Guarded center move/i)).toBeTruthy()
    );
    expect(screen.queryByText(/flips sign with direction/)).toBeNull();
  });

  it('a failed measurement does not follow the panel to another device', async () => {
    vi.spyOn(api, 'getLockApproachSettings').mockResolvedValue(settings());
    vi.spyOn(api, 'measureLockApproach').mockRejectedValue(
      new Error('Device is already locked. Start sweep first.')
    );
    const view = render(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-a" />
      </MantineProvider>
    );

    fireEvent.click(await screen.findByRole('button', { name: /Measure hysteresis/i }));
    await waitFor(() => expect(screen.getByText(/already locked/)).toBeTruthy());

    view.rerender(
      <MantineProvider>
        <LockApproachPanel deviceKey="dev-b" />
      </MantineProvider>
    );

    await waitFor(() =>
      expect(screen.getByLabelText(/Guarded center move/i)).toBeTruthy()
    );
    expect(screen.queryByText(/already locked/)).toBeNull();
  });
});
