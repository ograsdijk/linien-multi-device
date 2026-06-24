import { apiBase } from './api';
import type {
  LogsStreamMessage,
  PsdStreamMessage,
  StreamMessage,
} from './types';
import {
  registerLogsParser,
  registerStreamParser,
} from './workers/streamParserClient';

type StreamOptions = {
  maxFps?: number;
  detail?: 'summary' | 'full';
};

const CLIENT_PLOT_MAX_FPS = 60;
// Request the binary plot-frame protocol from the gateway. ~300x
// faster to decode than JSON (M2 bench) and ~5x smaller on the wire.
// The gateway falls back to JSON for clients that don't pass this
// flag, so this stays backwards-compatible.
const BINARY_PROTOCOL = true;

// Returned alongside the raw WebSocket so callers know to dispose the
// per-stream parser registration when they close the socket.
export type DeviceStreamHandle = {
  socket: WebSocket;
  disposeParser: () => void;
};

export function openDeviceStream(
  deviceKey: string,
  onMessage: (msg: StreamMessage) => void,
  options?: StreamOptions
): DeviceStreamHandle {
  const wsBase = apiBase.replace(/^http/, 'ws');
  const params = new URLSearchParams();
  if (options?.maxFps && options.maxFps > 0) {
    params.set('max_fps', String(Math.min(options.maxFps, CLIENT_PLOT_MAX_FPS)));
  }
  if (options?.detail) {
    params.set('detail', options.detail);
  }
  if (BINARY_PROTOCOL) {
    params.set('binary', '1');
  }
  const suffix = params.toString();
  const url = suffix
    ? `${wsBase}/devices/${deviceKey}/stream?${suffix}`
    : `${wsBase}/devices/${deviceKey}/stream`;
  const socket = new WebSocket(url);
  // We receive plot frames as binary ArrayBuffers. Tell the browser
  // to deliver Blob messages as ArrayBuffer so the worker can slice
  // typed-array views with zero copy. Non-plot messages are still
  // text and unaffected by this setting.
  socket.binaryType = 'arraybuffer';

  // Hand parsing + shape validation off to a shared worker so the main
  // thread is not consumed by JSON.parse + validation across N streams.
  const { parse, parseBinary, dispose } = registerStreamParser(onMessage);
  socket.onmessage = (event) => {
    if (typeof event.data === 'string') {
      parse(event.data);
    } else if (event.data instanceof ArrayBuffer) {
      parseBinary(event.data);
    }
  };

  return { socket, disposeParser: dispose };
}

export function openLogsStream(onMessage: (msg: LogsStreamMessage) => void): WebSocket {
  const wsBase = apiBase.replace(/^http/, 'ws');
  const socket = new WebSocket(`${wsBase}/logs/stream`);
  const { parse, dispose } = registerLogsParser(onMessage);
  socket.onmessage = (event) => {
    if (typeof event.data !== 'string') return;
    parse(event.data);
  };
  const handleClose = () => {
    dispose();
    socket.removeEventListener('close', handleClose);
  };
  socket.addEventListener('close', handleClose);
  return socket;
}

// PSD events are low-rate (a handful per acquisition), so parse inline on the
// main thread rather than routing through the stream worker.
export function openPsdStream(onMessage: (msg: PsdStreamMessage) => void): WebSocket {
  const wsBase = apiBase.replace(/^http/, 'ws');
  const socket = new WebSocket(`${wsBase}/psd/stream`);
  socket.onmessage = (event) => {
    if (typeof event.data !== 'string') return;
    try {
      const msg = JSON.parse(event.data);
      if (msg && msg.type === 'psd' && msg.entry) {
        onMessage(msg as PsdStreamMessage);
      }
    } catch {
      // Ignore malformed frames.
    }
  };
  return socket;
}
