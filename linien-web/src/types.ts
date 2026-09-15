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
  /** The board's kernel boot ID. A change between probes proves a reboot. */
  boot_id?: string | null;
};

// --- Board diagnostics ---------------------------------------------------

/**
 * One retained board/server transition. Kinds mirror the gateway's
 * once-per-transition log codes, so the timeline never fills with repeats of a
 * steady state. See linien-gateway/app/board_event_store.py.
 */
export type BoardEventKind =
  | 'disconnected'
  | 'diagnosis'
  | 'reboot_detected'
  | 'reboot_requested'
  | 'telemetry_offline'
  | 'telemetry_recovered'
  | 'persistent_log_enabled';

export type BoardEvent = {
  ts: number;
  device_key: string;
  kind: BoardEventKind;
  detail: string;
  data?: Record<string, unknown>;
  boot_id?: string | null;
};

/** One command's worth of the diagnostics bundle. */
export type DiagnosticsSection = {
  name: string;
  title: string;
  command: string;
  output: string;
  /** Set when the command failed. `output` may still hold partial output. */
  error: string | null;
};

export type DiagnosticsBundle = {
  ok: boolean;
  error: string | null;
  collected_at: number;
  sections: DiagnosticsSection[];
  /**
   * Whether the board keeps logs across a reboot. `false` means the next crash
   * will again leave nothing behind; `null` means it could not be determined.
   */
  persistent_journal: boolean | null;
};

export type DeviceRecovery = {
  operation_id: string;
  action: 'reboot';
  phase:
    | 'queued'
    | 'dispatching'
    | 'waiting_for_boot'
    | 'host_online'
    | 'completed'
    | 'failed'
    | 'cancelled';
  started_at: number;
  updated_at: number;
  error?: string | null;
};

// Red Pitaya (Zynq die) telemetry, served from the gateway's cache.
// 'unknown' means the gateway has not completed a poll yet; 'stale' means the
// last good sample is older than the staleness window and must not be shown
// as a live reading. See linien-gateway/app/rp_telemetry.py.
export type RpTelemetryState =
  | 'unknown'
  | 'not_installed'
  | 'running'
  | 'stopped'
  | 'offline'
  | 'stale'
  | 'error'
  | 'version_mismatch';

export type RpTelemetry = {
  state: RpTelemetryState;
  version?: string | null;
  bundled_version?: string | null;
  update_available?: boolean;
  installed?: boolean;
  port?: number | null;
  error?: string | null;
  /** The window the gateway uses to call a reading stale, in seconds. */
  stale_after_s?: number | null;
};

