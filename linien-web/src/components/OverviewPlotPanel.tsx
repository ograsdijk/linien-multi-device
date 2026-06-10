import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { PlotFrame } from '../types';
import {
  BAND_CONFIGS,
  LABELS,
  N_POINTS,
  SERIES_INDEX,
  SERIES_KEYS,
  SERIES_STYLE,
  type PlotData,
  type SeriesKey,
  type SeriesStats,
  getAxisTheme,
  getCachedAccentColor,
  getXBuffer,
  refreshThemeCache,
  padYRange,
  toFinite,
  toRgba,
  writeSeriesInto,
} from './plotShared';

// Thumbnail variant of PlotPanel used by DeviceOverviewCard.
//
// Imperative API: instead of receiving plot data as a React prop, the
// parent grabs a ref and calls `applyFrame(frame)` directly on each WS
// message. This bypasses React reconciliation entirely on the plot
// hot path -- the panel renders only on lockState / initActive changes
// (rare). Lighthouse showed 46 s of React script-evaluation time
// across a 70 s window with 12 cards streaming; almost all of it was
// reconciliation triggered by the prior plotFrame-prop pattern.
//
// Differences from the full PlotPanel still apply:
//   - cursor disabled
//   - legend hidden (so legend-toggle state is ref-only)
//   - axis values reduced to plain numbers
//   - smaller default height
//   - no selection / autolock / optimization handlers
//   - no manual-target overlay
//   - lock-target line drawn via the imperative draw hook
//
// Shares the buffer-reuse, y-autoscale, and alias pipeline with
// PlotPanel via plotShared.

const POINT_STYLE: uPlot.Series.Points = { show: false };
const DEFAULT_HEIGHT = 220;

export type OverviewPlotPanelHandle = {
  applyFrame: (frame: PlotFrame) => void;
};

type OverviewPlotPanelProps = {
  lockState?: boolean;
  // When false (or absent), defer the uPlot constructor until the
  // card becomes active+visible. Frames that arrive before init
  // completes are buffered and replayed once uPlot is up.
  initActive?: boolean;
};

export const OverviewPlotPanel = forwardRef<
  OverviewPlotPanelHandle,
  OverviewPlotPanelProps
