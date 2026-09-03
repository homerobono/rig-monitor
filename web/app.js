/* rig-monitor dashboard */
'use strict';

const $ = (sel) => document.querySelector(sel);
const SYNC = uPlot.sync('rig');
const LS = {
  get(k, d) { try { return JSON.parse(localStorage.getItem('rigmon.' + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('rigmon.' + k, JSON.stringify(v)); } catch {} },
};

const state = {
  cfg: null,
  metrics: {},           // key -> metric definition
  rangeId: LS.get('range', '1h'),
  span: 3600,
  window: null,          // {from,to} when zoomed; null means "last span"
  live: LS.get('live', true),
  data: null,
  summary: {},
  bands: [],
  charts: [],
  threshold: 85,
};

/* ------------------------------------------------------------------ charts */
const COL = {
  red: '#f38ba8', maroon: '#eba0ac', peach: '#fab387', yellow: '#f9e2af',
  green: '#a6e3a1', teal: '#94e2d5', sky: '#89dceb', sapphire: '#74c7ec',
  blue: '#89b4fa', lavender: '#b4befe', mauve: '#cba6f7', pink: '#f5c2e7',
  subtext: '#a6adc8', muted: '#7f849c',
};

const CHART_SPECS = [
  {
    id: 'cpu', title: 'CPU — temperature vs fan vs load', height: 250,
    hint: 'solid = °C · dashed = fan % · filled = load %',
    dual: true, threshold: true,
    series: [
      { key: 'cpu.temp', scale: 'c', color: COL.red, width: 2 },
      { key: 'cpu.load', scale: 'p', color: COL.mauve, fill: true },
      { key: 'fan.cpu', scale: 'p', color: COL.teal, dash: [5, 3] },
      { key: 'fan.pump', scale: 'p', color: COL.sky, dash: [5, 3], off: true },
    ],
  },
  {
    id: 'gpu', title: 'GPU — temperature vs fan vs load', height: 250,
    hint: 'solid = °C · dashed = fan % · filled = load %',
    dual: true, threshold: true,
    series: [
      { key: 'gpu.temp_hotspot', scale: 'c', color: COL.mauve, width: 2 },
      { key: 'gpu.temp', scale: 'c', color: COL.blue, width: 2 },
      { key: 'gpu.load', scale: 'p', color: COL.peach, fill: true },
      { key: 'fan.gpu1', scale: 'p', color: COL.teal, dash: [5, 3] },
      { key: 'fan.gpu2', scale: 'p', color: COL.green, dash: [5, 3], off: true },
    ],
  },
  {
    id: 'temps', title: 'All temperatures', height: 270, wide: true,
    hint: '85 °C limit shaded', group: 'temp', scale: 'c', threshold: true,
  },
  {
    id: 'fans', title: 'Fan speeds', height: 230, group: 'fan', scale: 'p',
    hint: 'duty cycle reported by the controller', yrange: [0, 100],
  },
  {
    id: 'load', title: 'Utilisation', height: 230, group: 'load', scale: 'p',
    yrange: [0, 100],
  },
  {
    id: 'power', title: 'Power draw', height: 200, group: 'power', scale: 'w',
  },
  {
    id: 'rpm', title: 'Fan speeds (RPM)', height: 200, group: 'rpm', scale: 'r',
    hint: 'useful for spotting a stalled fan',
  },
];

/* KPI tiles: [key, accent, scaleMax] */
const KPI_SPECS = [
  ['cpu.temp', COL.red, 100],
  ['cpu.load', COL.mauve, 100],
  ['fan.cpu', COL.teal, 100],
  ['cpu.power', COL.maroon, 160],
  ['gpu.temp_hotspot', COL.mauve, 110],
  ['gpu.temp', COL.blue, 100],
  ['gpu.load', COL.peach, 100],
  ['fan.gpu1', COL.green, 100],
  ['@sysfans', COL.sky, 100],
];
const SYS_FAN_KEYS = ['fan.sys1', 'fan.sys2', 'fan.sys3', 'fan.sys4'];

/* Hidden from every chart, legend, and KPI tile. */
const DISMISSED = new Set([
  'gpu.power',
  'cpu.temp_ccd1', 'board.temp_vrm',
  'gpu.temp_vram', 'gpu.vram_load',
  'board.temp_socket', 'board.temp_sys', 'board.temp_chipset',
  'fan.sys5', 'fan.sys6', 'fan.ezconnect',
  'rpm.sys5', 'rpm.sys6', 'rpm.ezconnect',
  'gpu.load_memctl',
]);

/* ---------------------------------------------------------------- plumbing */
async function api(path, params) {
  const url = new URL(path, location.href);
  Object.entries(params || {}).forEach(([k, v]) => url.searchParams.set(k, v));
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

let toastTimer = null;
function toast(msg) {
  const el = $('#toast');
  if (!msg) { el.hidden = true; return; }
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

const fmtNum = (v, unit) => {
  if (v == null || Number.isNaN(v)) return '—';
  if (unit === 'RPM' || unit === 'MHz' || unit === 'FPS') return Math.round(v).toLocaleString();
  if (unit === '%') return v.toFixed(0);
  if (unit === 'W') return v.toFixed(0);
  return v.toFixed(1);
};

function fmtDuration(sec) {
  if (!sec) return '0s';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.round((sec % 3600) / 60)}m`;
}

const fmtClock = (ts) =>
  new Date(ts * 1000).toLocaleString([], { month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit' });

/* --------------------------------------------------------------- plugins */
function gradientFill(color) {
  let cache = null;
  return (u) => {
    const { top, height } = u.bbox;
    if (!cache || cache.h !== height) {
      const g = u.ctx.createLinearGradient(0, top, 0, top + height);
      g.addColorStop(0, color + '57'); // 50% more opaque than 3a
      g.addColorStop(1, color + '00');
      cache = { h: height, g };
    }
    return cache.g;
  };
}

function overlayPlugin(spec) {
  return {
    hooks: {
      drawClear: (u) => {
        const { ctx } = u;
        const { left, top, width, height } = u.bbox;
        ctx.save();
        ctx.beginPath();
        ctx.rect(left, top, width, height);
        ctx.clip();

        // Vertical bands wherever any temperature exceeded the limit.
        if (state.bands.length) {
          ctx.fillStyle = 'rgba(243,139,168,0.11)';
          for (const [a, b] of state.bands) {
            const x0 = u.valToPos(a, 'x', true);
            const x1 = u.valToPos(b, 'x', true);
            if (x1 < left || x0 > left + width) continue;
            ctx.fillRect(x0, top, Math.max(1.5, x1 - x0), height);
          }
        }

        // Horizontal limit line, with the danger zone tinted above it.
        if (spec.threshold && u.scales.c && u.scales.c.min != null) {
          const y = u.valToPos(state.threshold, 'c', true);
          if (y > top && y < top + height) {
            ctx.fillStyle = 'rgba(243,139,168,0.055)';
            ctx.fillRect(left, top, width, y - top);
          }
          if (y >= top - 2 && y <= top + height + 2) {
            ctx.save();
            ctx.setLineDash([5, 4]);
            ctx.strokeStyle = 'rgba(243,139,168,0.55)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(left, Math.round(y) + 0.5);
            ctx.lineTo(left + width, Math.round(y) + 0.5);
            ctx.stroke();
            ctx.restore();
            ctx.fillStyle = 'rgba(243,139,168,0.8)';
            ctx.font = '10px ui-monospace, monospace';
            ctx.textAlign = 'right';
            ctx.fillText(`${state.threshold}°C`, left + width - 4, y - 4);
          }
        }
        ctx.restore();
      },
    },
  };
}

/* ------------------------------------------------------------ chart build */
const AXIS_STYLE = {
  stroke: COL.muted,
  grid: { stroke: 'rgba(69,71,90,0.45)', width: 1 },
  ticks: { stroke: 'rgba(69,71,90,0.6)', width: 1, size: 4 },
  font: '11px ui-sans-serif, system-ui, sans-serif',
};

const UNIT_OF_SCALE = { c: '°C', p: '%', w: 'W', r: 'RPM', m: 'MHz' };

function seriesForSpec(spec) {
  if (spec.series) {
    return spec.series
      .filter((s) => state.metrics[s.key] && state.metrics[s.key].available && !DISMISSED.has(s.key))
      .map((s) => ({ ...s, meta: state.metrics[s.key] }));
  }
  return Object.values(state.metrics)
    .filter((m) => m.group === spec.group && m.available && !DISMISSED.has(m.key))
    .map((m) => ({
      key: m.key, scale: spec.scale, color: m.color, meta: m,
      off: !m.default_on, width: 1.5,
    }));
}

function buildChart(spec) {
  const list = seriesForSpec(spec);
  if (!list.length) return null;

  const card = document.createElement('section');
  card.className = 'card' + (spec.wide ? ' wide' : '');
  card.innerHTML = `<div class="card-head"><h2>${spec.title}</h2>
      <span class="hint" data-hint>${spec.hint || ''}</span></div>
      <div class="plot"></div>
      <div class="legend" data-legend></div>`;
  $('#charts').appendChild(card);
  const host = card.querySelector('.plot');

  const shown = LS.get('series.' + spec.id, {});
  const series = [{}];
  const scales = { x: { time: true } };
  const axes = [{ ...AXIS_STYLE, space: 70 }];

  const usedScales = [...new Set(list.map((s) => s.scale))];
  usedScales.forEach((sc, i) => {
    const unit = UNIT_OF_SCALE[sc] || '';
    // Fixed-ish y ranges keep the eye calibrated when comparing one window to another,
    // and guarantee the 85 °C line stays on screen.
    scales[sc] = {
      c: { range: (u, min, max) => [Math.min(30, min - 3), Math.max(92, max + 3)] },
      p: { range: spec.yrange || [0, 100] },
      w: { range: (u, min, max) => [0, Math.max(50, max * 1.12)] },
      r: { range: (u, min, max) => [0, Math.max(600, max * 1.12)] },
    }[sc] || {};
    axes.push({
      ...AXIS_STYLE,
      scale: sc,
      side: i === 0 ? 3 : 1,
      grid: { show: i === 0, ...AXIS_STYLE.grid },
      size: 42,
      values: (u, ticks) => ticks.map((v) => `${fmtNum(v, unit)}`),
      label: usedScales.length > 1 ? unit : undefined,
      labelSize: usedScales.length > 1 ? 16 : 0,
      labelFont: '10px ui-sans-serif, system-ui, sans-serif',
    });
  });

  for (const s of list) {
    const unit = s.meta.unit;
    series.push({
      label: s.meta.short || s.meta.label,
      scale: s.scale,
      stroke: s.color,
      width: s.width || 1.5,
      dash: s.dash,
      fill: s.fill ? gradientFill(s.color) : undefined,
      spanGaps: false,
      points: { show: false },
      show: shown[s.key] !== undefined ? shown[s.key] : !s.off,
      value: (u, v) => (v == null ? '—' : `${fmtNum(v, unit)}${unit === '%' ? '%' : ' ' + unit}`),
      _key: s.key,
    });
  }

  let chart = null;
  const u = new uPlot({
    width: host.clientWidth || 600,
    height: spec.height,
    padding: [10, 6, 0, 0],
    cursor: {
      sync: { key: SYNC.key, scales: ['x', null] },
      points: { size: 6 },
      focus: { prox: 24 },
    },
    focus: { alpha: 0.35 },
    legend: { show: false },
    scales, axes, series,
    plugins: [overlayPlugin(spec)],
    hooks: {
      setSelect: [(self) => {
        if (self.select.width < 8) return;
        const from = Math.round(self.posToVal(self.select.left, 'x'));
        const to = Math.round(self.posToVal(self.select.left + self.select.width, 'x'));
        self.setSelect({ width: 0, height: 0 }, false);
        if (to - from >= 30) zoomTo(from, to);
      }],
      setCursor: [() => chart && chart.syncLegend()],
      setData: [() => chart && chart.syncLegend()],
    },
  }, [[]].concat(list.map(() => [])), host);

  new ResizeObserver(() => u.setSize({ width: host.clientWidth, height: spec.height }))
    .observe(host);

  chart = { spec, u, keys: list.map((s) => s.key), card, list };
  attachLegend(chart);
  return chart;
}

/* uPlot's own legend is a table that eats vertical space and reads "--" whenever the
   pointer is elsewhere. This one is a single wrapped row that falls back to the latest
   sample, and doubles as the series on/off control. */
function attachLegend(chart) {
  const { u, list, card, spec } = chart;
  const host = card.querySelector('[data-legend]');
  const swatch = (s) => (s.dash
    ? `repeating-linear-gradient(90deg, ${s.color} 0 3px, transparent 3px 6px)`
    : s.color);
  host.innerHTML = list.map((s, i) => `
    <button class="lg" data-i="${i + 1}" title="${s.meta.label}"
            aria-pressed="${u.series[i + 1].show}">
      <i style="background:${swatch(s)}"></i>
      <span class="lg-name">${s.meta.short || s.meta.label}</span>
      <span class="lg-val" data-v>—</span>
    </button>`).join('');

  host.onclick = (e) => {
    const btn = e.target.closest('.lg');
    if (!btn) return;
    const i = Number(btn.dataset.i);
    const show = !u.series[i].show;
    u.setSeries(i, { show });
    btn.setAttribute('aria-pressed', String(show));
    const map = LS.get('series.' + spec.id, {});
    map[u.series[i]._key] = show;
    LS.set('series.' + spec.id, map);
  };

  const nodes = [...host.querySelectorAll('.lg')];
  chart.syncLegend = () => {
    const idx = u.cursor.idx;
    for (let i = 0; i < list.length; i++) {
      const data = u.data[i + 1] || [];
      let v = idx != null ? data[idx] : null;
      if (v == null) {
        for (let j = data.length - 1; j >= 0 && v == null; j--) v = data[j];
      }
      const unit = list[i].meta.unit;
      nodes[i].querySelector('[data-v]').textContent =
        v == null ? '—' : `${fmtNum(v, unit)}${unit === '%' ? '%' : ' ' + unit}`;
    }
  };
}

function rebuildCharts() {
  $('#charts').innerHTML = '';
  state.charts = CHART_SPECS.map(buildChart).filter(Boolean);
}

/* ------------------------------------------------------------------- data */
function neededKeys() {
  const keys = new Set();
  for (const c of state.charts) c.keys.forEach((k) => keys.add(k));
  KPI_SPECS.forEach(([k]) => { if (!k.startsWith('@')) keys.add(k); });
  SYS_FAN_KEYS.forEach((k) => keys.add(k));
  return [...keys].filter((k) => state.metrics[k] && state.metrics[k].available && !DISMISSED.has(k));
}

function currentWindow() {
  if (state.window) return state.window;
  const now = Math.floor(Date.now() / 1000);
  return { from: now - state.span, to: now };
}

function pickArray(key) {
  const s = state.data && state.data.series[key];
  if (!s) return null;
  // Temperatures are drawn as the peak of each bucket so a spike over 85 °C is never
  // averaged away; everything else reads better as the bucket mean.
  const meta = state.metrics[key];
  const arr = meta && meta.group === 'temp' ? (s.max || s.avg) : (s.avg || s.max);
  return arr || null;
}

function computeBands() {
  const d = state.data;
  if (!d) return [];
  const tempKeys = Object.keys(state.metrics).filter((k) => state.metrics[k].group === 'temp');
  const n = d.t.length;
  const step = d.bucket;
  const bands = [];
  let open = null;
  for (let i = 0; i < n; i++) {
    let hot = false;
    for (const k of tempKeys) {
      const s = d.series[k];
      const v = s && s.max ? s.max[i] : null;
      if (v != null && v >= state.threshold) { hot = true; break; }
    }
    if (hot) {
      if (open == null) open = d.t[i];
    } else if (open != null) {
      bands.push([open, d.t[i]]);
      open = null;
    }
  }
  if (open != null) bands.push([open, d.t[n - 1] + step]);

  // Merge bands that are only a bucket or two apart, otherwise a flapping sensor
  // turns the plot into a picket fence.
  const merged = [];
  for (const b of bands) {
    const prev = merged[merged.length - 1];
    if (prev && b[0] - prev[1] <= step * 2) prev[1] = b[1];
    else merged.push([...b]);
  }
  return merged;
}

async function loadSeries() {
  const keys = neededKeys();
  if (!keys.length) return;
  const w = currentWindow();
  const data = await api('/api/series', {
    keys: keys.join(','), from: w.from, to: w.to, points: 1100, aggs: 'avg,max',
  });
  state.data = data;
  state.bands = computeBands();

  for (const c of state.charts) {
    const cols = [data.t];
    for (const k of c.keys) {
      const arr = pickArray(k);
      cols.push(arr && arr.length === data.t.length ? arr : new Array(data.t.length).fill(null));
    }
    c.u.setData(cols, true);
    const hint = c.card.querySelector('[data-hint]');
    if (c.spec.group === 'temp' && hint) {
      hint.textContent = data.bucket > 10
        ? `85 °C limit shaded · peak per ${fmtDuration(data.bucket)}`
        : '85 °C limit shaded';
    }
  }

  $('#footBucket').textContent =
    `${data.t.length} points · ${fmtDuration(data.bucket)} per point · ${data.source}`;
}

async function loadSummary() {
  const keys = neededKeys();
  if (!keys.length) return;
  const w = currentWindow();
  const res = await api('/api/summary', {
    keys: keys.join(','), from: w.from, to: w.to, threshold: state.threshold,
  });
  state.summary = res.metrics || {};
  renderAlerts();
}

/* --------------------------------------------------------------- rendering */
function renderKpis(values) {
  const host = $('#kpis');
  if (!host.children.length) {
    host.innerHTML = KPI_SPECS.map(([key, accent]) => {
      const m = key === '@sysfans'
        ? { label: 'System fans', unit: '%' }
        : state.metrics[key];
      if (!m) return '';
      return `<article class="kpi" data-key="${key}" style="--accent:${accent}">
        <div class="k-label">${m.label}</div>
        <div class="k-value"><span data-v>—</span><span class="u">${m.unit === '%' ? '%' : ' ' + m.unit}</span></div>
        <div class="k-peak" data-peak></div>
        <div class="k-bar"><i style="width:0"></i></div>
      </article>`;
    }).join('');
  }

  for (const [key, , scaleMax] of KPI_SPECS) {
    const el = host.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (!el) continue;
    const meta = key === '@sysfans' ? { unit: '%', group: 'fan' } : state.metrics[key];
    if (!meta) continue;

    let value = null;
    let peak = null;
    if (key === '@sysfans') {
      const live = SYS_FAN_KEYS.map((k) => values[k] && values[k].value).filter((v) => v != null);
      if (live.length) value = live.reduce((a, b) => a + b, 0) / live.length;
      const peaks = SYS_FAN_KEYS.map((k) => state.summary[k] && state.summary[k].max)
        .filter((v) => v != null);
      if (peaks.length) peak = Math.max(...peaks);
    } else {
      value = values[key] ? values[key].value : null;
      peak = state.summary[key] ? state.summary[key].max : null;
    }

    el.querySelector('[data-v]').textContent = fmtNum(value, meta.unit);
    el.querySelector('.k-bar i').style.width =
      value == null ? '0' : `${Math.min(100, (value / scaleMax) * 100)}%`;
    el.querySelector('[data-peak]').innerHTML =
      peak == null ? '' : `peak <b>${fmtNum(peak, meta.unit)}${meta.unit === '%' ? '%' : ' ' + meta.unit}</b>`;

    el.classList.toggle('hot', meta.group === 'temp' && value != null && value >= state.threshold);
    el.classList.toggle('warm', meta.group === 'temp' && value != null &&
      value >= state.threshold - 10 && value < state.threshold);
  }
}

function renderAlerts() {
  const strip = $('#alertStrip');
  const hot = Object.entries(state.summary)
    .filter(([k, s]) => state.metrics[k] && state.metrics[k].group === 'temp' && s.seconds_above > 0)
    .sort((a, b) => b[1].seconds_above - a[1].seconds_above);

  strip.hidden = false;
  if (!hot.length) {
    const peak = Object.entries(state.summary)
      .filter(([k]) => state.metrics[k] && state.metrics[k].group === 'temp')
      .reduce((best, [k, s]) => (!best || s.max > best.v ? { k, v: s.max } : best), null);
    strip.className = 'alertstrip calm';
    strip.innerHTML = `<span class="title">✓ Nothing crossed ${state.threshold} °C</span>` +
      (peak ? `<span class="chip">hottest <b style="color:var(--green)">${peak.v.toFixed(1)} °C</b>
        <span>${state.metrics[peak.k].label}</span></span>` : '');
    return;
  }

  strip.className = 'alertstrip hot';
  strip.innerHTML = `<span class="title">⚠ ${hot.length} sensor${hot.length > 1 ? 's' : ''} above ${state.threshold} °C</span>` +
    hot.map(([k, s]) => `<span class="chip" title="${s.episode_count} episode(s), last peak ${fmtClock(s.peak_ts)}">
        ${state.metrics[k].label}
        <b>${s.max.toFixed(1)} °C</b>
        <span>for ${fmtDuration(s.seconds_above)}${s.episode_count > 1 ? ` · ${s.episode_count}×` : ''}</span>
      </span>`).join('');
}

function renderStatus(st) {
  const pill = $('#liveToggle');
  const age = st.last_ok ? st.server_time - st.last_ok : null;
  const stale = !st.ok || (age != null && age > st.poll_interval * 4);
  pill.classList.toggle('stale', stale && state.live);
  $('#liveLabel').textContent = !state.live ? 'Paused'
    : stale ? 'No data' : 'Live';
  $('#sourceLine').textContent = st.ok
    ? `${st.bound_metrics} metrics · every ${st.poll_interval}s · ${location.host}`
    : `source unreachable — ${st.last_error || 'unknown error'}`;
  if (!st.ok && st.last_error) toast(st.last_error);

  const cov = st.coverage || {};
  $('#footSpan').textContent = cov.first
    ? `history ${fmtClock(cov.first)} → ${fmtClock(cov.last)}`
    : 'no history yet';
  $('#footDb').textContent = st.db_bytes ? `${(st.db_bytes / 1e6).toFixed(1)} MB on disk` : '';
}

/* ---------------------------------------------------------------- controls */
function renderRanges() {
  const host = $('#ranges');
  host.innerHTML = state.cfg.ranges
    .map((r) => `<button data-range="${r.id}" data-seconds="${r.seconds}"
        aria-pressed="${r.id === state.rangeId}">${r.label}</button>`)
    .join('');
  host.onclick = (e) => {
    const btn = e.target.closest('button[data-range]');
    if (!btn) return;
    setRange(btn.dataset.range, Number(btn.dataset.seconds));
  };
}

function setRange(id, seconds) {
  state.rangeId = id;
  state.span = seconds;
  state.window = null;
  LS.set('range', id);
  history.replaceState(null, '', `?range=${id}`);
  document.querySelectorAll('#ranges button')
    .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.range === id)));
  scheduleLoops();
  refresh(true);
}

function zoomTo(from, to) {
  state.window = { from, to };
  state.live = false;
  $('#liveToggle').setAttribute('aria-pressed', 'false');
  document.querySelectorAll('#ranges button').forEach((b) => b.setAttribute('aria-pressed', 'false'));
  refresh(true);
}

/* -------------------------------------------------------------- main loop */
let summaryTimer = null;

let liveTimer = null;

/* Redrawing a 12-hour chart every second would be pure waste - one bucket is 40 s wide,
   so nothing changes. Redraw as often as a new point can appear instead, and keep the
   readouts at 1 Hz regardless of range. */
function seriesInterval() {
  const bucket = (state.data && state.data.bucket) || state.cfg.poll_interval;
  return Math.min(15000, Math.max(1000, bucket * 1000));
}

async function refreshValues() {
  const [latest, status] = await Promise.all([api('/api/latest'), api('/api/status')]);
  renderKpis(latest.values || {});
  renderStatus(status);
}

async function refresh(withSummary) {
  try {
    await loadSeries();
    if (withSummary) await loadSummary();
    await refreshValues();
    toast(null);
  } catch (e) {
    toast(String(e.message || e));
  }
}

function scheduleLoops() {
  clearInterval(liveTimer);
  clearInterval(summaryTimer);
  let sinceSeries = 0;
  let sinceSummary = 0;

  liveTimer = setInterval(() => {
    if (!state.live || document.hidden) return;
    sinceSeries += 1000;
    sinceSummary += 1000;
    refreshValues().catch(() => {});
    if (sinceSeries >= seriesInterval()) {
      sinceSeries = 0;
      loadSeries().catch(() => {});
    }
    if (sinceSummary >= 15000) {
      sinceSummary = 0;
      loadSummary().catch(() => {});
    }
  }, 1000);
}

async function boot() {
  let cfg;
  for (let attempt = 0; ; attempt++) {
    cfg = await api('/api/config');
    if (cfg.metrics.some((m) => m.available) || attempt >= 3) break;
    $('#charts').innerHTML = '<section class="card wide"><div class="skeleton"></div></section>';
    await new Promise((r) => setTimeout(r, 3000));
  }
  state.cfg = cfg;
  state.threshold = cfg.temp_alert_c;
  cfg.metrics.forEach((m) => { state.metrics[m.key] = m; });

  // ?range=24h makes a view bookmarkable, which is handy on a phone.
  const wanted = new URLSearchParams(location.search).get('range') || state.rangeId;
  const chosen = cfg.ranges.find((r) => r.id === wanted)
    || cfg.ranges.find((r) => r.id === state.rangeId)
    || cfg.ranges[2];
  state.rangeId = chosen.id;
  state.span = chosen.seconds;

  renderRanges();
  rebuildCharts();

  $('#liveToggle').setAttribute('aria-pressed', String(state.live));
  $('#liveToggle').onclick = () => {
    state.live = !state.live;
    if (state.live) state.window = null;
    LS.set('live', state.live);
    $('#liveToggle').setAttribute('aria-pressed', String(state.live));
    if (state.live) {
      document.querySelectorAll('#ranges button')
        .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.range === state.rangeId)));
      refresh(true);
    }
  };

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.live) refresh(true).catch(() => {});
  });

  await refresh(true);
  scheduleLoops();
}

boot().catch((e) => {
  toast('failed to start: ' + e.message);
  console.error(e);
});
