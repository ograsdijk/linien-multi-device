import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Group, Stack, Switch, Text } from '@mantine/core';

import { api } from '../api';
import type { LockApproachProbeResult, LockApproachSettings } from '../types';
import { toFiniteNumberOr, toRoundedIntOr } from '../utils/numberInput';
import { DeferredNumberInput } from './DeferredNumberInput';

const DEFAULTS: LockApproachSettings = {
  enabled: false,
  capture_fraction: 0.5,
  max_correction_span: 4,
  max_direct_jump_v: 2,
  approach_offset_v: 0.05,
  ramp_step_v: 0.005,
  ramp_step_delay_ms: 20,
  settle_ms: 300,
  approach_from_below: true,
  max_approach_iterations: 2,
};

const VERDICT_COLOR: Record<LockApproachProbeResult['verdict'], string> = {
  backlash: 'yellow',
  creep: 'yellow',
  drift_or_creep: 'orange',
  negligible: 'green',
  inconclusive: 'gray',
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
  settingsFromStream?: LockApproachSettings | null;
};

export const LockApproachPanel = memo(function LockApproachPanel({
  deviceKey,
  halfRangeSweepV,
  active = true,
  settingsFromStream,
}: Props) {
  const [settings, setSettings] = useState<LockApproachSettings>(DEFAULTS);
  // Saves stay disabled until the device's own settings are in hand. Without
  // this, a failed load leaves the panel showing DEFAULTS and the first edit
  // writes those defaults over whatever the device actually had stored.
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [probe, setProbe] = useState<LockApproachProbeResult | null>(null);
  // The configuration the probe was taken under. Its verdict describes the
  // actuator and stays valid, but the acceptance window it reports describes a
  // configuration -- so that comparison has to be withdrawn once the window
  // settings change under it.
  const [probeSettings, setProbeSettings] = useState<LockApproachSettings | null>(
    null
  );
  const [probeError, setProbeError] = useState<string | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const saveTimer = useRef<number | null>(null);
  // Latest unsaved value, so an unmount inside the debounce window can flush it
  // rather than drop the edit. The device key travels with it: flushing against
  // whatever device is current at unmount would write one device's settings
  // onto another.
  const pending = useRef<{ key: string; settings: LockApproachSettings } | null>(null);
  // Mirrors `settings` so edits can be computed outside the state updater.
  // React may invoke an updater more than once (StrictMode does so in dev), and
  // scheduling a save from inside one would leak a timer and issue the PUT
  // twice.
  const settingsRef = useRef<LockApproachSettings>(DEFAULTS);

  const applySettings = useCallback((next: LockApproachSettings) => {
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
      .getLockApproachSettings(deviceKey)
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
    // A measurement belongs to the device it was taken on. Cleared on a device
    // change for the same reason the settings fetch checks its own identity:
    // this component takes deviceKey as a prop, so correctness here must not
    // depend on a parent remembering to key it.
    setProbe(null);
    setProbeSettings(null);
    setProbeError(null);
  }, [deviceKey]);

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
        api.updateLockApproachSettings(key, unsaved).catch(() => null);
        pending.current = null;
      }
    },
    []
  );

  const update = useCallback(
    (patch: Partial<LockApproachSettings>) => {
      // Computed and scheduled outside the state updater, which must stay pure.
      const next = { ...settingsRef.current, ...patch };
      applySettings(next);
      pending.current = { key: deviceKey, settings: next };
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => {
        api
          .updateLockApproachSettings(deviceKey, next)
          .catch(() => null)
          .finally(() => {
            if (pending.current?.settings === next) pending.current = null;
          });
        saveTimer.current = null;
      }, 250);
    },
    [applySettings, deviceKey]
  );

  const measure = useCallback(async () => {
    setMeasuring(true);
    setProbeError(null);
    try {
      // Two settle times are the minimum that can show a decay; more only makes
      // an already slow, actuator-moving request slower.
      setProbe(await api.measureLockApproach(deviceKey, [50, 500]));
      setProbeSettings(settingsRef.current);
    } catch (error) {
      setProbe(null);
      setProbeSettings(null);
      setProbeError(error instanceof Error ? error.message : String(error));
    } finally {
      setMeasuring(false);
    }
  }, [deviceKey]);

  const toleranceV =
    halfRangeSweepV != null ? settings.capture_fraction * halfRangeSweepV : null;
  // What the lock actually applies. On a closely spaced signal the configured
  // window is narrowed to stay clear of the neighbouring feature, so showing
  // only the configured figure would overstate the real threshold -- in the
  // worst measured case by more than three times. Known once a probe has run.
  const appliedToleranceV = probe?.capture_tolerance_v ?? null;
  // Only while the window settings still match the ones the probe ran under.
  const windowUnchanged =
    probeSettings != null &&
    probeSettings.capture_fraction === settings.capture_fraction &&
    probeSettings.max_correction_span === settings.max_correction_span;
  const narrowed =
    windowUnchanged &&
    toleranceV != null &&
    appliedToleranceV != null &&
    appliedToleranceV < toleranceV - 1e-9;

  if (status !== 'ready') {
    return (
      <Stack gap="xs">
        {status === 'loading' ? (
          <Text size="xs" c="dimmed">
            Loading approach settings…
          </Text>
        ) : (
          <Alert color="red" variant="light" title="Could not load approach settings">
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
      <Switch
        label="Guarded center move (anti-backlash)"
        description="Confirm the feature landed inside the capture region before locking."
        checked={settings.enabled}
        onChange={(event) => update({ enabled: event.currentTarget.checked })}
      />
      {settings.enabled ? (
        <>
          <Group grow>
            <DeferredNumberInput
              label="Capture fraction"
              description={
                toleranceV == null
                  ? 'x the calibrated feature half-width'
                  : narrowed
                    ? `= ${toleranceV.toFixed(4)} V, applied as ${appliedToleranceV!.toFixed(4)} V`
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
              label="Approach offset (V)"
              description="Overshoot; must exceed the backlash width"
              value={settings.approach_offset_v}
              min={0}
              step={0.01}
              decimalScale={4}
              onCommit={(value) =>
                update({
                  approach_offset_v: toFiniteNumberOr(
                    value,
                    DEFAULTS.approach_offset_v
                  ),
                })
              }
            />
          </Group>
          <Group grow>
            <DeferredNumberInput
              label="Ramp step (V)"
              value={settings.ramp_step_v}
              min={0.0001}
              step={0.001}
              decimalScale={4}
              onCommit={(value) =>
                update({ ramp_step_v: toFiniteNumberOr(value, DEFAULTS.ramp_step_v) })
              }
            />
            <DeferredNumberInput
              label="Ramp step delay (ms)"
              value={settings.ramp_step_delay_ms}
              min={0}
              step={5}
              decimalScale={0}
              onCommit={(value) =>
                update({
                  ramp_step_delay_ms: toRoundedIntOr(
                    value,
                    DEFAULTS.ramp_step_delay_ms
                  ),
                })
              }
            />
          </Group>
          <Group grow>
            <DeferredNumberInput
              label="Settle (ms)"
              description="Raise this when the offset is creep, not backlash"
              value={settings.settle_ms}
              min={0}
              step={50}
              decimalScale={0}
              onCommit={(value) =>
                update({ settle_ms: toRoundedIntOr(value, DEFAULTS.settle_ms) })
              }
            />
            <DeferredNumberInput
              label="Corrections per direction"
              value={settings.max_approach_iterations}
              min={1}
              step={1}
              decimalScale={0}
              onCommit={(value) =>
                update({
                  max_approach_iterations: toRoundedIntOr(
                    value,
                    DEFAULTS.max_approach_iterations
                  ),
                })
              }
            />
          </Group>
          <Group grow>
            <DeferredNumberInput
              label="Skip direct probe above (V)"
              description="Jump size that goes straight to the guarded move"
              value={settings.max_direct_jump_v}
              min={0}
              step={0.05}
              decimalScale={4}
              onCommit={(value) =>
                update({
                  max_direct_jump_v: toFiniteNumberOr(
                    value,
                    DEFAULTS.max_direct_jump_v
                  ),
                })
              }
            />
            <DeferredNumberInput
              label="Neighbour guard (feature widths)"
              description="Reject a re-detection further out as a different crossing"
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
          </Group>
          <Switch
            label="Approach from below"
            description="Matches the rising branch the crossing is detected on."
            checked={settings.approach_from_below}
            onChange={(event) =>
              update({ approach_from_below: event.currentTarget.checked })
            }
          />
        </>
      ) : null}
      <Group gap="xs">
        <Button
          variant="light"
          color="grape"
          size="xs"
          loading={measuring}
          onClick={() => {
            measure().catch(() => null);
          }}
        >
          Measure hysteresis
        </Button>
        <Text size="xs" c="dimmed">
          {measuring
            ? 'Moving the sweep center — up to about half a minute.'
            : 'Approaches from both directions without locking. Takes up to about half a minute.'}
        </Text>
      </Group>
      {probeError ? (
        <Alert color="red" variant="light" title="Measurement failed">
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {probeError}
          </Text>
        </Alert>
      ) : null}
      {probe ? (
        <Alert color={VERDICT_COLOR[probe.verdict]} variant="light" title={probe.verdict}>
          <Text size="sm">{probe.detail}</Text>
          {narrowed ? (
            <Text size="xs" c="dimmed" mt={4}>
              Acceptance window narrowed to {appliedToleranceV!.toFixed(4)} V — the
              neighbouring feature is close, so this signal is more tightly spaced
              than capture_fraction assumes.
            </Text>
          ) : null}
          <Text size="xs" c="dimmed" mt={4}>
            {probe.samples
              .map(
                (sample) =>
                  `${sample.from_below ? 'below' : 'above'} @${sample.settle_ms}ms: ` +
                  (sample.offset_v == null
                    ? 'no detection'
                    : `${sample.offset_v >= 0 ? '+' : ''}${sample.offset_v.toFixed(4)} V`)
              )
              .join(' | ')}
          </Text>
        </Alert>
      ) : null}
    </Stack>
  );
});
