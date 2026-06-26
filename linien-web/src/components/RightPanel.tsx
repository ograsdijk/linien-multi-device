import { Accordion, Button, Group } from '@mantine/core';
import type {
  AutoRelockConfig,
  AutoRelockStatus,
  AutoLockCalibrateRequest,
  AutoLockCalibrationResult,
  AutoLockScanResult,
  AutoLockScanSettings,
  LockIndicatorConfig,
  LockIndicatorSnapshot,
} from '../types';
import { useStablePick } from '../hooks/useStablePick';
import { AutoRelockPanel } from './AutoRelockPanel';
import { GeneralPanel } from './GeneralPanel';
import { LockIndicatorPanel } from './LockIndicatorPanel';
import { LockingPanel } from './LockingPanel';
import { ModSweepPanel } from './ModSweepPanel';
import { OptimizationPanel } from './OptimizationPanel';

// Per-panel param key lists -- the exact fields each panel reads. Used
// by useStablePick to hand each memoized panel a referentially-stable
// params object that only changes when one of ITS fields changes, so
// the param flood on tab activation doesn't reconcile every panel's
// inputs. Keep in sync with the `params.X` reads in each panel.
const GENERAL_KEYS = [
  'dual_channel', 'pid_only_mode', 'channel_mixing', 'mod_channel',
  'control_channel', 'sweep_channel', 'pid_on_slow_enabled',
  'slow_control_channel', 'polarity_fast_out1', 'polarity_fast_out2',
  'polarity_analog_out0', 'analog_out_1', 'analog_out_2', 'analog_out_3',
] as const;
const MODSWEEP_KEYS = [
  'dual_channel', 'pid_only_mode', 'modulation_amplitude',
  'modulation_frequency', 'sweep_speed',
  // Per-channel demod params read via bracket notation (suffix _a/_b).
  'demodulation_multiplier_a', 'demodulation_multiplier_b',
  'demodulation_phase_a', 'demodulation_phase_b',
  'offset_a', 'offset_b',
  'invert_a', 'invert_b',
  'filter_automatic_a', 'filter_automatic_b',
  'filter_1_enabled_a', 'filter_1_enabled_b',
  'filter_2_enabled_a', 'filter_2_enabled_b',
  'filter_1_type_a', 'filter_1_type_b',
  'filter_2_type_a', 'filter_2_type_b',
  'filter_1_frequency_a', 'filter_1_frequency_b',
  'filter_2_frequency_a', 'filter_2_frequency_b',
] as const;
const OPTIMIZATION_KEYS = [
  'demodulation_phase_a', 'demodulation_phase_b', 'dual_channel',
  'modulation_amplitude', 'modulation_frequency', 'optimization_approaching',
  'optimization_channel', 'optimization_failed', 'optimization_improvement',
  'optimization_mod_amp_enabled', 'optimization_mod_amp_max',
  'optimization_mod_amp_min', 'optimization_mod_freq_enabled',
  'optimization_mod_freq_max', 'optimization_mod_freq_min',
  'optimization_optimized_parameters', 'optimization_running',
  'optimization_selection', 'pid_only_mode',
] as const;
const LOCKING_KEYS = [
  'autolock_determine_offset', 'autolock_failed', 'autolock_mode_preference',
  'autolock_running', 'd', 'i', 'p', 'target_slope_rising',
] as const;

type RightPanelProps = {
  deviceKey: string;
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
  onStartLock: () => void;
  onStartAutolockSelection: () => void;
  onAbortAutolockSelection: () => void;
  onAutoLockFromScan: (
    settings: AutoLockScanSettings
  ) => Promise<AutoLockScanResult>;
  onCalibrateAutoLock?: (
    options: AutoLockCalibrateRequest
  ) => Promise<AutoLockCalibrationResult>;
  autoLockSettings?: AutoLockScanSettings | null;
  onAutoLockSettingsChange?: (settings: AutoLockScanSettings) => void;
  onStartOptimizationSelection: () => void;
  onAbortOptimizationSelection: () => void;
  onStopTask: (useNew: boolean) => void;
  onStopLock: () => void;
  onStartScanAutoLock: () => void;
  connected?: boolean;
  lockEnabled?: boolean;
  autoLockBusy?: boolean;
  lockBusy?: boolean;
  lockMode?: 'manual' | 'autolock_scan' | 'autolock';
  onLockModeChange?: (mode: 'manual' | 'autolock_scan' | 'autolock') => void;
  selectionMode?: 'autolock' | 'optimization' | null;
  selectionSubmitting?: boolean;
  autolockTemporarilyDisabled?: boolean;
  optimizationTemporarilyDisabled?: boolean;
  automationDisableReason?: string;
  lockIndicatorConfig?: LockIndicatorConfig | null;
  lockIndicatorSaving?: boolean;
  lockIndicatorError?: string | null;
  onSaveLockIndicatorConfig?: (config: LockIndicatorConfig) => Promise<void>;
  lockIndicatorSnapshot?: LockIndicatorSnapshot | null;
  autoRelockConfig?: AutoRelockConfig | null;
  autoRelockStatus?: AutoRelockStatus | null;
  autoRelockStalled?: boolean;
  autoRelockSaving?: boolean;
  autoRelockError?: string | null;
  onSaveAutoRelockConfig?: (config: AutoRelockConfig) => Promise<void>;
};

