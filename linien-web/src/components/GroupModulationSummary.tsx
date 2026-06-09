import { memo } from 'react';
import { Button, Popover, Table, Text } from '@mantine/core';
import type { Device } from '../types';
import { useDeviceStateEntry } from '../state/deviceStatesStore';

const MHz = 0x10000000 / 8;

type GroupModulationSummaryProps = {
  devices: Device[];
};

const toFiniteNumber = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const formatDemodFrequency = (params: Record<string, any>): string => {
  const modulationFrequency = toFiniteNumber(params.modulation_frequency);
  const demodMultiplier = toFiniteNumber(params.demodulation_multiplier_a);
  if (modulationFrequency == null || demodMultiplier == null) return '—';
  return `${((modulationFrequency / MHz) * demodMultiplier).toFixed(2)} MHz`;
};

const formatState = (params: Record<string, any>): string => {
  const amplitude = toFiniteNumber(params.modulation_amplitude);
  if (amplitude == null) return 'unknown';
  return amplitude > 0 ? 'active' : 'inactive';
};

// Each row subscribes to its own device entry so unrelated devices'
// updates don't trigger a re-render of the entire table.
const ModFreqRow = memo(function ModFreqRow({ device }: { device: Device }) {
  const entry = useDeviceStateEntry(device.key);
  const params = entry.params;
  return (
    <Table.Tr>
      <Table.Td>{device.name || device.key}</Table.Td>
      <Table.Td>{formatDemodFrequency(params)}</Table.Td>
      <Table.Td>{formatState(params)}</Table.Td>
    </Table.Tr>
  );
});

export function GroupModulationSummary({ devices }: GroupModulationSummaryProps) {
  return (
    <Popover width={420} position="bottom-end" shadow="md" withinPortal>
      <Popover.Target>
        <Button size="xs" variant="light">
          Mod freqs
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <Text fw={700} mb="xs">
          Demod A frequencies
        </Text>
        {devices.length === 0 ? (
          <Text size="sm" c="dimmed">
            No devices in this group.
          </Text>
        ) : (
          <Table striped highlightOnHover withTableBorder withColumnBorders>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Device</Table.Th>
                <Table.Th>Demod A frequency</Table.Th>
                <Table.Th>State</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {devices.map((device) => (
                <ModFreqRow key={device.key} device={device} />
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Popover.Dropdown>
    </Popover>
  );
}
