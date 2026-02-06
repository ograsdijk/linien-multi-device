import { useCallback, useState } from 'react';
import type { Device, PlotFrame, StreamMessage, DeviceStatus } from '../types';
import { api } from '../api';
import { useDeviceStream } from '../hooks/useDeviceStream';
import { PlotPanel } from './PlotPanel';
import { RightPanel } from './RightPanel';
import { StatusRow } from './StatusRow';
import { SweepControls } from './SweepControls';

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
  const [lockMode, setLockMode] = useState<'manual' | 'autolock'>('manual');
  const connected = Boolean(state.status?.connected);
  const lockState = typeof state.params.lock === 'boolean' ? state.params.lock : undefined;
  const sweepCenterRaw = state.params.sweep_center;
  const sweepCenterNum = sweepCenterRaw == null ? NaN : Number(sweepCenterRaw);
  const sweepCenter = Number.isFinite(sweepCenterNum) ? sweepCenterNum : undefined;
  const sweepAmplitudeRaw = state.params.sweep_amplitude;
  const sweepAmplitudeNum = sweepAmplitudeRaw == null ? NaN : Number(sweepAmplitudeRaw);
  const sweepAmplitude = Number.isFinite(sweepAmplitudeNum) ? sweepAmplitudeNum : undefined;

  const onMessage = useCallback(
    (msg: StreamMessage) => {
      onStateUpdate(device.key, msg);
    },
    [device.key, onStateUpdate]
  );

  useDeviceStream(device.key, active, onMessage);

  const setParam = (name: string, value: any, writeRegisters = true) => {
    if (!connected) return;
    onStateUpdate(device.key, { type: 'param_update', name, value });
    api.setParam(device.key, name, value, writeRegisters).catch(() => null);
  };

  const handleSelectRange = (x0: number, x1: number) => {
    if (!connected) return;
    const min = Math.max(0, Math.min(2047, x0));
    const max = Math.max(0, Math.min(2047, x1));
    if (selectionMode === 'autolock') {
      api.startAutolock(device.key, min, max).catch(() => null);
    }
    if (selectionMode === 'optimization') {
      api.startOptimization(device.key, min, max).catch(() => null);
    }
    setSelectionMode(null);
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
            onStartAutolockSelection={() => setSelectionMode('autolock')}
            onAbortAutolockSelection={() => setSelectionMode(null)}
            onStartOptimizationSelection={() => setSelectionMode('optimization')}
            onAbortOptimizationSelection={() => setSelectionMode(null)}
            onStopTask={(useNew) => api.stopTask(device.key, useNew).catch(() => null)}
            onStopLock={() => api.stopLock(device.key).catch(() => null)}
            onShutdownServer={() => api.shutdownServer(device.key).catch(() => null)}
            lockMode={lockMode}
            onLockModeChange={setLockMode}
          />
        </div>
      </div>
    </div>
  );
}
