# Performance Findings - Pass 2

Date: 2026-06-08

Scope: Second performance investigation pass over `linien-gateway` and `linien-web`, focused on the 12-device case. No code changes made in this pass.

## Backend (linien-gateway)

### High impact

1. **`session.snapshot()` deepcopies entire plot frame for every `/statuses` poll**
   - Location: `linien-gateway/app/session.py:782` `_snapshot_cached_state`
   - Calls `deepcopy(self.last_plot_frame)`. A full frame can carry ~12 series × 2048 floats.
   - `status()` (`session.py:1007`) calls this on every poll just to read `lock` and `last_plot_timestamp`.
   - With 12 devices polled every 5 s from `/api/devices/statuses` (`main.py:469`), that's 12 deep copies of multi-megabyte frames per poll.
   - Fix: status callers only need the boolean lock state and timestamp, not the whole frame.

2. **`/api/devices/statuses` does serial RPyC round trips under per-device locks**
   - Location: `linien-gateway/app/main.py:469-476`
   - Iterates devices, acquires `session_registry.lock_for(device.key)`, and calls `session.status()`.
   - `status()` in turn does `exposed_get_logging_status()` and `self.parameters.lock.value` (RPyC attribute access) for each connected device.
   - For 12 connected devices that's up to 24 sequential RPyC round trips blocking one threadpool worker on every poll.
   - Fix: parallelize with `concurrent.futures.ThreadPoolExecutor` or replace with a cached/event-driven status path.

3. **`_on_to_plot` deepcopies `auto_relock.get_status()` twice per plot frame**
   - Locations: `linien-gateway/app/session.py:978` and `session.py:789`
   - `get_status()` already returns a freshly-constructed flat dict (see `auto_relock.py:287`), so `deepcopy` is wasted CPU.

4. **`build_detail = "full"` when there are no subscribers**
   - Location: `linien-gateway/app/session.py:945-951`
   - Builds the heavy full frame whenever `required_detail is None` (no WS subscribers).
   - Justification was REST snapshot consumers, but a snapshot reader can request full on demand.
   - Current logic builds the most expensive variant exactly when no one needs it.

5. **`websocket.send_json` uses stdlib `json.dumps` on every plot frame, every subscriber**
   - Location: `linien-gateway/app/stream.py:185`
   - For a 12-device deployment at 30 fps that's ~360 serializations/sec of dicts with ~10k floats.
   - FastAPI/Starlette is happy to use `orjson` or `ujson` (a couple of orders of magnitude faster on numeric arrays).
   - Fix: swap `send_json` for `send_bytes(orjson.dumps(payload))` or add `orjson` to `pyproject.toml` and use it on the hot path.

6. **`filter_plot_frame` rebuilds the frame dict per subscriber per broadcast**
   - Location: `linien-gateway/app/stream.py:212-225`
   - When a frame is built at `detail="full"` and all current subscribers want summary, you allocate a new dict for each subscriber.
   - Fix: cache the filtered version once per broadcast.

7. **Per-frame `(arr / V).tolist()` and history list rebuilds**
   - Location: `linien-gateway/app/plot_processing.py:177-244`
   - Each `(error_signal / V).tolist()` allocates a 2048-element Python list of floats every frame.
   - The history series do `[(v/V) if v is not None else None for v in control_series]` rebuilds another 2048-element list.
   - Even at summary detail the basic series cost is unavoidable, but the full-detail history conversion is heavy and should be skipped or cached when the underlying history hasn't changed.

### Medium impact

8. **Poll thread runs at fixed 20 Hz (`time.sleep(0.05)`) even when nothing is happening**
   - Location: `linien-gateway/app/session.py:747`
   - With 12 sessions that's 240 RPyC `get_changed_parameters_queue` calls per second across the gateway, even on an idle system.
   - Fix: adaptive backoff when no params changed in the last N polls would cut idle load.

9. **`_on_param_changed` publishes every individual param immediately**
   - Location: `linien-gateway/app/session.py:763-776`
   - No coalescing on the server side. Bursty parameter changes during e.g. a sweep adjustment fan out as N small WS messages per device per second.
   - Fix: batch into a single `{type:"param_batch", values:{...}}` message per tick.

10. **`_on_param_changed` acquires `_state_lock` twice and `to_jsonable` outside lock**
    - Fine for correctness but two extra lock acquires per param-change message.
    - Fix: a single lock region would be cheaper.

11. **`_plot_params` makes 12 RPyC attribute accesses under `_rpyc_lock` every frame**
    - Location: `linien-gateway/app/session.py:867-885`
    - Most are cached params (`use_cache=True`), so they hit local cache — but each one still does Python attribute lookup + lock.
    - The poll thread also takes `_rpyc_lock` 3 separate times in `_on_to_plot` (`_derive_lock_value`, `_plot_params`, and during sub-operations), each one a chance for contention with API request handlers.

