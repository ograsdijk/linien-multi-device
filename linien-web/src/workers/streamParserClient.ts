import StreamParserWorker from './streamParserWorker?worker';
import type { LogsStreamMessage, StreamMessage } from '../types';

type StreamCallback = (message: StreamMessage) => void;
type LogsCallback = (message: LogsStreamMessage) => void;

// Single shared worker instance. Lazy-init so we don't pay the spawn
// cost during tests or before any stream is actually opened.
let workerInstance: Worker | null = null;
let nextStreamId = 1;
let nextLogsId = 1;
const streamCallbacks = new Map<number, StreamCallback>();
const logsCallbacks = new Map<number, LogsCallback>();

type WorkerOutbound =
  | { type: 'message'; streamId: number; message: StreamMessage }
  | { type: 'logsMessage'; logsId: number; message: LogsStreamMessage };

const ensureWorker = (): Worker => {
  if (workerInstance) return workerInstance;
  const worker = new StreamParserWorker();
  worker.onmessage = (event: MessageEvent<WorkerOutbound>) => {
    const payload = event.data;
    if (!payload) return;
    if (payload.type === 'message') {
      const cb = streamCallbacks.get(payload.streamId);
      if (cb) cb(payload.message);
      return;
    }
    if (payload.type === 'logsMessage') {
      const cb = logsCallbacks.get(payload.logsId);
      if (cb) cb(payload.message);
    }
  };
  workerInstance = worker;
  return worker;
};

// Reserve a stream id for a websocket and wire the message callback.
// Returns a `parse` function that forwards each raw event.data string to
// the worker, and a `dispose` to release the callback slot. After
// disposal `parse` is a no-op so post-dispose websocket events do not
// waste a worker round trip just to be silently dropped.
export const registerStreamParser = (
  onMessage: StreamCallback
): {
  parse: (data: string) => void;
  parseBinary: (data: ArrayBuffer) => void;
  dispose: () => void;
} => {
  const worker = ensureWorker();
  const streamId = nextStreamId++;
  streamCallbacks.set(streamId, onMessage);
  let active = true;
  return {
    parse: (data: string) => {
      if (!active) return;
      worker.postMessage({ type: 'parse', streamId, data });
    },
    parseBinary: (data: ArrayBuffer) => {
      if (!active) return;
      // Transfer ownership of the buffer to the worker (zero copy).
      // We never read the original buffer on the main thread after
      // this, so detaching is safe.
      worker.postMessage(
        { type: 'parseBinary', streamId, data },
        [data]
      );
    },
    dispose: () => {
      active = false;
      streamCallbacks.delete(streamId);
    },
  };
};

// Same shape as `registerStreamParser` for the logs WS. Uses an id-keyed
// map so multiple registrations (e.g. React StrictMode double-mount)
// route messages to their own callback instead of clobbering a single
// module-level slot.
export const registerLogsParser = (
  onMessage: LogsCallback
): { parse: (data: string) => void; dispose: () => void } => {
  const worker = ensureWorker();
  const logsId = nextLogsId++;
  logsCallbacks.set(logsId, onMessage);
  let active = true;
  return {
    parse: (data: string) => {
      if (!active) return;
      worker.postMessage({ type: 'parseLogs', logsId, data });
    },
    dispose: () => {
      active = false;
      logsCallbacks.delete(logsId);
    },
  };
};
