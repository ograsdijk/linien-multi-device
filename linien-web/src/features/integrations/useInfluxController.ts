import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import type { Device, DeviceStatus, InfluxCredentials, ParamMeta } from '../../types';

const DEFAULT_INFLUX_CREDENTIALS: InfluxCredentials = {
  url: 'http://localhost:8086',
  org: 'my-org',
  token: 'my-token',
  bucket: 'my-bucket',
  measurement: '',
};

const toErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

const defaultMeasurementForDevice = (device: Device | null | undefined) =>
  device?.name?.trim() || device?.key?.trim() || 'linien';

const shouldUseDeviceMeasurementDefault = (measurement: string) => {
  const normalized = measurement.trim();
  return normalized === '' || normalized === 'my-measurement';
};

const withDeviceMeasurementDefault = (
  credentials: InfluxCredentials,
  device: Device | null | undefined
): InfluxCredentials => {
  if (!shouldUseDeviceMeasurementDefault(credentials.measurement)) {
    return credentials;
  }
  return {
    ...credentials,
    measurement: defaultMeasurementForDevice(device),
  };
};

const credentialsForTargetDevice = (
  credentials: InfluxCredentials,
  device: Device
): InfluxCredentials => ({
  ...credentials,
  measurement: defaultMeasurementForDevice(device),
});

type UseInfluxControllerArgs = {
  devices: Device[];
  activeDeviceKeys: string[];
  deviceStatusMap: Record<string, DeviceStatus | undefined>;
  onLoggingStateChange: (deviceKey: string, loggingActive: boolean) => void;
};

export type InfluxApplyAllOptions = {
  applyCredentials: boolean;
  applyParams: boolean;
  applyInterval: boolean;
  applyLoggingState: boolean;
};

export type InfluxApplyAllResult = {
  total: number;
  succeeded: number;
  failed: number;
  failures: Array<{ deviceKey: string; message: string }>;
};

