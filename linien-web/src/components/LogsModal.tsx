import {
  Badge,
  Button,
  Card,
  Group,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import type { MutableRefObject } from 'react';
import type { Device, UiLogEntry } from '../types';

type LogsModalProps = {
  opened: boolean;
  onClose: () => void;
  connected: boolean;
  loading: boolean;
  logs: UiLogEntry[];
  filteredLogs: UiLogEntry[];
  devices: Device[];
  levelFilter: string;
  onLevelFilterChange: (value: string) => void;
  sourceFilter: string;
  onSourceFilterChange: (value: string) => void;
  deviceFilter: string;
  onDeviceFilterChange: (value: string) => void;
  searchText: string;
  onSearchTextChange: (value: string) => void;
  autoScroll: boolean;
  onAutoScrollChange: (value: boolean) => void;
  onReload: () => void;
  onClear: () => void;
  onCopyMessage: (value: string) => void;
  onCopyJson: (entry: UiLogEntry) => void;
  viewportRef: MutableRefObject<HTMLDivElement | null>;
};

const formatLogTime = (ts: number) => {
  if (!Number.isFinite(ts)) return '--:--:--';
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
};

const levelColor = (levelName: string) => {
  const normalized = String(levelName || '').toLowerCase();
  if (normalized === 'error' || normalized === 'critical') return 'red';
  if (normalized === 'warning') return 'yellow';
  if (normalized === 'info') return 'blue';
  return 'gray';
};

export function LogsModal({
  opened,
  onClose,
  connected,
  loading,
  logs,
  filteredLogs,
  devices,
  levelFilter,
  onLevelFilterChange,
  sourceFilter,
  onSourceFilterChange,
  deviceFilter,
  onDeviceFilterChange,
  searchText,
  onSearchTextChange,
  autoScroll,
  onAutoScrollChange,
  onReload,
  onClear,
  onCopyMessage,
  onCopyJson,
  viewportRef,
}: LogsModalProps) {
  const sourceValues = Array.from(
    new Set(
      logs
        .map((entry) => String(entry.source || '').trim())
        .filter((value) => value.length > 0)
    )
  ).sort((a, b) => a.localeCompare(b));

  const deviceValues = Array.from(
    new Set(
      logs
        .map((entry) => String(entry.device_key || '').trim())
        .filter((value) => value.length > 0)
    )
  ).sort((a, b) => a.localeCompare(b));

  const deviceNameByKey = new Map(devices.map((device) => [device.key, device.name || device.key]));

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Logs"
      size="clamp(56rem, 92vw, 96rem)"
      centered
      zIndex={430}
    >
      <Stack gap="sm">
        <Group justify="space-between">
          <Group gap="xs">
            <Badge variant="light" color={connected ? 'teal' : 'red'}>
              {connected ? 'Live' : 'Disconnected'}
            </Badge>
            <Text size="xs" c="dimmed">
              {filteredLogs.length} shown / {logs.length} loaded
            </Text>
          </Group>
          <Group gap="xs">
            <Switch
              size="sm"
              checked={autoScroll}
              onChange={(event) => onAutoScrollChange(event.currentTarget.checked)}
              label="Auto-scroll"
            />
            <Button size="xs" variant="light" loading={loading} onClick={onReload}>
              Reload
            </Button>
            <Button size="xs" variant="light" color="red" onClick={onClear}>
              Clear
            </Button>
          </Group>
        </Group>

        <Group grow align="flex-end">
          <Select
            label="Level"
            comboboxProps={{ zIndex: 500 }}
            value={levelFilter}
            onChange={(value) => onLevelFilterChange(value ?? 'all')}
            data={[
              { value: 'all', label: 'All levels' },
              { value: 'info', label: 'Info' },
              { value: 'warning', label: 'Warning' },
              { value: 'error', label: 'Error' },
            ]}
          />
          <Select
            label="Source"
            comboboxProps={{ zIndex: 500 }}
            value={sourceFilter}
            onChange={(value) => onSourceFilterChange(value ?? 'all')}
            data={[
              { value: 'all', label: 'All sources' },
              ...sourceValues.map((value) => ({ value, label: value })),
            ]}
          />
          <Select
            label="Device"
            comboboxProps={{ zIndex: 500 }}
            value={deviceFilter}
            onChange={(value) => onDeviceFilterChange(value ?? 'all')}
            data={[
              { value: 'all', label: 'All devices' },
              ...deviceValues.map((value) => ({
                value,
                label: deviceNameByKey.get(value) ?? value,
              })),
            ]}
          />
        </Group>

        <TextInput
          label="Search"
          placeholder="Search message/code/details"
          value={searchText}
          onChange={(event) => onSearchTextChange(event.currentTarget.value)}
        />

        <ScrollArea h="55vh" viewportRef={viewportRef}>
          <Stack gap={6}>
            {filteredLogs.length === 0 ? (
              <Text size="sm" c="dimmed">
                No log entries match the current filters.
              </Text>
            ) : null}
            {filteredLogs.map((entry) => {
              const levelName = String(entry.level_name || '').toLowerCase();
              const key = `${entry.id}:${entry.ts}`;
              const deviceKey = entry.device_key ? String(entry.device_key) : '';
              const resolvedDeviceName = deviceKey ? deviceNameByKey.get(deviceKey) : null;
              const deviceLabel = resolvedDeviceName || deviceKey || '-';
              const showDeviceKey = Boolean(deviceKey && resolvedDeviceName && resolvedDeviceName !== deviceKey);
              const detailsJson =
                entry.details && Object.keys(entry.details).length > 0
                  ? JSON.stringify(entry.details, null, 2)
                  : '';
              return (
                <Card
                  key={key}
                  p="xs"
                  radius="sm"
                  style={{ border: '1px solid var(--panel-border)' }}
                >
                  <Stack gap={4}>
                    <Group justify="space-between" align="flex-start" gap="xs">
                      <Group gap="xs" wrap="wrap">
                        <Text size="xs" c="dimmed">
                          {formatLogTime(entry.ts)}
                        </Text>
                        <Badge size="xs" variant="light" color={levelColor(levelName)}>
                          {levelName || 'info'}
                        </Badge>
                        <Badge size="xs" variant="outline" color="gray">
                          {entry.source || '-'}
                        </Badge>
                        <Text size="xs" c="dimmed">
                          {deviceLabel}
                        </Text>
                        {showDeviceKey ? (
                          <Text size="xs" c="dimmed">
                            [{deviceKey}]
                          </Text>
                        ) : null}
                        {entry.code ? (
                          <Text size="xs" c="dimmed">
                            {entry.code}
                          </Text>
                        ) : null}
                      </Group>
                      <Group gap="xs">
                        <Button
                          size="compact-xs"
                          variant="subtle"
                          color="gray"
                          disabled={!entry.message}
                          onClick={() => onCopyMessage(entry.message)}
                        >
                          Copy message
                        </Button>
                        <Button
                          size="compact-xs"
                          variant="subtle"
                          color="gray"
                          onClick={() => onCopyJson(entry)}
                        >
                          Copy JSON
                        </Button>
                      </Group>
                    </Group>
                    <Text
                      size="sm"
                      style={{
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        userSelect: 'text',
                      }}
                    >
                      {entry.message}
                    </Text>
                    {detailsJson ? (
                      <details>
                        <summary>
                          <Text span size="xs" c="dimmed">
                            Details
                          </Text>
                        </summary>
                        <Text
                          size="xs"
                          style={{
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            marginTop: 4,
                            userSelect: 'text',
                          }}
                        >
                          {detailsJson}
                        </Text>
                      </details>
                    ) : null}
                  </Stack>
                </Card>
              );
            })}
          </Stack>
        </ScrollArea>
      </Stack>
    </Modal>
  );
}
