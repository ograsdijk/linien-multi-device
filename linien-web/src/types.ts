export type Device = {
  key: string;
  name: string;
  host: string;
  port: number;
  username: string;
  password: string;
  parameters: Record<string, any>;
};

export type DeviceStatus = {
  connected: boolean;
  connecting: boolean;
  last_error?: string | null;
  last_plot?: number | null;
  logging_active?: boolean | null;
  lock?: boolean | null;
  auto_relock?: AutoRelockStatus | null;
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
  series: Record<string, Array<number | null>>;
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
  half_range_v: number;
  crossing_max_v: number;
  error_min: number;
  symmetry_min: number;
  allow_single_side: boolean;
  single_error_min: number;
  smooth_window_pts: number;
  use_monitor: boolean;
  monitor_contrast_min_v: number;
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

export type PostgresManualLockConfig = {
  enabled: boolean;
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
  sslmode: string;
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
