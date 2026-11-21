// Lock history: types for the gateway's lock-acquisition API, and the pure
// helpers the history view uses (labels, unit conversion, queries, export).

export type Timestamp = number | string | null;

/** Milliseconds since the epoch; accepts Unix seconds or an ISO string. */
export const toMillis = (value: Timestamp | undefined): number | null => {
  if (value === null || value === undefined) return null;
  const ms = typeof value === 'number' ? value * 1000 : Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
};

export type LockSource = 'manual_lock' | 'auto_lock_scan' | 'staged_autolock' | 'auto_relock';
export type LockCommandStatus = 'not_sent' | 'accepted' | 'rejected';
export type VerificationStatus = 'not_attempted' | 'pending' | 'verified' | 'failed' | 'unavailable';

export type LockAcquisitionSummary = {
  lock_acquisition_id: string;
  device_key: string;
  laser_name: string | null;
  lock_source: LockSource;
  success: boolean | null;
  // Unix seconds (as the gateway returns them).
  attempt_started_at: Timestamp;
  lock_command_at: Timestamp;
  lock_command_status: LockCommandStatus | null;
  failure_stage: string | null;
  failure_reason: string | null;
  verification_status: VerificationStatus | null;
  verification_reason: string | null;
  verification_completed_at: Timestamp;
  modulation_frequency_hz: number | null;
  modulation_amplitude: number | null;
  demod_phase_deg: number | null;
  pid_p: number | null;
  pid_i: number | null;
  pid_d: number | null;
  sweep_center_v: number | null;
  sweep_amplitude_v: number | null;
  characterization_status: string | null;
  zero_crossing_sweep_v: number | null;
  discriminator_slope_v_per_mhz: number | null;
  slope_error_v_per_sweep_v: number | null;
  peak_to_peak_v: number | null;
  trace_unavailable_reason: string | null;
  trace_meta: Record<string, unknown> | null;
  // Control voltage on the first locked frame after the command (plot volts).
  post_lock_control_v?: number | null;
  persistence?: string;
  source?: string;
};

export type LockAcquisitionRecord = LockAcquisitionSummary & {
  trace_x?: Array<number | null> | null;
  trace_y?: Array<number | null> | null;
  secondary_trace_y?: Array<number | null> | null;
  trace_y_role?: string | null;
  secondary_trace_role?: string | null;
  linien_config?: {
    parameters?: Record<string, unknown>;
    missing?: string[];
    captured_at_stage?: string;
    [key: string]: unknown;
  } | null;
  characterization?: Record<string, unknown> | null;
  refinement?: Record<string, unknown> | null;
  post_lock?: PostLock | null;
};

export type PostLockFrame = {
  t_s: number;
  lock: boolean;
  control_mean_v: number | null;
  control_std_v: number | null;
  error_std_v: number | null;
  error_mean_abs_v: number | null;
  monitor_mean_v: number | null;
  monitor_std_v: number | null;
};

export type PostLock = {
  version: number;
  window_s: number;
  end_reason: string | null;
  frames_seen: number;
  summary: {
    first_locked_at_s: number | null;
    locked_fraction: number | null;
    first_control_v: number | null;
    last_control_v: number | null;
    control_drift_v: number | null;
    mean_error_std_v: number | null;
  };
  frames: PostLockFrame[];
  first_locked_frame: Record<string, unknown> | null;
};

export type LockAcquisitionList = {
  device_key: string | null;
  source: 'postgres' | 'spool';
  postgres_unavailable_reason?: string;
  items: LockAcquisitionSummary[];
  count: number;
  limit: number;
  offset: number;
  unsent_records: number;
};

export type SettingRow = {
  name: string;
  at_lock: unknown;
  now: unknown;
  restorable: boolean;
  differs: boolean;
};

export type SettingsComparison = {
  device_key: string;
  lock_acquisition_id: string;
  locked: boolean;
  parameters: SettingRow[];
};

export type RestoreResult = {
  device_key: string;
  lock_acquisition_id: string;
  applied: Array<{ name: string; old: unknown; new: unknown }>;
};