>(function OverviewPlotPanel(
  { lockState, initActive = true },
  ref,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const uplotRef = useRef<uPlot | null>(null);
  const sizeRef = useRef({ width: 400, height: DEFAULT_HEIGHT });
  const lockTargetRef = useRef<number | null>(null);
  const suppressSeriesEventRef = useRef(false);
  const userShowRef = useRef<Record<number, boolean>>({});
  // Legend is hidden so visibility toggles can only happen via the
  // setSeries hook fired by uPlot itself. With cursor off this almost
  // never fires; storing as refs avoids any per-frame React work.
  const error1VisibleRef = useRef(true);
  const error2VisibleRef = useRef(true);

  const seriesBuffersRef = useRef<Float64Array[]>(
    SERIES_KEYS.map(() => new Float64Array(N_POINTS))
  );
  const hasDataByKeyRef = useRef<Record<SeriesKey, boolean>>(
    SERIES_KEYS.reduce((acc, key) => {
      acc[key] = false;
      return acc;
    }, {} as Record<SeriesKey, boolean>)
  );
  const seriesStatsRef = useRef<SeriesStats[]>(
    SERIES_KEYS.map(() => ({ hasFinite: false, min: Infinity, max: -Infinity }))
  );
  const lastVisibilityRef = useRef<{
    lockAxis: boolean | null;
    dual: boolean | null;
    hasDataKey: string;
  }>({ lockAxis: null, dual: null, hasDataKey: '' });
  const lastAppliedXMaxRef = useRef<number | null>(null);
  const lastAppliedYRef = useRef<{ min: number; max: number } | null>(null);

  // Latest frame stashed for:
  //   - lockState changes (visibility set differs between lock/sweep)
  //   - init completion (frames arriving pre-init are replayed)
  const latestFrameRef = useRef<PlotFrame | null>(null);

  // Track lockState in a ref so applyFrameInternal can read it
  // without re-closing on every render.
  const lockStateRef = useRef<boolean | undefined>(lockState);
  useEffect(() => {
    lockStateRef.current = lockState;
  }, [lockState]);

  // Flip to true the first time we apply a frame, so the
  // "Waiting for data..." placeholder disappears. The ref is the
  // authoritative read inside applyFrameInternal (which closes over
  // the first render's state via useImperativeHandle's [] deps);
  // the state mirror just drives the placeholder removal render.
  // Exactly one setHasData call per card lifetime.
  const hasDataRef = useRef(false);
  const [hasData, setHasData] = useState(false);

  const initializedRef = useRef(false);
  // Disposable resources captured at init time. They live in refs so
  // the unmount-only cleanup effect below can tear them down without
  // being part of the [initActive] effect's dep cycle. If we put the
  // cleanup inside the [initActive] effect, React would fire it
  // whenever initActive flipped to false (tab deactivate) and we'd
  // destroy uPlot. The init re-entry guard (initializedRef) would
  // then block reinit on the next initActive=true, leaving the panel
  // permanently blank after the first tab switch-away-and-back.
  const observerRef = useRef<ResizeObserver | null>(null);
  const schemeObserverRef = useRef<MutationObserver | null>(null);
  const resizeHandlerRef = useRef<(() => void) | null>(null);

  // The actual work. Mirrors the previous useLayoutEffect body but
  // is invoked directly (not via React state -> render -> effect).
  const applyFrameInternal = (frame: PlotFrame): void => {
    latestFrameRef.current = frame;
    const u = uplotRef.current;
    if (!u) {
      // Init hasn't run yet (lazy uPlot). The init effect will
      // replay latestFrameRef once uPlot is up.
      return;
    }
    if (!hasDataRef.current) {
      hasDataRef.current = true;
      setHasData(true);
    }

    const buffers = seriesBuffersRef.current;
    const hasDataByKey = hasDataByKeyRef.current;
    const stats = seriesStatsRef.current;
    const series = frame.series;

    // Derive point count from the longest series in this frame.
    let count = 0;
    for (const key of SERIES_KEYS) {
      const v = series?.[key];
      const len = Array.isArray(v)
        ? v.length
        : ArrayBuffer.isView(v)
        ? (v as unknown as ArrayLike<number>).length
        : 0;
      if (len > count) count = len;
    }
    if (count === 0) count = N_POINTS;

    // Grow buffers monotonically.
    for (let i = 0; i < buffers.length; i++) {
      if (buffers[i].length < count) {
        buffers[i] = new Float64Array(count);
      }
    }

    SERIES_KEYS.forEach((key, idx) => {
      const s = writeSeriesInto(buffers[idx], count, series?.[key]);
      hasDataByKey[key] = s.hasFinite;
      stats[idx] = s;
    });

    // lockAxis: explicit lockState prop wins; otherwise read from the
    // frame itself. Same precedence as before.
    const lockAxis =
      typeof lockStateRef.current === 'boolean'
        ? lockStateRef.current
        : Boolean(frame.lock);

    // Alias combined_error <- error_signal_1 in sweep mode (zero-copy).
    const combinedIdx = SERIES_KEYS.indexOf('combined_error');
    const errIdx = SERIES_KEYS.indexOf('error_signal_1');
    let aliasCombinedToErr1 = false;
    if (!lockAxis && hasDataByKey.error_signal_1) {
      hasDataByKey.combined_error = true;
      stats[combinedIdx] = stats[errIdx];
      aliasCombinedToErr1 = true;
    }

    const x = getXBuffer(count);
    const plotData = [x, ...buffers] as unknown as PlotData;
    if (aliasCombinedToErr1) {
      plotData[combinedIdx + 1] = buffers[errIdx];
    }

    const dual = Boolean(frame.dual_channel);
    const desiredVisibility: Record<SeriesKey, boolean> = {
      combined_error: false,
      control_signal: false,
      control_signal_history: false,
      slow_history: false,
      monitor_signal_history: false,
      error_signal_1: false,
      error_signal_2: false,
      monitor_signal: false,
      signal_strength_a_upper: false,
      signal_strength_a_lower: false,
      signal_strength_b_upper: false,
      signal_strength_b_lower: false,
    };
    if (lockAxis) {
      desiredVisibility.combined_error = true;
      desiredVisibility.control_signal = true;
    } else {
      desiredVisibility.combined_error = !dual;
      desiredVisibility.error_signal_1 = dual;
      desiredVisibility.error_signal_2 = dual;
      desiredVisibility.monitor_signal = true;
      desiredVisibility.signal_strength_a_upper = true;
      desiredVisibility.signal_strength_a_lower = true;
      desiredVisibility.signal_strength_b_upper = true;
      desiredVisibility.signal_strength_b_lower = true;
    }

    const error1Visible = error1VisibleRef.current;
    const error2Visible = error2VisibleRef.current;

    // Visibility-change fingerprint.
    let hasDataKey = '';
    for (const key of SERIES_KEYS) hasDataKey += hasDataByKey[key] ? '1' : '0';
    const last = lastVisibilityRef.current;
    const visibilityChanged =
      last.lockAxis !== lockAxis ||
      last.dual !== dual ||
      last.hasDataKey !== hasDataKey;

    // Aggregate y range over visible-and-data series.
    let yMin = Infinity;
    let yMax = -Infinity;
    SERIES_KEYS.forEach((key, idx) => {
      if (!desiredVisibility[key] || !hasDataByKey[key]) return;
      if (key === 'signal_strength_a_upper' || key === 'signal_strength_a_lower') {
        if (!error1Visible) return;
      }
      if (key === 'signal_strength_b_upper' || key === 'signal_strength_b_lower') {
        if (!error2Visible) return;
      }
      const s = stats[idx];
      if (!s.hasFinite) return;
      if (s.min < yMin) yMin = s.min;
      if (s.max > yMax) yMax = s.max;
    });
    const padded = padYRange(yMin, yMax);
    const lastY = lastAppliedYRef.current;
    const ySpan = Math.max(1e-9, padded.yMax - padded.yMin);
    const yChanged =
      lastY === null ||
      Math.abs(lastY.min - padded.yMin) > ySpan * 0.005 ||
      Math.abs(lastY.max - padded.yMax) > ySpan * 0.005;
    const xChanged = lastAppliedXMaxRef.current !== count;

    // Lock-target overlay is read off the ref inside the draw hook;
    // update before the batch so the redraw sees the new value.
    lockTargetRef.current = toFinite(frame.lock_target);

    u.batch((uplot: uPlot) => {
      if (visibilityChanged) {
        suppressSeriesEventRef.current = true;
        SERIES_KEYS.forEach((key, idx) => {
          const seriesIdx = idx + 1;
          let show = desiredVisibility[key] && hasDataByKey[key];
          if (show) {
            show = userShowRef.current[seriesIdx] ?? true;
          }
          if (key === 'signal_strength_a_upper' || key === 'signal_strength_a_lower') {
            show = desiredVisibility[key] && hasDataByKey[key] && error1Visible;
          }
          if (key === 'signal_strength_b_upper' || key === 'signal_strength_b_lower') {
            show = desiredVisibility[key] && hasDataByKey[key] && error2Visible;
          }
          uplot.setSeries(seriesIdx, { show }, false);
        });
        suppressSeriesEventRef.current = false;
        lastVisibilityRef.current = { lockAxis, dual, hasDataKey };
      }
      uplot.setData(plotData, false);
      if (xChanged) {
        uplot.setScale('x', { min: 0, max: Math.max(count - 1, 1) });
        lastAppliedXMaxRef.current = count;
      }
      if (yChanged) {
        uplot.setScale('y', { min: padded.yMin, max: padded.yMax });
        lastAppliedYRef.current = { min: padded.yMin, max: padded.yMax };
      }
    });
  };

  // Expose imperative API.
  useImperativeHandle(
    ref,
    () => ({
      applyFrame: applyFrameInternal,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Re-apply latest frame when lockState changes. Visibility set
  // differs between lock and sweep modes, so the displayed series
  // must update even though no new data arrived.
  useEffect(() => {
    if (uplotRef.current && latestFrameRef.current) {
      applyFrameInternal(latestFrameRef.current);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lockState]);

  // Init uPlot exactly once, the first time `initActive` becomes true.
  // No cleanup is returned from this effect on purpose -- see the
  // unmount-only effect below for resource teardown. Putting cleanup
  // here would destroy uPlot when initActive toggles back to false
  // (tab deactivate), and the initializedRef guard would then block
  // re-creation on the next activation.
  useEffect(() => {
    if (initializedRef.current || !initActive) return;
    initializedRef.current = true;
    // setSize is the only DOM-mutating side effect we drive from
    // resize events. Reading layout (clientWidth/getBoundingClientRect)
    // synchronously inside a ResizeObserver callback forces a reflow;
    // we pull the new size off the observer entry instead so the call
    // is layout-read-free.
    const applyContainerWidth = (width: number | null) => {
      if (!uplotRef.current) return;
      if (width != null && width > 10) sizeRef.current.width = width;
      uplotRef.current.setSize({
        width: sizeRef.current.width,
        height: sizeRef.current.height,
      });
    };

    const container = containerRef.current;
    if (!container || uplotRef.current) return;

    // One layout read at init time is unavoidable since the observer
    // hasn't fired yet. After init the observer keeps us up to date
    // without further layout flushes.
    const initialWidth =
      container.clientWidth > 10 ? container.clientWidth : sizeRef.current.width;
    sizeRef.current.width = initialWidth;

    const axisTheme = getAxisTheme();
    const makeStroke = (color: string) => () => color;

    const bandLinks = BAND_CONFIGS.map((band) => ({
      controllerIdx: SERIES_INDEX[band.controller],
      memberIdxs: [SERIES_INDEX[band.upper], SERIES_INDEX[band.lower]],
    }));

    // Force the canvas backing store to a 1:1 pixel ratio even on
    // HiDPI displays. Default behavior multiplies the pixel count 4-9x
    // per canvas, which dominates paint cost across 12 thumbnails. At
    // thumbnail size the loss of sharpness is invisible. uPlot reads
    // `pxRatio` from opts at construction time even though its TS
    // types omit it.
    const opts: uPlot.Options = {
      width: initialWidth,
      height: sizeRef.current.height,
      ...({ pxRatio: 1 } as { pxRatio: number }),
      scales: {
        x: { time: false, auto: false, range: [0, N_POINTS - 1] },
        y: { auto: false },
      },
      cursor: { show: false, drag: { setScale: false }, points: { show: false } },
      legend: { show: false },
      axes: [
        {
          stroke: makeStroke(axisTheme.axis),
          grid: { stroke: makeStroke(axisTheme.grid) },
          ticks: { stroke: makeStroke(axisTheme.tick) },
          values: (_u, ticks) => ticks.map((v) => v.toFixed(0)),
        },
        {
          stroke: makeStroke(axisTheme.axis),
          grid: { stroke: makeStroke(axisTheme.grid) },
          ticks: { stroke: makeStroke(axisTheme.tick) },
        },
      ],
      bands: BAND_CONFIGS.map((band) => ({
        series: [SERIES_INDEX[band.lower], SERIES_INDEX[band.upper]],
        fill: band.fill,
      })),
      series: [
        { label: 'x' },
        ...SERIES_KEYS.map((key) => {
          const style = SERIES_STYLE[key];
          const stroke = style.strokeAlpha ? toRgba(style.color, style.strokeAlpha) : style.color;
          return {
            label: LABELS[key],
            stroke,
            width: key.includes('signal_strength') ? 1 : 1.5,
            points: POINT_STYLE,
            spanGaps: true,
            show: true,
          };
        }),
      ],
      hooks: {
        setSeries: [
          (u, idx) => {
            if (idx == null) return;
            if (suppressSeriesEventRef.current) return;
            userShowRef.current[idx] = u.series[idx].show ?? false;
            const link = bandLinks.find((item) => item.controllerIdx === idx);
            if (!link) return;
            suppressSeriesEventRef.current = true;
            const show = u.series[idx].show ?? false;
            link.memberIdxs.forEach((memberIdx) => {
              u.setSeries(memberIdx, { show }, false);
            });
            suppressSeriesEventRef.current = false;
            // Stored as refs (legend is hidden so these never change
            // in practice, but keep the wiring correct).
            if (idx === SERIES_INDEX.error_signal_1) error1VisibleRef.current = show;
            if (idx === SERIES_INDEX.error_signal_2) error2VisibleRef.current = show;
          },
        ],
        draw: [
          (u) => {
            const lockTarget = lockTargetRef.current;
            if (lockTarget == null) return;
            const xPos = u.valToPos(lockTarget, 'x', true);
            if (!Number.isFinite(xPos)) return;
            const { top, height } = u.bbox;
            const ctx = u.ctx;
            ctx.save();
            // Cached accent -- no getComputedStyle on the per-draw
            // hot path. Refreshed by the scheme observer.
            ctx.strokeStyle = getCachedAccentColor();
            ctx.globalAlpha = 0.75;
            ctx.lineWidth = 1.25;
            ctx.setLineDash([6, 4]);
            ctx.beginPath();
            ctx.moveTo(xPos, top);
            ctx.lineTo(xPos, top + height);
            ctx.stroke();
            ctx.restore();
          },
        ],
      },
    };

    const initialData = [
      getXBuffer(N_POINTS),
      ...seriesBuffersRef.current,
    ] as unknown as PlotData;
    uplotRef.current = new uPlot(opts, initialData, container);
    uplotRef.current.setScale('x', { min: 0, max: N_POINTS - 1 });
    uplotRef.current.setScale('y', { min: -1, max: 1 });
    uplotRef.current.setData(initialData, false);
    applyContainerWidth(initialWidth);

    const applyAxisTheme = () => {
      if (!uplotRef.current) return;
      // Refresh the shared theme cache here (init + scheme change)
      // so the per-draw hot path reads the cache, never getComputedStyle.
      refreshThemeCache();
      const theme = getAxisTheme();
      uplotRef.current.axes.forEach((axis) => {
        axis.stroke = makeStroke(theme.axis);
        axis.grid = { ...(axis.grid || {}), stroke: makeStroke(theme.grid) };
        axis.ticks = { ...(axis.ticks || {}), stroke: makeStroke(theme.tick) };
      });
      uplotRef.current.redraw();
    };
    applyAxisTheme();

    let schemeObserver: MutationObserver | null = null;
    if (typeof document !== 'undefined') {
      schemeObserver = new MutationObserver(() => applyAxisTheme());
      schemeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-mantine-color-scheme'],
      });
    }

    // Read the new width off the ResizeObserverEntry directly --
    // entry.contentBoxSize is the post-layout box dimensions the
    // browser already computed for this notification. Reading
    // container.clientWidth here would force the browser to flush
    // pending layout work (forced reflow), which Lighthouse called
    // out as ~47ms in our hot paths.
    const observer = new ResizeObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (!entry) return;
      const size = entry.contentBoxSize?.[0];
      const width = size
        ? size.inlineSize
        : entry.contentRect?.width;
      applyContainerWidth(typeof width === 'number' ? width : null);
    });
    observer.observe(container);
    // ResizeObserver fires whenever the container's box size
    // changes -- including from window resizes affecting the
    // layout. No separate window resize listener needed.

    observerRef.current = observer;
    schemeObserverRef.current = schemeObserver;
    resizeHandlerRef.current = null;

    // Replay any frame that arrived before uPlot was constructed.
    if (latestFrameRef.current) {
      applyFrameInternal(latestFrameRef.current);
    }
  }, [initActive]);

  // Unmount-only cleanup. Tears down resources captured by the init
  // effect above. Does NOT fire when initActive toggles -- uPlot
  // stays alive across tab visibility changes (Mantine Tabs.Panel
  // keeps panels mounted by default; we keep the plot ready).
  useEffect(() => {
    return () => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      schemeObserverRef.current?.disconnect();
      schemeObserverRef.current = null;
      if (resizeHandlerRef.current) {
        window.removeEventListener('resize', resizeHandlerRef.current);
        resizeHandlerRef.current = null;
      }
      uplotRef.current?.destroy();
      uplotRef.current = null;
    };
  }, []);

  return (
    <div className="panel plot-panel" style={{ padding: 8 }}>
      {!hasData ? (
        <div style={{ color: '#7a6a58', fontSize: 12, marginBottom: 6 }}>
          Waiting for data...
        </div>
      ) : null}
      <div
        ref={containerRef}
        style={{ width: '100%', height: DEFAULT_HEIGHT, position: 'relative' }}
      />
    </div>
  );
});
