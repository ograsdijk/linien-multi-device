# Frontend Performance Implementation Plan

Date: 2026-06-08

Scope: Implement all frontend findings from `performance-findings-pass-2.md` (items 16-31 plus the frontend halves of the cross-cutting items 32-33).

Each step lists the files to touch, what to change, and how to verify. Steps are ordered to minimize churn: low-risk independent fixes first, larger refactors later, with verification gates between phases.

---

## Phase 1 — Independent low-risk fixes

These touch a single file each and do not interact with one another.

### Step 1.1 — Fix `appendLogEntries` anti-pattern (finding 23)

- Files: `linien-web/src/features/logs/useLogsController.ts`
- Change:
  - Move `setLogsErrorLatched(true)` out of the `setLogRows` updater into a `useEffect` keyed on `logRows`.
  - Track latched state with a ref that compares the last-seen tail to detect new error entries without scanning the whole array.
- Verify: `npm run build`; manually exercise the logs modal (open, generate error entry, close) — chip should still go red.

### Step 1.2 — Make `appendLogEntries` dedup O(1) (finding 21)

- Files: `linien-web/src/features/logs/useLogsController.ts`
- Change:
  - Add a `seenIdsRef = useRef<Set<string>>(new Set())` alongside `logRows`.
  - On append, check membership against the ref; mutate the ref when accepted; on `clearLogs` reset both.
  - When trimming to `MAX_LOG_ROWS`, also evict dropped ids from the set.
- Verify: build; simulate a log burst (open Logs modal, leave running for several minutes). Profile shows the append path now stays flat instead of growing with row count.

### Step 1.3 — Precompute log-row haystacks (finding 22)

- Files: `linien-web/src/features/logs/useLogsController.ts`
- Change:
  - Augment the in-memory log row shape with a precomputed lowercase `_haystack` string built once on append from `message`, `code`, `JSON.stringify(details)`, and device name.
  - `filteredLogRows` then does `entry._haystack.includes(needle)` instead of rebuilding the string per render.
- Verify: build; type a search query while a large log buffer is loaded — typing latency should drop.

### Step 1.4 — Guard `onCloseRef` against being fired during unmount cleanup (finding 27)

- Files: `linien-web/src/hooks/useDeviceStream.ts`
- Change:
  - Track `disposed` in a ref the cleanup sets first; `notifyClosed` checks the ref and only calls `onCloseRef.current?.()` when `!disposed`. Alternatively, queue the close notification via a `queueMicrotask` so React isn't mid-unmount when it fires.
- Verify: build; mount/unmount device cards rapidly (switch tabs); React no longer warns "Cannot update a component while rendering a different component".

### Step 1.5 — Stabilize `DeferredNumberInput.formatValue` handling (finding 28)

- Files: `linien-web/src/components/DeferredNumberInput.tsx`
- Change:
  - Store `formatValue` in a ref and update it in a `useEffect`.
  - Drop `formatValue` from the existing `useEffect`'s dependency array; only depend on `value`.
- Verify: build; touch inputs in GeneralPanel (e.g. analog-out V fields with derived formatter) — UI should still reflect external value changes when blurred.

### Step 1.6 — Stabilize `useDeviceCatalog.normalizeOrderKeys` (finding 29)

- Files: `linien-web/src/features/devices/useDeviceCatalog.ts`
- Change:
  - Keep `devices` in a ref and rewrite `normalizeOrderKeys` to read from that ref so its callback identity stays stable.
  - The `useEffect` that calls `normalizeOrderKeys` then runs only when `devices.length` or the device-key tuple changes (use a derived stable key list).
- Verify: build; rearrange devices in the navbar via drag — ordering still persists across refresh; React profiler shows fewer effect re-runs on incidental state changes.

### Step 1.7 — Skip `axisValues` / `xValueFormatter` recompute when only data changed (finding 30)

- Files: `linien-web/src/components/PlotPanel.tsx`
- Change:
  - Wrap the redraw effect at lines 610-618 so it only fires when `axisLabel` actually changed (compare via ref) rather than on every new `axisValues` / `xValueFormatter` identity.
  - Better: stop recreating these callbacks every render by holding sweep params in refs that the existing callbacks read.
- Verify: build; adjust sweep center on a device — plot updates without an extra redraw per data tick. Open uPlot inspector or sample with browser perf — `redraw(false, true)` should now fire only on actual axis-label changes.

### Step 1.8 — Trigger first `pollStatuses` when devices first arrive (finding 26)

- Files: `linien-web/src/features/devices/useDeviceStatusPolling.ts`
- Change:
  - Add an effect keyed on `devices.length` transitioning from `0` to `> 0` that calls `pollStatuses()` once.
  - Optionally, also fire when the set of device keys changes (compare via sorted joined string).
- Verify: build; load app with devices configured — first status results visible in <1 s instead of waiting up to the poll interval.

---

## Phase 2 — Plot data path (largest single win)