12. **`/api/devices/{key}/params` does N×3 RPyC attribute accesses**
    - Location: `linien-gateway/app/session.py:1424-1438`
    - `param.restorable`, `param.loggable`, `param.log` each go to the server.
    - For ~80 params, that's ~240 round trips per call.
    - Only called from the Influx popover, but it's still slow when opened.

13. **`param_cache` stores the raw RPyC `to_plot` blob**
    - Location: `linien-gateway/app/session.py:763-765`
    - Writes to `param_cache` before the `IGNORED_PARAMS` check, so `to_plot` (a large pickled object held as a remote reference) is retained.
    - Holding RPyC references can extend their lifetime on the server and creep memory.
    - Fix: skip `IGNORED_PARAMS` before assignment.

### Low impact

14. **`LockIndicatorEvaluator._plot_array` allocates a new `np.nan_to_num` array per series per frame**
    - Location: `linien-gateway/app/lock_indicator.py:42-54`
    - ~3 allocations/device/frame; could use `out=` or detect a NaN-free fast path.

15. **`stream.WebsocketManager.publish` adds a `done_callback` to every future just to log failures**
    - Location: `linien-gateway/app/stream.py:144-152`
    - The lambda + callback registration runs once per published message.
    - Fix: cheaper to inline only on failure.

## Frontend (linien-web)

### High impact

16. **`useLockSummary` rebuilds 5 maps over all devices on every `deviceStates` change**
    - Location: `linien-web/src/features/locks/useLockSummary.ts`
    - With the earlier batching change the inputs change less often, but every batched flush still reconstructs `deviceStatusMap`, `lockIndicatorMap`, `autoRelockMap`, `lockStateMap`, `effectiveLockStateMap`, and runs `computeLockHealthSummary`.
    - For 12 devices that's ~60 map operations per flush.
    - Fix: maps could be incremental — only the changed device's row needs to be replaced.

17. **`useDeviceStateUpdater` flushes via rAF, but `setStreamingDeviceKeys` and `setDeviceStates` still cascade through the entire App tree**
    - Every flush re-renders `<App>`, which re-runs `useLockSummary` (heavy) and re-creates `websocketActiveDeviceKeys` (`linien-web/src/App.tsx:323`) as a brand-new Set.
    - Memoizing this Set is hard because the input set itself changes identity often.
    - Fix: split App into independent provider components so a per-device update doesn't re-render the header/popovers/lock summary.

18. **`PlotPanel`'s `data` `useMemo` always misses**
    - Location: `linien-web/src/components/PlotPanel.tsx:238-260`
    - `activePlotFrame` is a new object every plot frame, so the memo recomputes.
    - Then `seriesRaw.map(normalizeSeries)` allocates 12 new arrays of 2048 floats per frame per visible device.
    - With 12 visible × 30 fps that's ~9M float allocations/sec.
    - Fix: write directly into pre-allocated `Float64Array` buffers reused across frames (uPlot accepts typed arrays).

19. **WS payload `JSON.parse` cost is significant**
    - Location: `linien-web/src/ws.ts:33`
    - ~360 parses/sec on the main thread of ~30–50 KB payloads.
    - With per-element validation now skipped (earlier change), JSON.parse is the next bottleneck.
    - Fix options: switch to a binary protocol (MessagePack), or move parsing to a Web Worker.

20. **`useInViewport` uses a separate IntersectionObserver per card**
    - Location: `linien-web/src/hooks/useInViewport.ts:34`
    - For 12 cards that's 12 observers. Mostly fine, but a single shared root-level observer with a key→callback registry is more efficient and reduces observer-construction churn on tab switches.

### Medium impact

21. **`useLogsController.appendLogEntries` rebuilds a Set from `prev.map(...)` on every log entry**
    - Location: `linien-web/src/features/logs/useLogsController.ts:114`
    - For a 5000-row buffer that's O(n) per append; for bursty logs it dominates.
    - Fix: keep a `seenIdsRef: Map<string, boolean>` alongside the array.

22. **`useLogsController.filteredLogRows` runs `JSON.stringify(entry.details || {})` per row per keystroke**
    - Location: `linien-web/src/features/logs/useLogsController.ts:244`
    - For 5000 entries × text search this is slow.
    - Fix: precompute a per-entry haystack string on append.

