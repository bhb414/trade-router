(() => {
  "use strict";

  const STORAGE_KEY = "trade-router.chart-settings.v1";
  const ATR_WARMUP_BARS = 100;
  const REFRESH_INTERVAL_MS = 10_000;
  const STREAM_STALE_MS = 15_000;
  const palette = ["#2f67d8", "#e59028", "#7c4dba", "#16856f", "#d24b5f", "#5c6f82"];
  const state = {
    code: "",
    name: "",
    bars: [],
    quote: null,
    source: null,
    sources: [],
    socket: null,
    fallbackTimer: null,
    refreshInFlight: null,
    lastStreamAt: 0,
    lastStreamQuote: null,
    lastRefreshAt: 0,
    lastBarsRefreshAt: 0,
    lastConnectAt: 0,
    refreshFailed: false,
    refreshError: "",
    searchTimer: null,
    searchRequest: 0,
    loadGeneration: 0,
    loadedGeneration: null,
    searchIndex: -1,
    resizeObserver: null,
    manualClose: true,
    viewCount: null,
    viewEnd: null,
    geometry: null,
    crosshair: null,
    drag: null,
    historyLoading: false,
    historyLoadingGeneration: null,
    noMoreHistory: false,
    historyRetryAt: 0,
    indicators: [
      { id: crypto.randomUUID(), type: "BOLLINGER", placement: "SERIES", period: 20, deviation: 2, priceSource: "RAW", color: "#2f67d8" },
      { id: crypto.randomUUID(), type: "VOLUME", placement: "PANEL", period: 20, color: "#7c8798" },
    ],
  };

  const $ = selector => document.querySelector(selector);
  const el = {};

  async function request(path, timeoutMs = 0) {
    const controller = timeoutMs ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store", ...(controller ? { signal: controller.signal } : {}) });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail;
        throw new Error(detail?.message || detail || `HTTP ${response.status}`);
      }
      return payload;
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function formatPrice(value) {
    return number(value).toLocaleString("ko-KR", { maximumFractionDigits: 2 });
  }

  function setLiveState(text, kind = "idle") {
    el.live.textContent = text;
    el.live.className = `chart-live-state ${kind}`;
  }

  function saveSettings() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      source: el.source.value,
      venue: el.venue.value,
      interval: el.interval.value,
      chartType: el.type.value,
      indicators: state.indicators,
    }));
  }

  function restoreSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      if (saved.venue) el.venue.value = saved.venue;
      if (saved.interval) el.interval.value = saved.interval;
      if (saved.chartType) el.type.value = saved.chartType;
      if (Array.isArray(saved.indicators) && saved.indicators.length <= 8) {
        const interval = saved.interval || el.interval.value || "3m";
        state.indicators = saved.indicators.map((item, index) => normalizeIndicator({
          id: item.id || crypto.randomUUID(),
          type: item.type || "SMA",
          placement: item.placement === "PANEL" ? "PANEL" : "SERIES",
          period: item.type === "ATR"
            ? clampAtrPeriod(item.period, interval)
            : Math.max(2, Number(item.period) || 20),
          deviation: Math.max(.1, Number(item.deviation) || 2),
          fast: Math.max(2, Number(item.fast) || 12),
          slow: Math.max(3, Number(item.slow) || 26),
          signal: Math.max(2, Number(item.signal) || 9),
          color: item.color || palette[index % palette.length],
          priceSource: item.priceSource || "CHART",
        }));
      }
      return saved.source || "AUTO";
    } catch (_) {
      return "AUTO";
    }
  }

  function barNumbers(row) {
    return {
      ...row,
      open: number(row.open), high: number(row.high), low: number(row.low),
      close: number(row.close), volume: number(row.volume),
    };
  }

  function heikinAshi(rows) {
    let previousOpen = null;
    let previousClose = null;
    return rows.map(row => {
      const close = (row.open + row.high + row.low + row.close) / 4;
      const open = previousOpen == null ? (row.open + row.close) / 2 : (previousOpen + previousClose) / 2;
      const value = { ...row, open, close, high: Math.max(row.high, open, close), low: Math.min(row.low, open, close) };
      previousOpen = open;
      previousClose = close;
      return value;
    });
  }

  function sma(values, period) {
    const result = Array(values.length).fill(null);
    let sum = 0;
    for (let index = 0; index < values.length; index += 1) {
      sum += values[index];
      if (index >= period) sum -= values[index - period];
      if (index >= period - 1) result[index] = sum / period;
    }
    return result;
  }

  function ema(values, period) {
    const result = Array(values.length).fill(null);
    if (!values.length) return result;
    const multiplier = 2 / (period + 1);
    let current = values[0];
    result[0] = current;
    for (let index = 1; index < values.length; index += 1) {
      current = values[index] * multiplier + current * (1 - multiplier);
      result[index] = current;
    }
    return result;
  }

  function sourceMaxHistory(interval) {
    if (!String(interval).endsWith("m")) return 400;
    const minutes = Math.max(1, Number.parseInt(String(interval), 10) || 1);
    return Math.floor(1440 / minutes);
  }

  function atrMaxPeriod(interval = el.interval?.value) {
    return Math.max(1, Math.min(400, sourceMaxHistory(interval) - 1));
  }

  function clampAtrPeriod(value, interval = el.interval?.value) {
    const maximum = atrMaxPeriod(interval);
    return Math.min(maximum, Math.max(1, Math.floor(Number(value) || Math.min(14, maximum))));
  }

  function clampAtrIndicators(interval = el.interval?.value) {
    state.indicators.forEach(indicator => {
      if (indicator.type === "ATR") indicator.period = clampAtrPeriod(indicator.period, interval);
    });
  }

  function requiredAtrHistory(interval = el.interval?.value) {
    const periods = state.indicators
      .filter(indicator => indicator.type === "ATR")
      .map(indicator => clampAtrPeriod(indicator.period, interval));
    if (!periods.length) return 0;
    return Math.min(sourceMaxHistory(interval), Math.max(...periods) + 1 + ATR_WARMUP_BARS);
  }

  function trueRangeAt(rows, index) {
    const row = rows[index];
    const previousClose = rows[index - 1].close;
    return Math.max(
      row.high - row.low,
      Math.abs(row.high - previousClose),
      Math.abs(row.low - previousClose),
    );
  }

  function wilderAtr(rows, period, maxHistory) {
    const values = Array(rows.length).fill(null);
    const historyLimit = Math.max(1, Math.floor(Number(maxHistory) || rows.length));
    const length = Math.min(
      Math.max(1, Math.min(400, historyLimit - 1)),
      Math.max(1, Math.floor(Number(period) || 14)),
    );
    if (length + 1 > historyLimit || rows.length < length + 1) return values;
    const warmup = Math.min(ATR_WARMUP_BARS, historyLimit - length - 1);
    const windowSize = length + 1 + warmup;
    const trueRanges = Array(rows.length).fill(null);
    for (let index = 1; index < rows.length; index += 1) {
      trueRanges[index] = trueRangeAt(rows, index);
    }
    for (let target = length; target < rows.length; target += 1) {
      // A target uses one primer close, `length` seed true ranges and at
      // most 100 later true ranges. Early chart points grow toward that
      // fixed window; once full, older bars cannot change the result.
      const start = Math.max(0, target - windowSize + 1);
      if (target - start < length) continue;
      let current = 0;
      for (let index = start + 1; index <= start + length; index += 1) {
        current += trueRanges[index];
      }
      current /= length;
      for (let index = start + length + 1; index <= target; index += 1) {
        current = (current * (length - 1) + trueRanges[index]) / length;
      }
      values[target] = current;
    }
    return values;
  }

  function indicatorSeries(indicator, rows, rawRows = rows) {
    indicator = normalizeIndicator({ ...indicator });
    if (indicator.priceSource === "RAW") rows = rawRows;
    else if (indicator.priceSource === "HEIKIN_ASHI") rows = heikinAshi(rawRows);
    const closes = rows.map(row => row.close);
    if (indicator.type === "SMA") return [{ name: `SMA ${indicator.period}`, values: sma(closes, indicator.period), color: indicator.color }];
    if (indicator.type === "EMA") return [{ name: `EMA ${indicator.period}`, values: ema(closes, indicator.period), color: indicator.color }];
    if (indicator.type === "BOLLINGER") {
      const middle = sma(closes, indicator.period);
      const upper = Array(rows.length).fill(null);
      const lower = Array(rows.length).fill(null);
      for (let index = indicator.period - 1; index < rows.length; index += 1) {
        const window = closes.slice(index - indicator.period + 1, index + 1);
        const mean = middle[index];
        const variance = window.reduce((total, value) => total + (value - mean) ** 2, 0) / indicator.period;
        const width = Math.sqrt(variance) * indicator.deviation;
        upper[index] = mean + width;
        lower[index] = mean - width;
      }
      return [
        { name: `BB 상단 ${indicator.period}, ${indicator.deviation}`, values: upper, color: indicator.color },
        { name: "BB 중심", values: middle, color: `${indicator.color}99` },
        { name: "BB 하단", values: lower, color: indicator.color },
      ];
    }
    if (indicator.type === "VOLUME") return [{ name: "거래량", values: rows.map(row => row.volume), color: indicator.color, bars: true }];
    if (indicator.type === "OBV") {
      // Standard OBV always follows the actual close and volume. Keep it
      // stable when only the visual candle type changes to Heikin-Ashi.
      const obvCloses = rawRows.map(row => row.close);
      const values = Array(rawRows.length).fill(0);
      for (let index = 1; index < rawRows.length; index += 1) {
        const direction = obvCloses[index] > obvCloses[index - 1] ? 1 : obvCloses[index] < obvCloses[index - 1] ? -1 : 0;
        values[index] = values[index - 1] + direction * rawRows[index].volume;
      }
      return [{ name: "OBV", values, color: indicator.color }];
    }
    if (indicator.type === "ATR") {
      // ATR is a volatility measure of the actual traded OHLC. It must not
      // change when the chart is displayed with transformed Heikin-Ashi bars.
      const maxHistory = sourceMaxHistory(el.interval?.value);
      return [{ name: `ATR ${indicator.period} · 진행봉 포함`, values: wilderAtr(rawRows, indicator.period, maxHistory), color: indicator.color }];
    }
    if (indicator.type === "RSI") {
      const values = Array(rows.length).fill(null);
      let gains = 0;
      let losses = 0;
      for (let index = 1; index < rows.length; index += 1) {
        const change = closes[index] - closes[index - 1];
        gains += Math.max(change, 0);
        losses += Math.max(-change, 0);
        if (index > indicator.period) {
          const old = closes[index - indicator.period] - closes[index - indicator.period - 1];
          gains -= Math.max(old, 0);
          losses -= Math.max(-old, 0);
        }
        if (index >= indicator.period) values[index] = losses === 0 ? 100 : 100 - 100 / (1 + gains / losses);
      }
      return [{ name: `RSI ${indicator.period}`, values, color: indicator.color, fixedRange: [0, 100] }];
    }
    const fast = ema(closes, indicator.fast);
    const slow = ema(closes, indicator.slow);
    const macd = closes.map((_, index) => fast[index] - slow[index]);
    const signal = ema(macd, indicator.signal);
    return [
      { name: `MACD ${indicator.fast},${indicator.slow}`, values: macd, color: indicator.color },
      { name: `Signal ${indicator.signal}`, values: signal, color: "#e59028" },
      { name: "Histogram", values: macd.map((value, index) => value - signal[index]), color: "#8a94a4", bars: true, baseline: 0 },
    ];
  }

  function line(ctx, values, start, end, xAt, yAt, color, width = 1.25) {
    ctx.beginPath();
    let drawing = false;
    for (let index = start; index < end; index += 1) {
      const value = values[index];
      if (value == null || !Number.isFinite(value)) { drawing = false; continue; }
      const x = xAt(index);
      const y = yAt(value);
      if (!drawing) { ctx.moveTo(x, y); drawing = true; } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  }

  function drawSeriesPanel(ctx, series, panel, start, end, xAt, label) {
    const visible = series.flatMap(item => item.values.slice(start, end)).filter(Number.isFinite);
    if (!visible.length) return;
    const fixed = series.find(item => item.fixedRange)?.fixedRange;
    let minimum = fixed ? fixed[0] : Math.min(...visible);
    let maximum = fixed ? fixed[1] : Math.max(...visible);
    if (minimum === maximum) { minimum -= 1; maximum += 1; }
    const margin = fixed ? 0 : (maximum - minimum) * .08;
    minimum -= margin; maximum += margin;
    const yAt = value => panel.y + panel.h - 18 - (value - minimum) / (maximum - minimum) * (panel.h - 34);
    ctx.strokeStyle = "#edf0f4";
    ctx.lineWidth = 1;
    for (let index = 0; index <= 2; index += 1) {
      const y = panel.y + 16 + (panel.h - 34) * index / 2;
      ctx.beginPath(); ctx.moveTo(panel.x, y); ctx.lineTo(panel.x + panel.w, y); ctx.stroke();
    }
    series.forEach(item => {
      if (item.bars) {
        const baseline = yAt(item.baseline ?? Math.max(0, minimum));
        const width = Math.max(1, panel.w / (end - start) * .58);
        item.values.slice(start, end).forEach((value, offset) => {
          if (!Number.isFinite(value)) return;
          const x = xAt(start + offset) - width / 2;
          const y = yAt(value);
          ctx.fillStyle = value >= (item.baseline ?? 0) ? `${item.color}99` : "#d95d6a99";
          ctx.fillRect(x, Math.min(y, baseline), width, Math.max(1, Math.abs(baseline - y)));
        });
      } else line(ctx, item.values, start, end, xAt, yAt, item.color);
    });
    ctx.fillStyle = "#667085";
    ctx.font = "10px system-ui";
    ctx.fillText(label, panel.x + 5, panel.y + 12);
  }

  function formatBarTime(value, detailed = false) {
    const time = new Date(value);
    if (el.interval.value.endsWith("m")) {
      return time.toLocaleString("ko-KR", {
        timeZone: "Asia/Seoul",
        month: detailed ? "2-digit" : undefined,
        day: detailed ? "2-digit" : undefined,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
    }
    return time.toLocaleDateString("ko-KR", {
      timeZone: "Asia/Seoul",
      year: detailed ? "2-digit" : undefined,
      month: "2-digit",
      day: "2-digit",
    });
  }

  function barDateKey(value) {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    }).format(new Date(value));
  }

  function formatAxisBarTime(value, showDate) {
    if (!el.interval.value.endsWith("m")) return formatBarTime(value);
    const time = new Date(value);
    const clock = time.toLocaleTimeString("ko-KR", {
      timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    if (!showDate) return clock;
    const date = time.toLocaleDateString("ko-KR", {
      timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit",
    }).replace(/\s/g, "").replace(/\.$/, "");
    return `${date} ${clock}`;
  }

  function drawCrosshair(ctx, rows, geometry) {
    if (!state.crosshair || state.drag) return;
    const { pricePanel, start, end, xAt, yAt, minimum, maximum, bounds } = geometry;
    const index = Math.max(start, Math.min(end - 1, state.crosshair.index));
    const row = rows[index];
    if (!row) return;
    const x = xAt(index);
    const y = Math.max(pricePanel.y, Math.min(pricePanel.y + pricePanel.h, state.crosshair.y));
    ctx.save();
    ctx.strokeStyle = "#7c8798";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x, pricePanel.y); ctx.lineTo(x, bounds.height - 22); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(pricePanel.x, y); ctx.lineTo(pricePanel.x + pricePanel.w, y); ctx.stroke();
    ctx.setLineDash([]);

    const timeLabel = formatBarTime(row.open_time, true);
    ctx.font = "10px system-ui";
    const timeWidth = ctx.measureText(timeLabel).width + 12;
    const timeX = Math.max(pricePanel.x, Math.min(pricePanel.x + pricePanel.w - timeWidth, x - timeWidth / 2));
    ctx.fillStyle = "#475467";
    ctx.fillRect(timeX, bounds.height - 21, timeWidth, 18);
    ctx.fillStyle = "#fff";
    ctx.fillText(timeLabel, timeX + 6, bounds.height - 8);

    const price = maximum - (y - pricePanel.y) / pricePanel.h * (maximum - minimum);
    const priceLabel = formatPrice(price);
    ctx.fillStyle = "#475467";
    ctx.fillRect(pricePanel.x + pricePanel.w, y - 9, bounds.width - pricePanel.x - pricePanel.w, 18);
    ctx.fillStyle = "#fff";
    ctx.fillText(priceLabel, pricePanel.x + pricePanel.w + 5, y + 3);
    ctx.restore();
  }

  function drawChart() {
    const canvas = el.canvas;
    const panelCount = state.indicators.filter(item => item.placement === "PANEL").length;
    const stageHeight = canvas.parentElement?.clientHeight || 300;
    const desiredHeight = Math.max(stageHeight, 300 + panelCount * 82);
    canvas.style.height = desiredHeight > stageHeight ? `${desiredHeight}px` : "100%";
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    const pixelWidth = Math.round(bounds.width * ratio);
    const pixelHeight = Math.round(bounds.height * ratio);
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, bounds.width, bounds.height);
    if (!state.bars.length) return;

    const rows = el.type.value === "HEIKIN_ASHI" ? heikinAshi(state.bars) : state.bars;
    const panelIndicators = state.indicators.filter(item => item.placement === "PANEL");
    const seriesIndicators = state.indicators.filter(item => item.placement === "SERIES");
    const left = 12, right = 67, top = 10, bottom = 24;
    const gap = panelIndicators.length ? 5 : 0;
    const availableHeight = bounds.height - top - bottom - gap * panelIndicators.length;
    const panelHeight = panelIndicators.length ? Math.max(72, Math.min(110, availableHeight * .18)) : 0;
    const priceHeight = Math.max(150, availableHeight - panelHeight * panelIndicators.length);
    const pricePanel = { x: left, y: top, w: bounds.width - left - right, h: priceHeight };
    if (state.viewCount == null) {
      state.viewCount = Math.max(36, Math.min(140, Math.floor(pricePanel.w / 9)));
    }
    const visibleCount = Math.max(12, Math.min(300, Math.round(state.viewCount)));
    if (state.viewEnd == null) state.viewEnd = rows.length;
    const actualCount = Math.min(rows.length, visibleCount);
    const end = Math.max(actualCount, Math.min(rows.length, Math.round(state.viewEnd)));
    const start = Math.max(0, end - actualCount);
    state.viewEnd = end;
    const step = pricePanel.w / Math.max(1, actualCount);
    const xAt = index => pricePanel.x + (index - start + .5) * step;
    const overlays = seriesIndicators.flatMap(item => indicatorSeries(item, rows, state.bars));
    const priceValues = rows.slice(start, end).flatMap(row => [row.low, row.high]);
    overlays.filter(item => !item.bars && !item.fixedRange).forEach(item => {
      priceValues.push(...item.values.slice(start, end).filter(Number.isFinite));
    });
    let minimum = Math.min(...priceValues), maximum = Math.max(...priceValues);
    const margin = Math.max((maximum - minimum) * .08, maximum * .002);
    minimum -= margin; maximum += margin;
    const yAt = value => pricePanel.y + pricePanel.h - (value - minimum) / (maximum - minimum || 1) * pricePanel.h;

    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, bounds.width, bounds.height);
    ctx.strokeStyle = "#edf0f4";
    ctx.lineWidth = 1;
    ctx.font = "9px system-ui";
    ctx.fillStyle = "#7a8493";
    for (let index = 0; index <= 5; index += 1) {
      const y = pricePanel.y + pricePanel.h * index / 5;
      ctx.beginPath(); ctx.moveTo(pricePanel.x, y); ctx.lineTo(pricePanel.x + pricePanel.w, y); ctx.stroke();
      const value = maximum - (maximum - minimum) * index / 5;
      ctx.fillText(formatPrice(value), pricePanel.x + pricePanel.w + 6, y + 3);
    }
    for (let index = 0; index <= 6; index += 1) {
      const x = pricePanel.x + pricePanel.w * index / 6;
      ctx.beginPath(); ctx.moveTo(x, pricePanel.y); ctx.lineTo(x, pricePanel.y + pricePanel.h); ctx.stroke();
    }

    const candleWidth = Math.max(2, Math.min(9, step * .62));
    rows.slice(start, end).forEach((row, offset) => {
      const x = xAt(start + offset);
      const rising = row.close >= row.open;
      const color = rising ? "#d84b5b" : "#2f67d8";
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.moveTo(x, yAt(row.high)); ctx.lineTo(x, yAt(row.low)); ctx.stroke();
      const yOpen = yAt(row.open), yClose = yAt(row.close);
      ctx.fillRect(x - candleWidth / 2, Math.min(yOpen, yClose), candleWidth, Math.max(1, Math.abs(yClose - yOpen)));
    });

    overlays.forEach(item => {
      if (item.bars) {
        const values = item.values.slice(start, end).filter(Number.isFinite);
        const maxVolume = Math.max(...values, 1);
        item.values.slice(start, end).forEach((value, offset) => {
          if (!Number.isFinite(value)) return;
          const height = value / maxVolume * pricePanel.h * .18;
          ctx.fillStyle = `${item.color}55`;
          ctx.fillRect(xAt(start + offset) - candleWidth / 2, pricePanel.y + pricePanel.h - height, candleWidth, height);
        });
      } else if (item.fixedRange) {
        const localY = value => pricePanel.y + pricePanel.h - value / 100 * pricePanel.h;
        line(ctx, item.values, start, end, xAt, localY, `${item.color}99`);
      } else line(ctx, item.values, start, end, xAt, yAt, item.color);
    });

    let panelY = pricePanel.y + pricePanel.h + gap;
    panelIndicators.forEach(indicator => {
      const panel = { x: left, y: panelY, w: pricePanel.w, h: panelHeight };
      const label = indicator.type === "ATR"
        ? `${indicatorLabel(indicator)} · 진행봉 포함`
        : indicatorLabel(indicator);
      drawSeriesPanel(ctx, indicatorSeries(indicator, rows, state.bars), panel, start, end, xAt, label);
      panelY += panelHeight + gap;
    });

    const labelCount = 6;
    ctx.fillStyle = "#7a8493";
    let previousLabelDate = null;
    for (let index = 0; index <= labelCount; index += 1) {
      const rowIndex = Math.min(end - 1, start + Math.floor((visibleCount - 1) * index / labelCount));
      const dateKey = barDateKey(rows[rowIndex].open_time);
      const label = formatAxisBarTime(
        rows[rowIndex].open_time,
        index === 0 || dateKey !== previousLabelDate,
      );
      ctx.fillText(label, xAt(rowIndex) - (index === 0 || dateKey !== previousLabelDate ? 34 : 18), bounds.height - 7);
      previousLabelDate = dateKey;
    }
    state.geometry = { bounds, pricePanel, start, end, step, xAt, yAt, minimum, maximum };
    drawCrosshair(ctx, rows, state.geometry);
    canvas.dataset.start = String(start);
    canvas.dataset.end = String(end);
    canvas.dataset.step = String(step);
  }

  function indicatorLabel(indicator) {
    if (indicator.type === "BOLLINGER") return `볼린저밴드 (${indicator.period}, ${indicator.deviation})`;
    if (indicator.type === "MACD") return `MACD (${indicator.fast}, ${indicator.slow}, ${indicator.signal})`;
    if (indicator.type === "VOLUME") return "거래량";
    if (indicator.type === "OBV") return "OBV";
    if (indicator.type === "ATR") return `ATR (${indicator.period})`;
    return `${indicator.type} (${indicator.period})`;
  }

  function normalizeIndicator(indicator) {
    const positive = (value, fallback, minimum, integer = true) => {
      const numeric = Number(value);
      const valid = Number.isFinite(numeric) && numeric >= minimum ? numeric : fallback;
      return integer ? Math.floor(valid) : valid;
    };
    indicator.period = indicator.type === "ATR" ? clampAtrPeriod(indicator.period) : positive(indicator.period, 20, 2);
    indicator.deviation = positive(indicator.deviation, 2, .1, false);
    indicator.fast = positive(indicator.fast, 12, 2);
    indicator.slow = positive(indicator.slow, 26, 3);
    indicator.signal = positive(indicator.signal, 9, 2);
    if (!["RAW", "HEIKIN_ASHI", "CHART"].includes(indicator.priceSource)) indicator.priceSource = "CHART";
    return indicator;
  }

  function indicatorPriceSourceLabel(indicator) {
    if (["VOLUME", "OBV", "ATR"].includes(indicator.type)) return "실제 시세";
    if (indicator.priceSource === "RAW") return "실제 종가";
    if (indicator.priceSource === "HEIKIN_ASHI") return "하이킨아시 종가";
    return el.type.value === "HEIKIN_ASHI" ? "차트 종가: 하이킨아시" : "차트 종가: 실제";
  }

  function renderIndicatorParameters(indicator, container) {
    normalizeIndicator(indicator);
    container.replaceChildren();
    const definitions = indicator.type === "MACD"
      ? [["fast", "단기", 2], ["slow", "장기", 3], ["signal", "시그널", 2]]
      : ["VOLUME", "OBV"].includes(indicator.type)
        ? []
        : [["period", "기간", indicator.type === "ATR" ? 1 : 2], ...(indicator.type === "BOLLINGER" ? [["deviation", "표준편차", .1]] : [])];
    definitions.forEach(([key, label, min]) => {
      const wrapper = document.createElement("label");
      wrapper.textContent = label;
      const input = document.createElement("input");
      input.type = "number";
      input.min = String(min);
      if (indicator.type === "ATR" && key === "period") {
        indicator.period = clampAtrPeriod(indicator.period);
        input.max = String(atrMaxPeriod());
      }
      input.step = key === "deviation" ? "0.01" : "1";
      input.value = String(indicator[key] ?? (key === "deviation" ? 2 : indicator.type === "ATR" ? 14 : 20));
      input.addEventListener("change", () => {
        const value = Math.max(min, Number(input.value) || min);
        indicator[key] = indicator.type === "ATR" && key === "period" ? clampAtrPeriod(value) : value;
        normalizeIndicator(indicator);
        input.value = String(indicator[key]);
        saveSettings(); renderIndicatorSummary(); drawChart();
        if (indicator.type === "ATR" && key === "period") void ensureAtrHistory();
      });
      wrapper.append(input);
      container.append(wrapper);
    });
    if (!["VOLUME", "OBV", "ATR"].includes(indicator.type)) {
      const wrapper = document.createElement("label");
      wrapper.className = "chart-indicator-price-source";
      wrapper.textContent = "계산 기준";
      const source = document.createElement("select");
      for (const [value, label] of [["RAW", "실제 종가 (전략 지표 기준)"], ["HEIKIN_ASHI", "하이킨아시 종가"], ["CHART", "차트 유형과 동일"]]) source.add(new Option(label, value));
      source.value = indicator.priceSource;
      source.addEventListener("change", () => {
        indicator.priceSource = source.value;
        saveSettings(); renderIndicatorSummary(); drawChart();
      });
      wrapper.append(source);
      container.append(wrapper);
    }
  }

  function renderIndicators() {
    el.indicators.replaceChildren();
    state.indicators.forEach(indicator => {
      normalizeIndicator(indicator);
      const row = document.createElement("div");
      row.className = "chart-indicator-row";
      const type = document.createElement("select");
      for (const [value, label] of [["SMA", "단순이동평균"], ["EMA", "지수이동평균"], ["BOLLINGER", "볼린저밴드"], ["VOLUME", "거래량"], ["OBV", "OBV"], ["ATR", "ATR"], ["RSI", "RSI"], ["MACD", "MACD"]]) {
        type.add(new Option(label, value));
      }
      type.value = indicator.type;
      const placement = document.createElement("select");
      placement.add(new Option("가격판 시리즈", "SERIES"));
      placement.add(new Option("별도 패널", "PANEL"));
      placement.value = indicator.placement;
      const parameters = document.createElement("div");
      parameters.className = "chart-indicator-parameters";
      renderIndicatorParameters(indicator, parameters);
      const color = document.createElement("input");
      color.type = "color"; color.value = indicator.color;
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "chart-indicator-remove"; remove.textContent = "삭제";
      type.addEventListener("change", () => {
        indicator.type = type.value;
        if (["OBV", "ATR"].includes(indicator.type)) {
          indicator.placement = "PANEL";
          placement.value = "PANEL";
        }
        if (indicator.type === "ATR") indicator.period = 14;
        if (indicator.type === "ATR") indicator.period = clampAtrPeriod(indicator.period);
        normalizeIndicator(indicator);
        renderIndicatorParameters(indicator, parameters); saveSettings(); renderIndicatorSummary(); drawChart();
        if (indicator.type === "ATR") void ensureAtrHistory();
      });
      placement.addEventListener("change", () => { indicator.placement = placement.value; saveSettings(); drawChart(); });
      color.addEventListener("input", () => { indicator.color = color.value; saveSettings(); drawChart(); });
      remove.addEventListener("click", () => { state.indicators = state.indicators.filter(item => item !== indicator); renderIndicators(); saveSettings(); drawChart(); });
      row.append(type, placement, parameters, color, remove);
      el.indicators.append(row);
    });
    renderIndicatorSummary();
  }

  function renderIndicatorSummary() {
    el.indicatorSummary.textContent = state.indicators.length
      ? state.indicators.map(item => `${indicatorLabel(item)} · ${indicatorPriceSourceLabel(item)}`).join(" / ")
      : "설정 없음";
    el.indicatorSummary.title = el.indicatorSummary.textContent;
  }

  async function loadSources(preferred = "AUTO") {
    const payload = await request("/api/chart/sources");
    state.sources = payload.sources || [];
    el.source.replaceChildren(new Option("자동 선택 · 실전 시세 우선", "AUTO"));
    state.sources.forEach(item => {
      const environment = item.target === "LIVE" ? "실전" : item.target === "PAPER" ? "모의" : item.target;
      const capacity = `${Math.floor(item.available)}/${Math.floor(item.capacity)}`;
      el.source.add(new Option(`${item.label} · ${String(item.provider).toUpperCase()} ${environment} · 여유 ${capacity}`, item.id));
    });
    el.source.value = [...el.source.options].some(option => option.value === preferred) ? preferred : "AUTO";
  }

  function selectedChartSource() {
    if (el.source.value !== "AUTO") return el.source.value;
    const eligible = state.sources.filter(item => !item.venues || item.venues.includes(el.venue.value));
    // A spare mock quota does not imply a working realtime price stream.
    return eligible.find(item => item.target === "LIVE" && item.state === "connected")?.id
      || eligible.find(item => item.target === "LIVE")?.id || "AUTO";
  }

  function showSearchResults(rows) {
    el.searchResults.replaceChildren();
    state.searchIndex = -1;
    rows.forEach(item => {
      const button = document.createElement("button");
      button.type = "button"; button.className = "chart-search-result"; button.setAttribute("role", "option");
      const strong = document.createElement("strong"); strong.textContent = item.name || item.code;
      const small = document.createElement("small"); small.textContent = item.code;
      button.append(strong, small);
      button.addEventListener("click", () => selectInstrument(item));
      el.searchResults.append(button);
    });
    el.searchResults.hidden = !rows.length;
    el.search.setAttribute("aria-expanded", String(Boolean(rows.length)));
  }

  function showSearchStatus(text) {
    el.searchResults.replaceChildren();
    state.searchIndex = -1;
    const status = document.createElement("div");
    status.className = "chart-search-status";
    status.textContent = text;
    el.searchResults.append(status);
    el.searchResults.hidden = false;
    el.search.setAttribute("aria-expanded", "true");
  }

  async function searchInstruments() {
    const query = el.search.value.trim();
    if (!query) { showSearchResults([]); return; }
    const requestId = ++state.searchRequest;
    showSearchStatus("종목을 찾고 있습니다…");
    try {
      const payload = await request(`/api/chart/instruments?q=${encodeURIComponent(query)}&source=${encodeURIComponent(el.source.value)}`);
      if (requestId !== state.searchRequest) return;
      const rows = payload.instruments || [];
      if (rows.length) showSearchResults(rows);
      else showSearchStatus("일치하는 종목이 없습니다.");
    } catch (error) {
      if (requestId === state.searchRequest) showSearchStatus(error.message);
    }
  }

  function moveSearchSelection(direction) {
    const buttons = [...el.searchResults.querySelectorAll("button")];
    if (!buttons.length) return false;
    state.searchIndex = (state.searchIndex + direction + buttons.length) % buttons.length;
    buttons.forEach((button, index) => button.classList.toggle("active", index === state.searchIndex));
    buttons[state.searchIndex].scrollIntoView({ block: "nearest" });
    return true;
  }

  async function selectInstrument(item) {
    state.code = item.code;
    state.name = item.name && item.name !== item.code ? item.name : "";
    el.search.value = state.name ? `${state.name} · ${state.code}` : state.code;
    showSearchResults([]);
    await loadChart();
  }

  function mergeOlderBars(rows) {
    const existing = new Set(state.bars.map(row => row.open_time));
    const added = rows.filter(row => !existing.has(row.open_time));
    if (!added.length) return 0;
    state.bars = [...added, ...state.bars]
      .sort((a, b) => new Date(a.open_time) - new Date(b.open_time));
    state.viewEnd = (state.viewEnd ?? state.bars.length - added.length) + added.length;
    if (state.crosshair) state.crosshair.index += added.length;
    if (state.drag) state.drag.startEnd += added.length;
    return added.length;
  }

  function restoreStreamState() {
    const fresh = state.lastStreamAt && Date.now() - state.lastStreamAt < STREAM_STALE_MS;
    const updated = state.lastRefreshAt ? new Date(state.lastRefreshAt).toLocaleTimeString("ko-KR", { hour12: false }) : "";
    el.live.title = [updated ? `마지막 자동 조회 ${updated}` : "실제 시세 수신을 기다리고 있습니다.", state.refreshError].filter(Boolean).join("\n");
    if (fresh && state.refreshFailed) setLiveState("실시간 · 자동 조회 재시도", "fallback");
    else if (fresh) setLiveState("실시간", "live");
    else if (state.refreshFailed) setLiveState("자동 갱신 실패 · 재시도 중", "error");
    else if (state.lastRefreshAt) setLiveState(`자동 갱신 · 10초 · ${updated}`, "fallback");
    else if (state.fallbackTimer) setLiveState("시세 수신 대기 · 자동 갱신", "connecting");
    else setLiveState("조회됨", "idle");
  }

  async function loadOlderBars(requestedCount = 180, generation = state.loadGeneration) {
    if (generation !== state.loadGeneration || state.loadedGeneration !== generation) return 0;
    if (state.historyLoading && state.historyLoadingGeneration === generation) return 0;
    if (state.noMoreHistory || !state.code || !state.bars.length || !state.source) return 0;
    if (Date.now() < state.historyRetryAt) return 0;
    state.historyLoading = true;
    state.historyLoadingGeneration = generation;
    setLiveState("이전 봉 조회 중", "connecting");
    try {
      const earliest = new Date(state.bars[0].open_time).getTime() - 1;
      const query = new URLSearchParams({
        code: state.code,
        interval: el.interval.value,
        venue: el.venue.value,
        source: state.source.id,
        count: String(Math.min(400, Math.max(20, Math.floor(Number(requestedCount) || 180)))),
        end: new Date(earliest).toISOString(),
      });
      const payload = await request(`/api/chart/bars?${query}`);
      if (generation !== state.loadGeneration || state.loadedGeneration !== generation) return 0;
      const received = (payload.bars || []).map(barNumbers);
      const added = mergeOlderBars(received);
      if (!added) state.noMoreHistory = true;
      drawChart();
      restoreStreamState();
      return added;
    } catch (_) {
      if (generation !== state.loadGeneration || state.loadedGeneration !== generation) return 0;
      state.historyRetryAt = Date.now() + 5000;
      setLiveState("이전 봉 조회 실패", "error");
      setTimeout(() => {
        if (generation === state.loadGeneration && state.loadedGeneration === generation) restoreStreamState();
      }, 1500);
      return 0;
    } finally {
      if (state.historyLoadingGeneration === generation) {
        state.historyLoading = false;
        state.historyLoadingGeneration = null;
      }
    }
  }

  async function ensureAtrHistory(generation = state.loadGeneration) {
    while (true) {
      if (generation !== state.loadGeneration || state.loadedGeneration !== generation) return;
      const target = requiredAtrHistory();
      if (!target || !state.code || !state.source || state.noMoreHistory || state.bars.length >= target) return;
      if (state.historyLoading && state.historyLoadingGeneration === generation) {
        await new Promise(resolve => setTimeout(resolve, 50));
        continue;
      }
      const added = await loadOlderBars(target - state.bars.length, generation);
      if (!added) break;
    }
  }

  function maybeLoadOlder() {
    const geometry = state.geometry;
    if (!geometry || geometry.start > Math.max(6, state.viewCount * .08)) return;
    loadOlderBars();
  }

  function renderQuote() {
    if (!state.quote || !state.code) return;
    const bar = state.bars[state.bars.length - 1];
    const price = number(state.quote.price);
    const reference = bar ? number(bar.open) : price;
    const change = reference ? (price - reference) / reference * 100 : 0;
    const source = state.source?.label || state.quote.source_alias || "API";
    const delayed = state.quote.delayed || state.quote.stale ? " · 지연" : "";
    el.quote.innerHTML = `<strong>${state.name || state.code} · ${state.code}</strong><strong class="chart-price">${formatPrice(price)}원</strong><span>시 ${formatPrice(bar?.open)} 고 ${formatPrice(bar?.high)} 저 ${formatPrice(bar?.low)} · ${change >= 0 ? "+" : ""}${change.toFixed(2)}% · ${source}${delayed}</span>`;
    el.title.textContent = `${state.name || state.code} 차트`;
  }

  function bucketStart(date, interval) {
    const time = new Date(date);
    if (interval.endsWith("m")) {
      const minutes = Number.parseInt(interval, 10);
      const value = time.getTime();
      return Math.floor(value / (minutes * 60_000)) * minutes * 60_000;
    }
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(time);
    const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
    let day = new Date(`${map.year}-${map.month}-${map.day}T00:00:00+09:00`);
    if (interval === "1w") {
      const weekday = (day.getUTCDay() + 6) % 7;
      day = new Date(day.getTime() - weekday * 86_400_000);
    }
    return day.getTime();
  }

  function applyQuote(quote) {
    if (!validQuote(quote)) return false;
    const price = number(quote.price);
    const start = bucketStart(quote.market_time || quote.received_at, el.interval.value);
    if (!Number.isFinite(start)) return false;
    state.quote = quote;
    if (!state.bars.length) { renderQuote(); return true; }
    const last = state.bars[state.bars.length - 1];
    const lastStart = bucketStart(last.open_time, el.interval.value);
    const followingLatest = state.viewEnd == null || state.viewEnd >= state.bars.length;
    if (start > lastStart) {
      state.bars.push({ ...last, open_time: new Date(start).toISOString(), close_time: new Date(start).toISOString(), open: price, high: price, low: price, close: price, volume: 0, complete: false });
      const removed = Math.max(0, state.bars.length - Math.max(400, requiredAtrHistory()));
      if (removed) {
        state.bars.splice(0, removed);
        if (!followingLatest && state.viewEnd != null) state.viewEnd = Math.max(0, state.viewEnd - removed);
        if (state.crosshair) {
          state.crosshair.index -= removed;
          if (state.crosshair.index < 0) {
            state.crosshair = null;
            el.tooltip.hidden = true;
          }
        }
        if (state.drag) state.drag.startEnd = Math.max(0, state.drag.startEnd - removed);
      }
      if (followingLatest) state.viewEnd = state.bars.length;
    } else if (start === lastStart) {
      last.high = Math.max(last.high, price);
      last.low = Math.min(last.low, price);
      last.close = price;
      last.complete = false;
    }
    renderQuote(); drawChart();
    return true;
  }

  function validQuote(quote) {
    return quote && number(quote.price) > 0 && (!quote.instrument?.code || quote.instrument.code === state.code)
      && Number.isFinite(new Date(quote.market_time || quote.received_at).getTime());
  }

  function mergeRecentBars(rows, streamBars = new Map()) {
    if (!rows.length) return;
    const followingLatest = state.viewEnd == null || state.viewEnd >= state.bars.length;
    const anchor = state.bars[Math.max(0, (state.viewEnd ?? state.bars.length) - 1)]?.open_time;
    const crosshairTime = state.crosshair ? state.bars[state.crosshair.index]?.open_time : null;
    const byTime = new Map(state.bars.map(row => [new Date(row.open_time).getTime(), row]));
    for (const row of rows) {
      const key = new Date(row.open_time).getTime();
      if (Number.isFinite(key)) byTime.set(key, barNumbers(row));
    }
    // Preserve every tick's extrema during the request, including buckets that
    // have since closed. Replaying only the last tick can lose an intrabar spike.
    for (const [key, ticks] of streamBars) {
      const row = byTime.get(key);
      if (row) {
        row.high = Math.max(row.high, ticks.high);
        row.low = Math.min(row.low, ticks.low);
        row.close = ticks.close;
      }
    }
    state.bars = [...byTime.values()].sort((a, b) => new Date(a.open_time) - new Date(b.open_time));
    const indexOf = time => state.bars.findIndex(row => new Date(row.open_time).getTime() === new Date(time).getTime());
    const oldEnd = state.viewEnd;
    state.viewEnd = followingLatest ? state.bars.length : (anchor ? indexOf(anchor) + 1 : oldEnd);
    if (state.drag && oldEnd != null) state.drag.startEnd += state.viewEnd - oldEnd;
    if (crosshairTime) state.crosshair.index = indexOf(crosshairTime);
  }

  function stopRealtime() {
    state.manualClose = true;
    const socket = state.socket;
    state.socket = null;
    if (socket) socket.close();
    if (state.fallbackTimer) { clearInterval(state.fallbackTimer); state.fallbackTimer = null; }
    state.refreshInFlight = null;
    state.lastStreamAt = 0;
    state.lastStreamQuote = null;
    state.lastRefreshAt = 0;
    state.lastBarsRefreshAt = 0;
    state.refreshFailed = false;
    state.refreshError = "";
  }

  function realtimeActive(generation) {
    return !state.manualClose && generation === state.loadGeneration && state.loadedGeneration === generation;
  }

  async function refreshChart(generation = state.loadGeneration) {
    if (!realtimeActive(generation) || state.refreshInFlight || !state.code || !state.source) return;
    const token = { streamBars: new Map() };
    state.refreshInFlight = token;
    const startedAt = Date.now();
    const fresh = state.lastStreamAt && startedAt - state.lastStreamAt < STREAM_STALE_MS;
    const updateBars = !fresh || startedAt - state.lastBarsRefreshAt >= 30_000;
    const query = new URLSearchParams({ code: state.code, venue: el.venue.value, source: state.source.id });
    const barsQuery = new URLSearchParams(query);
    const interval = el.interval.value;
    const lastTime = new Date(state.bars.at(-1)?.open_time || startedAt).getTime();
    const intervalMs = interval.endsWith("m") ? parseInt(interval, 10) * 60_000 : 86_400_000;
    barsQuery.set("interval", interval);
    barsQuery.set("count", String(Math.min(400, Math.max(20, Math.ceil((startedAt - lastTime) / intervalMs) + 2))));
    restoreStreamState();
    try {
      const [quoteResult, barsResult] = await Promise.allSettled([
        request(`/api/chart/quote?${query}`, 15_000),
        updateBars ? request(`/api/chart/bars?${barsQuery}`, 15_000) : Promise.resolve(null),
      ]);
      if (!realtimeActive(generation) || state.refreshInFlight !== token) return;
      const quoteOk = quoteResult.status === "fulfilled" && validQuote(quoteResult.value.quote);
      const barsOk = barsResult.status === "fulfilled" && Array.isArray(barsResult.value?.bars);
      // REST quotes may use receipt time, so only broker bars may create REST candles.
      if (quoteOk && token.streamBars.size === 0) state.quote = quoteResult.value.quote;
      if (barsOk) {
        mergeRecentBars(barsResult.value.bars, token.streamBars);
        state.lastBarsRefreshAt = Date.now();
      }
      state.refreshFailed = !quoteOk || (updateBars && !barsOk);
      const describeFailure = (label, result) => `${label}: ${result.status === "rejected" ? (result.reason?.message || "조회 실패") : "유효한 데이터가 없습니다."}`;
      state.refreshError = [
        !quoteOk ? describeFailure("현재가", quoteResult) : "",
        updateBars && !barsOk ? describeFailure("봉", barsResult) : "",
      ].filter(Boolean).join(" / ");
      if (quoteOk || barsOk) state.lastRefreshAt = Date.now();
      el.empty.hidden = Boolean(state.bars.length);
      renderQuote(); drawChart(); restoreStreamState();
    } finally {
      if (state.refreshInFlight === token) state.refreshInFlight = null;
    }
  }

  function connectStream(generation) {
    if (!realtimeActive(generation) || !state.source?.realtime) return;
    const previous = state.socket;
    state.socket = null;
    if (previous) previous.close();
    state.lastConnectAt = Date.now();
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}/api/chart/stream?code=${encodeURIComponent(state.code)}&venue=${el.venue.value}&source=${encodeURIComponent(state.source.id)}`;
    let socket;
    try { socket = new WebSocket(url); }
    catch (_) { restoreStreamState(); return; }
    state.socket = socket;
    socket.onmessage = event => {
      if (!realtimeActive(generation) || state.socket !== socket) return;
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      if (payload.event_type === "quote" && validQuote(payload.quote)) {
        const quote = payload.quote;
        const time = new Date(quote.market_time || quote.received_at).getTime();
        const previousTime = new Date(state.lastStreamQuote?.market_time || 0).getTime();
        if (time < previousTime || quote.stale || quote.delayed || Date.now() - time > STREAM_STALE_MS || time > Date.now() + 5_000) return;
        if (applyQuote(quote)) {
          state.lastStreamAt = Date.now();
          state.lastStreamQuote = quote;
          const streamBars = state.refreshInFlight?.streamBars;
          if (streamBars) {
            const key = bucketStart(quote.market_time || quote.received_at, el.interval.value);
            const price = number(quote.price);
            const ticks = streamBars.get(key) || { high: price, low: price };
            streamBars.set(key, { high: Math.max(ticks.high, price), low: Math.min(ticks.low, price), close: price });
          }
          restoreStreamState();
        }
      } else if (payload.event_type === "error") {
        state.lastStreamAt = 0;
        socket.close();
      } else if (payload.event_type === "subscribed") {
        // Subscription registration is not proof of receiving broker prices.
        restoreStreamState();
      }
    };
    socket.onerror = () => {
      if (realtimeActive(generation) && state.socket === socket) socket.close();
    };
    socket.onclose = () => {
      if (state.socket !== socket) return;
      state.socket = null;
      state.lastStreamAt = 0;
      if (realtimeActive(generation)) { restoreStreamState(); void refreshChart(generation); }
    };
  }

  function startRealtime(generation = state.loadGeneration) {
    if (generation !== state.loadGeneration || state.loadedGeneration !== generation) return;
    stopRealtime();
    state.manualClose = false;
    state.lastBarsRefreshAt = Date.now();
    state.fallbackTimer = setInterval(() => {
      if (!realtimeActive(generation)) return;
      restoreStreamState();
      void refreshChart(generation);
      if (!state.socket && Date.now() - state.lastConnectAt >= 30_000) connectStream(generation);
    }, REFRESH_INTERVAL_MS);
    connectStream(generation);
    restoreStreamState();
  }

  async function loadChart() {
    const generation = ++state.loadGeneration;
    state.loadedGeneration = null;
    state.searchRequest += 1;
    clearTimeout(state.searchTimer);
    showSearchResults([]);
    const typedCode = el.search.value.match(/(?:^|·\s*)([A-Z0-9]{6,24})\s*$/i)?.[1];
    if (!state.code && typedCode) state.code = typedCode.toUpperCase();
    stopRealtime();
    if (!state.code) { await searchInstruments(); return; }
    setLiveState("조회 중", "connecting");
    el.empty.hidden = false; el.empty.textContent = "봉과 현재가를 조회하고 있습니다.";
    saveSettings();
    try {
      const initialCount = Math.min(400, Math.max(180, requiredAtrHistory()));
      const query = new URLSearchParams({ code: state.code, interval: el.interval.value, venue: el.venue.value, source: selectedChartSource(), count: String(initialCount) });
      const payload = await request(`/api/chart/data?${query}`);
      if (generation !== state.loadGeneration) return;
      state.source = payload.source;
      state.name = payload.instrument?.name || state.name;
      if (state.name) el.search.value = `${state.name} · ${state.code}`;
      state.bars = (payload.bars || []).map(barNumbers).sort((a, b) => new Date(a.open_time) - new Date(b.open_time));
      state.viewCount = null;
      state.viewEnd = state.bars.length;
      state.crosshair = null;
      state.noMoreHistory = false;
      state.historyRetryAt = 0;
      state.quote = payload.quote;
      state.loadedGeneration = generation;
      el.empty.hidden = Boolean(state.bars.length);
      if (!state.bars.length) { el.empty.hidden = false; el.empty.textContent = "표시할 봉이 없습니다."; }
      await ensureAtrHistory(generation);
      if (generation !== state.loadGeneration || state.loadedGeneration !== generation) return;
      renderQuote(); drawChart(); startRealtime(generation);
    } catch (error) {
      if (generation !== state.loadGeneration) return;
      state.loadedGeneration = null;
      state.bars = [];
      el.empty.hidden = false; el.empty.textContent = error.message;
      setLiveState("조회 실패", "error");
      drawChart();
    }
  }

  async function open(options = {}) {
    el.modal.hidden = false;
    const openGeneration = state.loadGeneration;
    const preferred = options.source || restoreSettings();
    renderIndicators();
    try { await loadSources(preferred); }
    catch (error) {
      if (openGeneration !== state.loadGeneration || el.modal.hidden) return;
      setLiveState("API 없음", "error"); el.empty.textContent = error.message;
    }
    if (openGeneration !== state.loadGeneration || el.modal.hidden) return;
    if (options.code) {
      state.code = String(options.code).trim().toUpperCase();
      state.name = String(options.name || "").trim();
      el.search.value = state.name ? `${state.name} · ${state.code}` : state.code;
      await loadChart();
    } else {
      state.code = ""; state.name = ""; state.bars = []; state.quote = null;
      state.loadedGeneration = null;
      state.viewCount = null; state.viewEnd = null; state.crosshair = null; state.noMoreHistory = false;
      el.search.value = ""; el.title.textContent = "종목 차트";
      el.empty.hidden = false; el.empty.textContent = "종목을 선택하면 차트를 표시합니다.";
      setLiveState("조회 전", "idle"); drawChart(); el.search.focus();
    }
  }

  function close() {
    state.loadGeneration += 1;
    state.loadedGeneration = null;
    state.searchRequest += 1;
    clearTimeout(state.searchTimer);
    stopRealtime();
    showSearchResults([]);
    el.indicatorModal.hidden = true;
    el.tooltip.hidden = true;
    el.modal.hidden = true;
  }

  function init() {
    Object.assign(el, {
      modal: $("#chart-modal"), title: $("#chart-title"), live: $("#chart-live-state"),
      search: $("#chart-search"), searchResults: $("#chart-search-results"),
      source: $("#chart-source"), venue: $("#chart-venue"), interval: $("#chart-interval"), type: $("#chart-type"),
      canvas: $("#chart-canvas"), empty: $("#chart-empty"), tooltip: $("#chart-tooltip"),
      quote: $("#chart-quote-strip"), indicators: $("#chart-indicator-list"),
      indicatorModal: $("#chart-indicator-modal"), indicatorSummary: $("#chart-indicator-summary"),
    });
    if (!el.modal) return;
    const resumeChart = () => {
      if (document.hidden || el.modal.hidden || !realtimeActive(state.loadGeneration)) return;
      void refreshChart();
      if (!state.socket) connectStream(state.loadGeneration);
    };
    document.addEventListener("visibilitychange", resumeChart);
    window.addEventListener("online", resumeChart);
    const restoredSource = restoreSettings();
    renderIndicators();
    $("#chart-close").addEventListener("click", close);
    el.modal.addEventListener("click", event => { if (event.target === el.modal) close(); });
    $("#chart-load").addEventListener("click", () => loadChart());
    const closeIndicatorSettings = () => { el.indicatorModal.hidden = true; };
    $("#chart-indicator-settings").addEventListener("click", () => {
      showSearchResults([]);
      renderIndicators();
      el.indicatorModal.hidden = false;
    });
    $("#chart-indicator-close").addEventListener("click", closeIndicatorSettings);
    $("#chart-indicator-done").addEventListener("click", closeIndicatorSettings);
    el.indicatorModal.addEventListener("click", event => { if (event.target === el.indicatorModal) closeIndicatorSettings(); });
    $("#chart-add-indicator").addEventListener("click", () => {
      if (state.indicators.length >= 8) return;
      state.indicators.push(normalizeIndicator({ id: crypto.randomUUID(), type: "SMA", placement: "SERIES", period: 20, priceSource: "RAW", color: palette[state.indicators.length % palette.length] }));
      renderIndicators(); saveSettings(); drawChart();
    });
    el.search.addEventListener("input", () => {
      state.code = ""; state.name = "";
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(searchInstruments, 160);
    });
    el.search.addEventListener("focus", () => { if (el.search.value.trim() && !state.code) searchInstruments(); });
    el.search.addEventListener("keydown", event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (moveSearchSelection(event.key === "ArrowDown" ? 1 : -1)) event.preventDefault();
        return;
      }
      if (event.key === "Escape") { showSearchResults([]); return; }
      if (event.key === "Enter") {
        event.preventDefault();
        const buttons = [...el.searchResults.querySelectorAll("button")];
        const selected = buttons[state.searchIndex] || buttons[0];
        if (selected) selected.click(); else loadChart();
      }
    });
    [el.source, el.venue].forEach(control => control.addEventListener("change", () => { saveSettings(); if (state.code) loadChart(); }));
    el.interval.addEventListener("change", () => {
      clampAtrIndicators();
      renderIndicators();
      saveSettings();
      if (state.code) loadChart();
    });
    el.type.addEventListener("change", () => { saveSettings(); renderIndicatorSummary(); drawChart(); });
    el.canvas.addEventListener("pointerdown", event => {
      if (!state.geometry || event.button !== 0) return;
      state.drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startEnd: state.viewEnd,
        step: state.geometry.step,
      };
      el.canvas.setPointerCapture(event.pointerId);
      el.canvas.classList.add("dragging");
      state.crosshair = null;
      el.tooltip.hidden = true;
    });
    el.canvas.addEventListener("pointermove", event => {
      if (!state.bars.length || !state.geometry) return;
      if (state.drag) {
        const shift = Math.round((event.clientX - state.drag.startX) / Math.max(1, state.drag.step));
        const count = Math.min(state.bars.length, Math.round(state.viewCount));
        state.viewEnd = Math.max(count, Math.min(state.bars.length, state.drag.startEnd - shift));
        drawChart();
        maybeLoadOlder();
        return;
      }
      const rect = el.canvas.getBoundingClientRect();
      const { pricePanel, start, end, step } = state.geometry;
      const relativeX = event.clientX - rect.left;
      const index = Math.max(start, Math.min(end - 1, start + Math.round((relativeX - pricePanel.x) / step - .5)));
      state.crosshair = { index, y: event.clientY - rect.top };
      const rows = el.type.value === "HEIKIN_ASHI" ? heikinAshi(state.bars) : state.bars;
      const row = rows[index];
      if (!row) return;
      const time = formatBarTime(row.open_time, true);
      el.tooltip.textContent = `${time}\n시 ${formatPrice(row.open)}  고 ${formatPrice(row.high)}\n저 ${formatPrice(row.low)}  종 ${formatPrice(row.close)}\n거래량 ${formatPrice(row.volume)}`;
      el.tooltip.style.left = `${Math.min(rect.width - 180, Math.max(8, event.clientX - rect.left + 12))}px`;
      el.tooltip.style.top = `${Math.max(8, event.clientY - rect.top - 68)}px`;
      el.tooltip.hidden = false;
      drawChart();
    });
    const finishDrag = event => {
      if (!state.drag || (event.pointerId != null && event.pointerId !== state.drag.pointerId)) return;
      state.drag = null;
      el.canvas.classList.remove("dragging");
      maybeLoadOlder();
      drawChart();
    };
    el.canvas.addEventListener("pointerup", finishDrag);
    el.canvas.addEventListener("pointercancel", finishDrag);
    el.canvas.addEventListener("wheel", event => {
      if (!state.geometry || !state.bars.length) return;
      event.preventDefault();
      const rect = el.canvas.getBoundingClientRect();
      const { pricePanel, start, end } = state.geometry;
      const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left - pricePanel.x) / pricePanel.w));
      const anchor = start + fraction * Math.max(1, end - start);
      const oldCount = Math.max(12, Math.round(state.viewCount));
      const factor = event.deltaY > 0 ? 1.16 : .86;
      const nextCount = Math.max(12, Math.min(300, Math.round(oldCount * factor)));
      if (nextCount === oldCount) return;
      const nextStart = anchor - fraction * nextCount;
      state.viewCount = nextCount;
      state.viewEnd = Math.round(nextStart + nextCount);
      state.viewEnd = Math.max(Math.min(nextCount, state.bars.length), Math.min(state.bars.length, state.viewEnd));
      drawChart();
      maybeLoadOlder();
    }, { passive: false });
    el.canvas.addEventListener("mouseleave", () => {
      if (!state.drag) { state.crosshair = null; el.tooltip.hidden = true; drawChart(); }
    });
    state.resizeObserver = new ResizeObserver(drawChart);
    state.resizeObserver.observe($("#chart-stage"));
    loadSources(restoredSource).catch(() => {});
  }

  window.tradeRouterChart = { open, close };
  init();
})();
