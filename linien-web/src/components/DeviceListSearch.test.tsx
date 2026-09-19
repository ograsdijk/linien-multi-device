import { MantineProvider } from '@mantine/core';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Device, DeviceStatus } from '../types';
import { DeviceList } from './DeviceList';

const device = (key: string, name: string, host: string): Device =>
  ({ key, name, host, port: 18862 }) as Device;

const devices = [
  device('dev-1', 'cavity-north', '10.0.0.11'),
  device('dev-2', 'cavity-south', '10.0.0.12'),
  device('dev-3', 'reference', '192.168.4.7'),
];

const noop = async () => {};

const renderList = (statuses: Record<string, DeviceStatus | undefined> = {}) => {
  render(
    <MantineProvider>
      <DeviceList
        devices={devices}
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
        onStartTelemetryAll={noop}
        onRequestDiagnostics={vi.fn()}
      />
    </MantineProvider>
  );
};

const search = () => screen.getByLabelText('Search devices') as HTMLInputElement;
// One "Details for X" icon per rendered row -- unlike the expander label, it
// cannot collide with the panel's own "Collapse devices panel" control.
const rowNames = () =>
  screen
    .queryAllByLabelText(/^Details for /)
    .map((node) => node.getAttribute('aria-label')?.replace('Details for ', ''));

describe('DeviceList search and rows', () => {
  it('narrows the list as you type over name and host', () => {
    renderList();
    expect(rowNames()).toHaveLength(3);

    fireEvent.change(search(), { target: { value: 'south' } });
    expect(rowNames()).toEqual(['cavity-south']);

    fireEvent.change(search(), { target: { value: '192.168' } });
    expect(rowNames()).toEqual(['reference']);
  });

  it('reports how many of the fleet survived the filter', () => {
    renderList();
    fireEvent.change(search(), { target: { value: 'cavity' } });
    expect(screen.getByText(/2 of 3 shown/)).toBeTruthy();
  });

  it('says so when nothing matches, rather than showing an empty panel', () => {
    renderList();
    fireEvent.change(search(), { target: { value: 'nope' } });
    expect(screen.getByText('No devices match the current filters.')).toBeTruthy();
  });

  it('promotes a typed status word into a chip and clears the text', () => {
    renderList({
      'dev-1': { connected: true } as DeviceStatus,
      'dev-2': { connected: false } as DeviceStatus,
      'dev-3': { connected: false } as DeviceStatus,
    });

    fireEvent.change(search(), { target: { value: 'connected' } });
    fireEvent.keyDown(search(), { key: 'Enter' });

    expect(search().value).toBe('');
    expect((screen.getByRole('checkbox', { name: 'Connected' }) as HTMLInputElement).checked).toBe(
      true
    );
    expect(rowNames()).toEqual(['cavity-north']);
  });

  it('suspends manual reordering while a filter hides part of the list', () => {
    renderList();
    // Drag is what persists the manual order; on a filtered subset it would
    // write an order derived from rows that are not on screen.
    expect(document.querySelector('[data-sortable="true"]')).toBeTruthy();

    fireEvent.change(search(), { target: { value: 'cavity' } });
    expect(document.querySelector('[data-sortable="true"]')).toBeNull();
    expect(screen.getByText(/clear filters to reorder/)).toBeTruthy();
  });

  it('carries state as a glyph, so the name keeps the row', () => {
    renderList({ 'dev-1': { connected: true, lock: true } as DeviceStatus });

    // Spelled out, these two tags left roughly 50px for the name in a 320px
    // navbar and every device rendered as an ellipsis. The state stays
    // reachable by its accessible name while costing no width: the glyph
    // renders an icon and no text at all.
    const connection = screen.getAllByRole('img', { name: 'Connected' });
    expect(connection).toHaveLength(1);
    expect(connection[0].textContent).toBe('');

    const lock = screen.getAllByRole('img', { name: /^Lock: / });
    expect(lock).toHaveLength(3);
    lock.forEach((glyph) => expect(glyph.textContent).toBe(''));
  });

  it('opens one row at a time', () => {
    renderList();
    fireEvent.click(screen.getByLabelText('Expand cavity-north'));
    expect(screen.getByLabelText('Collapse cavity-north')).toBeTruthy();

    fireEvent.click(screen.getByLabelText('Expand cavity-south'));
    expect(screen.getByLabelText('Collapse cavity-south')).toBeTruthy();
    expect(screen.getByLabelText('Expand cavity-north')).toBeTruthy();
  });
});