export const useInfluxController = ({
  devices,
  activeDeviceKeys,
  deviceStatusMap,
  onLoggingStateChange,
}: UseInfluxControllerArgs) => {
  const [influxPopoverOpen, setInfluxPopoverOpen] = useState(false);
  const [influxDeviceKey, setInfluxDeviceKey] = useState<string | null>(null);
  const [influxCredentials, setInfluxCredentials] = useState<InfluxCredentials>(
    DEFAULT_INFLUX_CREDENTIALS
  );
  const [influxParams, setInfluxParams] = useState<ParamMeta[]>([]);
  const [influxInterval, setInfluxInterval] = useState(1);
  const [influxBusy, setInfluxBusy] = useState(false);
  const [influxMessage, setInfluxMessage] = useState<string | null>(null);
  const [influxMessageError, setInfluxMessageError] = useState(false);

  const preferredInfluxDeviceKey = useMemo(() => {
    const groupDeviceKey =
      activeDeviceKeys.find((key) => devices.some((device) => device.key === key)) ?? null;
    if (groupDeviceKey) return groupDeviceKey;
    return devices[0]?.key ?? null;
  }, [activeDeviceKeys, devices]);

  const influxDeviceOptions = useMemo(
    () =>
      devices.map((device) => ({
        value: device.key,
        label: `${device.name || device.key} (${device.host}:${device.port})`,
      })),
    [devices]
  );

  const influxSelectedStatus = influxDeviceKey ? deviceStatusMap[influxDeviceKey] : undefined;
  const influxSelectedDevice = influxDeviceKey
    ? devices.find((device) => device.key === influxDeviceKey) ?? null
    : null;
  const influxDeviceConnected = Boolean(influxSelectedStatus?.connected);
  const influxLoggingActive = Boolean(influxSelectedStatus?.logging_active);

  useEffect(() => {
    if (influxDeviceKey && devices.some((device) => device.key === influxDeviceKey)) {
      return;
    }
    setInfluxDeviceKey(preferredInfluxDeviceKey);
  }, [devices, influxDeviceKey, preferredInfluxDeviceKey]);

  useEffect(() => {
    if (!influxPopoverOpen || !influxDeviceKey) return;
    if (!influxDeviceConnected) {
      setInfluxParams([]);
      setInfluxMessage('Connect the selected device to configure InfluxDB.');
      setInfluxMessageError(true);
      return;
    }
    let cancelled = false;
    setInfluxBusy(true);
    setInfluxMessage(null);
    setInfluxMessageError(false);
    Promise.all([api.loggingGetCredentials(influxDeviceKey), api.getParamMeta(influxDeviceKey)])
      .then(([credentials, metadata]) => {
        if (cancelled) return;
        setInfluxCredentials(withDeviceMeasurementDefault(credentials, influxSelectedDevice));
        setInfluxParams(
          metadata
            .filter((item) => item.loggable)
            .sort((a, b) => a.name.localeCompare(b.name))
        );
      })
      .catch((error) => {
        if (cancelled) return;
        setInfluxMessage(toErrorMessage(error, 'Failed to load InfluxDB configuration.'));
        setInfluxMessageError(true);
      })
      .finally(() => {
        if (cancelled) return;
        setInfluxBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [influxPopoverOpen, influxDeviceKey, influxDeviceConnected, influxSelectedDevice]);

  const updateInfluxCredential = (name: keyof InfluxCredentials, value: string) => {
    setInfluxCredentials((prev) => ({ ...prev, [name]: value }));
  };

  const saveInfluxCredentials = async () => {
    if (!influxDeviceKey || !influxDeviceConnected) return;
    setInfluxBusy(true);
    setInfluxMessage(null);
    setInfluxMessageError(false);
    try {
      const credentials = withDeviceMeasurementDefault(influxCredentials, influxSelectedDevice);
      const result = await api.loggingUpdateCredentials(influxDeviceKey, credentials);
      setInfluxCredentials(credentials);
      setInfluxMessage(result.message);
      setInfluxMessageError(!result.success);
    } catch (error) {
      setInfluxMessage(toErrorMessage(error, 'Failed to update InfluxDB credentials.'));
      setInfluxMessageError(true);
    } finally {
      setInfluxBusy(false);
    }
  };

  const startInfluxLogging = async () => {
    if (!influxDeviceKey || !influxDeviceConnected) return;
    setInfluxBusy(true);
    setInfluxMessage(null);
    setInfluxMessageError(false);
    try {
      const interval = Math.max(0.1, Number(influxInterval) || 1);
      await api.loggingStart(influxDeviceKey, interval);
      onLoggingStateChange(influxDeviceKey, true);
      setInfluxMessage('Logging started.');
      setInfluxMessageError(false);
    } catch (error) {
      setInfluxMessage(toErrorMessage(error, 'Failed to start logging.'));
      setInfluxMessageError(true);
    } finally {
      setInfluxBusy(false);
    }
  };

  const stopInfluxLogging = async () => {
    if (!influxDeviceKey || !influxDeviceConnected) return;
    setInfluxBusy(true);
    setInfluxMessage(null);
    setInfluxMessageError(false);
    try {
      await api.loggingStop(influxDeviceKey);
      onLoggingStateChange(influxDeviceKey, false);
      setInfluxMessage('Logging stopped.');
      setInfluxMessageError(false);
    } catch (error) {
      setInfluxMessage(toErrorMessage(error, 'Failed to stop logging.'));
      setInfluxMessageError(true);
    } finally {
      setInfluxBusy(false);
    }
  };

  const selectedInfluxParamNames = useMemo(
    () => influxParams.filter((param) => param.log).map((param) => param.name),
    [influxParams]
  );

  const updateInfluxParamSelection = async (selectedNames: string[]) => {
    if (!influxDeviceKey || !influxDeviceConnected || influxBusy) return;
    const previousSelected = selectedInfluxParamNames;
    const previousSorted = [...previousSelected].sort();
    const nextSorted = [...selectedNames].sort();
    if (
      previousSorted.length === nextSorted.length &&
      previousSorted.every((value, index) => value === nextSorted[index])
    ) {
      return;
    }
    const nextSet = new Set(selectedNames);

    setInfluxParams((prev) =>
      prev.map((param) => ({ ...param, log: nextSet.has(param.name) }))
    );
    setInfluxBusy(true);
    try {
      await api.loggingSetParams(influxDeviceKey, selectedNames);
      setInfluxMessage(null);
      setInfluxMessageError(false);
    } catch (error) {
      const rollback = new Set(previousSelected);
      setInfluxParams((prev) =>
        prev.map((param) => ({ ...param, log: rollback.has(param.name) }))
      );
      setInfluxMessage(toErrorMessage(error, 'Failed to update logged parameters.'));
      setInfluxMessageError(true);
    } finally {
      setInfluxBusy(false);
    }
  };

  const influxChipColor = !influxDeviceKey
    ? 'gray'
    : !influxDeviceConnected
    ? 'gray'
    : influxLoggingActive
    ? 'green'
    : influxMessageError
    ? 'yellow'
    : 'gray';

  const influxLabel = !influxDeviceKey
    ? 'No device'
    : !influxDeviceConnected
    ? 'Disconnected'
    : influxLoggingActive
    ? 'Active'
    : 'Idle';

  const applyInfluxToAll = async (
    options: InfluxApplyAllOptions
  ): Promise<InfluxApplyAllResult> => {
    const targetDevices = devices.filter((device) =>
      Boolean(deviceStatusMap[device.key]?.connected)
    );
    if (!influxDeviceKey || targetDevices.length === 0) {
      setInfluxMessage('No connected devices to apply InfluxDB settings to.');
      setInfluxMessageError(true);
      return { total: 0, succeeded: 0, failed: 0, failures: [] };
    }

    const targetSelectedNames = selectedInfluxParamNames;
    const targetSelectedSet = new Set(targetSelectedNames);
    const targetInterval = Math.max(0.1, Number(influxInterval) || 1);
    const targetLoggingActive = influxLoggingActive;

    setInfluxBusy(true);
    setInfluxMessage(null);
    setInfluxMessageError(false);
    try {
      const results = await Promise.all(
        targetDevices.map(async (device) => {
          const deviceKey = device.key;
          const status = deviceStatusMap[deviceKey];
          try {
            if (options.applyCredentials) {
              const result = await api.loggingUpdateCredentials(
                deviceKey,
                credentialsForTargetDevice(influxCredentials, device)
              );
              if (!result.success) {
                throw new Error(result.message);
              }
            }

            if (options.applyParams) {
              const metadata = await api.getParamMeta(deviceKey);
              const loggable = metadata
                .filter((item) => item.loggable)
                .map((item) => item.name);
              const selectedForDevice = loggable.filter((name) =>
                targetSelectedSet.has(name)
              );
              await api.loggingSetParams(deviceKey, selectedForDevice);
            }

            if (options.applyInterval && options.applyLoggingState) {
              if (targetLoggingActive) {
                await api.loggingStart(deviceKey, targetInterval);
                onLoggingStateChange(deviceKey, true);
              } else {
                await api.loggingStop(deviceKey);
                onLoggingStateChange(deviceKey, false);
              }
            } else if (options.applyInterval) {
              if (status?.logging_active) {
                await api.loggingStart(deviceKey, targetInterval);
                onLoggingStateChange(deviceKey, true);
              }
            } else if (options.applyLoggingState) {
              if (targetLoggingActive) {
                await api.loggingStart(deviceKey, targetInterval);
                onLoggingStateChange(deviceKey, true);
              } else {
                await api.loggingStop(deviceKey);
                onLoggingStateChange(deviceKey, false);
              }
            }

            return { deviceKey, success: true as const };
          } catch (error) {
            return {
              deviceKey,
              success: false as const,
              message: toErrorMessage(error, 'Failed to apply settings.'),
            };
          }
        })
      );
      const failures = results
        .filter(
          (
            result
          ): result is { deviceKey: string; success: false; message: string } =>
            !result.success
        )
        .map(({ deviceKey, message }) => ({ deviceKey, message }));
      const succeeded = results.length - failures.length;
      const failed = failures.length;
      const result: InfluxApplyAllResult = {
        total: targetDevices.length,
        succeeded,
        failed,
        failures,
      };

      if (failed > 0) {
        setInfluxMessage(
          `Applied to ${succeeded}/${targetDevices.length} connected devices (${failed} failed).`
        );
        setInfluxMessageError(true);
      } else {
        setInfluxMessage(`Applied to all ${targetDevices.length} connected devices.`);
        setInfluxMessageError(false);
      }

      return result;
    } finally {
      setInfluxBusy(false);
    }
  };

  return {
    influxPopoverOpen,
    setInfluxPopoverOpen,
    influxDeviceKey,
    setInfluxDeviceKey,
    influxCredentials,
    influxParams,
    influxInterval,
    setInfluxInterval,
    influxBusy,
    influxMessage,
    influxMessageError,
    influxDeviceOptions,
    influxSelectedDevice,
    influxDeviceConnected,
    influxLoggingActive,
    influxChipColor,
    influxLabel,
    selectedInfluxParamNames,
    updateInfluxCredential,
    saveInfluxCredentials,
    startInfluxLogging,
    stopInfluxLogging,
    updateInfluxParamSelection,
    setInfluxMessage,
    setInfluxMessageError,
    applyInfluxToAll,
  };
};
