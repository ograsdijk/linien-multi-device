import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  AutoRelockConfig,
  AutoLockCalibrateRequest,
  AutoLockCalibrationResult,
  AutoLockScanResult,
  AutoLockScanSettings,
  AutoRelockStatus,
  Device,
  LockIndicatorConfig,
  LockIndicatorSnapshot,
  PlotFrame,
  StreamMessage,
} from '../types';
import { api } from '../api';
import { useDeviceStream } from '../hooks/useDeviceStream';
import { useInViewport } from '../hooks/useInViewport';
import { PlotPanel, type PlotPanelHandle } from './PlotPanel';
import { RightPanel } from './RightPanel';
import { ThrottledStatusRow } from './ThrottledStatusRow';
import { SweepControls } from './SweepControls';
import { useDeviceStateEntry } from '../state/deviceStatesStore';

// Compare lock-indicator snapshots by the fields downstream UI reads.
// Avoids forcing a render when the backend sends a fresh indicator
// object every frame whose contents are identical.
const indicatorEqual = (
  a: LockIndicatorSnapshot | null,
  b: LockIndicatorSnapshot | null,
): boolean => {
  if (a === b) return true;
  if (!a || !b) return false;
  if (a.state !== b.state) return false;
  if (a.reasons === b.reasons) return true;
  if (!a.reasons || !b.reasons) return false;
  if (a.reasons.length !== b.reasons.length) return false;
  for (let i = 0; i < a.reasons.length; i++) {
    if (a.reasons[i] !== b.reasons[i]) return false;
  }
  return true;
};

// Compare auto-relock status by the fields the RightPanel reads
// (state, enabled, attempts, cooldown, last error). Skips frame-rate
// re-renders when nothing meaningful changed.
const autoRelockEqual = (
  a: AutoRelockStatus | null,
  b: AutoRelockStatus | null,
): boolean => {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.enabled === b.enabled &&
    a.state === b.state &&
    a.attempts === b.attempts &&
    a.max_attempts === b.max_attempts &&
    a.cooldown_remaining_s === b.cooldown_remaining_s &&
    a.last_error === b.last_error
  );
};

const AUTOMATION_TEMP_DISABLED_REASON =
  'Temporarily disabled due to NumPy pickle compatibility between gateway and server.';

type DeviceWorkspaceProps = {
  device: Device;
  active: boolean;
  onStateUpdate: (deviceKey: string, message: StreamMessage) => void;
  onStreamActiveChange?: (deviceKey: string, active: boolean) => void;
  maxFps?: number;
  detail?: 'summary' | 'full';
  onStartScanAutoLock?: (deviceKey: string) => Promise<void>;
  autoLockBusy?: boolean;
  lockBusy?: boolean;
  onDisableLock?: (deviceKey: string) => Promise<void>;
};

type SetParamOptions = {
  optimistic?: boolean;
};

