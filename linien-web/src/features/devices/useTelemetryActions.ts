import { useCallback, useState } from 'react';
import { api } from '../../api';
import type { UiToast } from '../../components/ToastStack';
import type { TelemetryCommand } from '../../components/DeviceList';
import { deviceStatesStore } from '../../state/deviceStatesStore';
import { isDeviceStatus } from '../runtime/messageGuards';

type UseTelemetryActionsArgs = {
  appendUiErrorLog: (
    source: string,
    code: string,
    message: string,
    deviceKey?: string
  ) => void;
  pushToast: (toast: Omit<UiToast, 'id'>) => void;
};

const toErrorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message ? error.message : fallback;

const COMMAND_LABEL: Record<TelemetryCommand, string> = {
  install: 'installed',
  start: 'started',
  stop: 'stopped',
  restart: 'restarted',
  uninstall: 'removed',
};

/**
 * Operator actions on the Red Pitaya telemetry service.
 *
 * Each action is an SSH round trip on the gateway, so it can take a few
 * seconds — the calling card shows a busy state meanwhile. Afterwards the
 * device's status is re-fetched once so the card reflects the new telemetry
 * state without waiting for the next 30 s backstop poll.
 */
export const useTelemetryActions = ({
  appendUiErrorLog,
  pushToast,
}: UseTelemetryActionsArgs) => {
  const [telemetryBusyKeys, setTelemetryBusyKeys] = useState<Record<string, boolean>>({});

  const refreshStatus = useCallback(async (deviceKey: string) => {
    try {
      const status = await api.getStatus(deviceKey);
      if (!isDeviceStatus(status)) return;
      deviceStatesStore.batchUpdate([
        { deviceKey, updater: (prev) => ({ ...prev, status }) },
      ]);
    } catch {
      // The backstop poll will pick it up; a failed refresh is not an error
      // worth surfacing on top of whatever the action itself reported.
    }
  }, []);

  const runTelemetryCommand = useCallback(
    async (deviceKey: string, command: TelemetryCommand) => {
      setTelemetryBusyKeys((prev) => ({ ...prev, [deviceKey]: true }));
      try {
        let active: boolean | undefined;
        if (command === 'install') await api.installTelemetry(deviceKey);
        else if (command === 'start') ({ active } = await api.startTelemetry(deviceKey));
        else if (command === 'stop') await api.stopTelemetry(deviceKey);
        else if (command === 'restart')
          ({ active } = await api.restartTelemetry(deviceKey));
        else await api.uninstallTelemetry(deviceKey);
        // `systemctl start` can succeed while the unit dies immediately after;
        // the gateway reports that as ok:true, active:false. Saying "started"
        // there would be a success toast for a service that is not running.
        const startFailed = (command === 'start' || command === 'restart') && active === false;
        pushToast({
          level: startFailed ? 'warning' : 'info',
          title: 'Red Pitaya telemetry',
          message: startFailed
            ? `Telemetry ${command} was accepted but the service is not running.`
            : `Telemetry ${COMMAND_LABEL[command]}.`,
        });
      } catch (error) {
        const message = toErrorMessage(error, `Failed to ${command} telemetry.`);
        appendUiErrorLog(
          'rp_telemetry',
          `telemetry_${command}_failed`,
          message,
          deviceKey
        );
        // Toast as well as log: an SSH timeout takes ~20 s, and without this
        // the spinner just stops with no visible explanation unless the
        // operator happens to open the logs modal.
        pushToast({
          level: 'error',
          title: 'Red Pitaya telemetry',
          message,
        });
      } finally {
        // Refresh first, then clear busy: the other order renders the card
        // non-busy with its pre-action state for a frame (e.g. still
        // "not installed" with an Install button right after a good install).
        await refreshStatus(deviceKey);
        setTelemetryBusyKeys((prev) => ({ ...prev, [deviceKey]: false }));
      }
    },
    [appendUiErrorLog, pushToast, refreshStatus]
  );

  const installTelemetryAll = useCallback(
    async (deviceKeys: string[]) => {
      if (deviceKeys.length === 0) return;
      // Mark every target busy for the duration: otherwise the cards stay
      // clickable and a second install against the same board races the first
      // over the shared remote upload path, failing a board that is fine.
      setTelemetryBusyKeys((prev) => {
        const next = { ...prev };
        for (const key of deviceKeys) next[key] = true;
        return next;
      });
      try {
        const result = await api.installTelemetryMany(deviceKeys);
        const failedKeys = Object.keys(result.failed ?? {});
        pushToast({
          level: failedKeys.length > 0 ? 'warning' : 'info',
          title: 'Red Pitaya telemetry',
          message:
            failedKeys.length > 0
              ? `Installed on ${result.installed.length}; ${failedKeys.length} failed.`
              : `Installed on ${result.installed.length} device(s).`,
        });
        for (const key of failedKeys) {
          appendUiErrorLog(
            'rp_telemetry',
            'telemetry_install_failed',
            result.failed[key] || 'Telemetry install failed.',
            key
          );
        }
      } catch (error) {
        const message = toErrorMessage(error, 'Failed to install telemetry.');
        appendUiErrorLog('rp_telemetry', 'telemetry_install_failed', message);
        // Same reasoning as the single-device path: the modal has already
        // closed and the spinner just stops, so without a toast the operator
        // has no sign the batch never ran.
        pushToast({ level: 'error', title: 'Red Pitaya telemetry', message });
      } finally {
        await Promise.all(deviceKeys.map((key) => refreshStatus(key)));
        setTelemetryBusyKeys((prev) => {
          const next = { ...prev };
          for (const key of deviceKeys) next[key] = false;
          return next;
        });
      }
    },
    [appendUiErrorLog, pushToast, refreshStatus]
  );

  return { telemetryBusyKeys, runTelemetryCommand, installTelemetryAll };
};
