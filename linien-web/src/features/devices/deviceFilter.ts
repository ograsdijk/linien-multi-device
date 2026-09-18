import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
} from '../../types';
import { resolveLockDisplay, resolveRelockTag } from '../locks/lockState';

// Status facets the device panel can filter on. Each one answers a question an
// operator actually asks of the fleet ("what is down?", "what lost lock?"), so
// they are deliberately coarser than the full DeviceStatus surface.
export type DeviceFilterTag =
  | 'connected'
  | 'disconnected'
  | 'error'
  | 'locked'
  | 'unlocked'
  | 'relock';

export const DEVICE_FILTER_TAGS: { tag: DeviceFilterTag; label: string }[] = [
  { tag: 'connected', label: 'Connected' },
  { tag: 'disconnected', label: 'Disconnected' },
  { tag: 'error', label: 'Error' },
  { tag: 'locked', label: 'Locked' },
  { tag: 'unlocked', label: 'Unlocked' },
  { tag: 'relock', label: 'Auto relock' },
];

// Typed words that promote to a chip. Kept explicit rather than prefix-matched:
// a device named "locke" should not silently become a state filter.
const TAG_ALIASES: Record<string, DeviceFilterTag> = {
  connected: 'connected',
  online: 'connected',
  up: 'connected',
  disconnected: 'disconnected',
  disc: 'disconnected',
  offline: 'disconnected',
  down: 'disconnected',
  error: 'error',
  err: 'error',
  failed: 'error',
  locked: 'locked',
  lock: 'locked',
  unlocked: 'unlocked',
  unlock: 'unlocked',
  relock: 'relock',
  autorelock: 'relock',
};

export const resolveFilterAlias = (word: string): DeviceFilterTag | null => {
  const normalized = word.trim().toLowerCase();
  if (!normalized) return null;
  return TAG_ALIASES[normalized] ?? null;
};

export type DeviceFilterContext = {
  statuses: Record<string, DeviceStatus | undefined>;
  lockIndicators: Record<string, LockIndicatorSnapshot | undefined>;
  autoRelockStates: Record<string, AutoRelockStatus | undefined>;
};

export const deviceHaystack = (device: Device): string =>
  `${device.name} ${device.host}:${device.port} ${device.key}`.toLowerCase();

const matchesTag = (
  tag: DeviceFilterTag,
  device: Device,
  ctx: DeviceFilterContext
): boolean => {
  const status = ctx.statuses[device.key];
  const connected = Boolean(status?.connected);
  switch (tag) {
    case 'connected':
      return connected;
    case 'disconnected':
      return !connected;
    case 'error':
      return Boolean(status?.last_error);
    case 'locked':
    case 'unlocked': {
      // Same derivation the row tag shows, so a chip can never disagree with
      // the badge next to it.
      const display = resolveLockDisplay({
        connected,
        lockEnabled: status?.lock,
        indicator: ctx.lockIndicators[device.key] ?? null,
      });
      return tag === 'locked' ? display.effectiveLocked : !display.effectiveLocked;
    }
    case 'relock':
      return resolveRelockTag(
        ctx.autoRelockStates[device.key] ?? status?.auto_relock ?? undefined
      ).enabled;
    default:
      return true;
  }
};

// Text and tags both narrow: several tags AND together, so "Connected" plus
// "Disconnected" correctly yields nothing rather than everything.
export const filterDevices = (
  devices: Device[],
  text: string,
  tags: DeviceFilterTag[],
  ctx: DeviceFilterContext
): Device[] => {
  const needle = text.trim().toLowerCase();
  if (!needle && tags.length === 0) return devices;
  return devices.filter((device) => {
    if (needle && !deviceHaystack(device).includes(needle)) return false;
    return tags.every((tag) => matchesTag(tag, device, ctx));
  });
};
