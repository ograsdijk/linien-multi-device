import { useCallback, useEffect, useState } from 'react';
import type { Device, PlotFrame, StreamMessage, DeviceStatus } from '../types';
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

export function DeviceWorkspace({ device, state, active, onStateUpdate }: DeviceWorkspaceProps) {
  const [selectionMode, setSelectionMode] = useState<'autolock' | 'optimization' | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [selectionSubmitting, setSelectionSubmitting] = useState(false);
  const [lockMode, setLockMode] = useState<'manual' | 'autolock'>('manual');
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
  const lockState = typeof state.params.lock === 'boolean' ? state.params.lock : undefined;
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
      onStateUpdate(device.key, msg);
    },
    [device.key, onStateUpdate]
  );

  useDeviceStream(device.key, active, onMessage);

  useEffect(() => {
    if (!hasAutomaticModeParam) {
      return;
    }
    setLockMode(Boolean(state.params.automatic_mode) ? 'autolock' : 'manual');
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

  const setParam = (name: string, value: any, writeRegisters = true) => {
    if (!connected) return;
    onStateUpdate(device.key, { type: 'param_update', name, value });
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
            showManualTarget={lockMode === 'manual'}
          />
          <StatusRow plotFrame={state.plotFrame} />
        </div>
        <div>
          <RightPanel
            deviceKey={device.key}
            params={state.params}
            onSetParam={setParam}
            onStartLock={() => api.startLock(device.key).catch(() => null)}
            onStartAutolockSelection={startAutolockSelection}
            onAbortAutolockSelection={clearSelection}
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
            selectionError={selectionError}
            selectionSubmitting={selectionSubmitting}
            autolockTemporarilyDisabled={autolockTemporarilyDisabled}
            optimizationTemporarilyDisabled={optimizationTemporarilyDisabled}
            automationDisableReason={AUTOMATION_TEMP_DISABLED_REASON}
          />
        </div>
      </div>
    </div>
  );
}
