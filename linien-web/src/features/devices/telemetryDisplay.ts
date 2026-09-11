import type { DeviceStatus, RpTelemetryState } from '../../types';

// Zynq-7010 die (junction) temperature thresholds.
//
// Rationale: the XC7Z010 in the Gen 1 STEMlab 125-14 is a commercial-grade
// part with an absolute maximum junction temperature of 85 °C (Xilinx DS187,
// T_j max for -1C/-2C speed grades). A board sitting in still air typically
// reads 55-70 °C, so:
//   * WARN at 75 °C   -- still inside spec, but the headroom is gone; usually
//                        means blocked airflow or a hot enclosure.
//   * CRITICAL at 85 °C -- at/over the rated maximum.
// These are display hints only. Nothing here shuts a board down or changes
// device behaviour; that stays an operator decision.
export const TEMPERATURE_WARN_C = 75;
export const TEMPERATURE_CRITICAL_C = 85;

export type TelemetryTone = 'normal' | 'warn' | 'critical' | 'muted';
export type TelemetryAction = 'install' | 'update' | 'restart' | 'start' | null;

export type TelemetryDisplay = {
  /** Rendered value, e.g. "57.3 °C" or "unavailable". */
  value: string;
  available: boolean;
  /** Short explanation shown under the value while unavailable. */
  detail: string | null;
  /** Suggested one-click remedy, or null when nothing obvious applies. */
  action: TelemetryAction;
  actionLabel: string | null;
  tone: TelemetryTone;
};

export const resolveTemperatureTone = (temperatureC: number): TelemetryTone => {
  if (temperatureC >= TEMPERATURE_CRITICAL_C) return 'critical';
  if (temperatureC >= TEMPERATURE_WARN_C) return 'warn';
  return 'normal';
};

export const formatTemperature = (temperatureC: number): string =>
  `${temperatureC.toFixed(1)} °C`;

const DETAIL_BY_STATE: Record<RpTelemetryState, string | null> = {
  unknown: 'Telemetry not polled yet',
  not_installed: 'Telemetry not installed',
  // The board itself did not answer, so there is nothing useful to click:
  // Restart is an SSH round trip to the same unreachable host and would just
  // spin for the SSH timeout before failing.
  offline: 'Telemetry offline — board unreachable',
  // Unused: `running` is handled by its own branches (a live reading, or the
  // "waiting for the first reading" case) before this table is consulted.
  running: null,
  stopped: 'Telemetry service stopped',
  // `stale` means state==running with no fresh sample, which a failed poll
  // never produces (it sets offline/stopped/error). So this is the gateway's
  // own poll loop having stopped, not a sick board -- and restarting the
  // daemon over SSH cannot fix it. Say so, and offer nothing.
  stale: 'No recent reading — gateway polling may have stopped',
  error: 'Telemetry error',
  version_mismatch: 'Unrecognised telemetry protocol',
};

const ACTION_BY_STATE: Record<RpTelemetryState, TelemetryAction> = {
  unknown: null,
  not_installed: 'install',
  running: null,
  stopped: 'start',
  offline: null,
  stale: null,
  error: 'restart',
  version_mismatch: 'install',
};

const ACTION_LABEL: Record<Exclude<TelemetryAction, null>, string> = {
  install: 'Install',
  update: 'Update',
  restart: 'Restart',
  start: 'Start',
};

/**
 * Turn a device status into what the card should show next to the host/IP.
 *
 * A cached temperature is only ever presented as a live reading while the
 * gateway reports `running` — every other state renders as "unavailable" so an
 * old number is never mistaken for the current one.
 */
export const resolveTelemetryDisplay = (
  status: DeviceStatus | null | undefined
): TelemetryDisplay => {
  const telemetry = status?.rp_telemetry ?? null;
  const state: RpTelemetryState = telemetry?.state ?? 'unknown';
  const temperature = status?.rp_temperature_c;

  if (state === 'running' && typeof temperature === 'number' && Number.isFinite(temperature)) {
    const updateAvailable = Boolean(telemetry?.update_available);
    return {
      value: formatTemperature(temperature),
      available: true,
      detail: updateAvailable
        ? `Telemetry ${telemetry?.version ?? '?'} installed, ${telemetry?.bundled_version ?? '?'} available`
        : null,
      action: updateAvailable ? 'update' : null,
      actionLabel: updateAvailable ? ACTION_LABEL.update : null,
      tone: resolveTemperatureTone(temperature),
    };
  }

  // Running, but no reading yet: right after a successful Install/Start the
  // gateway reports `running` while `temperature_c` is still null until the
  // next poll. Without this the card would show a bare "unavailable" with no
  // explanation and no action, immediately after an action that worked.
  if (state === 'running') {
    return {
      value: 'unavailable',
      available: false,
      detail: 'Waiting for the first reading',
      action: null,
      actionLabel: null,
      tone: 'muted',
    };
  }

  const action = ACTION_BY_STATE[state] ?? null;
  const detail = DETAIL_BY_STATE[state] ?? 'Telemetry unavailable';
  return {
    value: 'unavailable',
    available: false,
    detail: telemetry?.error && state === 'error' ? `${detail}: ${telemetry.error}` : detail,
    action,
    actionLabel: action ? ACTION_LABEL[action] : null,
    tone: 'muted',
  };
};
