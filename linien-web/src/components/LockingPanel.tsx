import { useEffect, useId, useState } from 'react';
import {
  Button,
  Divider,
  Group,
  NumberInput,
  Radio,
  Select,
  Stack,
  Switch,
  Tabs,
  Text,
} from '@mantine/core';
import type {
  AutoLockScanResult,
  AutoLockScanSettings,
} from '../types';
import { toClampedNumberOr, toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';

const DEFAULT_AUTO_LOCK_SETTINGS: AutoLockScanSettings = {
  half_range_v: 0.08,
  crossing_max_v: 0.03,
  error_min: 0.08,
  symmetry_min: 0.2,
  allow_single_side: false,
  single_error_min: 0.1,
  smooth_window_pts: 5,
  use_monitor: false,
  monitor_contrast_min_v: 0.03,
};

type LockingPanelProps = {
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
  onStartLock: () => void;
  onStartAutolockSelection: () => void;
  onAbortAutolockSelection: () => void;
  onAutoLockFromScan: (
    settings: AutoLockScanSettings
  ) => Promise<AutoLockScanResult>;
  autoLockSettingsConfig?: AutoLockScanSettings | null;
  onAutoLockSettingsChange?: (settings: AutoLockScanSettings) => void;
  onStopLock: () => void;
  lockMode?: 'manual' | 'autolock_scan' | 'autolock';
  onLockModeChange?: (mode: 'manual' | 'autolock_scan' | 'autolock') => void;
  autolockSelectionActive?: boolean;
  selectionSubmitting?: boolean;
  autolockTemporarilyDisabled?: boolean;
  disableReason?: string;
};

export function LockingPanel({
  params,
  onSetParam,
  onStartLock,
  onStartAutolockSelection,
  onAbortAutolockSelection,
  onAutoLockFromScan,
  autoLockSettingsConfig,
  onAutoLockSettingsChange,
  onStopLock,
  lockMode,
  onLockModeChange,
  autolockSelectionActive,
  selectionSubmitting,
  autolockTemporarilyDisabled,
  disableReason,
}: LockingPanelProps) {
  const slopeParam = params.target_slope_rising;
  const slopeValue = slopeParam === false || slopeParam === 0 ? 'falling' : 'rising';
  const slopeGroupName = useId();
  const autolockRunning = Boolean(params.autolock_running);
  const autolockFailed = Boolean(params.autolock_failed);
  const autolockModePreference = params.autolock_mode_preference ?? 0;
  const determineOffset = Boolean(params.autolock_determine_offset);
  const mode = lockMode ?? 'manual';
  const selectionArmed = Boolean(autolockSelectionActive);
  const slopeLabel = slopeValue === 'rising' ? 'Rising' : 'Falling';
  const disabledReasonText =
    disableReason ?? 'Temporarily disabled due to compatibility issue.';

  const [autoLockSettings, setAutoLockSettings] = useState<AutoLockScanSettings>(
    autoLockSettingsConfig ?? DEFAULT_AUTO_LOCK_SETTINGS
  );
  const [autoLockBusy, setAutoLockBusy] = useState(false);
  const [autoLockResult, setAutoLockResult] = useState<AutoLockScanResult | null>(null);

  useEffect(() => {
    if (!autoLockSettingsConfig) return;
    setAutoLockSettings(autoLockSettingsConfig);
  }, [autoLockSettingsConfig]);

  const updateAutoLockSettings = (
    patch:
      | Partial<AutoLockScanSettings>
      | ((current: AutoLockScanSettings) => AutoLockScanSettings)
  ) => {
    setAutoLockSettings((prev) => {
      const next =
        typeof patch === 'function'
          ? patch(prev)
          : { ...prev, ...patch };
      onAutoLockSettingsChange?.(next);
      return next;
    });
  };

  const setAutoLockNumber = (name: keyof AutoLockScanSettings, value: number) => {
    updateAutoLockSettings({ [name]: value } as Partial<AutoLockScanSettings>);
  };

  const runAutoLockFromScan = async () => {
    setAutoLockBusy(true);
    try {
      const result = await onAutoLockFromScan(autoLockSettings);
      setAutoLockResult(result);
    } catch (_error) {
      // Errors are surfaced through global logs + toast notifications.
    } finally {
      setAutoLockBusy(false);
    }
  };

  return (
    <Stack gap="sm">
      <Text fw={600}>PID</Text>
      <Group grow>
        <NumberInput
          label="P"
          value={params.p ?? 0}
          onChange={(value) => onSetParam('p', toFiniteNumberOr(value, params.p ?? 0), true)}
        />
        <NumberInput
          label="I"
          value={params.i ?? 0}
          onChange={(value) => onSetParam('i', toFiniteNumberOr(value, params.i ?? 0), true)}
        />
        <NumberInput
          label="D"
          value={params.d ?? 0}
          onChange={(value) => onSetParam('d', toFiniteNumberOr(value, params.d ?? 0), true)}
        />
      </Group>
      <Radio.Group
        name={`slope-${slopeGroupName}`}
        label="Target slope (applies to all lock modes)"
        value={slopeValue}
        onChange={(value) => {
          const next = value === 'rising';
          onSetParam('target_slope_rising', next, false);
        }}
      >
        <Group mt="xs">
          <Radio value="rising" label="Rising" />
          <Radio value="falling" label="Falling" />
        </Group>
      </Radio.Group>
      <Divider />
      <Tabs
        value={mode}
        onChange={(value) => {
          if (!value) return;
          onLockModeChange?.(value as 'manual' | 'autolock_scan' | 'autolock');
        }}
        variant="outline"
      >
        <Tabs.List>
          <Tabs.Tab value="manual">Manual</Tabs.Tab>
          <Tabs.Tab value="autolock_scan">Autolock</Tabs.Tab>
          <Tabs.Tab value="autolock">Autolock dev</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="manual" pt="xs">
          <Stack gap="sm">
            <Button color="green" variant="light" onClick={onStartLock}>
              Start lock
            </Button>
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="autolock_scan" pt="xs">
          <Stack gap="sm">
            <Text fw={600} size="sm">
              Auto-lock from scan
            </Text>
            <Text size="xs" c="dimmed">
              Current target slope: {slopeLabel}
            </Text>
            <Group grow>
              <NumberInput
                label="Half range (V)"
                value={autoLockSettings.half_range_v}
                min={0.001}
                step={0.01}
                decimalScale={3}
                onChange={(value) =>
                  setAutoLockNumber(
                    'half_range_v',
                    toFiniteNumberOr(value, DEFAULT_AUTO_LOCK_SETTINGS.half_range_v)
                  )
                }
              />
              <NumberInput
                label="Smooth (pts)"
                value={autoLockSettings.smooth_window_pts}
                min={1}
                max={101}
                step={2}
                onChange={(value) =>
                  setAutoLockNumber(
                    'smooth_window_pts',
                    toRoundedIntOr(value, DEFAULT_AUTO_LOCK_SETTINGS.smooth_window_pts, 1)
                  )
                }
              />
            </Group>
            <Group grow>
              <NumberInput
                label="Crossing max (V)"
                value={autoLockSettings.crossing_max_v}
                min={0.0001}
                step={0.01}
                decimalScale={4}
                onChange={(value) =>
                  setAutoLockNumber(
                    'crossing_max_v',
                    toFiniteNumberOr(value, DEFAULT_AUTO_LOCK_SETTINGS.crossing_max_v)
                  )
                }
              />
              <NumberInput
                label="Error min (V)"
                value={autoLockSettings.error_min}
                min={0.0001}
                step={0.01}
                decimalScale={4}
                onChange={(value) =>
                  setAutoLockNumber(
                    'error_min',
                    toFiniteNumberOr(value, DEFAULT_AUTO_LOCK_SETTINGS.error_min)
                  )
                }
              />
            </Group>
            <Group grow>
              <NumberInput
                label="Symmetry min"
                value={autoLockSettings.symmetry_min}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
                onChange={(value) =>
                  setAutoLockNumber(
                    'symmetry_min',
                    toClampedNumberOr(value, DEFAULT_AUTO_LOCK_SETTINGS.symmetry_min, 0, 1)
                  )
                }
              />
              <NumberInput
                label="Single error min (V)"
                value={autoLockSettings.single_error_min}
                min={0.0001}
                step={0.01}
                decimalScale={4}
                onChange={(value) =>
                  setAutoLockNumber(
                    'single_error_min',
                    toFiniteNumberOr(value, DEFAULT_AUTO_LOCK_SETTINGS.single_error_min)
                  )
                }
                disabled={!autoLockSettings.allow_single_side}
              />
            </Group>
            <Switch
              label="Allow single-side acceptance"
              checked={autoLockSettings.allow_single_side}
              onChange={(event) =>
                updateAutoLockSettings((prev) => ({
                  ...prev,
                  allow_single_side: event.currentTarget.checked,
                }))
              }
            />
            <Switch
              label="Use monitor"
              checked={autoLockSettings.use_monitor}
              onChange={(event) =>
                updateAutoLockSettings((prev) => ({
                  ...prev,
                  use_monitor: event.currentTarget.checked,
                }))
              }
            />
            {autoLockSettings.use_monitor ? (
              <NumberInput
                label="Monitor contrast min (V)"
                value={autoLockSettings.monitor_contrast_min_v}
                min={0.0001}
                step={0.01}
                decimalScale={4}
                onChange={(value) =>
                  setAutoLockNumber(
                    'monitor_contrast_min_v',
                    toFiniteNumberOr(value, DEFAULT_AUTO_LOCK_SETTINGS.monitor_contrast_min_v)
                  )
                }
              />
            ) : null}
            <Button
              variant="light"
              color="blue"
              onClick={() => {
                runAutoLockFromScan().catch(() => null);
              }}
              loading={autoLockBusy}
            >
              Auto-lock from scan
            </Button>
            {autoLockResult ? (
              <Text size="xs" c="dimmed">
                target={autoLockResult.target_voltage.toFixed(4)} V (idx {autoLockResult.target_index})
                {' | '}score={autoLockResult.score.toFixed(3)} | pair=
                {autoLockResult.pair_excursion_v.toFixed(3)} V | symmetry=
                {autoLockResult.symmetry.toFixed(3)}
              </Text>
            ) : null}
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="autolock" pt="xs">
          <Stack gap="sm">
            <Select
              label="Autolock algorithm"
              data={[
                { value: '0', label: 'Auto-detect' },
                { value: '1', label: 'Robust mode' },
                { value: '2', label: 'Simple mode' },
              ]}
              value={String(autolockModePreference)}
              onChange={(value) => {
                if (value == null) return;
                onSetParam('autolock_mode_preference', toRoundedIntOr(value, 0, 0), false);
              }}
              disabled={autolockTemporarilyDisabled}
            />
            <Switch
              label="Determine signal offset"
              checked={determineOffset}
              onChange={(event) =>
                onSetParam('autolock_determine_offset', event.currentTarget.checked, false)
              }
              disabled={autolockTemporarilyDisabled}
            />
            {autolockTemporarilyDisabled ? (
              <Text size="xs" c="red">
                {disabledReasonText}
              </Text>
            ) : null}
            {!selectionArmed ? (
              <Button
                variant="light"
                color="orange"
                onClick={onStartAutolockSelection}
                disabled={selectionSubmitting || autolockTemporarilyDisabled}
              >
                Select target line
              </Button>
            ) : (
              <Stack gap="xs">
                <Text size="sm" fw={500}>
                  Click and drag over the line you want to lock to.
                </Text>
                <Button
                  variant="default"
                  onClick={onAbortAutolockSelection}
                  disabled={selectionSubmitting || autolockTemporarilyDisabled}
                >
                  Abort
                </Button>
              </Stack>
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>
      <Group grow>
        <Button variant="outline" color="red" onClick={onStopLock}>
          Stop lock
        </Button>
      </Group>
      <Divider />
      <Text size="sm">
        Autolock: {autolockRunning ? 'Running' : autolockFailed ? 'Failed' : 'Idle'}
      </Text>
    </Stack>
  );
}
