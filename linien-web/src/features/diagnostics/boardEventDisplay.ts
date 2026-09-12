import type { BoardEvent, BoardEventKind } from '../../types';

export type EventTone = 'red' | 'amber' | 'green' | 'dimmed';

type EventDisplay = { label: string; tone: EventTone };

const BY_KIND: Record<BoardEventKind, EventDisplay> = {
  reboot_detected: { label: 'Rebooted', tone: 'red' },
  disconnected: { label: 'Connection lost', tone: 'red' },
  diagnosis: { label: 'Diagnosis', tone: 'amber' },
  telemetry_offline: { label: 'Telemetry lost', tone: 'amber' },
  telemetry_recovered: { label: 'Telemetry back', tone: 'green' },
  persistent_log_enabled: { label: 'Persistent logs on', tone: 'green' },
};

export const resolveBoardEventDisplay = (event: BoardEvent): EventDisplay =>
  BY_KIND[event.kind] ?? { label: event.kind, tone: 'dimmed' };

/**
 * How many times the board restarted within the retained window.
 *
 * The headline number: a board that rebooted once overnight is a different
 * problem from one that rebooted eleven times, and the raw list buries that.
 */
export const countReboots = (events: BoardEvent[]): number =>
  events.filter((event) => event.kind === 'reboot_detected').length;

export const formatEventTime = (ts: number): string => {
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
};
