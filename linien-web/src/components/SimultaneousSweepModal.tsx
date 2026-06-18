import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Group,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Text,
} from '@mantine/core';
import { api } from '../api';
import type { Device, DeviceStatus } from '../types';
import type { UiToast } from './ToastStack';
import { SWEEP_SPEED_OPTIONS } from '../features/devices/sweepSpeed';

type SimultaneousSweepModalProps = {
  opened: boolean;
  onClose: () => void;
  devices: Device[];
  statuses: Record<string, DeviceStatus | undefined>;
  pushToast: (toast: Omit<UiToast, 'id'>) => void;
};

// '' = leave each device's current sweep_speed unchanged.
const SPEED_OPTIONS = [{ value: '', label: 'Leave unchanged' }, ...SWEEP_SPEED_OPTIONS];

export function SimultaneousSweepModal({
  opened,
  onClose,
  devices,
  statuses,
  pushToast,
}: SimultaneousSweepModalProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sweepSpeed, setSweepSpeed] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connectedKeys = useMemo(
    () => devices.filter((d) => statuses[d.key]?.connected).map((d) => d.key),
    [devices, statuses]
  );

  // Default to all connected devices selected each time the modal opens.
  useEffect(() => {
    if (opened) {
      setSelected(new Set(connectedKeys));
      setError(null);
    }
    // Only re-seed on open, not on every status poll while open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  const selectedConnected = useMemo(
    () => connectedKeys.filter((key) => selected.has(key)),
    [connectedKeys, selected]
  );

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const allConnectedSelected =
    connectedKeys.length > 0 && connectedKeys.every((key) => selected.has(key));
  const someConnectedSelected = connectedKeys.some((key) => selected.has(key));

  const toggleAllConnected = () => {
    setSelected(allConnectedSelected ? new Set() : new Set(connectedKeys));
  };

  const handleTrigger = async () => {
    if (selectedConnected.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const speed = sweepSpeed === '' ? undefined : Number(sweepSpeed);
      const result = await api.startSweepSimultaneous(selectedConnected, speed);
      const startedCount = result.started.length;
      const skippedCount = result.skipped_unconnected.length;
      pushToast({
        level: skippedCount > 0 ? 'warning' : 'info',
        title: 'Simultaneous sweep',
        message:
          `Started ${startedCount} device${startedCount === 1 ? '' : 's'}` +
          (skippedCount > 0 ? `, skipped ${skippedCount} unconnected` : ''),
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened={opened} onClose={onClose} title="Sweep multiple devices" size="md">
      <Stack gap="sm">
        <Text size="xs" c="dimmed">
          Starts a sweep on the selected devices at roughly the same time and forces each
          ramp to restart from center. This is a near-simultaneous software start (within
          ~tens of ms), not a hardware phase-lock.
        </Text>

        <Group justify="space-between" align="center">
          <Checkbox
            label={`Select all connected (${connectedKeys.length})`}
            checked={allConnectedSelected}
            indeterminate={someConnectedSelected && !allConnectedSelected}
            onChange={toggleAllConnected}
            disabled={connectedKeys.length === 0}
          />
        </Group>

        <ScrollArea.Autosize mah={260}>
          <Stack gap={4}>
            {devices.length === 0 ? (
              <Text size="sm" c="dimmed">
                No devices configured.
              </Text>
            ) : null}
            {devices.map((device) => {
              const connected = Boolean(statuses[device.key]?.connected);
              return (
                <Checkbox
                  key={device.key}
                  checked={selected.has(device.key)}
                  disabled={!connected}
                  onChange={() => toggle(device.key)}
                  label={
                    <span>
                      {device.name || 'Unnamed device'}{' '}
                      <Text span size="xs" c="dimmed">
                        {device.host}:{device.port}
                        {connected ? '' : ' · disconnected'}
                      </Text>
                    </span>
                  }
                />
              );
            })}
          </Stack>
        </ScrollArea.Autosize>

        <Divider />

        <Select
          label="Sweep speed (applied to all selected)"
          data={SPEED_OPTIONS}
          value={sweepSpeed}
          onChange={(value) => setSweepSpeed(value ?? '')}
          allowDeselect={false}
          comboboxProps={{ withinPortal: true }}
        />

        {error ? (
          <Alert color="red" variant="light">
            {error}
          </Alert>
        ) : null}

        <Group justify="flex-end" mt="xs">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            color="orange"
            onClick={handleTrigger}
            loading={busy}
            disabled={selectedConnected.length === 0}
          >
            Sweep selected ({selectedConnected.length})
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
