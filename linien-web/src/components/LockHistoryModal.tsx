import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Modal,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
} from '@mantine/core';
import { IconArrowBackUp, IconCopy, IconDownload, IconHistory, IconRefresh } from '@tabler/icons-react';
import { api } from '../api';
import { openDeviceStream } from '../ws';
import type { Device, StreamMessage } from '../types';
import { LockTracePlot } from './LockTracePlot';
import { sweepIndexToVoltage } from './plotShared';
import {
  LOCK_SOURCE_LABEL,
  SETTING_LABEL,
  commandDisplay,
  describeAttempt,
  exportRecord,
  formatSetting,
  historyQuery,
  lockMarkers,
  orderSettings,
  verificationDisplay,
  type HistoryFilters,
  type LockAcquisitionRecord,
  type LockAcquisitionSummary,
  type LockSource,
  type OutcomeFilter,
  type RangeFilter,
  type SettingsComparison,
  type Timestamp,
  toMillis,
} from '../features/locks/lockHistory';

const PAGE_SIZE = 50;
const ALL_DEVICES = '__all__';

export type LockHistoryModalProps = {
  opened: boolean;
  // Device to filter to when opened; null shows every device.
  initialDeviceKey: string | null;
  devices: Device[];
  onClose: () => void;
};

const formatTime = (value: Timestamp): string => {
  const ms = toMillis(value);
  if (ms === null) return '—';
  const date = new Date(ms);
  return date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
};

const formatMHz = (hz: number | null): string =>
  hz === null || hz === undefined ? '—' : `${Number((hz / 1e6).toFixed(3))} MHz`;

const formatNumber = (value: number | null | undefined, unit: string, digits = 3): string =>
  value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : `${Number(value.toFixed(digits))} ${unit}`;

const errorMessage = (error: unknown): string =>
  error instanceof Error && error.message ? error.message : 'Request failed.';

type LiveTrace = { x: Array<number | null>; y: Array<number | null>; at: number };

