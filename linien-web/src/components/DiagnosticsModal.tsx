import {
  Accordion,
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
} from '@mantine/core';
import type { Device, DeviceStatus } from '../types';
import { resolveConnectionDisplay } from '../features/connection/connectionState';
import {
  countReboots,
  formatEventTime,
  resolveBoardEventDisplay,
} from '../features/diagnostics/boardEventDisplay';
import { useDiagnosticsController } from '../features/diagnostics/useDiagnosticsController';

type DiagnosticsModalProps = {
  device: Device | null;
  status?: DeviceStatus | null;
  onClose: () => void;
  appendUiErrorLog: (
    source: string,
    code: string,
    message: string,
    deviceKey?: string
  ) => void;
};

const TONE_COLOR = {
  red: 'red',
  amber: 'yellow',
  green: 'green',
  dimmed: 'gray',
} as const;

export function DiagnosticsModal({
  device,
  status,
  onClose,
  appendUiErrorLog,
}: DiagnosticsModalProps) {
  const {
    events,
    eventsLoading,
    bundle,
    collecting,
    enabling,
    error,
    collect,
    enablePersistentLog,
  } = useDiagnosticsController({ deviceKey: device?.key ?? null, appendUiErrorLog });

  const connectionDisplay = resolveConnectionDisplay(status);
  const reboots = countReboots(events);
  // Only offer the action once we know it is needed. Offering it on `null`
  // would mean nagging about a board whose state we could not read.
  const offerPersistentLog = bundle?.persistent_journal === false;

  return (
    <Modal
      opened={device !== null}
      onClose={onClose}
      title={`Diagnostics — ${device?.name || device?.key || ''}`}
      size="clamp(48rem, 90vw, 88rem)"
      centered
      zIndex={430}
    >
      <Stack gap="md">
        {connectionDisplay.show ? (
          <Alert color={TONE_COLOR[connectionDisplay.color] ?? 'gray'} variant="light">
            {connectionDisplay.tooltip}
          </Alert>
        ) : null}

        {error ? (
          <Alert color="red" variant="light" title="Diagnostics">
            {error}
          </Alert>
        ) : null}

        <Group gap="xs" align="center">
          <Button size="xs" loading={collecting} onClick={() => void collect()}>
            Collect diagnostics
          </Button>
          {offerPersistentLog ? (
            <Button
              size="xs"
              color="orange"
              variant="light"
              loading={enabling}
              onClick={() => void enablePersistentLog()}
            >
              Enable persistent logs
            </Button>
          ) : null}
          {bundle?.persistent_journal === true ? (
            <Badge color="green" variant="light">
              Logs survive a reboot
            </Badge>
          ) : null}
          {reboots > 0 ? (
            <Badge color="red" variant="light">
              {reboots} reboot{reboots === 1 ? '' : 's'} recorded
            </Badge>
          ) : null}
        </Group>

        {offerPersistentLog ? (
          <Alert color="yellow" variant="light" title="This board keeps no logs">
            Its journal lives in RAM, so the next reset will again take the
            explanation with it. Enabling persistent logs writes a capped 32 MB
            journal to the SD card, which is what makes the previous boot&rsquo;s log
            readable after a crash.
          </Alert>
        ) : null}

        <Stack gap="xs">
          <Group gap="xs" align="center">
            <Text fw={600} size="sm">
              Timeline
            </Text>
            {eventsLoading ? <Loader size="xs" /> : null}
          </Group>
          {events.length === 0 && !eventsLoading ? (
            <Text size="sm" c="dimmed">
              Nothing recorded for this board yet.
            </Text>
          ) : (
            <ScrollArea.Autosize mah={220}>
              <Stack gap={4}>
                {events.map((event, index) => {
                  const display = resolveBoardEventDisplay(event);
                  return (
                    <Group key={`${event.ts}-${index}`} gap="xs" wrap="nowrap" align="flex-start">
                      <Text size="xs" c="dimmed" style={{ whiteSpace: 'nowrap' }}>
                        {formatEventTime(event.ts)}
                      </Text>
                      <Badge size="xs" color={TONE_COLOR[display.tone]} variant="light">
                        {display.label}
                      </Badge>
                      <Text size="xs">{event.detail}</Text>
                    </Group>
                  );
                })}
              </Stack>
            </ScrollArea.Autosize>
          )}
        </Stack>

        {bundle ? (
          <Stack gap="xs">
            <Text fw={600} size="sm">
              Collected {formatEventTime(bundle.collected_at)}
            </Text>
            <Accordion variant="contained" multiple>
              {bundle.sections.map((section) => (
                <Accordion.Item key={section.name} value={section.name}>
                  <Accordion.Control>
                    <Group gap="xs">
                      <Text size="sm">{section.title}</Text>
                      {section.error ? (
                        <Badge size="xs" color="yellow" variant="light">
                          {section.error.slice(0, 60)}
                        </Badge>
                      ) : null}
                      {!section.error && !section.output ? (
                        <Badge size="xs" color="gray" variant="light">
                          empty
                        </Badge>
                      ) : null}
                    </Group>
                  </Accordion.Control>
                  <Accordion.Panel>
                    <Stack gap={4}>
                      <Text size="xs" c="dimmed">
                        <Code>{section.command}</Code>
                      </Text>
                      <ScrollArea.Autosize mah={320}>
                        <Code block style={{ whiteSpace: 'pre-wrap' }}>
                          {section.output || '(no output)'}
                        </Code>
                      </ScrollArea.Autosize>
                    </Stack>
                  </Accordion.Panel>
                </Accordion.Item>
              ))}
            </Accordion>
          </Stack>
        ) : null}
      </Stack>
    </Modal>
  );
}
