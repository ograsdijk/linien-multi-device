import { Accordion, Button, Group } from '@mantine/core';
import type {
  AutoRelockConfig,
  AutoRelockStatus,
  AutoLockScanResult,
  AutoLockScanSettings,
  LockIndicatorConfig,
  LockIndicatorSnapshot,
} from '../types';
import { AutoRelockPanel } from './AutoRelockPanel';
import { GeneralPanel } from './GeneralPanel';
import { LockIndicatorPanel } from './LockIndicatorPanel';
import { LockingPanel } from './LockingPanel';
import { ModSweepPanel } from './ModSweepPanel';
import { OptimizationPanel } from './OptimizationPanel';

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
  autoRelockSaving?: boolean;
  autoRelockError?: string | null;
  onSaveAutoRelockConfig?: (config: AutoRelockConfig) => Promise<void>;
};

export function RightPanel(props: RightPanelProps) {
  const connected = Boolean(props.connected);
  const lockEnabled = Boolean(props.lockEnabled);

  return (
    <div className="panel" style={{ padding: 12 }}>
      <Accordion multiple defaultValue={[]}>
        <Accordion.Item value="general">
          <Accordion.Control>General</Accordion.Control>
          <Accordion.Panel>
            <GeneralPanel params={props.params} onSetParam={props.onSetParam} />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="mod">
          <Accordion.Control>Modulation / Sweep</Accordion.Control>
          <Accordion.Panel>
            <ModSweepPanel params={props.params} onSetParam={props.onSetParam} />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="optimization">
          <Accordion.Control>Optimization</Accordion.Control>
          <Accordion.Panel>
            <OptimizationPanel
              params={props.params}
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
              params={props.params}
              onSetParam={props.onSetParam}
              onStartLock={props.onStartLock}
              onStartAutolockSelection={props.onStartAutolockSelection}
              onAbortAutolockSelection={props.onAbortAutolockSelection}
              onAutoLockFromScan={props.onAutoLockFromScan}
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
