import { apiBase } from './api';
import type { LogsStreamMessage, StreamMessage } from './types';
import { parseLogsStreamMessage, parseStreamMessage } from './features/runtime/messageGuards';

type StreamOptions = {
  maxFps?: number;
};

const CLIENT_PLOT_MAX_FPS = 60;

export function openDeviceStream(
  deviceKey: string,
  onMessage: (msg: StreamMessage) => void,
  options?: StreamOptions
) {
  const wsBase = apiBase.replace(/^http/, 'ws');
  const params = new URLSearchParams();
  if (options?.maxFps && options.maxFps > 0) {
    params.set('max_fps', String(Math.min(options.maxFps, CLIENT_PLOT_MAX_FPS)));
  }
  const suffix = params.toString();
  const url = suffix
    ? `${wsBase}/devices/${deviceKey}/stream?${suffix}`
    : `${wsBase}/devices/${deviceKey}/stream`;
  const socket = new WebSocket(url);

  socket.onmessage = (event) => {
    try {
      const parsed = JSON.parse(event.data) as unknown;
      const message = parseStreamMessage(parsed);
      if (!message) {
        console.warn('Bad WS message shape', parsed);
        return;
      }
      onMessage(message);
    } catch (err) {
      console.warn('Bad WS message', err);
    }
  };

  return socket;
}

export function openLogsStream(onMessage: (msg: LogsStreamMessage) => void) {
  const wsBase = apiBase.replace(/^http/, 'ws');
  const socket = new WebSocket(`${wsBase}/logs/stream`);

  socket.onmessage = (event) => {
    try {
      const parsed = JSON.parse(event.data) as unknown;
      const message = parseLogsStreamMessage(parsed);
      if (!message) {
        console.warn('Bad logs WS message shape', parsed);
        return;
      }
      onMessage(message);
    } catch (err) {
      console.warn('Bad logs WS message', err);
    }
  };

  return socket;
}
