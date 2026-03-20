import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import type { Device, DeviceStatus, InfluxCredentials, ParamMeta } from '../../types';

const DEFAULT_INFLUX_CREDENTIALS: InfluxCredentials = {
  url: 'http://localhost:8086',
  org: 'my-org',
  token: 'my-token',
  bucket: 'my-bucket',
  measurement: 'my-measurement',
};

const toErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

type UseInfluxControllerArgs = {
  devices: Device[];
  activeDeviceKeys: string[];
  deviceStatusMap: Record<string, DeviceStatus | undefined>;
  onLoggingStateChange: (deviceKey: string, loggingActive: boolean) => void;
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
        setInfluxCredentials(credentials);
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
  }, [influxPopoverOpen, influxDeviceKey, influxDeviceConnected]);

  const updateInfluxCredential = (name: keyof InfluxCredentials, value: string) => {
    setInfluxCredentials((prev) => ({ ...prev, [name]: value }));
  };

  const saveInfluxCredentials = async () => {
    if (!influxDeviceKey || !influxDeviceConnected) return;
    setInfluxBusy(true);
    setInfluxMessage(null);
    setInfluxMessageError(false);
    try {
      const result = await api.loggingUpdateCredentials(influxDeviceKey, influxCredentials);
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
    const previousSet = new Set(previousSelected);
    const nextSet = new Set(selectedNames);
    const toEnable = selectedNames.filter((name) => !previousSet.has(name));
    const toDisable = previousSelected.filter((name) => !nextSet.has(name));
    if (toEnable.length === 0 && toDisable.length === 0) return;

    setInfluxParams((prev) =>
      prev.map((param) => ({ ...param, log: nextSet.has(param.name) }))
    );
    setInfluxBusy(true);
    try {
      await Promise.all([
        ...toEnable.map((name) => api.loggingSetParam(influxDeviceKey, name, true)),
        ...toDisable.map((name) => api.loggingSetParam(influxDeviceKey, name, false)),
      ]);
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
  };
};
