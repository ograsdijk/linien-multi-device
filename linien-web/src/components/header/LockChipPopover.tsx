import { memo } from 'react';
import { Button, Group, Popover, Stack, Text } from '@mantine/core';
import { IconLock } from '@tabler/icons-react';
import type { Device } from '../../types';
import { resolveLockDisplay } from '../../features/locks/lockState';
import { useLockSummary } from '../../features/locks/useLockSummary';

type LockChipPopoverProps = {
  opened: boolean;
  onOpenedChange: (open: boolean) => void;
  devices: Device[];
  lockBusyKeys: Record<string, boolean>;
  autoLockBusyKeys: Record<string, boolean>;
  autoRelockBusyKeys: Record<string, boolean>;
  onStartAutoLock: (deviceKey: string) => void;
  onDisableLock: (deviceKey: string) => void;
  onToggleAutoRelock: (deviceKey: string, enabled: boolean) => void;
};

// Self-subscribing lock-chip popover. Reads the lock summary directly so
// adding this popover to the header does not require AppHeaderControls
// to receive every map + counter as a prop.
export const LockChipPopover = memo(function LockChipPopover({
  opened,
  onOpenedChange,
  devices,
  lockBusyKeys,
  autoLockBusyKeys,
  autoRelockBusyKeys,
  onStartAutoLock,
  onDisableLock,
  onToggleAutoRelock,
}: LockChipPopoverProps) {
  const {
    deviceStatusMap,
    lockStateMap,
    lockIndicatorMap,
    autoRelockMap,
    lockHealthSummary,
    connectedDeviceCount,
    lockedDeviceCount,
    connectedRelockEnabledCount,
  } = useLockSummary(devices);

  const lockChipTone =
    connectedDeviceCount === 0
      ? 'gray'
      : lockHealthSummary.lost > 0
      ? 'red'
      : lockHealthSummary.considered === 0
      ? 'gray'
      : lockHealthSummary.marginalOrUnknown > 0
      ? 'yellow'
      : 'green';
  const lockLabel =
    devices.length === 0
      ? 'No devices'
      : `${lockedDeviceCount}/${connectedDeviceCount} locked | ${connectedRelockEnabledCount}/${connectedDeviceCount} relock`;

  return (
    <Popover
      opened={opened}
      onChange={onOpenedChange}
      position="bottom-end"
      shadow="md"
      width={480}
      withArrow
    >
      <Popover.Target>
        <Button
          size="xs"
          variant="light"
          className="lock-chip-button"
          data-tone={lockChipTone}
          leftSection={<IconLock size={14} />}
          onClick={() => onOpenedChange(!opened)}
        >
          Lock - {lockLabel}
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Text fw={600}>Lock controls</Text>
          <Text size="xs" c="dimmed">
            Locked: {lockedDeviceCount}/{connectedDeviceCount} connected devices | Auto relock
            enabled: {connectedRelockEnabledCount}/{connectedDeviceCount}
          </Text>
          {devices.length === 0 ? (
            <Text size="xs" c="dimmed">
              No devices configured.
            </Text>
          ) : null}
          <Stack gap={6}>
            {/* Skip rendering the per-device row tree when the popover is
                closed. Mantine mounts Popover.Dropdown children regardless
                of `opened`, so without this gate we re-render 12 device
                rows + 36 Mantine Buttons on every lock-summary flush. */}
            {opened ? devices.map((device) => {
              const status = deviceStatusMap[device.key];
              const connected = Boolean(status?.connected);
              const lockState = lockStateMap[device.key];
              const indicator = lockIndicatorMap[device.key];
              const lockDisplay = resolveLockDisplay({
                connected,
                lockEnabled: lockState,
                indicator,
              });
              const autoRelock =
                autoRelockMap[device.key] ?? status?.auto_relock ?? undefined;
              const autoRelockEnabled = Boolean(autoRelock?.enabled);
              const disableBusy = Boolean(lockBusyKeys[device.key]);
              const autoLockBusy = Boolean(autoLockBusyKeys[device.key]);
              const relockBusy = Boolean(autoRelockBusyKeys[device.key]);
              return (
                <Group key={device.key} justify="space-between" align="flex-start" wrap="nowrap">
                  <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
                    <Text size="sm" fw={500}>
                      {device.name || device.key}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {device.host}:{device.port}
                    </Text>
                    <Text size="xs" c={lockDisplay.color}>
                      {lockDisplay.label}
                    </Text>
                  </Stack>
                  <Stack gap={4} align="flex-end" style={{ minWidth: 300 }}>
                    <Group gap={6} wrap="nowrap">
                      <Button
                        size="xs"
                        variant="light"
                        color="blue"
                        disabled={!connected || lockState === true || autoLockBusy}
                        loading={autoLockBusy}
                        onClick={() => onStartAutoLock(device.key)}
                      >
                        Auto lock
                      </Button>
                      <Button
                        size="xs"
                        variant="light"
                        color="red"
                        disabled={!connected || lockState !== true || disableBusy}
                        loading={disableBusy}
                        onClick={() => onDisableLock(device.key)}
                      >
                        Disable lock
                      </Button>
                      <Button
                        size="xs"
                        variant="light"
                        color={autoRelockEnabled ? 'green' : 'gray'}
                        disabled={!connected || relockBusy}
                        loading={relockBusy}
                        onClick={() => onToggleAutoRelock(device.key, !autoRelockEnabled)}
                      >
                        {autoRelockEnabled ? 'Relock on' : 'Relock off'}
                      </Button>
                    </Group>
                  </Stack>
                </Group>
              );
            }) : null}
          </Stack>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
});
