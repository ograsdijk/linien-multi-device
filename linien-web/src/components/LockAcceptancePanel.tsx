import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Group, Stack, Text } from '@mantine/core';

import { api } from '../api';
import type { LockAcceptanceSettings } from '../types';
import { toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';
import { DeferredNumberInput } from './DeferredNumberInput';

const DEFAULTS: LockAcceptanceSettings = {
  capture_fraction: 0.5,
  max_correction_span: 4,
  settle_ms: 300,
};

type Props = {
  deviceKey: string;
  // The calibrated feature half-width, used to show the derived acceptance
  // window in volts -- it is never typed in directly.
  halfRangeSweepV?: number;
  // Mantine keeps tab panels mounted, so this component exists for every device
  // workspace whether or not anyone opens the Autolock tab. Loading is deferred
  // until it is actually shown.
  active?: boolean;
  // Latest value broadcast by the gateway, so another tab's edit is picked up
  // rather than each client keeping its own stale copy.
  settingsFromStream?: LockAcceptanceSettings | null;
};

export const LockAcceptancePanel = memo(function LockAcceptancePanel({
  deviceKey,
  halfRangeSweepV,
  active = true,
  settingsFromStream,
}: Props) {
  const [settings, setSettings] = useState<LockAcceptanceSettings>(DEFAULTS);
  // Saves stay disabled until the device's own settings are in hand. Without
  // this, a failed load leaves the panel showing DEFAULTS and the first edit
  // writes those defaults over whatever the device actually had stored.
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const saveTimer = useRef<number | null>(null);
  // Latest unsaved value, so an unmount inside the debounce window can flush it
  // rather than drop the edit. The device key travels with it: flushing against
  // whatever device is current at unmount would write one device's settings
  // onto another.
  const pending = useRef<{ key: string; settings: LockAcceptanceSettings } | null>(null);
  // Mirrors `settings` so edits can be computed outside the state updater.
  // React may invoke an updater more than once (StrictMode does so in dev), and
  // scheduling a save from inside one would leak a timer and issue the PUT
  // twice.
  const settingsRef = useRef<LockAcceptanceSettings>(DEFAULTS);

  const applySettings = useCallback((next: LockAcceptanceSettings) => {
    settingsRef.current = next;
    setSettings(next);
  }, []);

  // Which device the current settings were fetched for, so returning to the tab
  // does not refetch and flash the controls away behind a loading state.
  const loadedFor = useRef<string | null>(null);

  const load = useCallback(() => {
    setStatus('loading');
    loadedFor.current = deviceKey;
    const requestedFor = deviceKey;
    return api
      .getLockAcceptanceSettings(deviceKey)
      .then((loaded) => {
        // Another device's load started while this one was in flight: two
        // responses race, and without this the slower one wins and shows one
        // device's stored settings under another's name.
        if (loadedFor.current !== requestedFor) return;
        applySettings(loaded);
        setStatus('ready');
      })
      .catch(() => {
        if (loadedFor.current !== requestedFor) return;
        // Clear the marker so a later visit retries rather than being stuck
        // behind a one-off failure.
        loadedFor.current = null;
        setStatus('error');
      });
  }, [applySettings, deviceKey]);

  useEffect(() => {
    if (!active) return;
    if (loadedFor.current === deviceKey) return;
    load().catch(() => null);
  }, [active, deviceKey, load]);

  useEffect(() => {
    if (!settingsFromStream || status !== 'ready') return;
    // Never let a broadcast -- possibly this client's own -- overwrite an edit
    // that has not been saved yet.
    if (pending.current) return;
    applySettings(settingsFromStream);
  }, [applySettings, settingsFromStream, status]);

  useEffect(
    () => () => {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current);
        saveTimer.current = null;
      }
      if (pending.current) {
        const { key, settings: unsaved } = pending.current;
        api.updateLockAcceptanceSettings(key, unsaved).catch(() => null);
        pending.current = null;
      }
    },
    []
  );

  const update = useCallback(
    (patch: Partial<LockAcceptanceSettings>) => {
      // Computed and scheduled outside the state updater, which must stay pure.
      const next = { ...settingsRef.current, ...patch };
      applySettings(next);
      pending.current = { key: deviceKey, settings: next };
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => {
        api
          .updateLockAcceptanceSettings(deviceKey, next)
          .catch(() => null)
          .finally(() => {
            if (pending.current?.settings === next) pending.current = null;
          });
        saveTimer.current = null;
      }, 250);
    },
    [applySettings, deviceKey]
  );

  const toleranceV =
    halfRangeSweepV != null ? settings.capture_fraction * halfRangeSweepV : null;

  if (status !== 'ready') {
    return (
      <Stack gap="xs">
        {status === 'loading' ? (
          <Text size="xs" c="dimmed">
            Loading acceptance settings…
          </Text>
        ) : (
          <Alert color="red" variant="light" title="Could not load acceptance settings">
            <Text size="sm">
              Editing is disabled so the stored settings are not overwritten with
              defaults.
            </Text>
            <Button
              size="xs"
              variant="light"
              mt="xs"
              onClick={() => {
                load().catch(() => null);
              }}
            >
              Retry
            </Button>
          </Alert>
        )}
      </Stack>
    );
  }

  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        How close the auto-lock refinement walk has to land before it commits the
        lock, and how long it lets the mechanics settle before believing a trace.
      </Text>
      <Group grow>
        <DeferredNumberInput
          label="Capture fraction"
          description={
            toleranceV == null
              ? 'x the calibrated feature half-width'
              : `= ${toleranceV.toFixed(4)} V acceptance window`
          }
          value={settings.capture_fraction}
          min={0}
          step={0.05}
          decimalScale={3}
          onCommit={(value) =>
            update({
              capture_fraction: toFiniteNumberOr(value, DEFAULTS.capture_fraction),
            })
          }
        />
        <DeferredNumberInput
          label="Neighbour guard (feature widths)"
          description="A detection further out is a different crossing. 0 disables it."
          value={settings.max_correction_span}
          min={0}
          step={0.5}
          decimalScale={2}
          onCommit={(value) =>
            update({
              max_correction_span: toFiniteNumberOr(
                value,
                DEFAULTS.max_correction_span
              ),
            })
          }
        />
        <DeferredNumberInput
          label="Settle (ms)"
          description="Dwell after a geometry write, and the drift-gate handover."
          value={settings.settle_ms}
          min={0}
          step={50}
          decimalScale={0}
          onCommit={(value) =>
            update({ settle_ms: toRoundedIntOr(value, DEFAULTS.settle_ms) })
          }
        />
      </Group>
    </Stack>
  );
});
