import { memo, useEffect, useState } from 'react';
import { Button, Group, Stack, Switch, Text } from '@mantine/core';
import type { AutoRelockConfig, AutoRelockStatus } from '../types';
import { toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';
import { DeferredNumberInput } from './DeferredNumberInput';

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

export const AutoRelockPanel = memo(function AutoRelockPanel({
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
    // Don't let an incoming config broadcast (often our own save echoed back
    // over the websocket, or another client's update) clobber edits the user
    // is in the middle of making. `dirty` is read fresh each time `config`
    // changes, so it reflects whether the user was editing when this arrived.
    if (dirty) return;
    setDraft(config);
    setDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        <DeferredNumberInput
          label="Trigger hold (s)"
          value={draft.trigger_hold_s}
          min={0.05}
          step={0.1}
          onCommit={(value) =>
            updateField('trigger_hold_s', toFiniteNumberOr(value, 0.8))
          }
        />
        <DeferredNumberInput
          label="Verify hold (s)"
          value={draft.verify_hold_s}
          min={0.05}
          step={0.1}
          onCommit={(value) =>
            updateField('verify_hold_s', toFiniteNumberOr(value, 1.2))
          }
        />
      </Group>
      <Group grow>
        <DeferredNumberInput
          label="Unlocked trace timeout (s)"
          value={draft.unlocked_trace_timeout_s}
          min={0.1}
          step={0.1}
          onCommit={(value) =>
            updateField('unlocked_trace_timeout_s', toFiniteNumberOr(value, 2.0))
          }
        />
        <DeferredNumberInput
          label="Cooldown (s)"
          value={draft.cooldown_s}
          min={0}
          step={0.5}
          onCommit={(value) => updateField('cooldown_s', toFiniteNumberOr(value, 8.0))}
        />
      </Group>
      <DeferredNumberInput
        label="Max attempts"
        value={draft.max_attempts}
        min={1}
        step={1}
        parseCommit={(value) => toRoundedIntOr(value, 2, 1)}
        onCommit={(value) =>
          updateField('max_attempts', value)
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
});