export function RightPanel(props: RightPanelProps) {
  const connected = Boolean(props.connected);
  const lockEnabled = Boolean(props.lockEnabled);

  // Stable narrowed param slices, one per memoized panel. RightPanel
  // itself still re-renders when DeviceWorkspace does (its `params`
  // ref changes every store write), but its render is just the
  // Accordion shell -- the heavy input trees live in the memoized
  // children, which skip when their slice is unchanged.
  const generalParams = useStablePick(props.params, GENERAL_KEYS);
  const modSweepParams = useStablePick(props.params, MODSWEEP_KEYS);
  const optimizationParams = useStablePick(props.params, OPTIMIZATION_KEYS);
  const lockingParams = useStablePick(props.params, LOCKING_KEYS);

  return (
    <div className="panel" style={{ padding: 12 }}>
      <Accordion multiple defaultValue={[]}>
        <Accordion.Item value="general">
          <Accordion.Control>General</Accordion.Control>
          <Accordion.Panel>
            <GeneralPanel params={generalParams} onSetParam={props.onSetParam} />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="mod">
          <Accordion.Control>Modulation / Sweep</Accordion.Control>
          <Accordion.Panel>
            <ModSweepPanel params={modSweepParams} onSetParam={props.onSetParam} />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="optimization">
          <Accordion.Control>Optimization</Accordion.Control>
          <Accordion.Panel>
            <OptimizationPanel
              params={optimizationParams}
              onSetParam={props.onSetParam}
              onStartSelection={props.onStartOptimizationSelection}
              onAbortSelection={props.onAbortOptimizationSelection}
              onStopTask={props.onStopTask}
              selectionActive={props.selectionMode === 'optimization'}
              selectionSubmitting={props.selectionSubmitting}
              optimizationTemporarilyDisabled={props.optimizationTemporarilyDisabled}
              disableReason={props.automationDisableReason}
            />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="locking">
          <Accordion.Control>Locking</Accordion.Control>
          <Accordion.Panel>
            <LockingPanel
              params={lockingParams}
              onSetParam={props.onSetParam}
              onStartLock={props.onStartLock}
              onStartAutolockSelection={props.onStartAutolockSelection}
              onAbortAutolockSelection={props.onAbortAutolockSelection}
              onAutoLockFromScan={props.onAutoLockFromScan}
              onCalibrateAutoLock={props.onCalibrateAutoLock}
              autoLockSettingsConfig={props.autoLockSettings}
              onAutoLockSettingsChange={props.onAutoLockSettingsChange}
              onStopLock={props.onStopLock}
              lockMode={props.lockMode}
              onLockModeChange={props.onLockModeChange}
              autolockSelectionActive={props.selectionMode === 'autolock'}
              selectionSubmitting={props.selectionSubmitting}
              autolockTemporarilyDisabled={props.autolockTemporarilyDisabled}
              disableReason={props.automationDisableReason}
            />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="lock-indicator">
          <Accordion.Control>Lock indicator</Accordion.Control>
          <Accordion.Panel>
            <LockIndicatorPanel
              config={props.lockIndicatorConfig}
              saving={props.lockIndicatorSaving}
              error={props.lockIndicatorError}
              onSaveConfig={props.onSaveLockIndicatorConfig}
              snapshot={props.lockIndicatorSnapshot}
            />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="auto-relock">
          <Accordion.Control>Auto relock</Accordion.Control>
          <Accordion.Panel>
            <AutoRelockPanel
              config={props.autoRelockConfig}
              status={props.autoRelockStatus}
              stalled={props.autoRelockStalled}
              saving={props.autoRelockSaving}
              error={props.autoRelockError}
              onSaveConfig={props.onSaveAutoRelockConfig}
            />
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
      <Group grow mt="md">
        {lockEnabled ? (
          <Button
            color="red"
            variant="light"
            onClick={props.onStopLock}
            disabled={!connected || props.lockBusy}
            loading={props.lockBusy}
          >
            Disable lock
          </Button>
        ) : (
          <Button
            color="green"
            variant="light"
            onClick={props.onStartLock}
            disabled={!connected || props.lockBusy}
            loading={props.lockBusy}
          >
            Manual lock
          </Button>
        )}
        <Button
          color="blue"
          variant="light"
          onClick={props.onStartScanAutoLock}
          disabled={!connected || lockEnabled || props.autoLockBusy}
          loading={props.autoLockBusy}
        >
          Auto-lock
        </Button>
      </Group>
    </div>
  );
}
