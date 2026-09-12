import type {
  AutoRelockConfig,
  AutoRelockState,
  AutoLockCalibrateRequest,
  AutoLockCalibrationResult,
  AutoLockScanResult,
  AutoLockScanSettings,
  Device,
  DeviceStatus,
  DeviceGroup,
  InfluxCredentials,
  InfluxUpdateResult,
  LockIndicatorConfig,
  LogsTailResponse,
  ParamMeta,
  PostgresManualLockConfig,
  PostgresManualLockState,
  PostgresManualLockTestResult,
  PsdTailResponse,
  RpTelemetry,
} from './types';

export type TelemetryServiceStatus = {
  installed: boolean;
  active: boolean;
  active_state: string;
  enabled_state: string;
  version: string | null;
  bundled_version: string;
};

export type TelemetryStatus = {
  rp_temperature_c: number | null;
  rp_temperature_sampled_at: number | null;
  rp_telemetry: RpTelemetry;
};

export type PsdStartOptions = {
  algorithm?: number;
  maxDecimation?: number;
};

const envBase = import.meta.env?.VITE_API_URL as string | undefined;
const browserBase = typeof window !== 'undefined' ? `${window.location.origin}/api` : undefined;
// Trim trailing slashes so `${API_BASE}${path}` (path starts with "/") and the
// ws-scheme rewrite in ws.ts don't produce a doubled slash.
const API_BASE = (envBase || browserBase || 'http://localhost:8000/api').replace(
  /\/+$/,
  ''
);

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    // The body can only be read once — read it as text, then try to parse the
    // FastAPI `detail` out of it. Reading res.json() then res.text() throws
    // "body stream already read" and masks the real error.
    const raw = await res.text().catch(() => '');
    const contentType = res.headers.get('content-type') ?? '';
    let detail: string | null = null;
    if (contentType.includes('application/json') && raw) {
      try {
        const payload = JSON.parse(raw);
        if (payload && typeof payload.detail === 'string') {
          detail = payload.detail;
        } else if (payload != null) {
          detail = JSON.stringify(payload);
        }
      } catch {
        // Not valid JSON despite the header — fall back to the raw text.
      }
    }
    throw new Error(detail || raw || res.statusText);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return res.json();
}

