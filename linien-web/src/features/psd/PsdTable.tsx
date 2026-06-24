import { memo } from 'react';
import { ActionIcon, Checkbox, ColorSwatch, Group, Table, Text } from '@mantine/core';
import { IconTrash } from '@tabler/icons-react';
import type { PsdCurveEntry } from './usePsdController';
import { bandRms } from './psdMath';

type PsdTableProps = {
  curves: PsdCurveEntry[];
  deviceNameByKey: Map<string, string>;
  fLo: number;
  fHi: number;
  onToggleVisible: (uuid: string) => void;
  onDelete: (uuid: string) => void;
};

const formatTime = (time: number | null): string => {
  if (time == null) return '—';
  try {
    return new Date(time * 1000).toLocaleTimeString();
  } catch {
    return '—';
  }
};

const formatNumber = (value: number | null): string =>
  value == null ? '—' : String(value);

const formatRms = (value: number | null): string =>
  value == null ? '—' : value.toExponential(3);

export const PsdTable = memo(function PsdTable({
  curves,
  deviceNameByKey,
  fLo,
  fHi,
  onToggleVisible,
  onDelete,
}: PsdTableProps) {
  if (curves.length === 0) return null;

  return (
    <Table striped highlightOnHover withTableBorder verticalSpacing="xs" fz="sm">
      <Table.Thead>
        <Table.Tr>
          <Table.Th w={40}>Show</Table.Th>
          <Table.Th w={40} />
          <Table.Th>Device</Table.Th>
          <Table.Th>Time</Table.Th>
          <Table.Th>P</Table.Th>
          <Table.Th>I</Table.Th>
          <Table.Th>D</Table.Th>
          <Table.Th>Band RMS (V)</Table.Th>
          <Table.Th w={40} />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {curves.map((c) => (
          <Table.Tr key={c.uuid}>
            <Table.Td>
              <Checkbox
                size="xs"
                checked={c.visible}
                onChange={() => onToggleVisible(c.uuid)}
                aria-label="Toggle curve visibility"
              />
            </Table.Td>
            <Table.Td>
              <ColorSwatch color={c.color} size={14} />
            </Table.Td>
            <Table.Td>
              <Group gap={6} wrap="nowrap">
                <Text size="sm">
                  {deviceNameByKey.get(c.device_key) ?? c.device_key}
                </Text>
                {!c.complete ? (
                  <Text size="xs" c="dimmed">
                    (running…)
                  </Text>
                ) : null}
              </Group>
            </Table.Td>
            <Table.Td>{formatTime(c.time)}</Table.Td>
            <Table.Td>{formatNumber(c.p)}</Table.Td>
            <Table.Td>{formatNumber(c.i)}</Table.Td>
            <Table.Td>{formatNumber(c.d)}</Table.Td>
            <Table.Td>{formatRms(bandRms(c.curve, fLo, fHi))}</Table.Td>
            <Table.Td>
              <ActionIcon
                size="sm"
                variant="subtle"
                color="red"
                onClick={() => onDelete(c.uuid)}
                aria-label="Delete curve"
              >
                <IconTrash size={14} />
              </ActionIcon>
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
});
