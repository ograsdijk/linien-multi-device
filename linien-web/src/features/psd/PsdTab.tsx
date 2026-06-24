import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Group,
  MultiSelect,
  NumberInput,
  Paper,
  Select,
  Stack,
  Text,
} from '@mantine/core';
import {
  IconDownload,
  IconPlayerPlay,
  IconPlayerStop,
  IconTrash,
} from '@tabler/icons-react';
import type { Device, DeviceStatus } from '../../types';
import { usePsdController } from './usePsdController';
import { PsdPlot } from './PsdPlot';
import { PsdTable } from './PsdTable';
import { exportPsdCsv, exportPsdJson } from './exportPsd';
import { approxRunSeconds, floToMaxDecimation, formatRunTime } from './psdMath';

type PsdTabProps = {
  devices: Device[];
  deviceStatusMap: Record<string, DeviceStatus | undefined>;
};

const ALGORITHM_OPTIONS = [
  { value: '1', label: 'LPSD (log-spaced)' },
  { value: '0', label: 'Welch' },
];

export function PsdTab({ devices, deviceStatusMap }: PsdTabProps) {
  const psd = usePsdController();
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [algorithm, setAlgorithm] = useState('1');
  // Unified band of interest: f_lo sets acquisition depth (and the RMS lower
  // bound); f_hi bounds the RMS integral (e.g. piezo < 5 kHz).
  const [fLo, setFLo] = useState(10);
  const [fHi, setFHi] = useState(10000);

  const deviceNameByKey = useMemo(
    () => new Map(devices.map((d) => [d.key, (d.name || '').trim() || d.key])),
    [devices]
  );

  const deviceOptions = useMemo(
    () =>
      devices.map((d) => {
        const status = deviceStatusMap[d.key];
        const connected = Boolean(status?.connected);
        const locked = status?.lock === true;
        const name = deviceNameByKey.get(d.key) ?? d.key;
        const suffix = !connected
          ? ' (disconnected)'
          : !locked
            ? ' (not locked)'
            : status?.psd_running
              ? ' (running…)'
              : '';
        return {
          value: d.key,
          label: `${name}${suffix}`,
          disabled: !connected || !locked,
        };
      }),
    [devices, deviceStatusMap, deviceNameByKey]
  );

  const maxDecimation = floToMaxDecimation(fLo);
  const runHint = formatRunTime(approxRunSeconds(maxDecimation));
  const bandValid = fHi > fLo && fLo > 0;

  const startOpts = { algorithm: Number(algorithm), maxDecimation };

  const anyRunning = useMemo(
    () => devices.some((d) => deviceStatusMap[d.key]?.psd_running),
    [devices, deviceStatusMap]
  );

  const canStart = selectedKeys.length > 0 && !psd.busy && bandValid;

  return (
    <Stack gap="sm">
      <Paper withBorder p="sm" radius="md">
        <Stack gap="sm">
          <Group align="flex-end" gap="sm" wrap="wrap">
            <MultiSelect
              label="Devices"
              placeholder="Select locked devices"
              data={deviceOptions}
              value={selectedKeys}
              onChange={setSelectedKeys}
              searchable
              clearable
              w={300}
              maxDropdownHeight={260}
            />
            <Select
              label="Algorithm"
              data={ALGORITHM_OPTIONS}
              value={algorithm}
              onChange={(v) => v && setAlgorithm(v)}
              w={170}
              allowDeselect={false}
            />
            <NumberInput
              label="Band low (Hz)"
              value={fLo}
              onChange={(v) => setFLo(Number(v) || 0)}
              min={0.01}
              w={130}
              error={!bandValid ? 'low < high' : undefined}
            />
            <NumberInput
              label="Band high (Hz)"
              value={fHi}
              onChange={(v) => setFHi(Number(v) || 0)}
              min={0.02}
              w={130}
            />
            <Button
              leftSection={<IconPlayerPlay size={16} />}
              disabled={!canStart}
              onClick={() => psd.startPsd(selectedKeys, startOpts)}
            >
              Start PSD
            </Button>
            <Button
              variant="light"
              color="red"
              leftSection={<IconPlayerStop size={16} />}
              disabled={selectedKeys.length === 0 || psd.busy}
              onClick={() => psd.stopPsd(selectedKeys)}
            >
              Stop
            </Button>
          </Group>

          <Text size="xs" c="dimmed">
            f_lo sets acquisition depth ≈ decimation {maxDecimation} ({runHint} per
            device); the RMS column integrates ASD² over [{fLo}, {fHi}] Hz.
          </Text>

          <Group gap="sm" wrap="wrap">
            <Button
              size="xs"
              variant="default"
              leftSection={<IconDownload size={14} />}
              disabled={psd.curves.length === 0}
              onClick={() => exportPsdJson(psd.curves, deviceNameByKey)}
            >
              Export JSON
            </Button>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconDownload size={14} />}
              disabled={psd.curves.length === 0}
              onClick={() => exportPsdCsv(psd.curves, deviceNameByKey)}
            >
              Export CSV
            </Button>
            <Button
              size="xs"
              variant="default"
              color="red"
              leftSection={<IconTrash size={14} />}
              disabled={psd.curves.length === 0}
              onClick={() => psd.clearAll()}
            >
              Clear all
            </Button>
            <Text size="xs" c={psd.wsConnected ? 'dimmed' : 'red'}>
              {psd.wsConnected ? 'Live' : 'Reconnecting…'}
            </Text>
          </Group>

          <Text size="xs" c="dimmed">
            A PSD run takes over the device (the live plot pauses for the run
            duration) and requires the laser to be locked. Devices run in
            parallel, each on its own controller.
          </Text>

          {anyRunning ? (
            <Text size="xs" c="blue">
              Measurement in progress — partial curves update as each decimation
              band completes.
            </Text>
          ) : null}

          {psd.notice ? (
            <Alert
              color="yellow"
              variant="light"
              withCloseButton
              onClose={() => psd.setNotice(null)}
            >
              {psd.notice}
            </Alert>
          ) : null}
        </Stack>
      </Paper>

      <Paper withBorder p="sm" radius="md">
        <PsdPlot curves={psd.curves} fLo={fLo} fHi={fHi} />
      </Paper>

      <PsdTable
        curves={psd.curves}
        deviceNameByKey={deviceNameByKey}
        fLo={fLo}
        fHi={fHi}
        onToggleVisible={psd.toggleVisible}
        onDelete={psd.deleteCurve}
      />
    </Stack>
  );
}
