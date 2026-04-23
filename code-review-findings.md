# Code Review Findings

Date: 2026-04-21

Scope: `linien-gateway` and `linien-web`

## Implementation Checklist

- [x] Fix undefined `exc` in the `auto_lock_scan` Postgres fallback in `linien-gateway/app/main.py`
- [x] Add regression coverage for `auto_lock_scan` when Postgres enqueue raises
- [x] Add a dedicated session state lock for cached plot and parameter state in `linien-gateway/app/session.py`
- [x] Read session snapshots from stable copies instead of live mutable state
- [x] Add websocket close/error recovery and reconnect handling in the web client
- [x] Stop unnecessary group/device refetches on tab changes in the web client
- [x] Optimize `save_device` to avoid repeated full device-list loads
- [x] Optimize group membership maintenance to avoid repeated list membership scans
- [x] Replace per-device status polling requests with a batched status polling path

## Current Validation

- `pytest tests/test_main_auto_lock_scan_api.py tests/test_session_sync.py tests/test_group_store.py tests/test_device_store.py tests/test_main_statuses_api.py -q` -> 16 passed
- `npm run build` in `linien-web` -> passed

## High-priority issues

### 1. Exception handler uses `exc` without binding it

- Severity: High
- Location: `linien-gateway/app/main.py` around the `auto_lock_scan` Postgres enqueue fallback (`except Exception` near lines 681-693)
- Issue: The handler catches a generic exception with `except Exception:` and then logs `str(exc)`. Because `exc` is not bound in that branch, the fallback code can raise `UnboundLocalError` and hide the original enqueue failure.
- Impact: Error reporting in the auto-lock scan path can fail exactly when the code is trying to report an operational problem.
- Potential solution:
  - Change the handler to `except Exception as exc:`.
  - Add a regression test that forces `lock_result_postgres.enqueue_lock_result(...)` to fail and verifies that the API still returns the original warning/log path instead of crashing in the exception handler.

### 2. Shared session state is read and written without a dedicated lock

- Severity: High
- Location:
  - `linien-gateway/app/session.py:726`
  - `linien-gateway/app/session.py:872-880`
  - `linien-gateway/app/session.py:938-943`
  - `linien-gateway/app/session.py:1034`
- Issue: The poll thread updates `param_cache`, `param_cache_serialized`, `last_plot_frame`, and `plot_state`, while request handlers read the same structures for snapshots, auto-lock, and manual-lock trace extraction. These reads and writes are not protected by a shared state lock.
- Impact:
  - Inconsistent websocket snapshots
  - Stale or torn plot data consumed by auto-lock
  - Manual lock records built from partially updated state
- Potential solution:
  - Introduce a dedicated `_state_lock` for session-owned mutable caches and plot state.
  - Guard writes in `_on_param_changed` and `_on_to_plot` with that lock.
  - Read through small helper methods that return stable copies for `snapshot`, `auto_lock_from_scan`, and `_extract_manual_lock_traces`.
  - Keep `_rpyc_lock` limited to remote control/parameter access, and use `_state_lock` for local in-memory state.
  - Add concurrency-oriented tests that simulate poll updates while API handlers request snapshots or auto-lock data.

### 3. Device websocket path has no close/error recovery

- Severity: High
- Location:
  - `linien-web/src/ws.ts`
  - `linien-web/src/hooks/useDeviceStream.ts`
- Issue: Device streams define only `onmessage`. There is no `onclose` or `onerror` handling, and no reconnect strategy while a device view remains active.
- Impact:
  - UI can silently stop updating after a network hiccup or backend restart.
  - Users may see stale status and plots without any visible disconnected state.
- Potential solution:
  - Add socket lifecycle handlers in `openDeviceStream` or `useDeviceStream`.
  - Surface connection state to the caller so the UI can show degraded status.
  - Add bounded reconnect with backoff while `enabled === true`.
  - Reset stale stream state when the socket closes and the stream cannot be re-established.

### 4. Group/device data refetches on tab changes

- Severity: Medium
- Location:
  - `linien-web/src/features/devices/useDeviceCatalog.ts:38-56`
  - `linien-web/src/App.tsx:556`
- Issue: `loadGroups` closes over `activeTabKey`, and the initial loading effect depends on `loadGroups`. Changing tabs recreates the callback, which reruns the effect and refetches devices and groups even though the underlying data did not change.
- Impact:
  - Unnecessary API traffic on simple navigation
  - Increased chance of response-order races or visible UI flicker
- Potential solution:
  - Split initial data loading from active-tab validation.
  - Keep `loadGroups` stable and move the `activeTabKey` consistency check into a separate effect that reacts to `groups` and `activeTabKey`.
  - If the current tab becomes invalid, reset it locally without forcing a refetch.

## Refactor and performance opportunities

### 5. `save_device` reloads the device list to determine add vs update

- Priority: Medium
- Location: `linien-gateway/app/device_store.py:12-24`
- Issue: `save_device` calls `get_device`, and `get_device` reloads the full device list before deciding whether to add or update.
- Impact: Repeated file or storage reads on every save path.
- Potential solution:
  - Load the device list once inside `save_device` and decide from that result.
  - If the upstream storage library supports direct upsert semantics, use that instead.
  - If saves become frequent, consider a small repository abstraction instead of repeated module-level full-list scans.

### 6. Group membership maintenance uses repeated list membership checks

- Priority: Medium
- Location: `linien-gateway/app/group_store.py:80-104`
- Issue: `list_groups` repeatedly checks membership with Python lists while filtering and auto-including device keys.
- Impact: The cost grows quadratically as device and group counts increase.
- Potential solution:
  - Convert incoming `device_keys` to a set once.
  - Use set-based filtering for membership decisions, then write back lists in stable order if ordering matters.
  - Keep the current JSON schema unchanged while improving the in-memory algorithm.

### 7. Status polling scales linearly with device count

- Priority: Medium
- Location: `linien-web/src/features/devices/useDeviceStatusPolling.ts:60-64`
- Issue: The client sends one `GET /status` request per device on every interval for devices not covered by websockets.
- Impact: With more devices, polling load increases linearly and can become wasteful.
- Potential solution:
  - Add a batched status endpoint on the gateway.
  - Reduce polling frequency for idle or disconnected devices.
  - Consider reusing websocket status for more views so fewer devices fall back to HTTP polling.

## Suggested implementation order

1. Fix the undefined `exc` handler and add a regression test.
2. Introduce a session state lock and make snapshot/plot readers copy from stable state.
3. Add websocket close/error handling and reconnect behavior for device streams.
4. Stop refetching groups/devices on tab changes.
5. Clean up the lower-cost storage and polling optimizations.

## Test gaps worth adding

- A test that forces the Postgres enqueue fallback in `auto_lock_scan` to throw and verifies logging still works.
- A concurrency test for `DeviceSession.snapshot()` during poll-thread updates.
- A concurrency test for `auto_lock_from_scan()` while plot frames are still arriving.
- A frontend test for websocket disconnect/reconnect handling.
- A hook test showing that tab changes do not trigger redundant `loadDevices()` and `loadGroups()` calls.

## Validation note

Focused backend validation is now available in the configured gateway virtual environment. The currently implemented backend fixes were validated with `pytest tests/test_main_auto_lock_scan_api.py tests/test_session_sync.py tests/test_group_store.py tests/test_device_store.py tests/test_main_statuses_api.py -q`, which passed. The implemented frontend fixes were validated with `npm run build` in `linien-web`, which also passed.
