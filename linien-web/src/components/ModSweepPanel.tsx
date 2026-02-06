import { Divider, Group, NumberInput, Select, Stack, Switch, Tabs, Text } from '@mantine/core';

const MHz = 0x10000000 / 8;
const Vpp = ((1 << 14) - 1) / 4;
const OFFSET_SCALE = 8191;
const FILTER_FREQ_MAX = 50_000_000;

const SWEEP_SPEED_LABELS = [
  '3.8 kHz',
  '1.9 kHz',
  '954 Hz',
  '477 Hz',
  '238 Hz',
  '119 Hz',
  '59 Hz',
  '30 Hz',
  '15 Hz',
  '7.5 Hz',
  '3.7 Hz',
  '1.9 Hz',
  '0.93 Hz',
  '0.47 Hz',
  '0.23 Hz',
  '0.12 Hz',
];

const SWEEP_SPEED_OPTIONS = SWEEP_SPEED_LABELS.map((label, idx) => ({
  value: String(idx),
  label,
}));

const DEMOD_MULTIPLIER_OPTIONS = [1, 2, 3, 4, 5].map((value) => ({
  value: String(value),
  label: `${value}f`,
}));

const FILTER_TYPE_OPTIONS = [
  { value: '0', label: 'Low pass' },
  { value: '1', label: 'High pass' },
];

const toNumber = (value: unknown, fallback = 0) => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

type ModSweepPanelProps = {
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
};

