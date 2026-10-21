"use strict";

// Execute chart.js and its actual indicator-type change handler. No browser,
// network connection, or account is involved.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.join(__dirname, "../apps/studio/src/trade_router_studio/static/chart.js");
const SCRIPT = fs.readFileSync(SCRIPT_PATH, "utf8");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.value = "";
    this.dataset = {};
    this.style = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  add(option) { this.children.push(option); }
  addEventListener(event, handler) { this.listeners.set(event, handler); }
  dispatch(event) { this.listeners.get(event)?.({ target: this }); }
  setAttribute() {}
}

function chartHarness(saved = null) {
  const marker = "  window.tradeRouterChart = { open, close };";
  assert.ok(SCRIPT.includes(marker));
  let storage = saved;
  const context = {
    crypto: { randomUUID: () => "indicator-test" },
    document: { createElement: tag => new Element(tag) },
    Option: function Option(label, value) { return { label, value }; },
    localStorage: {
      getItem() { return storage; },
      setItem(key, value) { storage = value; },
    },
  };
  vm.runInNewContext(SCRIPT.replace(marker, `
    drawChart = () => {};
    globalThis.chart = { state, el, renderIndicators, restoreSettings,
      indicatorSeries, heikinAshi, normalizeIndicator, renderIndicatorSummary,
      selectedChartSource };
    return;
${marker}`), context, { filename: SCRIPT_PATH });
  const chart = context.chart;
  Object.assign(chart.el, {
    indicators: new Element("div"), indicatorSummary: new Element("div"),
    source: { value: "AUTO" }, venue: { value: "KRX" },
    interval: { value: "3m" }, type: { value: "CANDLE" },
  });
  chart.state.indicators = [];
  return { ...chart, saved: () => JSON.parse(storage) };
}

function sampleBars(count = 80) {
  return Array.from({ length: count }, (_, index) => ({
    open: 100 + index, close: 102 + index + index % 5,
    high: 113 + index, low: 98 + index, volume: 200 + index,
  }));
}

function selectType(chart, type) {
  chart.renderIndicators();
  const control = chart.el.indicators.children[0].children[0];
  control.value = type;
  control.dispatch("change");
}

test("switching a newly added moving average to Bollinger draws all three bands", () => {
  const chart = chartHarness();
  chart.state.indicators.push({ id: "new", type: "SMA", placement: "SERIES", period: 20, color: "#000000" });
  selectType(chart, "BOLLINGER");
  const indicator = chart.state.indicators[0];
  const series = chart.indicatorSeries(indicator, sampleBars());
  assert.equal(series.length, 3);
  const [upper, middle, lower] = series.map(item => item.values.at(-1));
  assert.ok([upper, middle, lower].every(Number.isFinite), "default standard deviation must enter the calculation, not just the input label");
  assert.ok(upper > middle && middle > lower);
  assert.equal(chart.saved().indicators[0].deviation, 2);
});

test("switching a newly added moving average to MACD uses all displayed default periods", () => {
  const chart = chartHarness();
  chart.state.indicators.push({ id: "new", type: "SMA", placement: "SERIES", period: 20, color: "#000000" });
  selectType(chart, "MACD");
  const series = chart.indicatorSeries(chart.state.indicators[0], sampleBars());
  assert.equal(series.length, 3);
  assert.ok(series.every(item => item.values.every(Number.isFinite)), "MACD must not contain NaN after a type switch");
  const saved = chart.saved().indicators[0];
  assert.equal(saved.fast, 12);
  assert.equal(saved.slow, 26);
  assert.equal(saved.signal, 9);
});

test("switching away from and back to Bollinger preserves custom parameters", () => {
  const chart = chartHarness();
  chart.state.indicators.push({ id: "custom", type: "BOLLINGER", placement: "SERIES", period: 55, deviation: 1.04, color: "#000000" });
  selectType(chart, "SMA");
  selectType(chart, "BOLLINGER");
  const indicator = chart.state.indicators[0];
  assert.equal(indicator.period, 55);
  assert.equal(indicator.deviation, 1.04);
  const series = chart.indicatorSeries(indicator, sampleBars());
  const values = sampleBars().slice(-55).map(row => row.close);
  const mean = values.reduce((sum, value) => sum + value, 0) / 55;
  const sigma = Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / 55);
  assert.ok(Math.abs(series[0].values.at(-1) - (mean + sigma * 1.04)) < 1e-10);
  assert.ok(Math.abs(series[2].values.at(-1) - (mean - sigma * 1.04)) < 1e-10);
});