export const api = {
  listDevices: () => request<Device[]>('/devices'),
  createDevice: (payload: Partial<Device>) =>
    request<Device>('/devices', { method: 'POST', body: JSON.stringify(payload) }),
  updateDevice: (key: string, payload: Partial<Device>) =>
    request<Device>(`/devices/${key}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteDevice: (key: string) => request(`/devices/${key}`, { method: 'DELETE' }),
  listGroups: () => request<DeviceGroup[]>('/groups'),
  createGroup: (payload: Partial<DeviceGroup>) =>
    request<DeviceGroup>('/groups', { method: 'POST', body: JSON.stringify(payload) }),
  updateGroup: (key: string, payload: Partial<DeviceGroup>) =>
    request<DeviceGroup>(`/groups/${key}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  reorderGroups: (keys: string[]) =>
    request<DeviceGroup[]>('/groups/order', { method: 'PUT', body: JSON.stringify({ keys }) }),
  deleteGroup: (key: string) => request(`/groups/${key}`, { method: 'DELETE' }),
  connectDevice: (key: string) => request(`/devices/${key}/connect`, { method: 'POST' }),
  startServer: (key: string) => request(`/devices/${key}/control/start_server`, { method: 'POST' }),
  rebootDevice: (key: string) =>
    request<{ ok: boolean; operation_id: string }>(`/devices/${key}/control/reboot`, {
      method: 'POST',
    }),
  disconnectDevice: (key: string) => request(`/devices/${key}/disconnect`, { method: 'POST' }),
  getStatus: (key: string) => request<DeviceStatus>(`/devices/${key}/status`),
  listStatuses: () => request<Record<string, DeviceStatus>>('/devices/statuses'),
  getParamMeta: (key: string) => request<ParamMeta[]>(`/devices/${key}/params`),
  getLockIndicatorConfig: (key: string) =>
    request<LockIndicatorConfig>(`/devices/${key}/lock-indicator`),
  updateLockIndicatorConfig: (key: string, payload: LockIndicatorConfig) =>
    request<LockIndicatorConfig>(`/devices/${key}/lock-indicator`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  setParam: (key: string, name: string, value: any, write_registers = true) =>
    request(`/devices/${key}/params/${name}`, {
      method: 'PATCH',
      body: JSON.stringify({ value, write_registers }),
    }),
  writeRegisters: (key: string) => request(`/devices/${key}/control/write_registers`, { method: 'POST' }),
  startLock: (key: string) => request(`/devices/${key}/control/start_lock`, { method: 'POST' }),
  startSweep: (key: string) => request(`/devices/${key}/control/start_sweep`, { method: 'POST' }),
  startSweepSimultaneous: (deviceKeys: string[], sweepSpeed?: number) =>
    request<{ started: string[]; skipped_unconnected: string[]; sweep_speed: number | null }>(
      '/control/start_sweep',
      {
        method: 'POST',
        body: JSON.stringify({
          device_keys: deviceKeys,
          sweep_speed: sweepSpeed ?? null,
        }),
      }
    ),
  startAutolock: (key: string, x0: number, x1: number) =>
    request(`/devices/${key}/control/start_autolock`, {
      method: 'POST',
      body: JSON.stringify({ x0, x1 }),
    }),
  autoLockFromScan: (key: string, payload: AutoLockScanSettings) =>
    request<AutoLockScanResult>(`/devices/${key}/control/auto_lock_scan`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  calibrateAutoLockScanSettings: (
    key: string,
    payload: AutoLockCalibrateRequest
  ) =>
    request<AutoLockCalibrationResult>(
      `/devices/${key}/control/auto_lock_scan/calibrate`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),
  getAutoLockScanSettings: (key: string) =>
    request<AutoLockScanSettings>(`/devices/${key}/auto-lock-scan-settings`),
  updateAutoLockScanSettings: (key: string, payload: AutoLockScanSettings) =>
    request<AutoLockScanSettings>(`/devices/${key}/auto-lock-scan-settings`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  getAutoRelockState: (key: string) =>
    request<AutoRelockState>(`/devices/${key}/auto-relock`),
  updateAutoRelockConfig: (key: string, payload: AutoRelockConfig) =>
    request<AutoRelockState>(`/devices/${key}/auto-relock`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  setAutoRelockEnabled: (key: string, enabled: boolean) =>
    request<AutoRelockState>(`/devices/${key}/auto-relock/enabled`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  startOptimization: (key: string, x0: number, x1: number) =>
    request(`/devices/${key}/control/start_optimization`, {
      method: 'POST',
      body: JSON.stringify({ x0, x1 }),
    }),
  startPidOptimization: (key: string) =>
    request(`/devices/${key}/control/start_pid_optimization`, { method: 'POST' }),
  stopLock: (key: string) =>
    request(`/devices/${key}/control/stop_lock`, { method: 'POST' }),
  stopTask: (key: string, use_new_parameters = false) =>
    request(`/devices/${key}/control/stop_task`, {
      method: 'POST',
      body: JSON.stringify({ use_new_parameters }),
    }),
  shutdownServer: (key: string) =>
    request(`/devices/${key}/control/shutdown_server`, { method: 'POST' }),
  loggingStart: (key: string, interval: number) =>
    request(`/devices/${key}/logging/start`, {
      method: 'POST',
      body: JSON.stringify({ interval }),
    }),
  loggingStop: (key: string) => request(`/devices/${key}/logging/stop`, { method: 'POST' }),
  loggingSetParam: (key: string, name: string, enabled: boolean) =>
    request(`/devices/${key}/logging/param/${name}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),
  loggingSetParams: (key: string, names: string[]) =>
    request(`/devices/${key}/logging/params`, {
      method: 'PUT',
      body: JSON.stringify({ names }),
    }),
  loggingGetCredentials: (key: string) =>
    request<InfluxCredentials>(`/devices/${key}/logging/credentials`),
  loggingUpdateCredentials: (key: string, payload: InfluxCredentials) =>
    request<InfluxUpdateResult>(`/devices/${key}/logging/credentials`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  // --- Red Pitaya telemetry (Zynq die temperature) ---
  // Management actions are SSH-backed and can take a few seconds; the read
  // paths are served from the gateway's cache.
  getTelemetry: (key: string) => request<TelemetryStatus>(`/devices/${key}/telemetry`),
  installTelemetry: (key: string) =>
    request<{ ok: boolean; version: string; temperature_c: number | null }>(
      `/devices/${key}/telemetry/install`,
      { method: 'POST' }
    ),
  uninstallTelemetry: (key: string) =>
    request<{ ok: boolean }>(`/devices/${key}/telemetry/uninstall`, { method: 'POST' }),
  startTelemetry: (key: string) =>
    request<{ ok: boolean; active: boolean; state: string }>(
      `/devices/${key}/telemetry/start`,
      { method: 'POST' }
    ),
  stopTelemetry: (key: string) =>
    request<{ ok: boolean; active: boolean; state: string }>(
      `/devices/${key}/telemetry/stop`,
      { method: 'POST' }
    ),
  restartTelemetry: (key: string) =>
    request<{ ok: boolean; active: boolean; state: string }>(
      `/devices/${key}/telemetry/restart`,
      { method: 'POST' }
    ),
  getTelemetryServiceStatus: (key: string) =>
    request<TelemetryServiceStatus>(`/devices/${key}/telemetry/service`),
  readTelemetryNow: (key: string) =>
    request<TelemetryStatus>(`/devices/${key}/telemetry/read`, { method: 'POST' }),
  installTelemetryMany: (deviceKeys: string[]) =>
    request<{ installed: string[]; failed: Record<string, string> }>(
      '/telemetry/install',
      { method: 'POST', body: JSON.stringify({ device_keys: deviceKeys }) }
    ),
  // A board whose unit starts and then dies immediately is reported in
  // `failed`, not `started` -- see the gateway endpoint.
  startTelemetryMany: (deviceKeys: string[]) =>
    request<{ started: string[]; failed: Record<string, string> }>(
      '/telemetry/start',
      { method: 'POST', body: JSON.stringify({ device_keys: deviceKeys }) }
    ),
  postgresManualLockState: () =>
    request<PostgresManualLockState>('/postgres/manual-lock'),
  updatePostgresManualLockState: (payload: PostgresManualLockConfig) =>
    request<PostgresManualLockState>('/postgres/manual-lock', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  testPostgresManualLockState: () =>
    request<PostgresManualLockTestResult>('/postgres/manual-lock/test', {
      method: 'POST',
    }),
  getLogsTail: (limit = 500) =>
    request<LogsTailResponse>(`/logs/tail?limit=${Math.max(1, Math.floor(limit))}`),
  clearLogs: () => request<{ ok: boolean; cleared: number }>('/logs', { method: 'DELETE' }),
  startPsdAcquisition: (key: string, opts?: PsdStartOptions) =>
    request(`/devices/${key}/control/start_psd_acquisition`, {
      method: 'POST',
      body: JSON.stringify({
        algorithm: opts?.algorithm ?? null,
        max_decimation: opts?.maxDecimation ?? null,
      }),
    }),
  stopPsdAcquisition: (key: string) =>
    request(`/devices/${key}/control/stop_psd_acquisition`, { method: 'POST' }),
  startPsdAcquisitionMany: (deviceKeys: string[], opts?: PsdStartOptions) =>
    request<{ started: string[]; skipped: Record<string, string> }>(
      '/control/start_psd_acquisition',
      {
        method: 'POST',
        body: JSON.stringify({
          device_keys: deviceKeys,
          algorithm: opts?.algorithm ?? null,
          max_decimation: opts?.maxDecimation ?? null,
        }),
      }
    ),
  stopPsdAcquisitionMany: (deviceKeys: string[]) =>
    request<{ stopped: string[]; skipped: Record<string, string> }>(
      '/control/stop_psd_acquisition',
      { method: 'POST', body: JSON.stringify({ device_keys: deviceKeys }) }
    ),
  getPsdTail: (limit = 200) =>
    request<PsdTailResponse>(`/psd/tail?limit=${Math.max(1, Math.floor(limit))}`),
  clearPsd: () => request<{ ok: boolean; cleared: number }>('/psd', { method: 'DELETE' }),
};

export const apiBase = API_BASE;
