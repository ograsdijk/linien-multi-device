import { describe, expect, it } from 'vitest';
import type { Device, DeviceStatus, LockIndicatorSnapshot } from '../../types';
import { filterDevices, resolveFilterAlias } from './deviceFilter';

const device = (key: string, name: string, host: string): Device =>
  ({ key, name, host, port: 18862 }) as Device;

const devices = [
  device('dev-1', 'cavity-north', '10.0.0.11'),
  device('dev-2', 'cavity-south', '10.0.0.12'),
  device('dev-3', 'reference', '192.168.4.7'),
];

const status = (overrides: Partial<DeviceStatus>): DeviceStatus =>
  ({ connected: false, connecting: false, ...overrides }) as DeviceStatus;

const locked = { state: 'locked', reasons: [] } as unknown as LockIndicatorSnapshot;

const emptyCtx = { statuses: {}, lockIndicators: {}, autoRelockStates: {} };

describe('deviceFilter', () => {
  it('returns the list untouched when nothing is filtered', () => {
    expect(filterDevices(devices, '', [], emptyCtx)).toBe(devices);
  });

  it('matches name and host case-insensitively', () => {
    expect(filterDevices(devices, 'CAVITY', [], emptyCtx).map((d) => d.key)).toEqual([
      'dev-1',
      'dev-2',
    ]);
    expect(filterDevices(devices, '192.168', [], emptyCtx).map((d) => d.key)).toEqual(['dev-3']);
  });

  it('promotes only exact status words to a chip', () => {
    expect(resolveFilterAlias('locked')).toBe('locked');
    expect(resolveFilterAlias('  OFFLINE ')).toBe('disconnected');
    // A device could be named "locke"; a prefix must not hijack the search.
    expect(resolveFilterAlias('locke')).toBeNull();
    expect(resolveFilterAlias('')).toBeNull();
  });

  it('filters by connection and lock state', () => {
    const ctx = {
      statuses: {
        'dev-1': status({ connected: true, lock: true }),
        'dev-2': status({ connected: true, lock: false }),
        'dev-3': status({ connected: false, last_error: 'refused' }),
      },
      lockIndicators: { 'dev-1': locked },
      autoRelockStates: {},
    };
    expect(filterDevices(devices, '', ['connected'], ctx).map((d) => d.key)).toEqual([
      'dev-1',
      'dev-2',
    ]);
    expect(filterDevices(devices, '', ['locked'], ctx).map((d) => d.key)).toEqual(['dev-1']);
    expect(filterDevices(devices, '', ['error'], ctx).map((d) => d.key)).toEqual(['dev-3']);
  });

  it('ANDs several tags, and ANDs them with the text', () => {
    const ctx = {
      statuses: {
        'dev-1': status({ connected: true, lock: true }),
        'dev-2': status({ connected: true, lock: false }),
        'dev-3': status({ connected: false }),
      },
      lockIndicators: { 'dev-1': locked },
      autoRelockStates: {},
    };
    expect(filterDevices(devices, 'cavity', ['unlocked'], ctx).map((d) => d.key)).toEqual([
      'dev-2',
    ]);
    // Contradictory chips are read literally rather than widened.
    expect(filterDevices(devices, '', ['connected', 'disconnected'], ctx)).toEqual([]);
  });
});
