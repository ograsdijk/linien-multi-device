import type { Device, DeviceGroup } from './types';

const envBase = (import.meta as any).env?.VITE_API_URL as string | undefined;
const browserBase = typeof window !== 'undefined' ? `${window.location.origin}/api` : undefined;
const API_BASE = envBase || browserBase || 'http://localhost:8000/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
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
  getStatus: (key: string) => request(`/devices/${key}/status`),
  getParamMeta: (key: string) => request(`/devices/${key}/params`),
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
  loggingGetCredentials: (key: string) => request(`/devices/${key}/logging/credentials`),
  loggingUpdateCredentials: (key: string, payload: any) =>
    request(`/devices/${key}/logging/credentials`, { method: 'PUT', body: JSON.stringify(payload) }),
};

export const apiBase = API_BASE;
