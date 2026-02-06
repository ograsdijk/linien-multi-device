import { useEffect, useRef } from 'react';
import { openDeviceStream } from '../ws';
import type { StreamMessage } from '../types';

type StreamOptions = {
  maxFps?: number;
};

export function useDeviceStream(
  deviceKey: string | null,
  enabled: boolean,
  onMessage: (msg: StreamMessage) => void,
  options?: StreamOptions
) {
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!deviceKey || !enabled) {
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
      return;
    }

    const socket = openDeviceStream(deviceKey, onMessage, options);
    socketRef.current = socket;

    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [deviceKey, enabled, onMessage, options?.maxFps]);
}
