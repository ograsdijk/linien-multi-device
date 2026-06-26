export type Device = {
  key: string;
  name: string;
  host: string;
  port: number;
  username: string;
  password: string;
  parameters: Record<string, any>;
};

export type DiagnosisCategory =
  | 'recovering'
  | 'host_unreachable'
  | 'server_down_unknown'
  | 'rebooted'
  | 'server_crashed';

export type DiagnosisLockState =
  | 'locked'
  | 'unlocked'
  | 'likely_held'
  | 'lost'
  | 'unknown';

export type DeviceDiagnosis = {
  category: DiagnosisCategory;
  lock_state: DiagnosisLockState;
  message: string;
  probed_at?: number | null;
  uptime_s?: number | null;
  host_reachable?: boolean | null;
  server_running?: boolean | null;
  fpga_operating?: boolean | null;
  seconds_since_last_connected?: number | null;
};

export type DeviceStatus = {
  connected: boolean;
  connecting: boolean;
  last_error?: string | null;
  last_plot?: number | null;
  logging_active?: boolean | null;
  lock?: boolean | null;
  psd_running?: boolean | null;
  auto_relock?: AutoRelockStatus | null;
  // Seconds since the last plot frame (null if none yet), and whether the stream
  // is stale enough that auto-relock (if enabled) is effectively frozen.
  stream_age_s?: number | null;
  stalled?: boolean;
  diagnosis?: DeviceDiagnosis | null;
};

export type LockIndicatorConfig = {
  enabled: boolean;
  bad_hold_s: number;
  good_hold_s: number;
  use_control: boolean;
  control_stuck_delta_counts: number;
  control_stuck_time_s: number;
  control_rail_threshold_v: number;
  control_rail_hold_s: number;
  use_error: boolean;
  error_mean_abs_max_v: number;
  error_std_min_v: number;
  error_std_max_v: number;
  use_monitor: boolean;
  monitor_mode: 'locked_above' | 'locked_below';
  monitor_threshold_v: number;
};

export type LockIndicatorSnapshot = {
  state: 'unknown' | 'locked' | 'marginal' | 'lost';
  reasons: string[];
  metrics: {
    error_std_v?: number | null;
    error_mean_abs_v?: number | null;
    control_std_v?: number | null;
    control_mean_v?: number | null;
    control_range_counts?: number | null;
    monitor_mean_v?: number | null;
    control_stuck_s: number;
    control_rail_s: number;
  };
  last_transition_at?: number | null;
};

export type PlotFrame = {
  type: 'plot_frame';
  lock: boolean;
  dual_channel: boolean;
  // Series values may arrive as Array<number | null> (JSON path, with
  // nulls for missing samples) or Float32Array (binary path, with
  // NaN for missing samples). PlotPanel/OverviewPlotPanel's
  // writeSeriesInto handles both via its ArrayBuffer.isView branch.
  series: Record<string, Array<number | null> | Float32Array>;
  signal_power: { channel1?: number | null; channel2?: number | null };
  stats: { error_std?: number | null; control_std?: number | null };
  lock_indicator?: LockIndicatorSnapshot;
  auto_relock?: AutoRelockStatus;
  lock_target?: number | null;
  x_label: string;
  x_unit: string;
};

export type AutoRelockConfig = {
  enabled: boolean;
  trigger_hold_s: number;
  verify_hold_s: number;
  cooldown_s: number;
  unlocked_trace_timeout_s: number;
  max_attempts: number;
};

export type AutoRelockStatus = {
  enabled: boolean;
  state: 'idle' | 'lost_pending' | 'waiting_unlocked_trace' | 'verifying' | 'cooldown';
  attempts: number;
  max_attempts: number;
  cooldown_remaining_s: number;
  last_trigger_at?: number | null;
  last_attempt_at?: number | null;
  last_success_at?: number | null;
  last_failure_at?: number | null;
  last_error?: string | null;
};

export type AutoRelockState = {
  config: AutoRelockConfig;
  status: AutoRelockStatus;
};

export type AutoLockScanSettings = {
  // Units: *_sweep_v are real sweep volts (x-axis); *_frac are normalized
  // full-scale error amplitude (y-axis); symmetry_min is a dimensionless ratio.
  half_range_sweep_v: number;
  crossing_max_frac: number;
  error_min_frac: number;
  symmetry_min: number;
  allow_single_side: boolean;
  single_error_min_frac: number;
  smooth_window_pts: number;
  use_monitor: boolean;
  monitor_contrast_min_frac: number;
};

