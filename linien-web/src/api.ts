import type {
  AutoRelockConfig,
  AutoRelockState,
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
} from './types';

const envBase = import.meta.env?.VITE_API_URL as string | undefined;
const browserBase = typeof window !== 'undefined' ? `${window.location.origin}/api` : undefined;
const API_BASE = envBase || browserBase || 'http://localhost:8000/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const contentType = res.headers.get('content-type') ?? '';
    if (contentType.includes('application/json')) {
      const payload = await res.json().catch(() => null);
      if (payload && typeof payload.detail === 'string') {
        throw new Error(payload.detail);
      }
      if (payload != null) {
        throw new Error(JSON.stringify(payload));
      }
    }
    const text = await res.text();
    throw new Error(text || res.statusText);
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
  deleteGroup: (key: string) => request(`/groups/${key}`, { method: 'DELETE' }),
  connectDevice: (key: string) => request(`/devices/${key}/connect`, { method: 'POST' }),
  startServer: (key: string) => request(`/devices/${key}/control/start_server`, { method: 'POST' }),
  disconnectDevice: (key: string) => request(`/devices/${key}/disconnect`, { method: 'POST' }),
  getStatus: (key: string) => request<DeviceStatus>(`/devices/${key}/status`),
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
  loggingGetCredentials: (key: string) =>
    request<InfluxCredentials>(`/devices/${key}/logging/credentials`),
  loggingUpdateCredentials: (key: string, payload: InfluxCredentials) =>
    request<InfluxUpdateResult>(`/devices/${key}/logging/credentials`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
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
};

export const apiBase = API_BASE;
