import { Badge, ScrollArea, Table, Text } from '@mantine/core';
import type { InfluxFleetRow } from '../../features/integrations/useInfluxController';

type InfluxFleetTableProps = {
  rows: InfluxFleetRow[];
  selectedDeviceKey: string | null;
  onSelect: (deviceKey: string) => void;
};

const stateBadge = (row: InfluxFleetRow) => {
  if (row.error) return { color: 'red', label: 'Error' };
  if (!row.connected) return { color: 'gray', label: 'Offline' };
  if (row.loggingActive) return { color: 'green', label: 'Logging' };
  return { color: 'yellow', label: 'Idle' };
};

/** Where every board writes, without selecting any of them.
 *
 * The credentials behind this come from one fleet-wide request, so rendering a
 * row costs nothing extra -- the whole point is that reading the fleet's
 * configuration no longer means clicking through the device dropdown.
 */
export const InfluxFleetTable = ({
  rows,
  selectedDeviceKey,
  onSelect,
}: InfluxFleetTableProps) => {
  if (rows.length === 0) return null;
  return (
    <ScrollArea.Autosize mah={180} type="auto">
      <Table striped highlightOnHover verticalSpacing={2} fz="xs">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Device</Table.Th>
            <Table.Th>Bucket / measurement</Table.Th>
            <Table.Th>State</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const badge = stateBadge(row);
            const selected = row.deviceKey === selectedDeviceKey;
            return (
              <Table.Tr
                key={row.deviceKey}
                onClick={() => onSelect(row.deviceKey)}
                style={{ cursor: 'pointer' }}
              >
                <Table.Td>
                  <Text size="xs" fw={selected ? 700 : 400} truncate="end">
                    {row.label}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {/* An offline board has no readable settings; say so rather
                      than showing an empty cell that reads as "unconfigured". */}
                  <Text size="xs" c="dimmed" truncate="end">
                    {row.error
                      ? row.error
                      : row.bucket
                        ? `${row.bucket} / ${row.measurement || '-'}`
                        : row.connected
                          ? 'Not loaded'
                          : 'Offline'}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Badge size="xs" color={badge.color} variant="light">
                    {badge.label}
                  </Badge>
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </ScrollArea.Autosize>
  );
};
