import { apiBase } from './api';
import type { LogsStreamMessage, StreamMessage } from './types';
import {
  registerLogsParser,
  registerStreamParser,
} from './workers/streamParserClient';

type StreamOptions = {
  maxFps?: number;
  detail?: 'summary' | 'full';
};

const CLIENT_PLOT_MAX_FPS = 60;

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
  const suffix = params.toString();
  const url = suffix
    ? `${wsBase}/devices/${deviceKey}/stream?${suffix}`
    : `${wsBase}/devices/${deviceKey}/stream`;
  const socket = new WebSocket(url);

  // Hand parsing + shape validation off to a shared worker so the main
  // thread is not consumed by JSON.parse + validation across N streams.
  const { parse, dispose } = registerStreamParser(onMessage);
  socket.onmessage = (event) => {
    if (typeof event.data !== 'string') return;
    parse(event.data);
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
