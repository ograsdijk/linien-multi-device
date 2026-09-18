import { MantineProvider } from '@mantine/core';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Device, DeviceStatus } from '../types';
import { DeviceList } from './DeviceList';

const device = (key: string): Device =>
  ({ key, name: key, host: '10.0.0.1', port: 18862 }) as Device;

const noop = async () => {};

const renderList = (statuses: Record<string, DeviceStatus | undefined>) => {
  const onStartTelemetryAll = vi.fn(async () => {});
  render(
    <MantineProvider>
      <DeviceList
        devices={[device('dev-1'), device('dev-2'), device('dev-3')]}
        statuses={statuses}
        lockIndicators={{}}
        autoRelockStates={{}}
        activeKeys={[]}
        canAddToGroup={false}
        sortMode="manual"
        onSortModeChange={vi.fn()}
        onCollapse={vi.fn()}
        onAddToGroup={vi.fn()}
        onToggleAutoRelock={vi.fn()}
        onAdd={noop}
        onEdit={noop}
        onDelete={noop}
        onStartServer={noop}
        onConnect={noop}
        onDisconnect={noop}
        onShutdownServer={noop}
        onRebootDevice={noop}
        onTelemetryCommand={noop}
        onInstallTelemetryAll={noop}
        onStartTelemetryAll={onStartTelemetryAll}
        onRequestDiagnostics={vi.fn()}
      />
    </MantineProvider>
  );
  return { onStartTelemetryAll };
};

const status = (overrides: Partial<DeviceStatus>): DeviceStatus =>
  ({ connected: true, connecting: false, ...overrides }) as DeviceStatus;

describe('DeviceList fleet telemetry menu', () => {
  it('keeps both fleet actions behind one header control', () => {
    renderList({});

    // The header offers the frequent actions plus one telemetry entry point --
    // not two full-width telemetry buttons competing with Connect all.
    expect(screen.getByTitle('Red Pitaya telemetry actions for every device')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Telemetry: install all/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Telemetry: start all/ })).toBeNull();
  });

  it('counts the boards a start would actually act on', async () => {
    renderList({
      'dev-1': status({ rp_telemetry: { state: 'stopped', installed: true } }),
      'dev-2': status({ rp_telemetry: { state: 'stopped', installed: true } }),
      'dev-3': status({ rp_telemetry: { state: 'running', installed: true } }),
    });

    fireEvent.click(screen.getByTitle('Red Pitaya telemetry actions for every device'));

    expect(await screen.findByText(/Start on all devices — 2 stopped/)).toBeTruthy();
  });

  it('does not offer a start when nothing is stopped', async () => {
    renderList({
      'dev-1': status({ rp_telemetry: { state: 'running', installed: true } }),
      // Offline and never-installed boards are not something a start can fix,
      // so they must not be counted as actionable.
      'dev-2': status({ rp_telemetry: { state: 'offline', installed: true } }),
      'dev-3': status({ rp_telemetry: { state: 'not_installed', installed: false } }),
    });

    fireEvent.click(screen.getByTitle('Red Pitaya telemetry actions for every device'));

    const item = await screen.findByText(/Start on all devices — none stopped/);
    expect(item.closest('[data-disabled]')).toBeTruthy();
  });

  it('starts every device from the menu item', async () => {
    const { onStartTelemetryAll } = renderList({
      'dev-1': status({ rp_telemetry: { state: 'stopped', installed: true } }),
    });

    fireEvent.click(screen.getByTitle('Red Pitaya telemetry actions for every device'));
    fireEvent.click(await screen.findByText(/Start on all devices/));

    expect(onStartTelemetryAll).toHaveBeenCalledWith(['dev-1', 'dev-2', 'dev-3']);
  });

  it('asks before reinstalling on every board', async () => {
    renderList({
      'dev-1': status({ rp_telemetry: { state: 'running', installed: true } }),
      'dev-2': status({ rp_telemetry: { state: 'running', installed: true } }),
      'dev-3': status({ rp_telemetry: { state: 'running', installed: true } }),
    });

    fireEvent.click(screen.getByTitle('Red Pitaya telemetry actions for every device'));
    fireEvent.click(await screen.findByText(/Update \/ reinstall on all devices/));

    // Install rewrites the binary on every board, so it keeps its confirmation
    // while start -- a no-op on a running service -- does not.
    expect(await screen.findByText(/Install telemetry on all devices\?/)).toBeTruthy();
  });
});
