// Per-device record of the last status payload received from the gateway: when
// it arrived, and how old the temperature reading in it already was.
//
// Statuses are pushed only when something changes, so a quiet device can go a
// long time without one. That is fine for fields that only move on an event,
// but not for the Red Pitaya temperature: if the stream dies, the tab is
// backgrounded, or the gateway stops polling, the last payload sticks and its
// reading would be shown as current indefinitely.
//
// Keeping the age here rather than in the device store is deliberate. The age
// changes on every poll while nothing visible does, so putting it in a store
// slice would re-render every card every 30 s to display the same number.
// This is receipt bookkeeping, not rendered state -- the same reasoning, and
// the same shape, as streamFreshness.
type StatusReceipt = {
  receivedAt: number;
  /** Age of the reading when the gateway sent it, per rp_temperature_age_s. */
  ageAtSendS: number | null;
};

const receipts = new Map<string, StatusReceipt>();

export const markStatusReceived = (
  deviceKey: string,
  ageAtSendS?: number | null
): void => {
  receipts.set(deviceKey, {
    receivedAt: Date.now(),
    ageAtSendS:
      typeof ageAtSendS === 'number' && Number.isFinite(ageAtSendS) ? ageAtSendS : null,
  });
};

export const clearStatusFreshness = (deviceKey: string): void => {
  receipts.delete(deviceKey);
};

/**
 * How old the last reading for `deviceKey` is *now*, in seconds.
 *
 * The gateway's age at send plus the time since that payload arrived. Falls
 * back to `fallbackAgeS` (the age carried by a status the caller holds) when
 * nothing has been recorded yet, and returns null when neither is known --
 * which callers must read as "no opinion", not "fresh".
 */
export const effectiveReadingAgeS = (
  deviceKey: string | undefined,
  fallbackAgeS?: number | null,
  nowMs: number = Date.now()
): number | null => {
  const receipt = deviceKey ? receipts.get(deviceKey) : undefined;
  const base =
    receipt?.ageAtSendS ??
    (typeof fallbackAgeS === 'number' && Number.isFinite(fallbackAgeS)
      ? fallbackAgeS
      : null);
  if (base === null) return null;
  if (!receipt) return base;
  return base + Math.max(0, nowMs - receipt.receivedAt) / 1000;
};
