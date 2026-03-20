import { useEffect, useState } from 'react';
import { Button, Group, NumberInput, Stack, Switch, Text } from '@mantine/core';
import type { AutoRelockConfig, AutoRelockStatus } from '../types';
import { toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';

const DEFAULT_AUTO_RELOCK_CONFIG: AutoRelockConfig = {
  enabled: false,
  trigger_hold_s: 0.8,
  verify_hold_s: 1.2,
  cooldown_s: 8.0,
  unlocked_trace_timeout_s: 2.0,
  max_attempts: 2,
};

type AutoRelockPanelProps = {
  config?: AutoRelockConfig | null;
  status?: AutoRelockStatus | null;
  saving?: boolean;
  error?: string | null;
  onSaveConfig?: (config: AutoRelockConfig) => Promise<void>;
};

export function AutoRelockPanel({
  config,
  status,
  saving,
  error,
  onSaveConfig,
}: AutoRelockPanelProps) {
  const [draft, setDraft] = useState<AutoRelockConfig>(
    config ?? DEFAULT_AUTO_RELOCK_CONFIG
  );
  const [dirty, setDirty] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (!config) return;
    setDraft(config);
    setDirty(false);
  }, [config]);

  const updateField = <K extends keyof AutoRelockConfig>(
    name: K,
    value: AutoRelockConfig[K]
  ) => {
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
          : 'Failed to save auto-relock settings.'
      );
    }
  };

  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        Uses auto-lock scan settings from the Locking panel.
      </Text>
      <Text size="xs" c="dimmed">
        state={status?.state ?? 'idle'} | attempts={status?.attempts ?? 0}/
        {status?.max_attempts ?? draft.max_attempts} | cooldown=
        {(status?.cooldown_remaining_s ?? 0).toFixed(1)}s
      </Text>
      <Switch
        label="Enable auto relock"
        checked={draft.enabled}
        onChange={(event) => updateField('enabled', event.currentTarget.checked)}
      />
      <Group grow>
        <NumberInput
          label="Trigger hold (s)"
          value={draft.trigger_hold_s}
          min={0.05}
          step={0.1}
          onChange={(value) =>
            updateField('trigger_hold_s', toFiniteNumberOr(value, 0.8))
          }
        />
        <NumberInput
          label="Verify hold (s)"
          value={draft.verify_hold_s}
          min={0.05}
          step={0.1}
          onChange={(value) =>
            updateField('verify_hold_s', toFiniteNumberOr(value, 1.2))
          }
        />
      </Group>
      <Group grow>
        <NumberInput
          label="Unlocked trace timeout (s)"
          value={draft.unlocked_trace_timeout_s}
          min={0.1}
          step={0.1}
          onChange={(value) =>
            updateField('unlocked_trace_timeout_s', toFiniteNumberOr(value, 2.0))
          }
        />
        <NumberInput
          label="Cooldown (s)"
          value={draft.cooldown_s}
          min={0}
          step={0.5}
          onChange={(value) => updateField('cooldown_s', toFiniteNumberOr(value, 8.0))}
        />
      </Group>
      <NumberInput
        label="Max attempts"
        value={draft.max_attempts}
        min={1}
        step={1}
        onChange={(value) =>
          updateField('max_attempts', toRoundedIntOr(value, 2, 1))
        }
      />
      <Button
        variant="light"
        color="orange"
        onClick={() => {
          save().catch(() => null);
        }}
        disabled={!onSaveConfig || !dirty}
        loading={Boolean(saving)}
      >
        Save auto-relock settings
      </Button>
      {status?.last_error ? (
        <Text size="xs" c="red">
          {status.last_error}
        </Text>
      ) : null}
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