export const LOCK_SOURCE_LABEL: Record<LockSource, string> = {
  manual_lock: 'Manual',
  auto_lock_scan: 'Auto-lock scan',
  staged_autolock: 'Staged',
  auto_relock: 'Auto-relock',
};

export type Tone = 'teal' | 'red' | 'yellow' | 'gray' | 'blue';

export const commandDisplay = (
  status: LockCommandStatus | null,
): { label: string; tone: Tone } => {
  switch (status) {
    case 'accepted':
      return { label: 'Accepted', tone: 'teal' };
    case 'rejected':
      return { label: 'Rejected', tone: 'red' };
    default:
      return { label: 'Not sent', tone: 'gray' };
  }
};

const secondsBetween = (start: Timestamp, end: Timestamp): number | null => {
  const s = toMillis(start);
  const e = toMillis(end);
  return s !== null && e !== null ? (e - s) / 1000 : null;
};

export const verificationDisplay = (
  record: Pick<
    LockAcquisitionSummary,
    'verification_status' | 'lock_command_at' | 'verification_completed_at'
  >,
): { label: string; tone: Tone } => {
  const took = secondsBetween(record.lock_command_at, record.verification_completed_at);
  const suffix = took !== null && took >= 0 ? ` ${took.toFixed(1)} s` : '';
  switch (record.verification_status) {
    case 'verified':
      return { label: `Verified${suffix}`, tone: 'teal' };
    case 'failed':
      return { label: `Failed${suffix}`, tone: 'red' };
    case 'pending':
      return { label: 'Pending', tone: 'blue' };
    case 'unavailable':
      return { label: 'Unavailable', tone: 'gray' };
    default:
      return { label: 'Not attempted', tone: 'gray' };
  }
};

// Linien's raw units, as the web UI's own controls convert them.
const RAW_PER_MHZ = 0x10000000 / 8;
const RAW_PER_VPP = ((1 << 14) - 1) / 4;
const OFFSET_SCALE = 8191;

const toNumber = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const trimNumber = (value: number, digits: number): string =>
  String(Number(value.toFixed(digits)));

/** A saved Linien parameter in the units the UI shows it in. */
export const formatSetting = (name: string, value: unknown): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  const n = toNumber(value);
  if (n === null) return String(value);
  if (name === 'modulation_frequency') return `${trimNumber(n / RAW_PER_MHZ, 3)} MHz`;
  if (name === 'modulation_amplitude') return `${trimNumber(n / RAW_PER_VPP, 3)} Vpp`;
  if (name.startsWith('demodulation_phase')) return `${trimNumber(n, 1)}°`;
  if (name.startsWith('offset_')) return `${trimNumber(n / OFFSET_SCALE, 4)} V`;
  if (/^filter_\d_frequency/.test(name)) return `${trimNumber(n / 1000, 2)} kHz`;
  if (/^filter_\d_enabled/.test(name)) return n ? 'On' : 'Off';
  if (name.startsWith('filter_automatic')) return n ? 'On' : 'Off';
  if (name === 'sweep_center' || name === 'sweep_amplitude') return `${trimNumber(n, 4)} V`;
  return trimNumber(n, 4);
};

/** Shown first and always; the rest only behind "Show all". */
export const KEY_SETTINGS = [
  'modulation_frequency',
  'modulation_amplitude',
  'demodulation_phase_a',
  'offset_a',
  'filter_automatic_a',
  'filter_1_enabled_a',
  'filter_1_frequency_a',
  'filter_2_enabled_a',
  'filter_2_frequency_a',
  'p',
  'i',
  'd',
  'target_slope_rising',
  'sweep_center',
  'sweep_amplitude',
];

export const SETTING_LABEL: Record<string, string> = {
  modulation_frequency: 'Modulation frequency',
  modulation_amplitude: 'Modulation amplitude',
  demodulation_phase_a: 'Demod phase A',
  demodulation_phase_b: 'Demod phase B',
  offset_a: 'Offset A',
  offset_b: 'Offset B',
  filter_automatic_a: 'Auto filter A',
  filter_1_enabled_a: 'Filter 1 A',
  filter_1_frequency_a: 'Filter 1 A frequency',
  filter_2_enabled_a: 'Filter 2 A',
  filter_2_frequency_a: 'Filter 2 A frequency',
  p: 'P',
  i: 'I',
  d: 'D',
  target_slope_rising: 'Target slope rising',
  sweep_center: 'Sweep center',
  sweep_amplitude: 'Sweep amplitude',
};

