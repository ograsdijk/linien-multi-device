import { useEffect, useRef } from 'react';
import { openDeviceStream } from '../ws';
import type { StreamMessage } from '../types';

type StreamOptions = {
  maxFps?: number;
};

const INITIAL_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 5000;

export function useDeviceStream(
  deviceKey: string | null,
  enabled: boolean,
  onMessage: (msg: StreamMessage) => void,
  options?: StreamOptions
) {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY_MS);

  useEffect(() => {
    let disposed = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const closeCurrentSocket = () => {
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onopen = null;
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
    };

    if (!deviceKey || !enabled) {
      clearReconnectTimer();
      closeCurrentSocket();
      return;
    }

    reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;

    const connect = () => {
      if (disposed || !deviceKey || !enabled) {
        return;
      }
      clearReconnectTimer();
      closeCurrentSocket();

      const socket = openDeviceStream(deviceKey, onMessage, options);
      socketRef.current = socket;
      socket.onopen = () => {
        reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;
      };
      socket.onerror = () => {
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close();
        }
      };
      socket.onclose = () => {
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
        if (disposed || !deviceKey || !enabled) {
          return;
        }
        const nextDelay = reconnectDelayRef.current;
        reconnectDelayRef.current = Math.min(nextDelay * 2, MAX_RECONNECT_DELAY_MS);
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          connect();
        }, nextDelay);
      };
    };

    connect();

    return () => {
      disposed = true;
      clearReconnectTimer();
      closeCurrentSocket();
    };
  }, [deviceKey, enabled, onMessage, options?.maxFps]);
}