export type AutoLockScanResult = {
  target_index: number;
  target_voltage: number;
  target_slope_rising: boolean;
  score: number;
  center_abs_v: number;
  left_excursion_v: number;
  right_excursion_v: number;
  pair_excursion_v: number;
  symmetry: number;
  monitor_contrast_v?: number | null;
  detail?: string | null;
};

export type AutoLockCalibrateRequest = {
  include_monitor: boolean;
  allow_single_side: boolean;
  // Optional override of the dead-trace amplitude floor (normalised full-scale).
  min_amplitude_frac?: number;
};

export type AutoLockCalibrationResult = {
  settings: AutoLockScanSettings;
  amplitude_v: number;
  feature_half_width_v: number;
  target_index: number;
  target_voltage: number;
  target_slope_rising: boolean;
  symmetry: number;
  monitor_contrast_v?: number | null;
  detail?: string | null;
};

export type DeviceGroup = {
  key: string;
  name: string;
  device_keys: string[];
  auto_include?: boolean;
};

export type ConfigUpdateName =
  | 'auto_lock_scan_settings'
  | 'lock_indicator_config'
  | 'auto_relock_config';

export type ConfigUpdateMessage = {
  type: 'config_update';
  config_name: ConfigUpdateName;
  value: AutoLockScanSettings | LockIndicatorConfig | AutoRelockConfig;
};

export type StreamMessage =
  | { type: 'param_update'; name: string; value: any }
  | PlotFrame
  | ConfigUpdateMessage
  | ({ type: 'status' } & DeviceStatus);

// Mirrors the gateway's Literal["disable","allow","prefer","require",
// "verify-ca","verify-full"] (schemas.py PostgresManualLockConfig).
export type PostgresSslMode =
  | 'disable'
  | 'allow'
  | 'prefer'
  | 'require'
  | 'verify-ca'
  | 'verify-full';

export type PostgresManualLockConfig = {
  enabled: boolean;
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
  sslmode: PostgresSslMode;
  connect_timeout_s: number;
};

export type PostgresManualLockStatus = {
  active: boolean;
  last_test_ok?: boolean | null;
  last_test_at?: number | null;
  last_write_ok?: boolean | null;
  last_write_at?: number | null;
  last_error?: string | null;
  enqueued_count: number;
  write_ok_count: number;
  write_error_count: number;
  dropped_count: number;
  queue_size: number;
};

export type PostgresManualLockState = {
  config: PostgresManualLockConfig;
  status: PostgresManualLockStatus;
};

export type PostgresManualLockTestResult = {
  ok: boolean;
  detail: string;
  state: PostgresManualLockState;
};

export type UiLogEntry = {
  id: string;
  ts: number;
  level: number;
  level_name: string;
  device_key?: string | null;
  source: string;
  code?: string | null;
  message: string;
  details: Record<string, any>;
};

export type LogsTailResponse = {
  entries: UiLogEntry[];
};

export type LogsStreamMessage = {
  type: 'log';
  entry: UiLogEntry;
};

// One point of a stitched PSD curve: frequency (Hz) and amplitude
// (V / Sqrt[Hz]). Both are linear values; the plot renders them on log axes.
export type PsdCurvePoint = {
  f: number;
  psd: number;
};

// One PSD measurement (partial or complete) as relayed by the gateway. The
// large raw `signals` are stripped server-side; only the ready-to-plot curve
// plus the PID gains / fitness metadata reach the browser.
export type PsdMeasurement = {
  device_key: string;
  uuid: string;
  time: number | null;
  p: number | null;
  i: number | null;
  d: number | null;
  // Band-limited integrated RMS of the error signal in Volts (sqrt(∫ASD²df)).
  rms_v: number | null;
  // Raw uncalibrated upstream sum; kept for export, not shown in the table.
  fitness: number | null;
  complete: boolean;
  curve: PsdCurvePoint[];
};

export type PsdStreamMessage = {
  type: 'psd';
  entry: PsdMeasurement;
};

export type PsdTailResponse = {
  entries: PsdMeasurement[];
};

export type ParamMeta = {
  name: string;
  restorable: boolean;
  loggable: boolean;
  log: boolean;
};

export type InfluxCredentials = {
  url: string;
  org: string;
  token: string;
  bucket: string;
  measurement: string;
};

export type InfluxUpdateResult = {
  success: boolean;
  message: string;
};
