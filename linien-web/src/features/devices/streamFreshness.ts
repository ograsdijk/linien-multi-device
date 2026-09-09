// Per-device timestamp of the last plot frame received over the websocket.
//
// The status backstop poll skips devices with an open stream, on the
// assumption that the stream keeps their status current. When a stream goes
// silent without closing, that assumption turns a transient fault into a
// permanent one: the device is skipped forever and only a page reload
// recovers it. Callers pair the "stream is open" check with `isStreamFresh`
// so a silent stream falls back to polling instead.
const lastFrameAt = new Map<string, number>();

export const markStreamFrame = (deviceKey: string): void => {
  lastFrameAt.set(deviceKey, Date.now());
};

export const clearStreamFreshness = (deviceKey: string): void => {
  lastFrameAt.delete(deviceKey);
};

export const isStreamFresh = (deviceKey: string, maxAgeMs: number): boolean => {
  const at = lastFrameAt.get(deviceKey);
  return at !== undefined && Date.now() - at <= maxAgeMs;
};
