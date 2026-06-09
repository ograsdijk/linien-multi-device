import {
  Button,
  Group,
  MultiSelect,
  PasswordInput,
  Popover,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import {
  IconDatabase,
  IconFileText,
  IconMoonStars,
  IconSun,
} from '@tabler/icons-react';
import { useState } from 'react';
import type {
  Device,
  InfluxCredentials,
  ParamMeta,
  PostgresManualLockConfig,
  PostgresManualLockStatus,
} from '../types';
import type {
  InfluxApplyAllOptions,
  InfluxApplyAllResult,
} from '../features/integrations/useInfluxController';
import { toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';
import { DeferredNumberInput } from './DeferredNumberInput';
import { LockChipPopover } from './header/LockChipPopover';

const formatTimestamp = (value: number | null | undefined) => {
  if (!value || !Number.isFinite(value)) return 'never';
  return new Date(value * 1000).toLocaleString();
};

type AppHeaderControlsProps = {
  logsChipColor: string;
  onOpenLogs: () => void;
  influxPopoverOpen: boolean;
  setInfluxPopoverOpen: (open: boolean) => void;
  influxChipColor: string;
  influxLabel: string;
  influxDeviceOptions: Array<{ value: string; label: string }>;
  influxDeviceKey: string | null;
  onInfluxDeviceChange: (value: string | null) => void;
  influxBusy: boolean;
  influxDeviceConnected: boolean;
  influxLoggingActive: boolean;
  influxSelectedDevice: Device | null;
  influxCredentials: InfluxCredentials;
  onInfluxCredentialChange: (name: keyof InfluxCredentials, value: string) => void;
  influxInterval: number;
  setInfluxInterval: (value: number) => void;
  startInfluxLogging: () => void;
  stopInfluxLogging: () => void;
  saveInfluxCredentials: () => void;
  influxParams: ParamMeta[];
  selectedInfluxParamNames: string[];
  onInfluxParamSelection: (values: string[]) => void;
  applyInfluxToAll: (options: InfluxApplyAllOptions) => Promise<InfluxApplyAllResult>;
  influxMessage: string | null;
  influxMessageError: boolean;
  lockPopoverOpen: boolean;
  setLockPopoverOpen: (open: boolean) => void;
  devices: Device[];
  lockBusyKeys: Record<string, boolean>;
  autoLockBusyKeys: Record<string, boolean>;
  autoRelockBusyKeys: Record<string, boolean>;
  onStartAutoLock: (deviceKey: string) => void;
  onDisableLock: (deviceKey: string) => void;
  onToggleAutoRelock: (deviceKey: string, enabled: boolean) => void;
  postgresPopoverOpen: boolean;
  setPostgresPopoverOpen: (open: boolean) => void;
  postgresChipColor: string;
  postgresLabel: string;
  postgresStatus?: PostgresManualLockStatus | null;
  postgresConfig: PostgresManualLockConfig;
  postgresDraft: PostgresManualLockConfig;
  updatePostgresDraft: (name: keyof PostgresManualLockConfig, value: unknown) => void;
  postgresBusy: boolean;
  postgresMessage: string | null;
  testPostgresConnection: () => void;
  savePostgresConfig: () => void;
  colorScheme: 'light' | 'dark' | 'auto';
  setColorScheme: (value: 'light' | 'dark' | 'auto') => void;
};

export function AppHeaderControls(props: AppHeaderControlsProps) {
  const [applyAllCredentials, setApplyAllCredentials] = useState(true);
  const [applyAllParams, setApplyAllParams] = useState(false);
  const [applyAllInterval, setApplyAllInterval] = useState(false);
  const [applyAllLoggingState, setApplyAllLoggingState] = useState(false);
  const [applyAllResult, setApplyAllResult] = useState<InfluxApplyAllResult | null>(null);

  return (
    <Group gap="xs" align="center">
      <div style={{ order: 98 }}>
        <Button
          size="xs"
          variant="light"
          color={props.logsChipColor}
          leftSection={<IconFileText size={14} />}
          onClick={props.onOpenLogs}
        >
          Logs
        </Button>
      </div>
      <div style={{ order: 2 }}>
        <Popover
          opened={props.influxPopoverOpen}
          onChange={props.setInfluxPopoverOpen}
          position="bottom-end"
          shadow="md"
          width={420}
          withArrow
        >
          <Popover.Target>
            <Button
              size="xs"
              variant="light"
              color={props.influxChipColor}
              leftSection={<IconDatabase size={14} />}
              onClick={() => props.setInfluxPopoverOpen(!props.influxPopoverOpen)}
            >
              InfluxDB - {props.influxLabel}
            </Button>
          </Popover.Target>
          <Popover.Dropdown>
            <Stack gap="xs">
              <Text fw={600}>InfluxDB logging</Text>
              <Select
                label="Device"
                comboboxProps={{ withinPortal: false }}
                data={props.influxDeviceOptions}
                value={props.influxDeviceKey}
                onChange={(value) => {
                  props.onInfluxDeviceChange(value);
                }}
                searchable
                disabled={props.influxBusy || props.influxDeviceOptions.length === 0}
                placeholder={
                  props.influxDeviceOptions.length > 0
                    ? 'Select device'
                    : 'No devices configured'
                }
              />
              <Text size="xs" c="dimmed">
                Status:{' '}
                {props.influxDeviceConnected
                  ? props.influxLoggingActive
                    ? 'Logging active'
                    : 'Connected, idle'
                  : 'Disconnected'}
                {props.influxSelectedDevice
                  ? ` | ${props.influxSelectedDevice.host}:${props.influxSelectedDevice.port}`
                  : ''}
              </Text>
              <TextInput
                label="URL"
                value={props.influxCredentials.url}
                onChange={(event) =>
                  props.onInfluxCredentialChange('url', event.currentTarget.value)
                }
                disabled={props.influxBusy || !props.influxDeviceConnected}
              />
              <Group grow>
                <TextInput
                  label="Org"
                  value={props.influxCredentials.org}
                  onChange={(event) =>
                    props.onInfluxCredentialChange('org', event.currentTarget.value)
                  }
                  disabled={props.influxBusy || !props.influxDeviceConnected}
                />
                <TextInput
                  label="Bucket"
                  value={props.influxCredentials.bucket}
                  onChange={(event) =>
                    props.onInfluxCredentialChange('bucket', event.currentTarget.value)
                  }
                  disabled={props.influxBusy || !props.influxDeviceConnected}
                />
              </Group>
              <PasswordInput
                label="Token"
                value={props.influxCredentials.token}
                onChange={(event) =>
                  props.onInfluxCredentialChange('token', event.currentTarget.value)
                }
                disabled={props.influxBusy || !props.influxDeviceConnected}
              />
              <TextInput
                label="Measurement"
                value={props.influxCredentials.measurement}
                onChange={(event) =>
                  props.onInfluxCredentialChange('measurement', event.currentTarget.value)
                }
                disabled={props.influxBusy || !props.influxDeviceConnected}
              />
              <Group grow>
                <DeferredNumberInput
                  label="Interval (s)"
                  value={props.influxInterval}
                  min={0.1}
                  step={0.1}
                  onCommit={(value) => props.setInfluxInterval(toFiniteNumberOr(value, 1))}
                  disabled={
                    props.influxBusy ||
                    !props.influxDeviceConnected ||
                    props.influxLoggingActive
                  }
                />
                <Button
                  mt={22}
                  variant={props.influxLoggingActive ? 'light' : 'filled'}
                  color={props.influxLoggingActive ? 'red' : 'green'}
                  onClick={
                    props.influxLoggingActive
                      ? props.stopInfluxLogging
                      : props.startInfluxLogging
                  }
                  disabled={!props.influxDeviceConnected}
                  loading={props.influxBusy}
                >
                  {props.influxLoggingActive ? 'Stop logging' : 'Start logging'}
                </Button>
              </Group>
              <Button
                size="xs"
                variant="light"
                color="orange"
                onClick={props.saveInfluxCredentials}
                disabled={!props.influxDeviceConnected}
                loading={props.influxBusy}
              >
                Update credentials
              </Button>
              <Text fw={500} size="sm" mt={4}>
                Logged parameters
              </Text>
              <MultiSelect
                data={props.influxParams.map((param) => ({
                  value: param.name,
                  label: param.name,
                }))}
                value={props.selectedInfluxParamNames}
                onChange={props.onInfluxParamSelection}
                searchable
                clearable
                maxDropdownHeight={220}
                comboboxProps={{ withinPortal: false }}
                placeholder={
                  props.influxDeviceConnected
                    ? props.influxParams.length === 0
                      ? 'No loggable parameters available'
                      : 'Select parameters to log'
                    : 'Connect device to load parameters'
                }
                disabled={
                  props.influxBusy ||
                  !props.influxDeviceConnected ||
                  props.influxParams.length === 0
                }
              />
              {props.influxMessage ? (
                <Text size="xs" c={props.influxMessageError ? 'red' : 'dimmed'}>
                  {props.influxMessage}
                </Text>
              ) : null}
              <Text fw={500} size="sm" mt={4}>
                Apply to all devices
              </Text>
              <Stack gap={4}>
                <Switch
                  label="Credentials"
                  checked={applyAllCredentials}
                  onChange={(event) => setApplyAllCredentials(event.currentTarget.checked)}
                  disabled={props.influxBusy}
                />
                <Switch
                  label="Logged parameters"
                  checked={applyAllParams}
                  onChange={(event) => setApplyAllParams(event.currentTarget.checked)}
                  disabled={props.influxBusy}
                />
                <Switch
                  label="Interval"
                  checked={applyAllInterval}
                  onChange={(event) => setApplyAllInterval(event.currentTarget.checked)}
                  disabled={props.influxBusy}
                />
                <Switch
                  label="Logging state (start/stop)"
                  checked={applyAllLoggingState}
                  onChange={(event) => setApplyAllLoggingState(event.currentTarget.checked)}
                  disabled={props.influxBusy}
                />
              </Stack>
              <Button
                size="xs"
                variant="default"
                disabled={
                  props.influxBusy ||
                  (!applyAllCredentials &&
                    !applyAllParams &&
                    !applyAllInterval &&
                    !applyAllLoggingState)
                }
                loading={props.influxBusy}
                onClick={() => {
                  props
                    .applyInfluxToAll({
                      applyCredentials: applyAllCredentials,
                      applyParams: applyAllParams,
                      applyInterval: applyAllInterval,
                      applyLoggingState: applyAllLoggingState,
                    })
                    .then((result) => setApplyAllResult(result))
                    .catch(() => setApplyAllResult(null));
                }}
              >
                Apply to all devices
              </Button>
              {applyAllResult ? (
                <Text size="xs" c={applyAllResult.failed > 0 ? 'red' : 'dimmed'}>
                  {applyAllResult.succeeded}/{applyAllResult.total} succeeded
                  {applyAllResult.failed > 0 ? ` (${applyAllResult.failed} failed)` : ''}
                </Text>
              ) : null}
              {applyAllResult && applyAllResult.failures.length > 0 ? (
                <details>
                  <summary>
                    <Text span size="xs" c="dimmed">
                      Failed devices
                    </Text>
                  </summary>
                  <Stack gap={2} mt={4}>
                    {applyAllResult.failures.map((item) => (
                      <Text key={item.deviceKey} size="xs" c="red">
                        {item.deviceKey}: {item.message}
                      </Text>
                    ))}
                  </Stack>
                </details>
              ) : null}
            </Stack>
          </Popover.Dropdown>
        </Popover>
      </div>
      <div style={{ order: 0 }}>
        <LockChipPopover
          opened={props.lockPopoverOpen}
          onOpenedChange={props.setLockPopoverOpen}
          devices={props.devices}
          lockBusyKeys={props.lockBusyKeys}
          autoLockBusyKeys={props.autoLockBusyKeys}
          autoRelockBusyKeys={props.autoRelockBusyKeys}
          onStartAutoLock={props.onStartAutoLock}
          onDisableLock={props.onDisableLock}
          onToggleAutoRelock={props.onToggleAutoRelock}
        />
      </div>
      <div style={{ order: 3 }}>
        <Popover
          opened={props.postgresPopoverOpen}
          onChange={props.setPostgresPopoverOpen}
          position="bottom-end"
          shadow="md"
          width={360}
          withArrow
        >
          <Popover.Target>
            <Button
              size="xs"
              variant="light"
              color={props.postgresChipColor}
              leftSection={<IconDatabase size={14} />}
              onClick={() => props.setPostgresPopoverOpen(!props.postgresPopoverOpen)}
            >
              Postgres - {props.postgresLabel}
            </Button>
          </Popover.Target>
          <Popover.Dropdown>
            <Stack gap="xs">
              <Text fw={600}>Lock Postgres logging</Text>
              <Text size="xs" c="dimmed">
                Active: {props.postgresStatus?.active ? 'yes' : 'no'} | Host: {props.postgresConfig.host}:{' '}
                {props.postgresConfig.port}
              </Text>
              <Switch
                label="Enable lock logging"
                checked={Boolean(props.postgresDraft.enabled)}
                onChange={(event) =>
                  props.updatePostgresDraft('enabled', event.currentTarget.checked)
                }
                disabled={props.postgresBusy}
              />
              <Group grow>
                <TextInput
                  label="Host / IP"
                  value={props.postgresDraft.host}
                  onChange={(event) =>
                    props.updatePostgresDraft('host', event.currentTarget.value)
                  }
                  disabled={props.postgresBusy}
                />
                <DeferredNumberInput
                  label="Port"
                  value={props.postgresDraft.port}
                  min={1}
                  max={65535}
                  parseCommit={(value) => toRoundedIntOr(value, 5432, 1, 65535)}
                  onCommit={(value) =>
                    props.updatePostgresDraft(
                      'port',
                      value
                    )
                  }
                  disabled={props.postgresBusy}
                />
              </Group>
              <Group grow>
                <TextInput
                  label="Database"
                  value={props.postgresDraft.database}
                  onChange={(event) =>
                    props.updatePostgresDraft('database', event.currentTarget.value)
                  }
                  disabled={props.postgresBusy}
                />
                <TextInput
                  label="User"
                  value={props.postgresDraft.user}
                  onChange={(event) =>
                    props.updatePostgresDraft('user', event.currentTarget.value)
                  }
                  disabled={props.postgresBusy}
                />
              </Group>
              <PasswordInput
                label="Password"
                value={props.postgresDraft.password}
                onChange={(event) => props.updatePostgresDraft('password', event.currentTarget.value)}
                disabled={props.postgresBusy}
              />
              <Group grow>
                <Select
                  label="SSL mode"
                  data={[
                    { value: 'disable', label: 'disable' },
                    { value: 'allow', label: 'allow' },
                    { value: 'prefer', label: 'prefer' },
                    { value: 'require', label: 'require' },
                  ]}
                  value={props.postgresDraft.sslmode}
                  onChange={(value) =>
                    props.updatePostgresDraft('sslmode', value ?? props.postgresDraft.sslmode)
                  }
                  disabled={props.postgresBusy}
                />
                <DeferredNumberInput
                  label="Timeout (s)"
                  value={props.postgresDraft.connect_timeout_s}
                  min={1}
                  max={30}
                  step={1}
                  parseCommit={(value) => toRoundedIntOr(value, 3, 1, 30)}
                  onCommit={(value) =>
                    props.updatePostgresDraft(
                      'connect_timeout_s',
                      value
                    )
                  }
                  disabled={props.postgresBusy}
                />
              </Group>
              <Text size="xs" c="dimmed">
                Queue: {props.postgresStatus?.queue_size ?? 0} | Enqueued:{' '}
                {props.postgresStatus?.enqueued_count ?? 0} | Writes OK:{' '}
                {props.postgresStatus?.write_ok_count ?? 0} | Writes failed:{' '}
                {props.postgresStatus?.write_error_count ?? 0}
              </Text>
              <Text size="xs" c="dimmed">
                Last test: {formatTimestamp(props.postgresStatus?.last_test_at)} | Last write:{' '}
                {formatTimestamp(props.postgresStatus?.last_write_at)}
              </Text>
              {props.postgresStatus?.last_error ? (
                <Text size="xs" c="red">
                  {props.postgresStatus.last_error}
                </Text>
              ) : null}
              {props.postgresMessage ? <Text size="xs">{props.postgresMessage}</Text> : null}
              <Group justify="flex-end" mt={4}>
                <Button
                  size="xs"
                  variant="default"
                  onClick={props.testPostgresConnection}
                  loading={props.postgresBusy}
                >
                  Test connection
                </Button>
                <Button
                  size="xs"
                  color="orange"
                  onClick={props.savePostgresConfig}
                  loading={props.postgresBusy}
                >
                  Save
                </Button>
              </Group>
            </Stack>
          </Popover.Dropdown>
        </Popover>
      </div>
      <SegmentedControl
        style={{ order: 99 }}
        size="xs"
        value={props.colorScheme}
        onChange={(value) => props.setColorScheme(value as 'light' | 'dark' | 'auto')}
        data={[
          { value: 'light', label: <IconSun size={14} /> },
          { value: 'dark', label: <IconMoonStars size={14} /> },
          { value: 'auto', label: 'Auto' },
        ]}
      />
    </Group>
  );
}
