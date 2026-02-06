import { apiBase } from './api';
import type { StreamMessage } from './types';

type StreamOptions = {
  maxFps?: number;
};

export function openDeviceStream(
  deviceKey: string,
  onMessage: (msg: StreamMessage) => void,
  options?: StreamOptions
) {
  const wsBase = apiBase.replace(/^http/, 'ws');
  const params = new URLSearchParams();
  if (options?.maxFps && options.maxFps > 0) {
    params.set('max_fps', String(options.maxFps));
  }
  const suffix = params.toString();
  const url = suffix
    ? `${wsBase}/devices/${deviceKey}/stream?${suffix}`
    : `${wsBase}/devices/${deviceKey}/stream`;
  const socket = new WebSocket(url);

  socket.onmessage = (event) => {
    try {
      const parsed = JSON.parse(event.data);
      onMessage(parsed as StreamMessage);
    } catch (err) {
      console.warn('Bad WS message', err);
    }
  };

  return socket;
}
