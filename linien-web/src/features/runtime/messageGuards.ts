import type { DeviceStatus, LogsStreamMessage, StreamMessage, UiLogEntry } from '../../types';

const isObject = (value: unknown): value is Record<string, unknown> =>
  value != null && typeof value === 'object' && !Array.isArray(value);

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

const isOptionalFiniteNumber = (value: unknown): value is number | null | undefined =>
  value == null || isFiniteNumber(value);

const isBoolean = (value: unknown): value is boolean => typeof value === 'boolean';

const isString = (value: unknown): value is string => typeof value === 'string';

const isStringArray = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((entry) => typeof entry === 'string');

const isSeriesRecord = (value: unknown): boolean => {
  if (!isObject(value)) return false;
  return Object.values(value).every(
    (series) =>
      Array.isArray(series) &&
      series.every((point) => point == null || (typeof point === 'number' && Number.isFinite(point)))
  );
};

export const isDeviceStatus = (value: unknown): value is DeviceStatus => {
  if (!isObject(value)) return false;
  if (!isBoolean(value.connected) || !isBoolean(value.connecting)) return false;
  if (value.last_error != null && !isString(value.last_error)) return false;
  if (!isOptionalFiniteNumber(value.last_plot)) return false;
  if (value.logging_active != null && !isBoolean(value.logging_active)) return false;
  if (value.lock != null && !isBoolean(value.lock)) return false;
  return true;
};

const isConfigUpdateValue = (configName: unknown, value: unknown): boolean => {
  if (configName === 'auto_lock_scan_settings') {
    return isObject(value);
  }
  if (configName === 'lock_indicator_config') {
    return isObject(value);
  }
  if (configName === 'auto_relock_config') {
    return isObject(value);
  }
  return false;
};

export const parseStreamMessage = (value: unknown): StreamMessage | null => {
  if (!isObject(value) || !isString(value.type)) return null;

  if (value.type === 'param_update') {
    if (!isString(value.name)) return null;
    return value as StreamMessage;
  }

  if (value.type === 'status') {
    if (!isDeviceStatus(value)) return null;
    return value as StreamMessage;
  }

  if (value.type === 'config_update') {
    if (!isString(value.config_name) || !isConfigUpdateValue(value.config_name, value.value)) {
      return null;
    }
    return value as StreamMessage;
  }

  if (value.type === 'plot_frame') {
    if (!isBoolean(value.lock) || !isBoolean(value.dual_channel)) return null;
    if (!isSeriesRecord(value.series)) return null;
    if (value.x_label != null && !isString(value.x_label)) return null;
    if (value.x_unit != null && !isString(value.x_unit)) return null;
    return value as StreamMessage;
  }

  return null;
};

export const isUiLogEntry = (value: unknown): value is UiLogEntry => {
  if (!isObject(value)) return false;
  if (!isString(value.id)) return false;
  if (!isFiniteNumber(value.ts)) return false;
  if (!isFiniteNumber(value.level)) return false;
  if (!isString(value.level_name)) return false;
  if (!isString(value.source)) return false;
  if (!isString(value.message)) return false;
  if (value.device_key != null && !isString(value.device_key)) return false;
  if (value.code != null && !isString(value.code)) return false;
  if (value.details != null && !isObject(value.details)) return false;
  return true;
};

export const sanitizeUiLogEntries = (value: unknown): UiLogEntry[] => {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is UiLogEntry => isUiLogEntry(entry));
};

export const parseLogsStreamMessage = (value: unknown): LogsStreamMessage | null => {
  if (!isObject(value) || value.type !== 'log') return null;
  if (!isUiLogEntry(value.entry)) return null;
  return value as LogsStreamMessage;
};

export const isKnownLogSourceFilter = (value: unknown): value is string | 'all' =>
  value === 'all' || isString(value);

export const isKnownLogDeviceFilter = (value: unknown): value is string | 'all' =>
  value === 'all' || isString(value);

export const isKnownLevelFilter = (value: unknown): value is 'all' | 'info' | 'warning' | 'error' =>
  value === 'all' || value === 'info' || value === 'warning' || value === 'error';

export const toUniqueSources = (entries: UiLogEntry[]): string[] =>
  Array.from(new Set(entries.map((entry) => entry.source).filter(Boolean)));

export const toUniqueDeviceKeys = (entries: UiLogEntry[]): string[] =>
  Array.from(new Set(entries.map((entry) => entry.device_key).filter((key): key is string => !!key)));

export const hasReasons = (value: unknown): value is { reasons: string[] } =>
  isObject(value) && isStringArray(value.reasons);
