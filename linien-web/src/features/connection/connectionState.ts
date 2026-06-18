import type { DeviceDiagnosis, DeviceStatus } from '../../types';

export type ConnectionDisplayColor = 'green' | 'amber' | 'red' | 'dimmed';

export type ConnectionDisplay = {
  /** Whether there is a diagnosis worth surfacing (only while disconnected). */
  show: boolean;
  /** Short badge label, e.g. "Server down · lock held". */
  label: string;
  /** Maps to a `.device-tag.diag-*` color variant. */
  color: ConnectionDisplayColor;
  /** Full human-readable explanation for the tooltip. */
  tooltip: string;
  /** The raw category, used for the CSS class suffix. */
  category: string;
};

const labelForDiagnosis = (
  diagnosis: DeviceDiagnosis
): { label: string; color: ConnectionDisplayColor } => {
  switch (diagnosis.category) {
    case 'recovering':
      return { label: 'Server back · reconnecting', color: 'amber' };
    case 'host_unreachable':
      return { label: 'Unreachable', color: 'red' };
    case 'rebooted':
      return { label: 'Rebooted · lock lost', color: 'red' };
    case 'server_down_unknown':
      return { label: 'Server down · state unknown', color: 'amber' };
    case 'server_crashed':
      if (diagnosis.lock_state === 'locked') {
        return { label: 'Server down · lock held', color: 'amber' };
      }
      if (diagnosis.lock_state === 'unlocked') {
        return { label: 'Server down · not locked', color: 'red' };
      }
      if (diagnosis.lock_state === 'lost') {
        return { label: 'Server down · lock lost', color: 'red' };
      }
      if (diagnosis.lock_state === 'unknown') {
        return { label: 'Server down · state unknown', color: 'amber' };
      }
      return { label: 'Server down · lock likely', color: 'amber' };
    default:
      return { label: 'Disconnected', color: 'dimmed' };
  }
};

/**
 * Resolve a richer connection-loss display from the device status. Mirrors
 * `resolveLockDisplay`. Returns `show: false` when the device is connected or
 * when no diagnosis has been produced yet.
 */
export const resolveConnectionDisplay = (
  status?: DeviceStatus | null
): ConnectionDisplay => {
  const hidden: ConnectionDisplay = {
    show: false,
    label: '',
    color: 'dimmed',
    tooltip: '',
    category: '',
  };
  if (!status || status.connected || status.connecting) {
    return hidden;
  }
  const diagnosis = status.diagnosis;
  if (!diagnosis) {
    return hidden;
  }
  const { label, color } = labelForDiagnosis(diagnosis);
  return {
    show: true,
    label,
    color,
    tooltip: diagnosis.message || label,
    category: diagnosis.category,
  };
};