test("reloading older Bollinger settings supplies missing standard deviation", () => {
  const chart = chartHarness(JSON.stringify({ indicators: [{ type: "BOLLINGER", period: 20, placement: "SERIES", color: "#000000" }] }));
  chart.restoreSettings();
  const series = chart.indicatorSeries(chart.state.indicators[0], sampleBars());
  assert.ok(series.every(item => Number.isFinite(item.values.at(-1))));
  assert.equal(chart.state.indicators[0].deviation, 2);
});

const plain = value => JSON.parse(JSON.stringify(value));

test("raw-price indicators remain unchanged when visual candles use Heikin-Ashi", () => {
  const chart = chartHarness();
  const raw = sampleBars();
  const heikin = chart.heikinAshi(raw);
  for (const type of ["SMA", "EMA", "BOLLINGER", "RSI", "MACD"]) {
    const indicator = { type, period: 20, deviation: 1.04, priceSource: "RAW", color: "#000000" };
    const rawView = chart.indicatorSeries(indicator, raw, raw);
    const heikinView = chart.indicatorSeries(indicator, heikin, raw);
    assert.deepEqual(plain(heikinView), plain(rawView), `${type} must use actual closes for RAW`);
  }
  const values = raw.slice(-20).map(row => row.close);
  const mean = values.reduce((sum, value) => sum + value, 0) / 20;
  const sigma = Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / 20);
  const band = chart.indicatorSeries({ type: "BOLLINGER", period: 20, deviation: 1.04, priceSource: "RAW" }, heikin, raw);
  assert.ok(Math.abs(band[0].values.at(-1) - (mean + sigma * 1.04)) < 1e-10);
  assert.ok(Math.abs(band[1].values.at(-1) - mean) < 1e-10);
});

test("explicit Heikin-Ashi price source calculates transformed closes independently of visual candles", () => {
  const chart = chartHarness();
  const raw = sampleBars();
  const heikin = chart.heikinAshi(raw);
  const indicator = { type: "BOLLINGER", period: 20, deviation: 1.04, priceSource: "HEIKIN_ASHI", color: "#000000" };
  const rawView = chart.indicatorSeries(indicator, raw, raw);
  const heikinView = chart.indicatorSeries(indicator, heikin, raw);
  assert.deepEqual(plain(rawView), plain(heikinView));
  const expectedMean = raw.slice(-20).reduce((sum, row) => sum + (row.open + row.high + row.low + row.close) / 4, 0) / 20;
  assert.ok(Math.abs(rawView[1].values.at(-1) - expectedMean) < 1e-10);
  const rawBasis = chart.indicatorSeries({ ...indicator, priceSource: "RAW" }, raw, raw);
  assert.notEqual(rawBasis[1].values.at(-1), rawView[1].values.at(-1), "fixture must distinguish raw and Heikin-Ashi prices");
});

test("legacy saved indicators retain chart-following calculation and expose that basis", () => {
  const chart = chartHarness(JSON.stringify({ chartType: "HEIKIN_ASHI", indicators: [{ type: "BOLLINGER", period: 55, deviation: 1.04, placement: "SERIES", color: "#000000" }] }));
  chart.restoreSettings();
  chart.renderIndicators();
  const indicator = chart.state.indicators[0];
  assert.equal(indicator.priceSource, "CHART", "migration must not silently recalculate existing indicators");
  assert.match(chart.el.indicatorSummary.textContent, /55, 1\.04/);
  assert.match(chart.el.indicatorSummary.textContent, /하이킨아시/);
  const raw = sampleBars();
  const heikin = chart.heikinAshi(raw);
  const values = chart.indicatorSeries(indicator, heikin, raw);
  const explicit = chart.indicatorSeries({ ...indicator, priceSource: "HEIKIN_ASHI" }, raw, raw);
  assert.deepEqual(plain(values), plain(explicit));
});

