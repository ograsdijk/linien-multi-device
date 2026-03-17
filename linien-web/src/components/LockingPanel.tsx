import { useId } from 'react';
import { Button, Divider, Group, NumberInput, Radio, Select, Stack, Switch, Tabs, Text } from '@mantine/core';

type LockingPanelProps = {
  params: Record<string, any>;
  onSetParam: (name: string, value: any, writeRegisters?: boolean) => void;
  onStartLock: () => void;
  onStartAutolockSelection: () => void;
  onAbortAutolockSelection: () => void;
  onStopLock: () => void;
  lockMode?: 'manual' | 'autolock';
  onLockModeChange?: (mode: 'manual' | 'autolock') => void;
  autolockSelectionActive?: boolean;
  selectionError?: string | null;
  selectionSubmitting?: boolean;
  autolockTemporarilyDisabled?: boolean;
  disableReason?: string;
};

export function LockingPanel({
  params,
  onSetParam,
  onStartLock,
  onStartAutolockSelection,
  onAbortAutolockSelection,
  onStopLock,
  lockMode,
  onLockModeChange,
  autolockSelectionActive,
  selectionError,
  selectionSubmitting,
  autolockTemporarilyDisabled,
  disableReason,
}: LockingPanelProps) {
  const slopeParam = params.target_slope_rising;
  const slopeValue =
    slopeParam === false || slopeParam === 0 ? 'falling' : 'rising';
  const slopeGroupName = useId();
  const autolockRunning = Boolean(params.autolock_running);
  const autolockFailed = Boolean(params.autolock_failed);
  const autolockModePreference = params.autolock_mode_preference ?? 0;
  const determineOffset = Boolean(params.autolock_determine_offset);
  const mode = lockMode ?? 'manual';
  const selectionArmed = Boolean(autolockSelectionActive);
  const disabledReasonText =
    disableReason ?? 'Temporarily disabled due to compatibility issue.';


  return (
    <Stack gap="sm">
      <Text fw={600}>PID</Text>
      <Group grow>
        <NumberInput
          label="P"
          value={params.p ?? 0}
          onChange={(value) => onSetParam('p', Number(value), true)}
        />
        <NumberInput
          label="I"
          value={params.i ?? 0}
          onChange={(value) => onSetParam('i', Number(value), true)}
        />
        <NumberInput
          label="D"
          value={params.d ?? 0}
          onChange={(value) => onSetParam('d', Number(value), true)}
        />
      </Group>
      <Divider />
      <Tabs
        value={mode}
        onChange={(value) => {
          if (!value) return;
          onLockModeChange?.(value as 'manual' | 'autolock');
        }}
        variant="outline"
      >
        <Tabs.List>
          <Tabs.Tab value="manual">Manual</Tabs.Tab>
          <Tabs.Tab value="autolock">Autolock</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="manual" pt="xs">
          <Stack gap="sm">
            <Radio.Group
              name={`slope-${slopeGroupName}`}
              label="Target slope"
              value={slopeValue}
              onChange={(value) => {
                const next = value === 'rising';
                onSetParam('target_slope_rising', next, false);
              }}
            >
              <Group mt="xs">
                <Radio value="rising" label="Rising" />
                <Radio value="falling" label="Falling" />
              </Group>
            </Radio.Group>
            <Group grow>
              <Button color="green" variant="light" onClick={onStartLock}>
                Start lock
              </Button>
            </Group>
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="autolock" pt="xs">
          <Stack gap="sm">
            <Select
              label="Autolock algorithm"
              data={[
                { value: '0', label: 'Auto-detect' },
                { value: '1', label: 'Robust mode' },
                { value: '2', label: 'Simple mode' },
              ]}
              value={String(autolockModePreference)}
              onChange={(value) => {
                if (value == null) return;
                onSetParam('autolock_mode_preference', Number(value), false);
              }}
              disabled={autolockTemporarilyDisabled}
            />
            <Switch
              label="Determine signal offset"
              checked={determineOffset}
              onChange={(event) =>
                onSetParam('autolock_determine_offset', event.currentTarget.checked, false)
              }
              disabled={autolockTemporarilyDisabled}
            />
            {autolockTemporarilyDisabled ? (
              <Text size="xs" c="red">
                {disabledReasonText}
              </Text>
            ) : null}
            {!selectionArmed ? (
              <Button
                variant="light"
                color="orange"
                onClick={onStartAutolockSelection}
                disabled={selectionSubmitting || autolockTemporarilyDisabled}
              >
                Select target line
              </Button>
            ) : (
              <Stack gap="xs">
                <Text size="sm" fw={500}>
                  Click and drag over the line you want to lock to.
                </Text>
                {selectionError ? (
                  <Text size="xs" c="red">
                    {selectionError}
                  </Text>
                ) : null}
                <Button
                  variant="default"
                  onClick={onAbortAutolockSelection}
                  disabled={selectionSubmitting || autolockTemporarilyDisabled}
                >
                  Abort
                </Button>
              </Stack>
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>
      <Group grow>
        <Button variant="outline" color="red" onClick={onStopLock}>
          Stop lock
        </Button>
      </Group>
      <Text size="sm">Autolock: {autolockRunning ? 'Running' : autolockFailed ? 'Failed' : 'Idle'}</Text>
    </Stack>
  );
}