Tackles findings 18 and 31 together; they share the same buffer reuse change.

### Step 2.1 — Reusable `Float64Array` buffers per device card

- Files:
  - `linien-web/src/components/PlotPanel.tsx`
- Change:
  - Replace the per-frame `Array<number | null>` allocation in `normalizeSeries` / `data` `useMemo` with a per-`PlotPanel` `useRef<{ x: Float64Array; series: Float64Array[] }>` allocated to `N_POINTS` (2048).
  - Define a helper `writeSeriesInto(buffer, value)` that mirrors `normalizeSeries` but writes into `buffer` and uses `NaN` for nulls (uPlot's `spanGaps` already handles NaN as gap).
  - Update the x buffer once when `pointCount` changes; otherwise reuse.
  - Pass the typed arrays to uPlot via `setData` — uPlot accepts typed arrays.
- Risk: existing series visibility logic at line 528-607 uses `values.some((v) => typeof v === 'number' && Number.isFinite(v))`. Update that to read from the typed array (a small numeric loop with `isFinite`).
- Verify:
  - `npm run build`.
  - Manual: 12-device overview — check FPS and JS heap. Heap should be flat; GC pauses should mostly disappear.

### Step 2.2 — Stop calling `setData` when frame contents are byte-identical

- Files: `linien-web/src/components/PlotPanel.tsx`
- Change:
  - After writing into typed arrays, compute a tiny fingerprint (e.g. `pointCount + first/last values per series` or a monotonically increasing counter passed from the stream payload) and skip `batch+setData+setScale` when unchanged.
  - Simpler alternative: only skip the effect when `activePlotFrame` reference is the same (already true if the parent passes the previous frame, which it now does for `selectionMode !== null`).
- Verify: build; with selection mode active no extra `setData` calls fire on each frame.

---

## Phase 3 — App-level fan-out

These reduce the cost of each batched flush and are interrelated; do them together so we only verify the end-to-end behavior once.

### Step 3.1 — Split App-wide state into per-concern providers

- New files:
  - `linien-web/src/state/DeviceStatesContext.tsx`
  - `linien-web/src/state/LockSummaryContext.tsx`
- Files to change:
  - `linien-web/src/App.tsx`
  - `linien-web/src/components/AppHeaderControls.tsx`
  - `linien-web/src/components/DeviceList.tsx`
  - `linien-web/src/components/GroupModulationSummary.tsx`
  - Any other consumer of `deviceStates` / `lockSummary` data.
- Change:
  - Hoist `deviceStates`, `deviceStatesUpdater`, and `streamingDeviceKeys` into `DeviceStatesProvider` exposing stable selector hooks (`useDeviceState(deviceKey)`, `useDeviceStatesUpdater()`).
  - Hoist `useLockSummary` outputs into `LockSummaryProvider`. Consumers grab only the slice they need via dedicated hooks.
  - `App` itself stops re-rendering on per-device flushes; only the `Tabs` panel content and the header chip subscribers re-render.
- Verify: build; manually inspect with React Profiler — App's render count during steady 12-device streaming drops dramatically; device cards still update.

### Step 3.2 — Make `useLockSummary` incremental (finding 16)

- Files: `linien-web/src/features/locks/useLockSummary.ts`
- Change:
  - Replace the five `useMemo` rebuilds with a single reducer state `{ statusByKey, indicatorByKey, autoRelockByKey, lockByKey, effectiveLockByKey }`.
  - Drive it from the new `DeviceStatesProvider` by subscribing to per-device deltas (`{ deviceKey, status?, plotFrame? }`). On each delta, copy only that one map's row and recompute aggregates incrementally.
  - Aggregates `connectedDeviceCount`, `lockedDeviceCount`, `connectedRelockEnabledCount`, `lockHealthSummary` are tracked as plain numbers updated O(1) per delta.
- Verify: build; the values shown in the lock-chip popover must match the previous full-rebuild implementation. Add a small smoke test by toggling lock on/off on one device and confirming counts update.

### Step 3.3 — Stabilize `websocketActiveDeviceKeys`

- Files: `linien-web/src/App.tsx` (or wherever it lands after the provider split)
- Change:
  - Replace the `useMemo` that allocates a new `Set` on every streamingDeviceKeys change with a ref-backed Set updated in place and a version counter for cache invalidation.
  - Polling hook continues reading via ref.
- Verify: build; React Profiler shows App body not re-rendering on stream lifecycle events.

---

## Phase 4 — Network/wire format

Bigger ergonomic changes; do after Phase 3 so the parsing wins land on top of fewer re-renders.

### Step 4.1 — Single shared IntersectionObserver (finding 20)

- Files:
  - `linien-web/src/hooks/useInViewport.ts`
- Change:
  - Move to a module-level singleton observer with a `WeakMap<Element, (visible: boolean) => void>` registry; `useInViewport` registers/unregisters callbacks instead of constructing an observer.
  - Honor per-call `rootMargin` by allowing the caller to pass it but mapping to a small set of shared observers keyed by serialized options (1-2 in practice).
- Verify: build; profile shows IntersectionObserver setup once at module load; scrolling the navbar/overview behaves identically.

### Step 4.2 — Move WS parsing off the main thread (finding 19)

- New files:
  - `linien-web/src/workers/streamParserWorker.ts`
- Files to change:
  - `linien-web/src/ws.ts`
  - `linien-web/src/hooks/useDeviceStream.ts`
- Change:
  - Add a single shared Worker that owns one or many WebSockets (decide whether the Worker holds each `WebSocket` itself — cleanest — or only does parsing while the main thread keeps sockets).
  - Cleanest approach: main thread creates `WebSocket`, the worker holds a `MessagePort`, and main thread forwards `event.data` strings to the worker for parse+validate. Worker posts back the typed message.
  - Use `Transferable` / `ArrayBuffer` only if we move to binary later; for now strings are fine.
  - Keep the parser logic centralized in `messageGuards.ts` and import it from both the main thread (for tests) and the worker.
- Risk: increases complexity of `useDeviceStream`; ensure proper teardown when the device card unmounts (port close).
- Verify:
  - `npm run build` (Vite/Rollup must bundle the worker — use `?worker` import).
  - Manual: 12 devices streaming, observe main-thread `JSON.parse` cost in Chrome perf flamegraph drop to near zero.

### Step 4.3 — Optional: switch to MessagePack binary frames

- Only if a Phase-4.2 worker isn't enough. Out of scope for the initial pass; tracked separately.

---

## Phase 5 — Header / popover cleanup

Smaller but mechanical changes that complete finding 24 and the related items.

### Step 5.1 — Extract popover bodies into self-subscribing components (finding 24)

- New files:
  - `linien-web/src/components/header/InfluxPopover.tsx`
  - `linien-web/src/components/header/LockChipPopover.tsx`
  - `linien-web/src/components/header/PostgresPopover.tsx`
- Files to change:
  - `linien-web/src/components/AppHeaderControls.tsx`
- Change:
  - Each popover component reads what it needs from the providers added in Phase 3 (lock summary, devices) rather than receiving it via 50+ props.
  - `AppHeaderControls` shrinks to a tiny composition of three popovers plus the logs button and color-scheme toggle.
- Verify: build; manually open each popover and confirm behavior matches.

### Step 5.2 — Memoize `GroupModulationSummary` rows (finding 25)

- Files: `linien-web/src/components/GroupModulationSummary.tsx`
- Change:
  - Memo each row by `(deviceKey, params)` so opening the popover doesn't recompute formatters for all rows when one device's params change.
  - Optionally, accept only the small subset of params the row needs (e.g. `modulation_frequency`, `demodulation_multiplier_a`, `modulation_amplitude`) via a selector hook.
- Verify: build; open Mod-freqs popover, watch values update without flashing the whole table.

---

## Phase 6 — Final cleanup and validation

### Step 6.1 — Delete now-dead per-prop wiring in `App.tsx`

- Files: `linien-web/src/App.tsx`
- Change: after Phase 3+5, remove the long prop lists and the corresponding interfaces in `AppHeaderControls`. Confirm no dead `useMemo` / `useCallback` left behind.

### Step 6.2 — Full build + sanity checks

- `npm run build` in `linien-web` — must pass.
- Manual smoke test:
  - 12-device overview at 30 Hz: FPS stable, GC pauses minimal, App component renders only when summary aggregates change.
  - Open a single device in a group at full detail: that card's stream switches to `detail=full`; others remain summary.
  - Logs modal: large buffer, fast search.
  - Sweep center change: plot updates once per data tick, not twice.
- Browser profiler: confirm `JSON.parse` no longer on the main-thread hot list (Phase 4 done).

---

## Test gaps to fill alongside the work

Where it's cheap, add lightweight tests next to the changes:

- A reducer test for the new incremental `useLockSummary` (Phase 3.2) covering the same outputs as the current full-rebuild version.
- A test for `useLogsController.appendLogEntries` dedup + haystack precompute (Phase 1.2, 1.3).
- A test (or storybook fixture) that drives `PlotPanel` with reused buffers (Phase 2.1) and asserts uPlot receives typed arrays.

---

## Rollout / risk notes

- Phases 1, 2, 3 each individually compile and can be verified independently; merge per-phase to keep blast radius small.
- Phase 3 is the riskiest because it refactors how state flows through `App`. Plan it as its own change with a clear before/after of the provider tree.
- Phase 4 introduces a Worker; verify it works under Vite's prod build (not just `vite dev`) — `import.meta.url`-based worker construction or `?worker` suffix imports both need to survive bundling.
- After all phases, re-baseline by re-reading `performance-findings-pass-2.md` and confirming each frontend finding is closed; carry forward any remaining items in a new pass document.