export function ModSweepPanel({ params, onSetParam }: ModSweepPanelProps) {
  const modulationFreq = typeof params.modulation_frequency === 'number'
    ? params.modulation_frequency / MHz
    : 0;
  const modulationAmp = typeof params.modulation_amplitude === 'number'
    ? params.modulation_amplitude / Vpp
    : 0;
  const pidOnly = Boolean(params.pid_only_mode);
  const dualChannel = Boolean(params.dual_channel);
  const modulationEnabled = modulationFreq > 0 && !pidOnly;

  const renderDemodChannel = (channel: 'a' | 'b', enabled: boolean) => {
    const suffix = `_${channel}`;
    const channelDisabled = !modulationEnabled || !enabled;
    const multiplier = toNumber(params[`demodulation_multiplier${suffix}`], 1);
    const phase = toNumber(params[`demodulation_phase${suffix}`], 0);
    const offset = toNumber(params[`offset${suffix}`], 0) / OFFSET_SCALE;
    const invert = Boolean(params[`invert${suffix}`]);
    const filterAutomatic = Boolean(params[`filter_automatic${suffix}`]);

    const filter1Enabled = Boolean(params[`filter_1_enabled${suffix}`]);
    const filter2Enabled = Boolean(params[`filter_2_enabled${suffix}`]);
    const filter1Type = toNumber(params[`filter_1_type${suffix}`], 0);
    const filter2Type = toNumber(params[`filter_2_type${suffix}`], 0);
    const filter1Freq = toNumber(params[`filter_1_frequency${suffix}`], 0);
    const filter2Freq = toNumber(params[`filter_2_frequency${suffix}`], 0);

    return (
      <Stack gap="xs">
        {!enabled && (
          <Text size="xs" c="dimmed">
            Enable dual channel to edit this channel.
          </Text>
        )}
        <Select
          label="Demodulation frequency"
          data={DEMOD_MULTIPLIER_OPTIONS}
          value={String(multiplier)}
          onChange={(value) => {
            if (value == null) return;
            onSetParam(`demodulation_multiplier${suffix}`, Number(value), true);
          }}
          disabled={channelDisabled}
        />
        <NumberInput
          label="Demodulation phase (deg)"
          value={phase}
          min={0}
          max={360}
          step={10}
          onChange={(value) => {
            const next = toNumber(value, phase);
            onSetParam(`demodulation_phase${suffix}`, next, true);
          }}
          disabled={channelDisabled}
        />
        <NumberInput
          label="Signal offset (V)"
          value={offset}
          min={-1}
          max={1}
          step={0.1}
          onChange={(value) => {
            const next = toNumber(value, offset);
            onSetParam(`offset${suffix}`, next * OFFSET_SCALE, true);
          }}
          disabled={channelDisabled}
        />
        <Switch
          label="Invert signal"
          checked={invert}
          onChange={(event) =>
            onSetParam(`invert${suffix}`, event.currentTarget.checked, true)
          }
          disabled={channelDisabled}
        />
        <Switch
          label="Automatic filtering"
          checked={filterAutomatic}
          onChange={(event) =>
            onSetParam(`filter_automatic${suffix}`, event.currentTarget.checked, true)
          }
          disabled={channelDisabled}
        />
        {filterAutomatic ? (
          <Text size="xs" c="dimmed">
            Automatic filter enabled.
          </Text>
        ) : (
          <Stack gap="xs" style={{ paddingLeft: 8 }}>
            <Switch
              label="Enable 1st order filter"
              checked={filter1Enabled}
              onChange={(event) =>
                onSetParam(`filter_1_enabled${suffix}`, event.currentTarget.checked, true)
              }
              disabled={channelDisabled}
            />
            <Group grow>
              <Select
                label="Filter 1 type"
                data={FILTER_TYPE_OPTIONS}
                value={String(filter1Type)}
                onChange={(value) => {
                  if (value == null) return;
                  onSetParam(`filter_1_type${suffix}`, Number(value), true);
                }}
                disabled={channelDisabled}
              />
              <NumberInput
                label="Filter 1 freq (Hz)"
                value={filter1Freq}
                min={0}
                max={FILTER_FREQ_MAX}
                step={1000}
                onChange={(value) => {
                  const next = toNumber(value, filter1Freq);
                  onSetParam(`filter_1_frequency${suffix}`, next, true);
                }}
                disabled={channelDisabled}
              />
            </Group>
            <Switch
              label="Enable 2nd order filter"
              checked={filter2Enabled}
              onChange={(event) =>
                onSetParam(`filter_2_enabled${suffix}`, event.currentTarget.checked, true)
              }
              disabled={channelDisabled}
            />
            <Group grow>
              <Select
                label="Filter 2 type"
                data={FILTER_TYPE_OPTIONS}
                value={String(filter2Type)}
                onChange={(value) => {
                  if (value == null) return;
                  onSetParam(`filter_2_type${suffix}`, Number(value), true);
                }}
                disabled={channelDisabled}
              />
              <NumberInput
                label="Filter 2 freq (Hz)"
                value={filter2Freq}
                min={0}
                max={FILTER_FREQ_MAX}
                step={1000}
                onChange={(value) => {
                  const next = toNumber(value, filter2Freq);
                  onSetParam(`filter_2_frequency${suffix}`, next, true);
                }}
                disabled={channelDisabled}
              />
            </Group>
          </Stack>
        )}
      </Stack>
    );
  };

  return (
    <Stack gap="md">
      <Group grow>
        <NumberInput
          label="Modulation freq (MHz)"
          value={modulationFreq}
          onChange={(value) => onSetParam('modulation_frequency', Number(value) * MHz, true)}
          step={0.1}
          disabled={pidOnly}
        />
        <NumberInput
          label="Modulation amp (Vpp)"
          value={modulationAmp}
          onChange={(value) => onSetParam('modulation_amplitude', Number(value) * Vpp, true)}
          step={0.05}
          disabled={pidOnly}
        />
      </Group>
      <Select
        label="Sweep speed"
        data={SWEEP_SPEED_OPTIONS}
        value={String(params.sweep_speed ?? 8)}
        onChange={(value) => onSetParam('sweep_speed', Number(value), true)}
        disabled={pidOnly}
      />
      <Divider my="xs" />
      <Tabs defaultValue="a" variant="outline">
        <Tabs.List>
          <Tabs.Tab value="a">Demod A</Tabs.Tab>
          <Tabs.Tab value="b" disabled={!dualChannel}>Demod B</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="a" pt="xs">
          {renderDemodChannel('a', true)}
        </Tabs.Panel>
        <Tabs.Panel value="b" pt="xs">
          {renderDemodChannel('b', dualChannel)}
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