// Board health sampled on the same request as the temperature, so it ages by
// `rp_temperature_age_s` too. Every field is independently optional: a daemon
// older than 1.2.0 reports none of them, and one it could not read is omitted
// rather than sent as zero. See linien-gateway/app/rp_telemetry.py.
export type RpMetrics = {
  /** Busy percent over the gateway's polling interval, not an instant. */
  cpu_percent?: number | null;
  load1?: number | null;
  mem_total_kb?: number | null;
  mem_available_kb?: number | null;
  mem_used_percent?: number | null;
  uptime_s?: number | null;
  /** Free space on the board's root filesystem (the SD card). */
  root_free_kb?: number | null;
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
  recovery?: DeviceRecovery | null;
  // Red Pitaya die temperature in degrees Celsius and the epoch seconds it was
  // sampled at. The gateway sends a reading ONLY while rp_telemetry.state is
  // 'running'; every other state sends null rather than an old number.
  rp_temperature_c?: number | null;
  rp_temperature_sampled_at?: number | null;
  /** Age of the reading when the gateway sent it. Skew-proof, unlike the
   *  absolute sample time: the UI ages it locally from here. */
  rp_temperature_age_s?: number | null;
  rp_telemetry?: RpTelemetry | null;
  // Null both for a board whose daemon does not report metrics and for a state
  // that does not vouch for them -- a consumer never has to tell those apart.
  rp_metrics?: RpMetrics | null;
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

// Raw per-frame signal statistics (null when the source signal is absent or
// the device is unlocked). Computed independently of the lock indicator.
export type SignalStats = {
  control_mean_v?: number | null;
  control_std_v?: number | null;
  control_range_counts?: number | null;
  error_std_v?: number | null;
  error_mean_abs_v?: number | null;
  monitor_mean_v?: number | null;
};

export type LockIndicatorSnapshot = {
  state: 'unknown' | 'locked' | 'marginal' | 'lost';
  reasons: string[];
  // Indicator-owned state only. Raw signal stats live in PlotFrame.signal_stats.
  metrics: {
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
  signal_stats?: SignalStats;
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
  // Amplitude thresholds are in plot units (the fixed ADC_SCALE display scale, same as the
  // plotted traces, ~±1 full scale; NOT per-trace normalized). half_range_sweep_v is sweep
  // volts (x-axis); symmetry_min is a dimensionless ratio.
  signal_type: "pdh" | "dispersive";
  allow_single_side: boolean;
  use_monitor: boolean;
  monitor_mode: "locked_above" | "locked_below";
  half_range_sweep_v: number;
  error_min: number;
  symmetry_min: number;
  single_error_min: number;
  min_amplitude: number;
  smooth_window_pts: number;
  monitor_threshold: number;
};

export type LockApproachSettings = {
  // Guarded sweep-center move. Voltages are sweep volts (x-axis). The acceptance
  // window is NOT a voltage here: it is capture_fraction x the calibrated feature
  // half-width (auto-lock half_range_sweep_v), because a lock succeeds whenever the
  // DC point lands between the two lobe extrema.
  enabled: boolean;
  capture_fraction: number;
  max_correction_span: number;
  max_direct_jump_v: number;
  approach_offset_v: number;
  ramp_step_v: number;
  ramp_step_delay_ms: number;
  settle_ms: number;
  approach_from_below: boolean;
  max_approach_iterations: number;
};

export type LockApproachAttempt = {
  attempt: number;
  from_below: boolean;
  direct: boolean;
  set_points: number;
  commanded_voltage: number;
  detected_voltage?: number | null;
  offset_v?: number | null;
  accepted: boolean;
  detail: string;
};

export type LockApproachReport = {
  enabled: boolean;
  accepted: boolean;
  target_voltage: number;
  commanded_voltage: number;
  start_voltage: number;
  // How far the center actually travelled, and how much of that was hysteresis
  // correction rather than the detected target (0 = target used untouched).
  center_move_v: number;
  center_correction_v: number;
  center_offset_v?: number | null;
  capture_tolerance_v: number;
  // Null when no usable neighbour guard could be derived for this signal.
  rejection_bound_v?: number | null;
  attempts: LockApproachAttempt[];
};

export type LockApproachSample = {
  from_below: boolean;
  settle_ms: number;
  offset_v?: number | null;
  detected_voltage?: number | null;
  detail: string;
};

export type LockApproachProbeResult = {
  target_voltage: number;
  start_voltage: number;
  capture_tolerance_v: number;
  samples: LockApproachSample[];
  verdict: 'backlash' | 'creep' | 'drift_or_creep' | 'negligible' | 'inconclusive';
  detail: string;
};

export type AutoLockScanResult = {
  target_index: number;
  target_voltage: number;
  target_slope_rising: boolean;
  score: number;
  left_excursion: number;
  right_excursion: number;
  pair_excursion: number;
  symmetry: number;
  monitor_level?: number | null;
  hz_per_v?: number | null;
  sideband_offset_v?: number | null;
  detail?: string | null;
  approach?: LockApproachReport | null;
};

export type AutoLockCalibrateRequest = {
  include_monitor: boolean;
  allow_single_side: boolean;
};

export type AutoLockCalibrationResult = {
  settings: AutoLockScanSettings;
  amplitude: number;
  feature_half_width_v: number;
  target_index: number;
  target_voltage: number;
  target_slope_rising: boolean;
  symmetry: number;
  monitor_level?: number | null;
  hz_per_v?: number | null;
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
  | 'auto_relock_config'
  | 'lock_approach_settings';

export type ConfigUpdateMessage = {
  type: 'config_update';
  config_name: ConfigUpdateName;
  value:
    | AutoLockScanSettings
    | LockIndicatorConfig
    | AutoRelockConfig
    | LockApproachSettings;
};

export type StreamMessage =
  | { type: 'param_update'; name: string; value: any }
  // Coalesced form of many param_update messages, sent on stream handshake
  // and on device connect. See DeviceSession._publish_param_snapshot.
  | { type: 'param_snapshot'; params: Record<string, any> }
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

/** One device's entry in the fleet-wide credentials response. */
export type InfluxCredentialsEntry = {
  connected: boolean;
  credentials: InfluxCredentials | null;
  error: string | null;
};

export type InfluxUpdateResult = {
  success: boolean;
  message: string;
};
