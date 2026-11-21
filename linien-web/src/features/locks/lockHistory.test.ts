import { describe, expect, it } from 'vitest';

import {
  describeAttempt,
  exportRecord,
  formatSetting,
  historyQuery,
  orderSettings,
  verificationDisplay,
  type LockAcquisitionRecord,
  type SettingRow,
} from './lockHistory';

describe('formatSetting', () => {
  it('shows Linien raw values in the units the controls use', () => {
    expect(formatSetting('modulation_frequency', 603979776)).toBe('18 MHz');
    expect(formatSetting('modulation_frequency', 402653184)).toBe('12 MHz');
    expect(formatSetting('modulation_amplitude', 4095.75)).toBe('1 Vpp');
    expect(formatSetting('modulation_amplitude', 4914.9)).toBe('1.2 Vpp');
    expect(formatSetting('demodulation_phase_a', 130)).toBe('130°');
    expect(formatSetting('offset_a', 2.4573)).toBe('0.0003 V');
    expect(formatSetting('filter_1_frequency_a', 10000)).toBe('10 kHz');
    expect(formatSetting('filter_1_enabled_a', 1)).toBe('On');
    expect(formatSetting('filter_automatic_a', 0)).toBe('Off');
    expect(formatSetting('target_slope_rising', true)).toBe('On');
    expect(formatSetting('p', 250)).toBe('250');
    expect(formatSetting('p', null)).toBe('—');
  });
});

describe('historyQuery', () => {
  const base = { deviceKey: null, source: null, outcome: 'any' as const, range: 'all' as const };

  it('asks for every device by default', () => {
    expect(historyQuery(base, 1000, 50, 0)).toBe('limit=50&offset=0');
  });

  it('maps filters onto the API parameters', () => {
    const query = new URLSearchParams(
      historyQuery(
        { deviceKey: 'abs', source: 'staged_autolock', outcome: 'failed', range: '24h' },
        100_000,
        50,
        50,
      ),
    );
    expect(query.get('device_key')).toBe('abs');
    expect(query.get('lock_source')).toBe('staged_autolock');
    expect(query.get('verification_status')).toBe('failed');
    expect(query.get('since')).toBe(String(100_000 - 86_400));
    expect(query.get('offset')).toBe('50');
  });

  it('filters rejected commands on the command status', () => {
    const query = new URLSearchParams(historyQuery({ ...base, outcome: 'rejected' }, 0, 10, 0));
    expect(query.get('lock_command_status')).toBe('rejected');
    expect(query.get('verification_status')).toBeNull();
  });
});

describe('verificationDisplay', () => {
  it('includes how long verification took', () => {
    expect(
      verificationDisplay({
        verification_status: 'verified',
        lock_command_at: '2026-11-21T17:08:14.000Z',
        verification_completed_at: '2026-11-21T17:08:15.200Z',
      }),
    ).toEqual({ label: 'Verified 1.2 s', tone: 'teal' });
  });
});

describe('orderSettings', () => {
  const row = (name: string): SettingRow => ({
    name, at_lock: 1, now: 1, restorable: true, differs: false,
  });

  it('lists key settings first, and the rest only when asked', () => {
    const rows = [row('sweep_speed'), row('p'), row('modulation_frequency')];
    expect(orderSettings(rows, false).map((r) => r.name)).toEqual(['modulation_frequency', 'p']);
    expect(orderSettings(rows, true).map((r) => r.name)).toEqual([
      'modulation_frequency',
      'p',
      'sweep_speed',
    ]);
  });
});

describe('exportRecord and describeAttempt', () => {
  const record = {
    lock_acquisition_id: '2418afae-ee5c-488a-8f37-9de8988de1f2',
    device_key: 'zgcbmduqgb',
    laser_name: 'Cavity 1 Cs',
    lock_source: 'auto_lock_scan',
    attempt_started_at: 1795280894.524653,
    lock_command_at: 1795280894.604742,
    lock_command_status: 'accepted',
    verification_status: 'unavailable',
    verification_reason: 'lock_indicator_disabled',
    failure_stage: null,
    failure_reason: null,
    trace_unavailable_reason: null,
  } as unknown as LockAcquisitionRecord;

  it('names the file after device, time and ID, and keeps the whole record', () => {
    const { filename, json } = exportRecord(record);
    expect(filename).toBe('lock_Cavity_1_Cs_2026-11-21T17-08-14_2418afae.json');
    expect(JSON.parse(json).lock_acquisition_id).toBe(record.lock_acquisition_id);
  });

  it('says what happened in one line', () => {
    expect(describeAttempt(record)).toBe(
      "Lock command accepted 80 ms after start. The lock indicator is off for this device, so the lock couldn't be verified.",
    );
  });
});
