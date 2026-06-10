/// <reference lib="webworker" />

// Worker-side stream payload parser. The main thread owns the WebSocket
// (so reconnect logic, fps gating, etc. all stay simple) and forwards
// every `event.data` string here for JSON.parse + shape validation. This
// removes the largest sustained main-thread cost in a 12-device
// deployment: 360 plot frames/sec * ~30-50 KB each was dominating the
// "Scripting" budget of the main thread.
//
// Binary plot frames (when the gateway is contacted with `binary=1`)
// land in `parseBinary` instead. Format mirrors gateway/stream.py
// encode_plot_frame_binary:
//
//   bytes 0..4    'PLOT' magic
//   bytes 4..8    header JSON length (uint32 BE)
//   bytes 8..N    header JSON (utf-8) including series_names + n_points
//   pad to 4
//   then n_series * n_points * Float32

import {
  parseLogsStreamMessage,
  parseStreamMessage,
} from '../features/runtime/messageGuards';

type WorkerInbound =
  | { type: 'parse'; streamId: number; data: string }
  | { type: 'parseBinary'; streamId: number; data: ArrayBuffer }
  | { type: 'parseLogs'; logsId: number; data: string };

type WorkerOutbound =
  | { type: 'message'; streamId: number; message: unknown }
  | { type: 'logsMessage'; logsId: number; message: unknown };

const ctx = self as unknown as DedicatedWorkerGlobalScope;
const textDecoder = new TextDecoder('utf-8');

const decodeBinaryPlotFrame = (buf: ArrayBuffer): unknown => {
  const view = new DataView(buf);
  if (buf.byteLength < 8) return null;
  // Magic 'PLOT' = 0x504C4F54.
  if (view.getUint32(0) !== 0x504c4f54) return null;
  const headerLen = view.getUint32(4);
  if (8 + headerLen > buf.byteLength) return null;
  const headerBytes = new Uint8Array(buf, 8, headerLen);
  let header: Record<string, unknown>;
  try {
    header = JSON.parse(textDecoder.decode(headerBytes));
  } catch {
    return null;
  }
  const seriesNames = header.series_names;
  const nPoints = Number(header.n_points || 0);
  if (!Array.isArray(seriesNames) || nPoints <= 0) {
    return { ...header, series: {} };
  }
  const pad = (4 - ((8 + headerLen) % 4)) % 4;
  const dataOffset = 8 + headerLen + pad;
  const expectedBytes = seriesNames.length * nPoints * 4;
  if (dataOffset + expectedBytes > buf.byteLength) return null;
  // Build the series map by slicing Float32 views into the underlying
  // buffer. Each `new Float32Array(buf, byteOffset, length)` is a
  // zero-copy view — no allocation beyond the wrapper object.
  const series: Record<string, Float32Array> = {};
  for (let i = 0; i < seriesNames.length; i++) {
    const name = seriesNames[i];
    if (typeof name !== 'string') continue;
    series[name] = new Float32Array(buf, dataOffset + i * nPoints * 4, nPoints);
  }
  return {
    type: 'plot_frame',
    lock: Boolean(header.lock),
    dual_channel: Boolean(header.dual_channel),
    series,
    signal_power: header.signal_power ?? { channel1: null, channel2: null },
    stats: header.stats ?? { error_std: null, control_std: null },
    lock_indicator: header.lock_indicator ?? undefined,
    auto_relock: header.auto_relock ?? undefined,
    lock_target: header.lock_target ?? null,
    x_label: header.x_label ?? '',
    x_unit: header.x_unit ?? '',
  };
};

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
  if (payload.type === 'parseBinary') {
    const message = decodeBinaryPlotFrame(payload.data);
    if (!message) return;
    // Binary frames bypass `parseStreamMessage` shape validation —
    // the encoder is gateway-internal and the format is fixed by
    // the magic + length-prefixed header. Validating the JSON
    // header alone would re-parse it; skip.
    // Note: the series-value Float32Arrays are transferred via the
    // structured-clone algorithm. The underlying buffer is sent as
    // a transferable on `postMessage` below to keep this zero-copy.
    const transfer: ArrayBuffer[] = [payload.data];
    ctx.postMessage(
      { type: 'message', streamId: payload.streamId, message },
      transfer
    );
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
