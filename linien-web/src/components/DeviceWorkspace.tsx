import { memo, useCallback, useEffect, useRef, useState } from 'react';
import type {
  AutoRelockConfig,
  AutoLockScanResult,
  AutoLockScanSettings,
  Device,
  LockIndicatorConfig,
  PlotFrame,
  StreamMessage,
  DeviceStatus,
} from '../types';
import { api } from '../api';
import { useDeviceStream } from '../hooks/useDeviceStream';
import { PlotPanel } from './PlotPanel';
import { RightPanel } from './RightPanel';
import { StatusRow } from './StatusRow';
import { SweepControls } from './SweepControls';

const AUTOMATION_TEMP_DISABLED_REASON =
  'Temporarily disabled due to NumPy pickle compatibility between gateway and server.';

export type DeviceState = {
  params: Record<string, any>;
  plotFrame?: PlotFrame | null;
  status?: DeviceStatus | null;
};

type DeviceWorkspaceProps = {
  device: Device;
  state: DeviceState;
  active: boolean;
  onStateUpdate: (deviceKey: string, message: StreamMessage) => void;
};

type SetParamOptions = {
  optimistic?: boolean;
};

export const DeviceWorkspace = memo(function DeviceWorkspace({
  device,
  state,
  active,
  onStateUpdate,
}: DeviceWorkspaceProps) {
  const [selectionMode, setSelectionMode] = useState<'autolock' | 'optimization' | null>(null);
  const [, setSelectionError] = useState<string | null>(null);
  const [selectionSubmitting, setSelectionSubmitting] = useState(false);
  const [lockMode, setLockMode] = useState<'manual' | 'autolock_scan' | 'autolock'>('manual');
  const [autoLockSettings, setAutoLockSettings] = useState<AutoLockScanSettings | null>(null);
  const [lockIndicatorConfig, setLockIndicatorConfig] = useState<LockIndicatorConfig | null>(null);
  const [lockIndicatorSaving, setLockIndicatorSaving] = useState(false);
  const [lockIndicatorError, setLockIndicatorError] = useState<string | null>(null);
  const [autoRelockConfig, setAutoRelockConfig] = useState<AutoRelockConfig | null>(null);
  const [autoRelockSaving, setAutoRelockSaving] = useState(false);
  const [autoRelockError, setAutoRelockError] = useState<string | null>(null);
  const autoLockSettingsSaveTimerRef = useRef<number | null>(null);
  const connected = Boolean(state.status?.connected);
  const hasAutolockSelectionParam = Object.prototype.hasOwnProperty.call(
    state.params,
    'autolock_selection'
  );
  const hasOptimizationSelectionParam = Object.prototype.hasOwnProperty.call(
    state.params,
    'optimization_selection'
  );
  const hasAutomaticModeParam = Object.prototype.hasOwnProperty.call(state.params, 'automatic_mode');
  const lockStateFromParams = typeof state.params.lock === 'boolean' ? state.params.lock : undefined;
  const lockStateFromStatus = typeof state.status?.lock === 'boolean' ? state.status.lock : undefined;
  const lockState = lockStateFromParams ?? lockStateFromStatus;
  const sweepCenterRaw = state.params.sweep_center;
  const sweepCenterNum = sweepCenterRaw == null ? NaN : Number(sweepCenterRaw);
  const sweepCenter = Number.isFinite(sweepCenterNum) ? sweepCenterNum : undefined;
  const sweepAmplitudeRaw = state.params.sweep_amplitude;
  const sweepAmplitudeNum = sweepAmplitudeRaw == null ? NaN : Number(sweepAmplitudeRaw);
  const sweepAmplitude = Number.isFinite(sweepAmplitudeNum) ? sweepAmplitudeNum : undefined;
  const autolockTemporarilyDisabled = true;
  const optimizationTemporarilyDisabled = true;

  const onMessage = useCallback(
    (msg: StreamMessage) => {
      if (msg.type === 'config_update') {
        if (msg.config_name === 'lock_indicator_config') {
          setLockIndicatorConfig(msg.value as LockIndicatorConfig);
          setLockIndicatorError(null);
        }
        if (msg.config_name === 'auto_lock_scan_settings') {
          setAutoLockSettings(msg.value as AutoLockScanSettings);
        }
        if (msg.config_name === 'auto_relock_config') {
          setAutoRelockConfig(msg.value as AutoRelockConfig);
          setAutoRelockError(null);
        }
      }
      onStateUpdate(device.key, msg);
    },
    [device.key, onStateUpdate]
  );

  useDeviceStream(device.key, active, onMessage);

  useEffect(() => {
    let cancelled = false;
    setLockIndicatorError(null);
    setAutoRelockError(null);
    Promise.all([
      api.getLockIndicatorConfig(device.key),
      api.getAutoLockScanSettings(device.key),
      api.getAutoRelockState(device.key),
    ])
      .then(([lockIndicator, scanSettings, autoRelock]) => {
        if (cancelled) return;
        setLockIndicatorConfig(lockIndicator);
        setAutoLockSettings(scanSettings);
        setAutoRelockConfig(autoRelock.config);
      })
      .catch((error) => {
        if (cancelled) return;
        const message =
          error instanceof Error && error.message ? error.message : 'Failed to load settings.';
        setLockIndicatorError(message);
        setAutoRelockError(message);
      });
    return () => {
      cancelled = true;
      if (autoLockSettingsSaveTimerRef.current !== null) {
        window.clearTimeout(autoLockSettingsSaveTimerRef.current);
        autoLockSettingsSaveTimerRef.current = null;
      }
    };
  }, [device.key]);

  useEffect(() => {
    if (!hasAutomaticModeParam) {
      return;
    }
    const automaticMode = Boolean(state.params.automatic_mode);
    setLockMode((current) => {
      if (automaticMode) {
        return 'autolock';
      }
      if (current === 'autolock_scan') {
        return current;
      }
      return 'manual';
    });
  }, [hasAutomaticModeParam, state.params.automatic_mode]);

  useEffect(() => {
    if (!hasAutolockSelectionParam && !hasOptimizationSelectionParam) {
      return;
    }
    const backendMode = Boolean(state.params.autolock_selection)
      ? 'autolock'
      : Boolean(state.params.optimization_selection)
      ? 'optimization'
      : null;
    setSelectionMode((current) => (current === backendMode ? current : backendMode));
    if (backendMode === null) {
      setSelectionError(null);
      setSelectionSubmitting(false);
    }
  }, [
    hasAutolockSelectionParam,
    hasOptimizationSelectionParam,
    state.params.autolock_selection,
    state.params.optimization_selection,
  ]);

  const setParam = (
    name: string,
    value: any,
    writeRegisters = true,
    options?: SetParamOptions
  ) => {
    if (!connected) return;
    if (options?.optimistic !== false) {
      onStateUpdate(device.key, { type: 'param_update', name, value });
    }
    api.setParam(device.key, name, value, writeRegisters).catch(() => null);
  };

  const clearSelection = () => {
    setSelectionMode(null);
    setSelectionError(null);
    setSelectionSubmitting(false);
    if (!connected) return;
    setParam('autolock_selection', false, false);
    setParam('optimization_selection', false, false);
  };

  const startAutolockSelection = () => {
    if (autolockTemporarilyDisabled) {
      setSelectionMode(null);
      setSelectionSubmitting(false);
      setSelectionError(AUTOMATION_TEMP_DISABLED_REASON);
      return;
    }
    setSelectionMode('autolock');
    setSelectionError(null);
    setSelectionSubmitting(false);
    if (!connected) return;
    setParam('autolock_selection', true, false);
    setParam('optimization_selection', false, false);
    setParam('automatic_mode', true, false);
  };

  const startOptimizationSelection = () => {
    if (optimizationTemporarilyDisabled) {
      setSelectionMode(null);
      setSelectionSubmitting(false);
      setSelectionError(AUTOMATION_TEMP_DISABLED_REASON);
      return;
    }
    setSelectionMode('optimization');
    setSelectionError(null);
    setSelectionSubmitting(false);
    if (!connected) return;
    setParam('optimization_selection', true, false);
    setParam('autolock_selection', false, false);
  };

  const handleSelectRange = async (x0: number, x1: number) => {
    if (!connected) return;
    if (selectionMode === null || selectionSubmitting) return;
    if (
      (selectionMode === 'autolock' && autolockTemporarilyDisabled) ||
      (selectionMode === 'optimization' && optimizationTemporarilyDisabled)
    ) {
      setSelectionError(AUTOMATION_TEMP_DISABLED_REASON);
      return;
    }
    const min = Math.max(0, Math.min(2047, x0));
    const max = Math.max(0, Math.min(2047, x1));
    const mode = selectionMode;
    setSelectionError(null);
    setSelectionSubmitting(true);
    try {
      if (mode === 'autolock') {
        await api.startAutolock(device.key, min, max);
      }
      if (mode === 'optimization') {
        await api.startOptimization(device.key, min, max);
      }
      clearSelection();
    } catch (error) {
      setSelectionError(
        error instanceof Error && error.message
          ? error.message
          : 'Failed to start task for selected range.'
      );
    } finally {
      setSelectionSubmitting(false);
    }
  };

  const handleStartLock = async () => {
    if (!connected) return;
    try {
      const centerRaw = state.params.sweep_center;
      const center = centerRaw == null ? NaN : Number(centerRaw);
      if (Number.isFinite(center)) {
        // Ensure latest sweep center is on backend before lock handover.
        await api.setParam(device.key, 'sweep_center', center, true);
      } else {
        await api.writeRegisters(device.key);
      }
    } catch {
      // Best effort only; still attempt lock start.
    }
    api.startLock(device.key).catch(() => null);
  };

  const handleAutoLockFromScan = async (
    settings: AutoLockScanSettings
  ): Promise<AutoLockScanResult> => {
    if (!connected) {
      throw new Error('Device not connected.');
    }
    return api.autoLockFromScan(device.key, settings);
  };

  const handleAutoLockSettingsChange = (settings: AutoLockScanSettings) => {
    setAutoLockSettings(settings);
    if (autoLockSettingsSaveTimerRef.current !== null) {
      window.clearTimeout(autoLockSettingsSaveTimerRef.current);
    }
    autoLockSettingsSaveTimerRef.current = window.setTimeout(() => {
      api.updateAutoLockScanSettings(device.key, settings).catch(() => null);
      autoLockSettingsSaveTimerRef.current = null;
    }, 250);
  };

  const handleSaveLockIndicatorConfig = async (config: LockIndicatorConfig) => {
    setLockIndicatorSaving(true);
    setLockIndicatorError(null);
    try {
      const saved = await api.updateLockIndicatorConfig(device.key, config);
      setLockIndicatorConfig(saved);
    } catch (error) {
      setLockIndicatorError(
        error instanceof Error && error.message
          ? error.message
          : 'Failed to save lock-indicator config.'
      );
      throw error;
    } finally {
      setLockIndicatorSaving(false);
    }
  };

  const handleSaveAutoRelockConfig = async (config: AutoRelockConfig) => {
    setAutoRelockSaving(true);
    setAutoRelockError(null);
    try {
      const saved = await api.updateAutoRelockConfig(device.key, config);
      setAutoRelockConfig(saved.config);
    } catch (error) {
      setAutoRelockError(
        error instanceof Error && error.message
          ? error.message
          : 'Failed to save auto-relock config.'
      );
      throw error;
    } finally {
      setAutoRelockSaving(false);
    }
  };

  return (
    <div>
      {!connected ? (
        <div className="panel" style={{ padding: 12, marginBottom: 12 }}>
          Not connected. Use the device list on the left to connect.
        </div>
      ) : null}
      <div
        className="workspace"
        style={!connected ? { pointerEvents: 'none', opacity: 0.5 } : undefined}
      >
        <div className="plot-stack">
          <SweepControls params={state.params} onSetParam={setParam} />
          <PlotPanel
            plotFrame={state.plotFrame}
            selectionMode={selectionMode}
            onSelectRange={handleSelectRange}
            lockState={lockState}
            sweepCenter={sweepCenter}
            sweepAmplitude={sweepAmplitude}
            showManualTarget={lockMode !== 'autolock'}
          />
          <StatusRow
            plotFrame={state.plotFrame}
            lockIndicator={state.plotFrame?.lock_indicator ?? null}
            connected={connected}
            lockEnabled={lockState}
          />
        </div>
        <div>
          <RightPanel
            deviceKey={device.key}
            params={state.params}
            onSetParam={setParam}
            onStartLock={handleStartLock}
            onStartAutolockSelection={startAutolockSelection}
            onAbortAutolockSelection={clearSelection}
            onAutoLockFromScan={handleAutoLockFromScan}
            autoLockSettings={autoLockSettings}
            onAutoLockSettingsChange={handleAutoLockSettingsChange}
            onStartOptimizationSelection={startOptimizationSelection}
            onAbortOptimizationSelection={clearSelection}
            onStopTask={(useNew) => api.stopTask(device.key, useNew).catch(() => null)}
            onStopLock={() => api.stopLock(device.key).catch(() => null)}
            onShutdownServer={() => api.shutdownServer(device.key).catch(() => null)}
            lockMode={lockMode}
            onLockModeChange={(mode) => {
              setLockMode(mode);
              if (!connected) return;
              setParam('automatic_mode', mode === 'autolock', false);
            }}
            selectionMode={selectionMode}
            selectionSubmitting={selectionSubmitting}
            autolockTemporarilyDisabled={autolockTemporarilyDisabled}
            optimizationTemporarilyDisabled={optimizationTemporarilyDisabled}
            automationDisableReason={AUTOMATION_TEMP_DISABLED_REASON}
            lockIndicatorConfig={lockIndicatorConfig}
            lockIndicatorSaving={lockIndicatorSaving}
            lockIndicatorError={lockIndicatorError}
            onSaveLockIndicatorConfig={handleSaveLockIndicatorConfig}
            lockIndicatorSnapshot={state.plotFrame?.lock_indicator ?? null}
            autoRelockConfig={autoRelockConfig}
            autoRelockStatus={
              state.plotFrame?.auto_relock ?? state.status?.auto_relock ?? null
            }
            autoRelockSaving={autoRelockSaving}
            autoRelockError={autoRelockError}
            onSaveAutoRelockConfig={handleSaveAutoRelockConfig}
          />
        </div>
      </div>
    </div>
  );
});