/** Key settings first in KEY_SETTINGS order, then the rest alphabetically. */
export const orderSettings = (rows: SettingRow[], showAll: boolean): SettingRow[] => {
  const byName = new Map(rows.map((row) => [row.name, row]));
  const key = KEY_SETTINGS.map((name) => byName.get(name)).filter(
    (row): row is SettingRow => row !== undefined,
  );
  if (!showAll) return key;
  const rest = rows
    .filter((row) => !KEY_SETTINGS.includes(row.name))
    .sort((a, b) => a.name.localeCompare(b.name));
  return [...key, ...rest];
};

export type OutcomeFilter = 'any' | 'verified' | 'failed' | 'unavailable' | 'rejected';
export type RangeFilter = '24h' | '7d' | '30d' | 'all';

export type HistoryFilters = {
  deviceKey: string | null;
  source: LockSource | null;
  outcome: OutcomeFilter;
  range: RangeFilter;
};

const RANGE_SECONDS: Record<Exclude<RangeFilter, 'all'>, number> = {
  '24h': 86_400,
  '7d': 7 * 86_400,
  '30d': 30 * 86_400,
};

/** Query string for GET /api/lock-acquisitions. `nowS` is Unix seconds. */
export const historyQuery = (
  filters: HistoryFilters,
  nowS: number,
  limit: number,
  offset: number,
): string => {
  const params = new URLSearchParams();
  if (filters.deviceKey) params.set('device_key', filters.deviceKey);
  if (filters.source) params.set('lock_source', filters.source);
  if (filters.outcome === 'rejected') params.set('lock_command_status', 'rejected');
  else if (filters.outcome !== 'any') params.set('verification_status', filters.outcome);
  if (filters.range !== 'all') params.set('since', String(nowS - RANGE_SECONDS[filters.range]));
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  return params.toString();
};

/** A full record as a JSON file: everything the gateway returned. */
export const exportRecord = (
  record: LockAcquisitionRecord,
): { filename: string; json: string } => {
  const startedMs = toMillis(record.attempt_started_at);
  const when =
    startedMs === null ? '' : new Date(startedMs).toISOString().replace(/[:]/g, '-').replace(/\..*$/, '');
  const device = (record.laser_name || record.device_key).replace(/[^A-Za-z0-9_-]+/g, '_');
  const id = record.lock_acquisition_id.slice(0, 8);
  return {
    filename: `lock_${device}_${when || 'unknown'}_${id}.json`,
    json: JSON.stringify(record, null, 2),
  };
};

/** One-line account of what happened, for the detail header. */
export const describeAttempt = (record: LockAcquisitionSummary): string => {
  const parts: string[] = [];
  const took = secondsBetween(record.attempt_started_at, record.lock_command_at);
  if (record.lock_command_status === 'accepted') {
    parts.push(
      took !== null ? `Lock command accepted ${Math.round(took * 1000)} ms after start.` : 'Lock command accepted.',
    );
  } else if (record.lock_command_status === 'rejected') {
    parts.push('The board rejected the lock command.');
  } else {
    parts.push(
      record.failure_stage
        ? `Stopped at ${record.failure_stage.replace(/_/g, ' ')} before a lock command was sent.`
        : 'No lock command was sent.',
    );
  }
  if (record.failure_reason) parts.push(record.failure_reason);
  if (record.verification_status === 'unavailable' && record.verification_reason === 'lock_indicator_disabled') {
    parts.push("The lock indicator is off for this device, so the lock couldn't be verified.");
  } else if (record.verification_reason && record.verification_status !== 'verified') {
    parts.push(`Verification: ${record.verification_reason.replace(/_/g, ' ')}.`);
  }
  if (record.trace_unavailable_reason) {
    parts.push(`No trace: ${record.trace_unavailable_reason.replace(/_/g, ' ')}.`);
  }
  return parts.join(' ');
};