export function LockHistoryModal({ opened, initialDeviceKey, devices, onClose }: LockHistoryModalProps) {
  const [filters, setFilters] = useState<HistoryFilters>({
    deviceKey: initialDeviceKey,
    source: null,
    outcome: 'any',
    range: '7d',
  });
  const [items, setItems] = useState<LockAcquisitionSummary[]>([]);
  const [listInfo, setListInfo] = useState<{ source: string; unsent: number; reason?: string } | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [record, setRecord] = useState<LockAcquisitionRecord | null>(null);
  const [recordError, setRecordError] = useState<string | null>(null);
  const [comparison, setComparison] = useState<SettingsComparison | null>(null);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const [showAllSettings, setShowAllSettings] = useState(false);
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [restoreMessage, setRestoreMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [overlayLive, setOverlayLive] = useState(false);
  const [live, setLive] = useState<LiveTrace | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const liveRequest = useRef(0);

  const deviceName = useMemo(
    () => new Map(devices.map((device) => [device.key, device.name || device.key])),
    [devices],
  );

  useEffect(() => {
    if (!opened) return;
    setFilters((current) => ({ ...current, deviceKey: initialDeviceKey }));
    setSelectedId(null);
  }, [opened, initialDeviceKey]);

  const loadPage = useCallback(
    async (offset: number) => {
      setListLoading(true);
      setListError(null);
      try {
        const query = historyQuery(filters, Date.now() / 1000, PAGE_SIZE, offset);
        const page = await api.listLockAcquisitions(query);
        setItems((current) => (offset === 0 ? page.items : [...current, ...page.items]));
        setHasMore(page.items.length === PAGE_SIZE);
        setListInfo({
          source: page.source,
          unsent: page.unsent_records,
          reason: page.postgres_unavailable_reason,
        });
      } catch (error) {
        setListError(errorMessage(error));
        if (offset === 0) setItems([]);
      } finally {
        setListLoading(false);
      }
    },
    [filters],
  );

  useEffect(() => {
    if (opened) loadPage(0).catch(() => null);
  }, [opened, loadPage]);

  const loadComparison = useCallback(async (target: LockAcquisitionSummary) => {
    setComparisonError(null);
    try {
      setComparison(await api.compareLockSettings(target.device_key, target.lock_acquisition_id));
    } catch (error) {
      setComparison(null);
      setComparisonError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    setRecord(null);
    setRecordError(null);
    setComparison(null);
    setComparisonError(null);
    setConfirmRestore(false);
    setRestoreMessage(null);
    setLive(null);
    setLiveError(null);
    setOverlayLive(false);
    if (!selectedId) return;
    let cancelled = false;
    api
      .getLockAcquisition(selectedId)
      .then((full) => {
        if (cancelled) return;
        setRecord(full);
        loadComparison(full).catch(() => null);
      })
      .catch((error) => {
        if (!cancelled) setRecordError(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, loadComparison]);

  const currentSweep = useMemo(() => {
    const byName = new Map(comparison?.parameters.map((row) => [row.name, row.now]) ?? []);
    const center = byName.get('sweep_center');
    const amplitude = byName.get('sweep_amplitude');
    return typeof center === 'number' && typeof amplitude === 'number' ? { center, amplitude } : null;
  }, [comparison]);

  const captureLive = useCallback(() => {
    if (!record) return;
    if (!currentSweep) {
      setLiveError("The board's current sweep isn't known, so its trace can't be placed.");
      return;
    }
    const request = ++liveRequest.current;
    setLiveError(null);
    const handle = openDeviceStream(record.device_key, (message: StreamMessage) => {
      if (request !== liveRequest.current || message.type !== 'plot_frame') return;
      handle.socket.close();
      handle.disposeParser();
      if (message.lock) {
        setLiveError('The device is locked, so there is no sweep to compare with.');
        return;
      }
      const raw = message.series.error_signal_1 ?? message.series.combined_error;
      if (!raw || raw.length === 0) {
        setLiveError('The current frame has no error signal.');
        return;
      }
      const n = raw.length;
      const y: Array<number | null> = [];
      const x: Array<number | null> = [];
      for (let i = 0; i < n; i += 1) {
        const value = raw[i];
        y.push(value === null || !Number.isFinite(value as number) ? null : (value as number));
        x.push(sweepIndexToVoltage(i, n, currentSweep.center, currentSweep.amplitude));
      }
      setLive({ x, y, at: Date.now() });
    });
    window.setTimeout(() => {
      if (request !== liveRequest.current) return;
      if (handle.socket.readyState <= WebSocket.OPEN) {
        handle.socket.close();
        handle.disposeParser();
      }
    }, 5000);
  }, [record, currentSweep]);

  useEffect(() => {
    if (overlayLive) captureLive();
    else {
      liveRequest.current += 1;
      setLive(null);
      setLiveError(null);
    }
  }, [overlayLive, captureLive]);

  const changes = useMemo(
    () => comparison?.parameters.filter((row) => row.restorable && row.differs) ?? [],
    [comparison],
  );

  const restore = async () => {
    if (!record) return;
    setRestoring(true);
    setRestoreMessage(null);
    try {
      const result = await api.restoreLockSettings(record.device_key, record.lock_acquisition_id);
      setRestoreMessage({
        ok: true,
        text:
          result.applied.length === 0
            ? 'Nothing to restore: the board already has these settings.'
            : `Restored ${result.applied.length} setting${result.applied.length === 1 ? '' : 's'}: ${result.applied
                .map((row) => SETTING_LABEL[row.name] ?? row.name)
                .join(', ')}.`,
      });
      setConfirmRestore(false);
      await loadComparison(record);
    } catch (error) {
      setRestoreMessage({ ok: false, text: errorMessage(error) });
    } finally {
      setRestoring(false);
    }
  };

  const downloadRecord = () => {
    if (!record) return;
    const { filename, json } = exportRecord(record);
    const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  };

  const copyId = () => {
    if (record) navigator.clipboard?.writeText(record.lock_acquisition_id).catch(() => null);
  };

  const deviceOptions = [
    { value: ALL_DEVICES, label: 'All devices' },
    ...devices.map((device) => ({ value: device.key, label: device.name || device.key })),
  ];
  // Without a board to compare with, still show what was saved.
  const savedRows = useMemo(
    () =>
      Object.entries(record?.linien_config?.parameters ?? {}).map(([name, value]) => ({
        name,
        at_lock: value,
        now: null,
        restorable: false,
        differs: false,
      })),
    [record],
  );
  const allRows = comparison?.parameters ?? savedRows;
  const markers = useMemo(() => (record ? lockMarkers(record) : null), [record]);
  const settingRows = orderSettings(allRows, showAllSettings);
  const traceX = record?.trace_x ?? null;
  const traceY = record?.trace_y ?? null;
  const title = filters.deviceKey
    ? `Lock history — ${deviceName.get(filters.deviceKey) ?? filters.deviceKey}`
    : 'Lock history';

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={
        <Group gap={6}>
          <IconHistory size={18} />
          <Text fw={600}>{title}</Text>
        </Group>
      }
      size="clamp(56rem, 92vw, 96rem)"
      centered
      zIndex={430}
    >
      <Stack gap="sm">
        <Group grow align="flex-end">
          <Select
            label="Device"
            comboboxProps={{ zIndex: 500 }}
            data={deviceOptions}
            value={filters.deviceKey ?? ALL_DEVICES}
            onChange={(value) =>
              setFilters((f) => ({ ...f, deviceKey: !value || value === ALL_DEVICES ? null : value }))
            }
          />
          <Select
            label="Source"
            comboboxProps={{ zIndex: 500 }}
            data={[
              { value: 'any', label: 'Any source' },
              ...Object.entries(LOCK_SOURCE_LABEL).map(([value, label]) => ({ value, label })),
            ]}
            value={filters.source ?? 'any'}
            onChange={(value) =>
              setFilters((f) => ({ ...f, source: !value || value === 'any' ? null : (value as LockSource) }))
            }
          />
          <Select
            label="Outcome"
            comboboxProps={{ zIndex: 500 }}
            data={[
              { value: 'any', label: 'Any outcome' },
              { value: 'verified', label: 'Verified' },
              { value: 'failed', label: 'Failed' },
              { value: 'unavailable', label: 'Not verifiable' },
              { value: 'rejected', label: 'Rejected' },
            ]}
            value={filters.outcome}
            onChange={(value) => setFilters((f) => ({ ...f, outcome: (value ?? 'any') as OutcomeFilter }))}
          />
          <Select
            label="Time"
            comboboxProps={{ zIndex: 500 }}
            data={[
              { value: '24h', label: 'Last 24 hours' },
              { value: '7d', label: 'Last 7 days' },
              { value: '30d', label: 'Last 30 days' },
              { value: 'all', label: 'All time' },
            ]}
            value={filters.range}
            onChange={(value) => setFilters((f) => ({ ...f, range: (value ?? '7d') as RangeFilter }))}
          />
          <Button
            variant="light"
            leftSection={<IconRefresh size={14} />}
            loading={listLoading}
            onClick={() => loadPage(0).catch(() => null)}
            style={{ flexGrow: 0 }}
          >
            Reload
          </Button>
        </Group>

        {listInfo ? (
          <Text size="xs" c="dimmed">
            {listInfo.source === 'postgres'
              ? 'From PostgreSQL.'
              : `From the gateway's local buffer: PostgreSQL ${
                  listInfo.reason === 'postgres_logging_disabled' ? 'logging is off' : "isn't reachable"
                }.`}
            {listInfo.unsent > 0 ? ` ${listInfo.unsent} record(s) not yet sent to PostgreSQL.` : ''}
          </Text>
        ) : null}
        {listError ? <Alert color="red">{listError}</Alert> : null}

        <Card withBorder padding={0}>
          {/* The whole window for the list until an attempt is picked; then it
              makes room for the detail below. */}
          <ScrollArea h={selectedId ? 260 : 'max(320px, calc(100vh - 300px))'}>
            <Table highlightOnHover stickyHeader verticalSpacing={6} fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Time</Table.Th>
                  <Table.Th>Device</Table.Th>
                  <Table.Th>Source</Table.Th>
                  <Table.Th>Command</Table.Th>
                  <Table.Th>Verification</Table.Th>
                  <Table.Th>Modulation</Table.Th>
                  <Table.Th>Slope</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {items.map((item) => {
                  const command = commandDisplay(item.lock_command_status);
                  const verification = verificationDisplay(item);
                  return (
                    <Table.Tr
                      key={item.lock_acquisition_id}
                      onClick={() => setSelectedId(item.lock_acquisition_id)}
                      style={{ cursor: 'pointer' }}
                      bg={item.lock_acquisition_id === selectedId ? 'var(--mantine-color-blue-light)' : undefined}
                    >
                      <Table.Td>{formatTime(item.attempt_started_at)}</Table.Td>
                      <Table.Td>{deviceName.get(item.device_key) ?? item.laser_name ?? item.device_key}</Table.Td>
                      <Table.Td>{LOCK_SOURCE_LABEL[item.lock_source] ?? item.lock_source}</Table.Td>
                      <Table.Td>
                        <Badge variant="light" color={command.tone}>
                          {command.label}
                        </Badge>
                      </Table.Td>
                      <Table.Td>
                        <Badge variant="light" color={verification.tone}>
                          {verification.label}
                        </Badge>
                      </Table.Td>
                      <Table.Td>{formatMHz(item.modulation_frequency_hz)}</Table.Td>
                      <Table.Td>{formatNumber(item.discriminator_slope_v_per_mhz, 'V/MHz')}</Table.Td>
                    </Table.Tr>
                  );
                })}
                {items.length === 0 && !listLoading ? (
                  <Table.Tr>
                    <Table.Td colSpan={7}>
                      <Text size="sm" c="dimmed" ta="center" py="md">
                        No lock attempts match these filters.
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ) : null}
              </Table.Tbody>
            </Table>
            {hasMore ? (
              <Group justify="center" py="xs">
                <Button size="xs" variant="subtle" loading={listLoading} onClick={() => loadPage(items.length)}>
                  Load more
                </Button>
              </Group>
            ) : null}
          </ScrollArea>
        </Card>

        {recordError ? <Alert color="red">{recordError}</Alert> : null}
        {selectedId && !record && !recordError ? (
          <Text size="sm" c="dimmed">
            Loading the attempt…
          </Text>
        ) : null}

        {record ? (
          <Card withBorder padding="md">
            <Stack gap="sm">
              <Group justify="space-between" align="flex-start">
                <Stack gap={2}>
                  <Text fw={600}>
                    {deviceName.get(record.device_key) ?? record.laser_name ?? record.device_key},{' '}
                    {(LOCK_SOURCE_LABEL[record.lock_source] ?? record.lock_source).toLowerCase()}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {formatTime(record.attempt_started_at)} · {record.lock_acquisition_id}
                  </Text>
                </Stack>
                <Group gap="xs">
                  <Button size="xs" variant="default" leftSection={<IconCopy size={14} />} onClick={copyId}>
                    Copy ID
                  </Button>
                  <Button size="xs" variant="default" leftSection={<IconDownload size={14} />} onClick={downloadRecord}>
                    Export JSON
                  </Button>
                  <Button size="xs" variant="subtle" color="gray" onClick={() => setSelectedId(null)}>
                    Back to list
                  </Button>
                </Group>
              </Group>
              <Text size="sm">{describeAttempt(record)}</Text>
              {markers?.summary ? <Text size="sm">{markers.summary}</Text> : null}

              <SimpleGrid cols={{ base: 2, md: 4 }} spacing="xs">
                {[
                  ['Zero crossing', formatNumber(record.zero_crossing_sweep_v, 'V')],
                  ['Slope', formatNumber(record.discriminator_slope_v_per_mhz, 'V/MHz')],
                  ['Peak to peak', formatNumber(record.peak_to_peak_v === null ? null : record.peak_to_peak_v * 1000, 'mV', 1)],
                  [
                    'Sweep',
                    record.sweep_center_v === null || record.sweep_amplitude_v === null
                      ? '—'
                      : `${Number(record.sweep_center_v.toFixed(3))} ± ${Number(record.sweep_amplitude_v.toFixed(3))} V`,
                  ],
                ].map(([label, value]) => (
                  <Card key={label} padding="xs" bg="var(--mantine-color-default-hover)">
                    <Text size="xs" c="dimmed">
                      {label}
                    </Text>
                    <Text fw={600}>{value}</Text>
                  </Card>
                ))}
              </SimpleGrid>

              {traceX && traceY ? (
                <>
                  <Group justify="space-between">
                    <Text size="sm" fw={600}>
                      Trace at lock time
                    </Text>
                    <Group gap="xs">
                      {overlayLive ? (
                        <Button size="xs" variant="subtle" onClick={captureLive}>
                          Refresh current trace
                        </Button>
                      ) : null}
                      <Switch
                        size="sm"
                        label="Overlay the current trace"
                        checked={overlayLive}
                        onChange={(event) => setOverlayLive(event.currentTarget.checked)}
                      />
                    </Group>
                  </Group>
                  {liveError ? (
                    <Text size="xs" c="red">
                      {liveError}
                    </Text>
                  ) : null}
                  <LockTracePlot
                    x={traceX}
                    error={traceY}
                    monitor={record.secondary_trace_y ?? null}
                    monitorLabel={record.secondary_trace_role === 'monitor' ? 'Monitor' : 'Error B'}
                    live={live}
                    markers={markers?.markers}
                  />
                  <Text size="xs" c="dimmed">
                    Blue: error signal (left axis). Grey: monitor (right axis). {markers?.legend} Drag across
                    the plot to zoom, double-click to reset.
                  </Text>
                </>
              ) : (
                <Text size="sm" c="dimmed">
                  No trace was recorded for this attempt.
                </Text>
              )}

              <Group justify="space-between" mt="xs">
                <Group gap="xs">
                  <Text size="sm" fw={600}>
                    Settings at lock time
                  </Text>
                  <Text size="xs" c="dimmed">
                    vs now; changes highlighted
                  </Text>
                </Group>
                <Group gap="xs">
                  <Button size="xs" variant="subtle" onClick={() => setShowAllSettings((v) => !v)}>
                    {showAllSettings ? 'Show key settings' : `Show all ${allRows.length} parameters`}
                  </Button>
                  <Button
                    size="xs"
                    variant="light"
                    color="orange"
                    leftSection={<IconArrowBackUp size={14} />}
                    onClick={() => setConfirmRestore(true)}
                    disabled={!comparison}
                  >
                    Restore these settings
                  </Button>
                </Group>
              </Group>

              {comparisonError ? (
                <Alert color="yellow">
                  Can't compare with the board right now: {comparisonError}
                </Alert>
              ) : null}

              {confirmRestore && comparison ? (
                <Alert color={comparison.locked ? 'red' : 'orange'} title="Restore settings">
                  {comparison.locked ? (
                    <Text size="sm">The device is locked. Unlock it before restoring settings.</Text>
                  ) : changes.length === 0 ? (
                    <Text size="sm">The board already has every restorable setting from this attempt.</Text>
                  ) : (
                    <Stack gap={4}>
                      <Text size="sm">
                        These {changes.length} setting{changes.length === 1 ? '' : 's'} will be written to the
                        board:
                      </Text>
                      {changes.map((row) => (
                        <Text size="sm" key={row.name}>
                          {SETTING_LABEL[row.name] ?? row.name}: {formatSetting(row.name, row.now)} →{' '}
                          {formatSetting(row.name, row.at_lock)}
                        </Text>
                      ))}
                    </Stack>
                  )}
                  <Group gap="xs" mt="sm">
                    {!comparison.locked && changes.length > 0 ? (
                      <Button size="xs" color="orange" loading={restoring} onClick={restore}>
                        Restore {changes.length} setting{changes.length === 1 ? '' : 's'}
                      </Button>
                    ) : null}
                    <Button size="xs" variant="default" onClick={() => setConfirmRestore(false)}>
                      Cancel
                    </Button>
                  </Group>
                </Alert>
              ) : null}
              {restoreMessage ? (
                <Alert color={restoreMessage.ok ? 'teal' : 'red'}>{restoreMessage.text}</Alert>
              ) : null}

              {settingRows.length > 0 ? (
                <Table fz="sm" verticalSpacing={4}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Parameter</Table.Th>
                      <Table.Th>At lock</Table.Th>
                      <Table.Th>Now</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {settingRows.map((row) => {
                      const color = row.differs ? 'var(--mantine-color-orange-text)' : undefined;
                      return (
                        <Table.Tr key={row.name}>
                          <Table.Td>{SETTING_LABEL[row.name] ?? row.name}</Table.Td>
                          <Table.Td style={{ color }}>{formatSetting(row.name, row.at_lock)}</Table.Td>
                          <Table.Td style={{ color }}>
                            {comparison ? formatSetting(row.name, row.now) : '—'}
                          </Table.Td>
                        </Table.Tr>
                      );
                    })}
                  </Table.Tbody>
                </Table>
              ) : null}
            </Stack>
          </Card>
        ) : null}
      </Stack>
    </Modal>
  );
}
