import { useEffect, useState } from 'react';
import { Button, Group, NumberInput, Select, Stack, Switch, Text } from '@mantine/core';
import type { LockIndicatorConfig, LockIndicatorSnapshot } from '../types';
import { toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';

const DEFAULT_LOCK_INDICATOR_CONFIG: LockIndicatorConfig = {
  enabled: true,
  bad_hold_s: 1.0,
  good_hold_s: 2.0,
  use_control: true,
  control_stuck_delta_counts: 0,
  control_stuck_time_s: 1.5,
  control_rail_threshold_v: 0.9,
  control_rail_hold_s: 1.0,
  use_error: true,
  error_mean_abs_max_v: 0.2,
  error_std_min_v: 0.001,
  error_std_max_v: 0.8,
  use_monitor: false,
  monitor_mode: 'locked_above',
  monitor_threshold_v: 0.0,
};

type LockIndicatorPanelProps = {
  config?: LockIndicatorConfig | null;
  saving?: boolean;
  error?: string | null;
  snapshot?: LockIndicatorSnapshot | null;
  onSaveConfig?: (config: LockIndicatorConfig) => Promise<void>;
};

export function LockIndicatorPanel({
  config,
  saving,
  error,
  snapshot,
  onSaveConfig,
}: LockIndicatorPanelProps) {
  const [draft, setDraft] = useState<LockIndicatorConfig>(
    config ?? DEFAULT_LOCK_INDICATOR_CONFIG
  );
  const [dirty, setDirty] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const lockIndicatorState = snapshot?.state ?? 'unknown';
  const lockIndicatorReason =
    snapshot?.reasons && snapshot.reasons.length > 0 ? snapshot.reasons[0] : null;

  useEffect(() => {
    if (!config) return;
    setDraft(config);
    setDirty(false);
  }, [config]);

  const updateField = <K extends keyof LockIndicatorConfig>(name: K, value: LockIndicatorConfig[K]) => {
    setDraft((prev) => ({ ...prev, [name]: value }));
    setDirty(true);
  };

  const save = async () => {
    if (!onSaveConfig) return;
    setLocalError(null);
    try {
      await onSaveConfig(draft);
      setDirty(false);
    } catch (saveError) {
      setLocalError(
        saveError instanceof Error && saveError.message
          ? saveError.message
          : 'Failed to save lock-indicator settings.'
      );
    }
  };

  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        state={lockIndicatorState}
        {lockIndicatorReason ? ` | reason=${lockIndicatorReason}` : ''}
      </Text>
      <Switch
        label="Enable lock indicator"
        checked={draft.enabled}
        onChange={(event) => updateField('enabled', event.currentTarget.checked)}
      />
      <Group grow>
        <NumberInput
          label="Bad hold (s)"
          value={draft.bad_hold_s}
          min={0.05}
          step={0.1}
          onChange={(value) => updateField('bad_hold_s', toFiniteNumberOr(value, 1.0))}
        />
        <NumberInput
          label="Good hold (s)"
          value={draft.good_hold_s}
          min={0.05}
          step={0.1}
          onChange={(value) => updateField('good_hold_s', toFiniteNumberOr(value, 2.0))}
        />
      </Group>
      <Switch
        label="Use control signal checks"
        checked={draft.use_control}
        onChange={(event) => updateField('use_control', event.currentTarget.checked)}
      />
      <Group grow>
        <NumberInput
          label="Control stuck Delta (counts)"
          value={draft.control_stuck_delta_counts}
          min={0}
          step={1}
          onChange={(value) =>
            updateField('control_stuck_delta_counts', toRoundedIntOr(value, 0, 0))
          }
          disabled={!draft.use_control}
        />
        <NumberInput
          label="Control stuck time (s)"
          value={draft.control_stuck_time_s}
          min={0.05}
          step={0.1}
          onChange={(value) => updateField('control_stuck_time_s', toFiniteNumberOr(value, 1.5))}
          disabled={!draft.use_control}
        />
      </Group>
      <Group grow>
        <NumberInput
          label="Control rail threshold (V)"
          value={draft.control_rail_threshold_v}
          min={0}
          max={1.2}
          step={0.01}
          decimalScale={3}
          onChange={(value) =>
            updateField('control_rail_threshold_v', toFiniteNumberOr(value, 0.9))
          }
          disabled={!draft.use_control}
        />
        <NumberInput
          label="Control rail hold (s)"
          value={draft.control_rail_hold_s}
          min={0.05}
          step={0.1}
          onChange={(value) => updateField('control_rail_hold_s', toFiniteNumberOr(value, 1.0))}
          disabled={!draft.use_control}
        />
      </Group>
      <Switch
        label="Use error signal checks"
        checked={draft.use_error}
        onChange={(event) => updateField('use_error', event.currentTarget.checked)}
      />
      <Group grow>
        <NumberInput
          label="|Error mean| max (V)"
          value={draft.error_mean_abs_max_v}
          min={0}
          step={0.01}
          decimalScale={4}
          onChange={(value) =>
            updateField('error_mean_abs_max_v', toFiniteNumberOr(value, 0.2))
          }
          disabled={!draft.use_error}
        />
        <NumberInput
          label="Error std min (V)"
          value={draft.error_std_min_v}
          min={0}
          step={0.001}
          decimalScale={4}
          onChange={(value) =>
            updateField('error_std_min_v', toFiniteNumberOr(value, 0.001))
          }
          disabled={!draft.use_error}
        />
        <NumberInput
          label="Error std max (V)"
          value={draft.error_std_max_v}
          min={0}
          step={0.01}
          decimalScale={4}
          onChange={(value) =>
            updateField('error_std_max_v', toFiniteNumberOr(value, 0.8))
          }
          disabled={!draft.use_error}
        />
      </Group>
      <Switch
        label="Use monitor checks"
        checked={draft.use_monitor}
        onChange={(event) => updateField('use_monitor', event.currentTarget.checked)}
      />
      <Group grow>
        <Select
          label="Monitor mode"
          data={[
            { value: 'locked_above', label: 'Locked above threshold' },
            { value: 'locked_below', label: 'Locked below threshold' },
          ]}
          value={draft.monitor_mode}
          onChange={(value) =>
            updateField('monitor_mode', (value as LockIndicatorConfig['monitor_mode']) ?? 'locked_above')
          }
          disabled={!draft.use_monitor}
        />
        <NumberInput
          label="Monitor threshold (V)"
          value={draft.monitor_threshold_v}
          step={0.01}
          decimalScale={4}
          onChange={(value) =>
            updateField('monitor_threshold_v', toFiniteNumberOr(value, 0.0))
          }
          disabled={!draft.use_monitor}
        />
      </Group>
      <Button
        variant="light"
        color="orange"
        onClick={() => {
          save().catch(() => null);
        }}
        disabled={!onSaveConfig || !dirty}
        loading={Boolean(saving)}
      >
        Save lock-indicator settings
      </Button>
      {error ? (
        <Text size="xs" c="red">
          {error}
        </Text>
      ) : null}
      {localError ? (
        <Text size="xs" c="red">
          {localError}
        </Text>
      ) : null}
    </Stack>
  );
}
