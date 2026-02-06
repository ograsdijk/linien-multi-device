import { Group, NumberInput, Select, Slider, Stack, Switch, Text } from '@mantine/core';

const ANALOG_OUT_V = 1.8 / ((2 ** 15) - 1);

const CHANNEL_OPTIONS = [
  { value: '0', label: 'FAST OUT 1' },
  { value: '1', label: 'FAST OUT 2' },
  { value: '2', label: 'ANALOG OUT 0' },
];

const MOD_CHANNEL_OPTIONS = [
  { value: '0', label: 'FAST OUT 1' },
  { value: '1', label: 'FAST OUT 2' },
];

const SLOW_CONTROL_OPTIONS = [
  ...CHANNEL_OPTIONS,
  { value: '3', label: 'Disabled' },
];

type GeneralPanelProps = {
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
};

export function GeneralPanel({ params, onSetParam }: GeneralPanelProps) {
  const dual = Boolean(params.dual_channel);
  const pidOnly = Boolean(params.pid_only_mode);
  const channelMix = typeof params.channel_mixing === 'number' ? params.channel_mixing : 0;

  return (
    <Stack gap="md">
      <Switch
        label="PID-only mode"
        checked={pidOnly}
        onChange={(event) => onSetParam('pid_only_mode', event.currentTarget.checked, true)}
      />
      <Switch
        label="Dual channel"
        checked={dual}
        onChange={(event) => onSetParam('dual_channel', event.currentTarget.checked, true)}
      />
      <div>
        <Text size="sm" fw={500} mb={6}>
          Channel mixing
        </Text>
        <Slider
          min={-128}
          max={128}
          value={channelMix}
          onChange={(value) => onSetParam('channel_mixing', value, true)}
          disabled={!dual}
        />
      </div>
      <Group grow>
        <Select
          label="Mod channel"
          data={MOD_CHANNEL_OPTIONS}
          value={String(params.mod_channel ?? 0)}
          onChange={(value) => onSetParam('mod_channel', Number(value), true)}
        />
        <Select
          label="Control channel"
          data={MOD_CHANNEL_OPTIONS}
          value={String(params.control_channel ?? 0)}
          onChange={(value) => onSetParam('control_channel', Number(value), true)}
        />
      </Group>
      <Group grow>
        <Select
          label="Sweep channel"
          data={CHANNEL_OPTIONS}
          value={String(params.sweep_channel ?? 1)}
          onChange={(value) => onSetParam('sweep_channel', Number(value), true)}
        />
        <Select
          label="Slow control channel"
          data={SLOW_CONTROL_OPTIONS}
          value={String(params.pid_on_slow_enabled ? params.slow_control_channel ?? 2 : 3)}
          onChange={(value) => {
            if (value == null) return;
            const num = Number(value);
            if (num > 2) {
              onSetParam('pid_on_slow_enabled', 0, true);
              return;
            }
            onSetParam('slow_control_channel', num, false);
            onSetParam('pid_on_slow_enabled', 1, true);
          }}
        />
      </Group>
      <Group grow>
        <Switch
          label="Invert Fast Out 1"
          checked={Boolean(params.polarity_fast_out1)}
          onChange={(event) => onSetParam('polarity_fast_out1', event.currentTarget.checked, true)}
        />
        <Switch
          label="Invert Fast Out 2"
          checked={Boolean(params.polarity_fast_out2)}
          onChange={(event) => onSetParam('polarity_fast_out2', event.currentTarget.checked, true)}
        />
      </Group>
      <Switch
        label="Invert Analog Out 0"
        checked={Boolean(params.polarity_analog_out0)}
        onChange={(event) => onSetParam('polarity_analog_out0', event.currentTarget.checked, true)}
      />
      <Group grow>
        <NumberInput
          label="Analog out 1 (V)"
          value={(params.analog_out_1 ?? 0) * ANALOG_OUT_V}
          onChange={(value) => onSetParam('analog_out_1', Math.round(Number(value) / ANALOG_OUT_V), true)}
        />
        <NumberInput
          label="Analog out 2 (V)"
          value={(params.analog_out_2 ?? 0) * ANALOG_OUT_V}
          onChange={(value) => onSetParam('analog_out_2', Math.round(Number(value) / ANALOG_OUT_V), true)}
        />
      </Group>
      <NumberInput
        label="Analog out 3 (V)"
        value={(params.analog_out_3 ?? 0) * ANALOG_OUT_V}
        onChange={(value) => onSetParam('analog_out_3', Math.round(Number(value) / ANALOG_OUT_V), true)}
      />
    </Stack>
  );
}
