import { useEffect, useRef } from 'react';
import { openDeviceStream } from '../ws';
import type { StreamMessage } from '../types';

type StreamOptions = {
  maxFps?: number;
  detail?: 'summary' | 'full';
  onOpen?: () => void;
  onClose?: () => void;
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
  const openedRef = useRef(false);
  // Keep current callbacks in refs so updates to handlers (e.g. when the
  // parent re-renders and creates a new closure) do not tear down the
  // websocket and trigger reconnect storms.
  const onMessageRef = useRef(onMessage);
  const onOpenRef = useRef(options?.onOpen);
  const onCloseRef = useRef(options?.onClose);
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);
  useEffect(() => {
    onOpenRef.current = options?.onOpen;
  }, [options?.onOpen]);
  useEffect(() => {
    onCloseRef.current = options?.onClose;
  }, [options?.onClose]);

  const maxFps = options?.maxFps;
  const detail = options?.detail;

  useEffect(() => {
    let disposed = false;

    const notifyClosed = () => {
      if (!openedRef.current) {
        return;
      }
      openedRef.current = false;
      // During unmount React may already be unmounting parent state
      // owners. Calling parent setState synchronously here would cause a
      // "cannot update a component while rendering a different component"
      // warning. Defer the notification when we are tearing down so the
      // parent's own cleanup wins the race.
      const fn = onCloseRef.current;
      if (!fn) return;
      if (disposed) {
        queueMicrotask(() => fn());
      } else {
        fn();
      }
    };

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
        socket.onmessage = null;
        socket.close();
      }
      notifyClosed();
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

      const socket = openDeviceStream(
        deviceKey,
        (msg) => onMessageRef.current(msg),
        { maxFps, detail }
      );
      socketRef.current = socket;
      socket.onopen = () => {
        reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;
        openedRef.current = true;
        onOpenRef.current?.();
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
        notifyClosed();
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
  }, [deviceKey, enabled, maxFps, detail]);
}
