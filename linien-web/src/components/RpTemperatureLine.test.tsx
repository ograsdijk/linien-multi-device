import { MantineProvider } from '@mantine/core';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { DeviceStatus } from '../types';
import { RpTemperatureLine } from './RpTemperatureLine';

const renderLine = (
  status: DeviceStatus | null,
  props: Partial<React.ComponentProps<typeof RpTemperatureLine>> = {}
) =>
  render(
    <MantineProvider>
      <RpTemperatureLine status={status} {...props} />
    </MantineProvider>
  );

const status = (overrides: Partial<DeviceStatus>): DeviceStatus => ({
  connected: true,
  connecting: false,
  ...overrides,
});

// The reading is rendered as several text nodes inside one <Text>, so assert on
// the element's whole textContent rather than a single node.
const readingLine = () => screen.getByTitle('Red Pitaya (Zynq) die temperature');

describe('RpTemperatureLine', () => {
  it('shows the live temperature', () => {
    renderLine(status({ rp_temperature_c: 57.34, rp_telemetry: { state: 'running' } }));
    expect(readingLine().textContent).toBe('RP temperature: 57.3 °C');
    // No remedy button while it is working.
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('labels the reading as the Red Pitaya die temperature, not a laser one', () => {
    renderLine(status({ rp_temperature_c: 57.3, rp_telemetry: { state: 'running' } }));
    const title = readingLine().getAttribute('title') ?? '';
    expect(title).toMatch(/Red Pitaya/);
    expect(title).toMatch(/die temperature/);
  });

  it('shows an Install action when telemetry is not installed', () => {
    const onAction = vi.fn();
    renderLine(status({ rp_telemetry: { state: 'not_installed' } }), { onAction });

    expect(readingLine().textContent).toBe('RP temperature: unavailable');
    expect(screen.getByText('Telemetry not installed')).toBeTruthy();
    screen.getByRole('button', { name: 'Install' }).click();
    expect(onAction).toHaveBeenCalledWith('install');
  });

  it('shows a Restart action when the daemon reports an error', () => {
    const onAction = vi.fn();
    renderLine(status({ rp_telemetry: { state: 'error', error: 'daemon error: XADC' } }), {
      onAction,
    });

    screen.getByRole('button', { name: 'Restart' }).click();
    expect(onAction).toHaveBeenCalledWith('restart');
  });

  it('offers no action when the board itself is unreachable', () => {
    // Restart would SSH to the same host that just failed to answer TCP.
    renderLine(status({ rp_telemetry: { state: 'offline' } }), { onAction: vi.fn() });

    expect(screen.getByText(/unreachable/i)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('does not show a stale reading as current', () => {
    renderLine(status({ rp_temperature_c: 57.3, rp_telemetry: { state: 'stale' } }));
    expect(readingLine().textContent).toBe('RP temperature: unavailable');
    expect(screen.queryByText(/57\.3/)).toBeNull();
  });

  it('offers an Update action alongside a live reading', () => {
    const onAction = vi.fn();
    renderLine(
      status({
        rp_temperature_c: 50,
        rp_telemetry: {
          state: 'running',
          version: '0.9.0',
          bundled_version: '1.0.0',
          update_available: true,
        },
      }),
      { onAction }
    );
    expect(readingLine().textContent).toBe('RP temperature: 50.0 °C');
    screen.getByRole('button', { name: 'Update' }).click();
    expect(onAction).toHaveBeenCalledWith('update');
  });

  it('renders read-only without an onAction handler (overview cards)', () => {
    renderLine(status({ rp_telemetry: { state: 'not_installed' } }));
    expect(screen.getByText('Telemetry not installed')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('handles a missing status', () => {
    renderLine(null);
    expect(readingLine().textContent).toBe('RP temperature: unavailable');
  });
});