23. **`appendLogEntries` calls `setLogsErrorLatched(true)` from inside `setLogRows`'s updater**
    - Location: `linien-web/src/features/logs/useLogsController.ts:122-124`
    - Side-effects in a state updater are an anti-pattern that breaks under StrictMode/concurrent rendering.
    - Fix: move the error-latch update to a `useEffect` keyed on `logRows`.

24. **`AppHeaderControls` re-renders on every flush with a ~50-prop interface**
    - All popover dropdowns are closed by default in Mantine v7, so the renders are mostly cheap (no popover children).
    - Still, the parent's many inline arrow props cause some allocation churn.
    - Fix: extract popover bodies into separate components that subscribe only to their slice of state.

25. **`GroupModulationSummary` renders the full devices table on each parent render even when popover is closed**
    - Mantine wraps it inside `Popover.Dropdown` which only mounts when opened, so this is OK at render time, but the `formatDemodFrequency` / `formatState` helpers allocate on every render once opened. Minor.

### Low impact / potential bugs

26. **`useDeviceStatusPolling` now reads `devices` and `skipDeviceKeys` from refs but the effect still depends on `intervalMs` and `setDeviceStates`**
    - On app startup `devicesRef.current` may be the initial empty list when the first `pollStatuses()` runs; the next tick (5 s later) catches up.
    - Fix: trigger an immediate `pollStatuses()` from an effect keyed on `devices` length transitions for faster first-paint.

27. **`useDeviceStream` notifies close via `notifyClosed()` when `disposed`+`closeCurrentSocket()` are called as a unit**
    - Location: `linien-web/src/hooks/useDeviceStream.ts:128`
    - On unmount this fires `onCloseRef.current?.()` synchronously.
    - If `onStreamActiveChange` triggers a parent setState during unmount, React will warn.
    - Fix: guard like "don't call onClose during cleanup".

28. **`DeferredNumberInput.useEffect` resets draft any time `formatValue` changes**
    - Location: `linien-web/src/components/DeferredNumberInput.tsx:44-49`
    - If a parent passes an unmemoized `formatValue`, the effect runs every render.
    - Fix: wrap callers in `useCallback` or compare with a stable formatter ref.

29. **`useDeviceCatalog` `normalizeOrderKeys` is re-created on every render**
    - Location: `linien-web/src/features/devices/useDeviceCatalog.ts:64`
    - Depends on `devices`. The effect at line 82 then re-runs whenever the callback identity changes.
    - Inside it does `setDeviceOrder((prev) => normalizeOrderKeys(prev))` which uses `[...prev]` semantics — fine, but the callback identity change is unnecessary churn.
    - Fix: use a `useRef` over `devices` if the work doesn't need to react synchronously.

30. **`PlotPanel` rebuilds `axisValues` and `xValueFormatter` `useMemo` callbacks whenever `pointCount`, `sweepCenterValue`, or `sweepAmplitudeValue` change**
    - Location: `linien-web/src/components/PlotPanel.tsx:266-291`
    - The dependent effect at 610-618 then calls `u.redraw(false, true)`.
    - With sweep params changing as user adjusts the device, this forces an extra full uPlot redraw outside the data path.

31. **`PlotPanel.setData` is called inside `uplotRef.current.batch()` even when the data reference is identical**
    - Location: `linien-web/src/components/PlotPanel.tsx:580-607`
    - The effect's dependency is `data` — but `data` is a new tuple every frame because of the `useMemo` miss above (finding 18). So the batch always runs.
    - Once `data` is built from reused buffers this becomes a non-issue.

## Cross-cutting

32. **Backend serializes ~10k floats per device per frame as JSON; frontend parses + validates them**
    - The largest sustained CPU sink end-to-end.
    - Adopting `orjson` on the gateway *plus* moving WS parsing to a worker (or switching to a binary frame) would likely give the biggest user-visible win for 12-device deployments.

33. **Both ends still build everything to "full" if any single subscriber is full**
    - A single open device-detail tab forces every overview card on that device to receive heavy frames.
    - Fix: split by per-subscriber detail at the publish boundary so summary subscribers always get summary even when a full subscriber exists.

## Suggested priority order

Top wins first, roughly ordered by expected user-visible impact for the 12-device case:

1. Finding 1 + 2 + 3: cheap `status()` snapshot path, parallel `/statuses`, drop redundant `deepcopy`s.
2. Finding 5 + 32: switch gateway WS sends to `orjson`.
3. Finding 18 + 31: reuse `Float64Array` buffers in `PlotPanel` to stop per-frame array allocation.
4. Finding 16 + 17: make `useLockSummary` incremental and isolate header/popover state from per-device flushes.
5. Finding 4 + 6 + 33: better summary-vs-full discrimination at the publish boundary.
6. Finding 9 + 10: batch param updates server-side.
7. Remaining medium/low items as cleanup.