export const DeviceWorkspace = memo(function DeviceWorkspace({
  device,
  active,
  onStateUpdate,
  onStreamActiveChange,
  maxFps,
  detail = 'full',
  onStartScanAutoLock,
  autoLockBusy,
  lockBusy,
  onDisableLock,
}: DeviceWorkspaceProps) {
  const state = useDeviceStateEntry(device.key);
  const [selectionMode, setSelectionMode] = useState<'autolock' | 'optimization' | null>(null);
  const [selectionSubmitting, setSelectionSubmitting] = useState(false);
  const [lockMode, setLockMode] = useState<'manual' | 'autolock_scan' | 'autolock'>('manual');
  const [autoLockSettings, setAutoLockSettings] = useState<AutoLockScanSettings | null>(null);
  const [lockIndicatorConfig, setLockIndicatorConfig] = useState<LockIndicatorConfig | null>(null);
  const [lockIndicatorSaving, setLockIndicatorSaving] = useState(false);
  const [lockIndicatorError, setLockIndicatorError] = useState<string | null>(null);
  const [autoRelockConfig, setAutoRelockConfig] = useState<AutoRelockConfig | null>(null);
  const [autoRelockSaving, setAutoRelockSaving] = useState(false);
  const [autoRelockError, setAutoRelockError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const autoLockSettingsSaveTimerRef = useRef<number | null>(null);
  // Imperative handle into PlotPanel: parent pushes plot frames
  // directly to uPlot via ref, bypassing React state on the hot
  // path. See OverviewPlotPanel for the same pattern.
  const panelRef = useRef<PlotPanelHandle>(null);
  // Stash for ThrottledStatusRow to pull at 2 Hz.
  const latestFrameRef = useRef<PlotFrame | null>(null);
  // Narrow React state for things RightPanel + the card UI display.
  // Only updates when the underlying primitives change (state +
  // enabled, etc.), so streaming plot frames don't force per-frame
  // renders of the entire DeviceWorkspace subtree.
  const [lockIndicator, setLockIndicator] = useState<LockIndicatorSnapshot | null>(null);
  const [autoRelockStatus, setAutoRelockStatus] = useState<AutoRelockStatus | null>(null);
  // selectionMode lives below as React state. Mirror in ref so the
  // onMessage callback (which is memoized) can read latest without
  // re-binding -- and so plot-frame skip during freeze stays atomic
  // with the React state update.
  const selectionModeRef = useRef<'autolock' | 'optimization' | null>(null);
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
  const visible = useInViewport(rootRef, { disabled: !active });
  const streamEnabled = active && visible;

  // Narrowed, referentially-stable params for SweepControls. Only
  // changes identity when one of the three sweep fields changes, so
  // the memoized SweepControls skips the param-flood re-renders.
  const sweepParams = useMemo(
    () => ({
      sweep_center: state.params.sweep_center,
      sweep_amplitude: state.params.sweep_amplitude,
      sweep_pause: state.params.sweep_pause,
    }),
    [state.params.sweep_center, state.params.sweep_amplitude, state.params.sweep_pause],
  );

  // All handlers below are wrapped in useCallback with empty deps so
  // they keep a stable identity across renders -- a prerequisite for
  // the memoized RightPanel sub-panels to actually skip reconciliation
  // during the param flood on tab activation. They read the
  // frequently-changing values they need from this single "latest"
  // ref, refreshed on every render, instead of closing over them.
  const latestRef = useRef({
    connected,
    selectionMode,
    selectionSubmitting,
    lockMode,
    params: state.params,
    onStateUpdate,
    onStartScanAutoLock,
    onDisableLock,
  });
  latestRef.current = {
    connected,
    selectionMode,
    selectionSubmitting,
    lockMode,
    params: state.params,
    onStateUpdate,
    onStartScanAutoLock,
    onDisableLock,
  };

  const onMessage = useCallback(
    (msg: StreamMessage) => {
      if (msg.type === 'plot_frame') {
        latestFrameRef.current = msg;
        // During selection (autolock / optimization range pick) the
        // plot freezes -- skip pushing new frames to uPlot. When the
        // selection clears, the lockState / latest-frame useEffect on
        // PlotPanel handles re-application.
        if (selectionModeRef.current === null) {
          panelRef.current?.applyFrame(msg);
        }
        const nextIndicator = msg.lock_indicator ?? null;
        setLockIndicator((prev) =>
          indicatorEqual(prev, nextIndicator) ? prev : nextIndicator,
        );
        const nextAutoRelock = msg.auto_relock ?? null;
        setAutoRelockStatus((prev) =>
          autoRelockEqual(prev, nextAutoRelock) ? prev : nextAutoRelock,
        );
        // Smart-diff store write for bookkeeper (commit 75a46b7).
        onStateUpdate(device.key, msg);
        return;
      }
      if (msg.type === 'config_update') {
        if (msg.config_name === 'lock_indicator_config') {
          setLockIndicatorConfig(msg.value as LockIndicatorConfig);
          setLockIndicatorError(null);
        }
        if (msg.config_name === 'auto_lock_scan_settings') {
          // Don't let a (possibly self-originated) broadcast clobber an
          // unsaved local edit while a debounced save is still pending.
          if (autoLockSettingsSaveTimerRef.current === null) {
            setAutoLockSettings(msg.value as AutoLockScanSettings);
          }
        }
        if (msg.config_name === 'auto_relock_config') {
          setAutoRelockConfig(msg.value as AutoRelockConfig);
          setAutoRelockError(null);
        }
        // config_update is fully handled above; the store updater ignores it,
        // so don't fall through to a dead onStateUpdate call.
        return;
      }
      onStateUpdate(device.key, msg);
    },
    [device.key, onStateUpdate]
  );

  // Keep selectionModeRef in sync with the React state, and re-apply
  // the latest frame on transition out of selection so the plot
  // immediately catches up to the live stream (rather than staying
  // frozen on the pre-selection frame until the next message arrives).
  useEffect(() => {
    const previous = selectionModeRef.current;
    selectionModeRef.current = selectionMode;
    if (previous !== null && selectionMode === null && latestFrameRef.current) {
      panelRef.current?.applyFrame(latestFrameRef.current);
    }
  }, [selectionMode]);

  const handleStreamOpen = useCallback(() => {
    onStreamActiveChange?.(device.key, true);
  }, [device.key, onStreamActiveChange]);
  const handleStreamClose = useCallback(() => {
    onStreamActiveChange?.(device.key, false);
  }, [device.key, onStreamActiveChange]);

  useDeviceStream(device.key, streamEnabled, onMessage, {
    maxFps,
    detail,
    onOpen: handleStreamOpen,
    onClose: handleStreamClose,
  });

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
    // Ignore a backend-reported selection flag for a temporarily-disabled
    // feature — otherwise a stale/echoed flag would freeze the plot in a
    // selection mode the user can never exit (the feature's controls are off).
    const backendMode =
      Boolean(state.params.autolock_selection) && !autolockTemporarilyDisabled
        ? 'autolock'
        : Boolean(state.params.optimization_selection) && !optimizationTemporarilyDisabled
        ? 'optimization'
        : null;
    setSelectionMode((current) => (current === backendMode ? current : backendMode));
    if (backendMode === null) {
      setSelectionSubmitting(false);
    }
  }, [
    hasAutolockSelectionParam,
    hasOptimizationSelectionParam,
    state.params.autolock_selection,
    state.params.optimization_selection,
    autolockTemporarilyDisabled,
    optimizationTemporarilyDisabled,
  ]);

  const setParam = useCallback((
    name: string,
    value: any,
    writeRegisters = true,
    options?: SetParamOptions
  ) => {
    const l = latestRef.current;
    if (!l.connected) return;
    if (options?.optimistic !== false) {
      l.onStateUpdate(device.key, { type: 'param_update', name, value });
    }
    api.setParam(device.key, name, value, writeRegisters).catch(() => null);
  }, [device.key]);

  const clearSelection = useCallback(() => {
    setSelectionMode(null);
    setSelectionSubmitting(false);
    if (!latestRef.current.connected) return;
    setParam('autolock_selection', false, false);
    setParam('optimization_selection', false, false);
  }, [setParam]);

  const startAutolockSelection = useCallback(() => {
    if (autolockTemporarilyDisabled) {
      setSelectionMode(null);
      setSelectionSubmitting(false);
      return;
    }
    setSelectionMode('autolock');
    setSelectionSubmitting(false);
    if (!latestRef.current.connected) return;
    setParam('autolock_selection', true, false);
    setParam('optimization_selection', false, false);
    setParam('automatic_mode', true, false);
  }, [autolockTemporarilyDisabled, setParam]);

  const startOptimizationSelection = useCallback(() => {
    if (optimizationTemporarilyDisabled) {
      setSelectionMode(null);
      setSelectionSubmitting(false);
      return;
    }
    setSelectionMode('optimization');
    setSelectionSubmitting(false);
    if (!latestRef.current.connected) return;
    setParam('optimization_selection', true, false);
    setParam('autolock_selection', false, false);
  }, [optimizationTemporarilyDisabled, setParam]);

  const handleSelectRange = useCallback(async (x0: number, x1: number) => {
    const l = latestRef.current;
    if (!l.connected) return;
    if (l.selectionMode === null || l.selectionSubmitting) return;
    if (
      (l.selectionMode === 'autolock' && autolockTemporarilyDisabled) ||
      (l.selectionMode === 'optimization' && optimizationTemporarilyDisabled)
    ) {
      return;
    }
    const min = Math.max(0, Math.min(2047, x0));
    const max = Math.max(0, Math.min(2047, x1));
    const mode = l.selectionMode;
    setSelectionSubmitting(true);
    try {
      if (mode === 'autolock') {
        await api.startAutolock(device.key, min, max);
      }
      if (mode === 'optimization') {
        await api.startOptimization(device.key, min, max);
      }
      clearSelection();
    } catch {
      // Errors from this legacy flow are not user-visible while it remains disabled.
    } finally {
      setSelectionSubmitting(false);
    }
  }, [autolockTemporarilyDisabled, optimizationTemporarilyDisabled, clearSelection, device.key]);

  const handleStartLock = useCallback(async () => {
    const l = latestRef.current;
    if (!l.connected) return;
    try {
      const centerRaw = l.params.sweep_center;
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
  }, [device.key]);

  const handleAutoLockFromScan = useCallback(async (
    settings: AutoLockScanSettings
  ): Promise<AutoLockScanResult> => {
    if (!latestRef.current.connected) {
      throw new Error('Device not connected.');
    }
    return api.autoLockFromScan(device.key, settings);
  }, [device.key]);

  const handleStartScanAutoLock = useCallback(() => {
    const l = latestRef.current;
    if (!l.connected) return;
    if (l.onStartScanAutoLock) {
      l.onStartScanAutoLock(device.key).catch(() => null);
      return;
    }
    api.getAutoLockScanSettings(device.key)
      .then((settings) => api.autoLockFromScan(device.key, settings))
      .catch(() => null);
  }, [device.key]);

  const handleStopLock = useCallback(() => {
    const l = latestRef.current;
    if (!l.connected) return;
    if (l.onDisableLock) {
      l.onDisableLock(device.key).catch(() => null);
      return;
    }
    api.stopLock(device.key).catch(() => null);
  }, [device.key]);

  const handleCalibrateAutoLock = useCallback(async (
    options: AutoLockCalibrateRequest
  ): Promise<AutoLockCalibrationResult> => {
    if (!latestRef.current.connected) {
      throw new Error('Device not connected.');
    }
    // Cancel any pending debounced settings save so it can't overwrite the
    // freshly calibrated values after they land.
    if (autoLockSettingsSaveTimerRef.current !== null) {
      window.clearTimeout(autoLockSettingsSaveTimerRef.current);
      autoLockSettingsSaveTimerRef.current = null;
    }
    const result = await api.calibrateAutoLockScanSettings(device.key, options);
    setAutoLockSettings(result.settings);
    return result;
  }, [device.key]);

  const handleAutoLockSettingsChange = useCallback((settings: AutoLockScanSettings) => {
    setAutoLockSettings(settings);
    if (autoLockSettingsSaveTimerRef.current !== null) {
      window.clearTimeout(autoLockSettingsSaveTimerRef.current);
    }
    autoLockSettingsSaveTimerRef.current = window.setTimeout(() => {
      api.updateAutoLockScanSettings(device.key, settings).catch(() => null);
      autoLockSettingsSaveTimerRef.current = null;
    }, 250);
  }, [device.key]);

  const handleSaveLockIndicatorConfig = useCallback(async (config: LockIndicatorConfig) => {
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
  }, [device.key]);

  const handleSaveAutoRelockConfig = useCallback(async (config: AutoRelockConfig) => {
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
  }, [device.key]);

  // Stable wrappers for the small inline callbacks RightPanel passes
  // down, so the memoized panels see stable identities.
  const handleStopTask = useCallback(
    (useNew: boolean) => api.stopTask(device.key, useNew).catch(() => null),
    [device.key],
  );
  const handleLockModeChange = useCallback(
    (mode: 'manual' | 'autolock_scan' | 'autolock') => {
      setLockMode(mode);
      if (!latestRef.current.connected) return;
      setParam('automatic_mode', mode === 'autolock', false);
    },
    [setParam],
  );

  return (
    <div ref={rootRef}>
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
          <SweepControls params={sweepParams} onSetParam={setParam} />
          <PlotPanel
            ref={panelRef}
            selectionMode={selectionMode}
            onSelectRange={handleSelectRange}
            lockState={lockState}
            sweepCenter={sweepCenter}
            sweepAmplitude={sweepAmplitude}
            showManualTarget
            initActive={streamEnabled}
          />
          <ThrottledStatusRow
            frameRef={latestFrameRef}
            intervalMs={500}
            lockIndicator={lockIndicator}
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
            onCalibrateAutoLock={handleCalibrateAutoLock}
            autoLockSettings={autoLockSettings}
            onAutoLockSettingsChange={handleAutoLockSettingsChange}
            onStartOptimizationSelection={startOptimizationSelection}
            onAbortOptimizationSelection={clearSelection}
            onStopTask={handleStopTask}
            onStopLock={handleStopLock}
            onStartScanAutoLock={handleStartScanAutoLock}
            connected={connected}
            lockEnabled={lockState === true}
            autoLockBusy={autoLockBusy}
            lockBusy={lockBusy}
            lockMode={lockMode}
            onLockModeChange={handleLockModeChange}
            selectionMode={selectionMode}
            selectionSubmitting={selectionSubmitting}
            autolockTemporarilyDisabled={autolockTemporarilyDisabled}
            optimizationTemporarilyDisabled={optimizationTemporarilyDisabled}
            automationDisableReason={AUTOMATION_TEMP_DISABLED_REASON}
            lockIndicatorConfig={lockIndicatorConfig}
            lockIndicatorSaving={lockIndicatorSaving}
            lockIndicatorError={lockIndicatorError}
            onSaveLockIndicatorConfig={handleSaveLockIndicatorConfig}
            lockIndicatorSnapshot={lockIndicator}
            autoRelockConfig={autoRelockConfig}
            autoRelockStatus={autoRelockStatus ?? state.status?.auto_relock ?? null}
            autoRelockSaving={autoRelockSaving}
            autoRelockError={autoRelockError}
            onSaveAutoRelockConfig={handleSaveAutoRelockConfig}
          />
        </div>
      </div>
    </div>
  );
});