test("the calculation-basis selector persists a raw-price choice and updates its visible label", () => {
  const chart = chartHarness();
  chart.state.indicators.push({ id: "custom", type: "BOLLINGER", placement: "SERIES", period: 55, deviation: 1.04, priceSource: "CHART", color: "#000000" });
  chart.el.type.value = "HEIKIN_ASHI";
  chart.renderIndicators();
  const parameters = chart.el.indicators.children[0].children[2];
  const source = parameters.children.find(item => item.className === "chart-indicator-price-source").children[0];
  source.value = "RAW";
  source.dispatch("change");
  assert.equal(chart.saved().indicators[0].priceSource, "RAW");
  assert.equal(chart.saved().indicators[0].deviation, 1.04);
  assert.match(chart.el.indicatorSummary.textContent, /실제 종가/);
  chart.restoreSettings();
  assert.equal(chart.state.indicators[0].priceSource, "RAW");
});

test("OBV and ATR always use actual OHLCV even if a migrated setting carries another basis", () => {
  const chart = chartHarness();
  const raw = sampleBars();
  const heikin = chart.heikinAshi(raw);
  for (const type of ["OBV", "ATR"]) {
    const expected = chart.indicatorSeries({ type, period: 14, priceSource: "RAW" }, raw, raw);
    for (const priceSource of ["RAW", "HEIKIN_ASHI", "CHART"]) {
      const actual = chart.indicatorSeries({ type, period: 14, priceSource }, heikin, raw);
      assert.deepEqual(plain(actual), plain(expected), `${type} must use actual OHLCV regardless of chart/source setting`);
    }
  }
});

test("missing, invalid, and fractional parameters normalize to finite usable values", () => {
  const chart = chartHarness();
  const defaults = chart.normalizeIndicator({ type: "BOLLINGER" });
  assert.equal(defaults.period, 20);
  assert.equal(defaults.deviation, 2);
  assert.equal(defaults.fast, 12);
  assert.equal(defaults.slow, 26);
  assert.equal(defaults.signal, 9);
  const invalid = chart.normalizeIndicator({ type: "BOLLINGER", period: Infinity, deviation: NaN, fast: -1, slow: "bad", signal: null });
  for (const key of ["period", "deviation", "fast", "slow", "signal"]) assert.equal(invalid[key], defaults[key]);
  const fractional = chart.normalizeIndicator({ type: "BOLLINGER", period: "55.8", deviation: "1.04", fast: 12.9, slow: 26.4, signal: 9.8 });
  assert.equal(fractional.period, 55);
  assert.equal(fractional.deviation, 1.04);
  assert.equal(fractional.fast, 12);
  assert.equal(fractional.slow, 26);
  assert.equal(fractional.signal, 9);
  const series = chart.indicatorSeries({ type: "BOLLINGER", period: Infinity, deviation: NaN }, sampleBars());
  assert.ok(series.every(item => Number.isFinite(item.values.at(-1))), "defensive rendering must not hide NaN bands");
});

test("automatic source selection prefers a connected live source over a spare mock quota", () => {
  const chart = chartHarness();
  chart.el.venue.value = "INTEGRATED";
  chart.state.sources = [
    { id: "paper-first", target: "PAPER", state: "connected", available: 10, capacity: 10, venues: ["KRX", "INTEGRATED"] },
    { id: "live-idle", target: "LIVE", state: "idle", available: 8, capacity: 10, venues: ["KRX", "INTEGRATED"] },
    { id: "live-connected", target: "LIVE", state: "connected", available: 0, capacity: 10, venues: ["KRX", "INTEGRATED"] },
  ];
  assert.equal(chart.selectedChartSource(), "live-connected");
});

test("an explicitly selected mock source remains selected", () => {
  const chart = chartHarness();
  chart.el.source.value = "paper-manual";
  chart.state.sources = [
    { id: "live-connected", target: "LIVE", state: "connected", venues: ["KRX"] },
    { id: "paper-manual", target: "PAPER", state: "connected", venues: ["KRX"] },
  ];
  assert.equal(chart.selectedChartSource(), "paper-manual");
});

test("automatic source selection excludes live sources that cannot serve the selected venue", () => {
  const chart = chartHarness();
  chart.el.venue.value = "NXT";
  chart.state.sources = [
    { id: "krx-only", target: "LIVE", state: "connected", venues: ["KRX"] },
    { id: "nxt-live", target: "LIVE", state: "connected", venues: ["KRX", "NXT", "INTEGRATED"] },
  ];
  assert.equal(chart.selectedChartSource(), "nxt-live");
  chart.state.sources = [chart.state.sources[0]];
  assert.equal(chart.selectedChartSource(), "AUTO", "server fallback must not be pinned to a venue-incompatible live source");
});
