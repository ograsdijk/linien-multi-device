/// <reference lib="webworker" />

// Worker-side stream payload parser. The main thread owns the WebSocket
// (so reconnect logic, fps gating, etc. all stay simple) and forwards
// every `event.data` string here for JSON.parse + shape validation. This
// removes the largest sustained main-thread cost in a 12-device
// deployment: 360 plot frames/sec * ~30-50 KB each was dominating the
// "Scripting" budget of the main thread.

import {
  parseLogsStreamMessage,
  parseStreamMessage,
} from '../features/runtime/messageGuards';

type WorkerInbound =
  | { type: 'parse'; streamId: number; data: string }
  | { type: 'parseLogs'; logsId: number; data: string };

type WorkerOutbound =
  | { type: 'message'; streamId: number; message: unknown }
  | { type: 'logsMessage'; logsId: number; message: unknown };

const ctx = self as unknown as DedicatedWorkerGlobalScope;

ctx.onmessage = (event: MessageEvent<WorkerInbound>) => {
  const payload = event.data;
  if (!payload) return;
  if (payload.type === 'parse') {
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload.data);
    } catch {
      return;
    }
    const message = parseStreamMessage(parsed);
    if (!message) return;
    const out: WorkerOutbound = {
      type: 'message',
      streamId: payload.streamId,
      message,
    };
    ctx.postMessage(out);
    return;
  }
  if (payload.type === 'parseLogs') {
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload.data);
    } catch {
      return;
    }
    const message = parseLogsStreamMessage(parsed);
    if (!message) return;
    const out: WorkerOutbound = {
      type: 'logsMessage',
      logsId: payload.logsId,
      message,
    };
    ctx.postMessage(out);
  }
};

export {};
