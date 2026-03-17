import { Button, Divider, Group, NumberInput, Select, Stack, Switch, Text } from '@mantine/core';

const MHz = 0x10000000 / 8;
const Vpp = ((1 << 14) - 1) / 4;

const CHANNEL_OPTIONS = [
  { value: '0', label: 'Channel 1' },
  { value: '1', label: 'Channel 2' },
];

const toNumber = (value: unknown, fallback = 0) => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

type OptimizationPanelProps = {
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
  onStartSelection: () => void;
  onAbortSelection: () => void;
  onStopTask: (useNew: boolean) => void;
  selectionActive?: boolean;
  selectionError?: string | null;
  selectionSubmitting?: boolean;
  optimizationTemporarilyDisabled?: boolean;
  disableReason?: string;
};

export function OptimizationPanel({
  params,
  onSetParam,
  onStartSelection,
  onAbortSelection,
  onStopTask,
  selectionActive,
  selectionError,
  selectionSubmitting,
  optimizationTemporarilyDisabled,
  disableReason,
}: OptimizationPanelProps) {
  const running = Boolean(params.optimization_running);
  const approaching = Boolean(params.optimization_approaching);
  const selecting = Boolean(selectionActive ?? params.optimization_selection);
  const failed = Boolean(params.optimization_failed);
  const improvement = toNumber(params.optimization_improvement, 0);
  const disabled = Boolean(params.pid_only_mode) || Boolean(optimizationTemporarilyDisabled);
  const disabledReasonText =
    disableReason ?? 'Temporarily disabled due to compatibility issue.';

  const modFreqEnabled = Boolean(params.optimization_mod_freq_enabled);
  const modFreqMin = toNumber(params.optimization_mod_freq_min, 0);
  const modFreqMax = toNumber(params.optimization_mod_freq_max, 10);
  const modAmpEnabled = Boolean(params.optimization_mod_amp_enabled);
  const modAmpMin = toNumber(params.optimization_mod_amp_min, 0);
  const modAmpMax = toNumber(params.optimization_mod_amp_max, 2);

  const dualChannel = Boolean(params.dual_channel);
  const channel = toNumber(params.optimization_channel, 0);

  const currentFreq = toNumber(params.modulation_frequency, 0) / MHz;
  const currentAmp = toNumber(params.modulation_amplitude, 0) / Vpp;
  const currentPhase = toNumber(
    dualChannel && channel === 1 ? params.demodulation_phase_b : params.demodulation_phase_a,
    0
  );

  const optimized = Array.isArray(params.optimization_optimized_parameters)
    ? params.optimization_optimized_parameters
    : null;
  const optimizedFreq = optimized ? toNumber(optimized[0], 0) / MHz : null;
  const optimizedAmp = optimized ? toNumber(optimized[1], 0) / Vpp : null;
  const optimizedPhase = optimized ? toNumber(optimized[2], 0) : null;

  const statusText = failed
    ? 'Failed'
    : running
    ? approaching
      ? 'Preparing'
      : 'Running'
    : selecting
    ? 'Selecting region'
    : 'Idle';

  return (
    <Stack gap="sm">
      <Text size="sm">Status: {statusText}</Text>
      {optimizationTemporarilyDisabled ? (
        <Text size="xs" c="red">
          {disabledReasonText}
        </Text>
      ) : null}
      <Text size="sm">Improvement: {(improvement * 100).toFixed(1)}%</Text>
      {optimized && (
        <Text size="xs" c="dimmed">
          Current: {currentFreq.toFixed(2)} MHz, {currentAmp.toFixed(2)} Vpp, {currentPhase.toFixed(1)}°
          <br />
          Optimized: {optimizedFreq?.toFixed(2)} MHz, {optimizedAmp?.toFixed(2)} Vpp, {optimizedPhase?.toFixed(1)}°
        </Text>
      )}

      {failed && (
        <Button
          variant="outline"
          color="red"
          onClick={() => onSetParam('optimization_failed', false, false)}
          disabled={disabled}
        >
          Reset failed state
        </Button>
      )}

      <Divider my="xs" />
      <Text fw={600}>Parameters</Text>
      <Switch
        label="Optimize modulation frequency"
        checked={modFreqEnabled}
        onChange={(event) =>
          onSetParam('optimization_mod_freq_enabled', event.currentTarget.checked ? 1 : 0, false)
        }
        disabled={disabled}
      />
      <Group grow>
        <NumberInput
          label="Min (MHz)"
          value={modFreqMin}
          onChange={(value) => onSetParam('optimization_mod_freq_min', Number(value), false)}
          disabled={disabled || !modFreqEnabled}
        />
        <NumberInput
          label="Max (MHz)"
          value={modFreqMax}
          onChange={(value) => onSetParam('optimization_mod_freq_max', Number(value), false)}
          disabled={disabled || !modFreqEnabled}
        />
      </Group>

      <Switch
        label="Optimize modulation amplitude"
        checked={modAmpEnabled}
        onChange={(event) =>
          onSetParam('optimization_mod_amp_enabled', event.currentTarget.checked ? 1 : 0, false)
        }
        disabled={disabled}
      />
      <Group grow>
        <NumberInput
          label="Min (Vpp)"
          value={modAmpMin}
          onChange={(value) => onSetParam('optimization_mod_amp_min', Number(value), false)}
          disabled={disabled || !modAmpEnabled}
        />
        <NumberInput
          label="Max (Vpp)"
          value={modAmpMax}
          onChange={(value) => onSetParam('optimization_mod_amp_max', Number(value), false)}
          disabled={disabled || !modAmpEnabled}
        />
      </Group>

      <Text size="xs" c="dimmed">
        Demodulation phase is always optimized (0–360°).
      </Text>

      {dualChannel && (
        <Select
          label="Channel to optimize"
          data={CHANNEL_OPTIONS}
          value={String(channel)}
          onChange={(value) => {
            if (value == null) return;
            onSetParam('optimization_channel', Number(value), false);
          }}
          disabled={disabled}
        />
      )}

      <Divider my="xs" />
      {!selecting ? (
        <Button
          variant="light"
          color="orange"
          onClick={onStartSelection}
          disabled={disabled || selectionSubmitting}
        >
          Select region
        </Button>
      ) : (
        <Stack gap="xs">
          <Text size="sm" fw={500}>
            Click and drag over the region to optimize.
          </Text>
          {selectionError ? (
            <Text size="xs" c="red">
              {selectionError}
            </Text>
          ) : null}
          <Button
            variant="default"
            onClick={onAbortSelection}
            disabled={disabled || selectionSubmitting}
          >
            Abort
          </Button>
        </Stack>
      )}
      <Group grow>
        <Button
          color="red"
          variant="light"
          onClick={() => onStopTask(false)}
          disabled={disabled || Boolean(optimizationTemporarilyDisabled)}
        >
          Abort
        </Button>
        <Button
          color="green"
          variant="light"
          onClick={() => onStopTask(true)}
          disabled={disabled || Boolean(optimizationTemporarilyDisabled)}
        >
          Use optimized
        </Button>
      </Group>
    </Stack>
  );
}