export const LOCK_POINT_COLOR = '#1D9E75';
export const CROSSING_COLOR = '#D85A30';

export type LockMarkers = {
  markers: Array<{ sweepV: number; color: string; dashed: boolean }>;
  // What the lines mean, for the note under the plot.
  legend: string;
  // Where the lock was engaged and where it should settle, when that is known.
  summary: string | null;
};

const num = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const mV = (volts: number): string => `${Number((Math.abs(volts) * 1000).toFixed(1))} mV`;
const V = (volts: number): string => `${Number(volts.toFixed(4))} V`;

/**
 * Markers for a record's trace. A manual lock is engaged at the sweep center
 * (its lock point) and the PID pulls the laser to the nearest crossing with
 * the configured slope, so both are drawn. Auto-locks mark the crossing the
 * detector chose, which is where they were placed.
 */
export const lockMarkers = (record: LockAcquisitionRecord): LockMarkers => {
  const characterization = (record.characterization ?? {}) as Record<string, unknown>;
  if (record.lock_source === 'manual_lock') {
    const lockPoint = num(characterization.lock_point_sweep_v) ?? num(record.sweep_center_v);
    const expected = characterization.expected_lock_crossing as
      | { sweep_v?: unknown; distance_from_lock_point_v?: unknown; slope_rising?: unknown }
      | null
      | undefined;
    const expectedV = num(expected?.sweep_v);
    const markers: LockMarkers['markers'] = [];
    if (lockPoint !== null) markers.push({ sweepV: lockPoint, color: LOCK_POINT_COLOR, dashed: false });
    if (expectedV !== null) markers.push({ sweepV: expectedV, color: CROSSING_COLOR, dashed: true });
    let summary: string | null = null;
    if (lockPoint !== null) {
      summary = `Engaged at ${V(lockPoint)} (the sweep center).`;
      if (expectedV !== null) {
        const slope = expected?.slope_rising ? 'rising' : 'falling';
        const distance = num(expected?.distance_from_lock_point_v) ?? expectedV - lockPoint;
        summary += ` With a ${slope} slope it should settle on the crossing at ${V(expectedV)}, ${mV(distance)} ${
          distance < 0 ? 'below' : 'above'
        }.`;
      } else if ('expected_lock_crossing' in characterization) {
        summary += ' No crossing with the configured slope was found near it.';
      }
    }
    return {
      markers,
      legend:
        'Solid green line: the lock point (sweep center). Dashed orange line: the nearest crossing with the configured slope, where the lock should settle.',
      summary,
    };
  }
  const target = num(record.zero_crossing_sweep_v);
  return {
    markers: target === null ? [] : [{ sweepV: target, color: CROSSING_COLOR, dashed: true }],
    legend: 'Dashed orange line: the zero crossing the lock was placed on.',
    summary: null,
  };
};

/** One line on what the board did after the lock command, or null if not recorded. */
export const describePostLock = (postLock: PostLock | null | undefined): string | null => {
  if (!postLock) return null;
  const s = postLock.summary;
  if (s.first_locked_at_s === null) {
    return postLock.frames_seen === 0
      ? 'No plot frames arrived after the lock command, so nothing was recorded.'
      : `No frame showed the board locked within ${postLock.window_s} s of the command.`;
  }
  const parts = [`First locked frame ${Math.round(s.first_locked_at_s * 1000)} ms after the command`];
  if (s.first_control_v !== null) parts.push(`control at ${Number(s.first_control_v.toFixed(4))} V`);
  if (s.control_drift_v !== null)
    parts.push(`moving ${Number((s.control_drift_v * 1000).toFixed(1))} mV over the next ${postLock.window_s} s`);
  let line = parts.join(', ') + '.';
  if (s.locked_fraction !== null && s.locked_fraction < 1)
    line += ` Locked on ${Math.round(s.locked_fraction * 100)}% of the frames.`;
  return line;
};
