"use strict";

// Run with: node --test tests/test_chart_realtime.js
// Execute the production IIFE, substituting only the DOM renderers and browser
// boundaries. No broker connection, real timer, or real network request is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.join(__dirname, "../apps/studio/src/trade_router_studio/static/chart.js");
const SCRIPT = fs.readFileSync(SCRIPT_PATH, "utf8");
const BASE_TIME = Date.parse("2026-10-21T09:01:00+09:00");
const jsonCopy = value => JSON.parse(JSON.stringify(value));

function bar(time, overrides = {}) {
  return {
    open_time: new Date(time).toISOString(), close_time: new Date(time).toISOString(),
    open: 100, high: 104, low: 98, close: 102, volume: 100, complete: false,
    ...overrides,
  };
}

function initialBars() {
  return [
    bar(BASE_TIME - 7 * 60_000, { open: 95, high: 99, low: 93, close: 96, volume: 80, complete: true }),
    bar(BASE_TIME - 4 * 60_000, { open: 96, high: 103, low: 94, close: 100, volume: 90, complete: true }),
    bar(BASE_TIME - 60_000),
  ];
}

function createChart() {
  let now = BASE_TIME;
  let nextTimer = 1;
  const timers = new Map();
  const requests = [];
  const sockets = [];
  const renders = [];
  const frames = [];
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  function schedule(callback, delay, repeat) {
    const id = nextTimer++;
    timers.set(id, { callback, at: now + delay, delay, repeat });
    return id;
  }
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.({}); }
    message(payload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
    close() { this.readyState = 2; }
    // Closing is asynchronous in browsers: an old socket can report closure
    // after its replacement has already subscribed and received a price.
    finishClose() { this.readyState = 3; this.onclose?.({}); }
  }
  function fetch(url, options) {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    const request = {
      url: new URL(url, "http://chart.test"), options, settled: false,
      respond(payload, status = 200) {
        if (this.settled) return;
        this.settled = true;
        resolve({ ok: status >= 200 && status < 300, status, json: async () => payload });
      },
      reject(error = new Error("network unavailable")) {
        if (this.settled) return;
        this.settled = true;
        reject(error);
      },
    };
    options.signal?.addEventListener("abort", () => request.reject(new Error("request aborted")));
    requests.push(request);
    return promise;
  }
  const marker = "  window.tradeRouterChart = { open, close };";
  assert.ok(SCRIPT.includes(marker), "production chart export marker must exist");
  const context = {
    crypto: { randomUUID: () => "test-indicator" }, Date: ClockDate,
    URLSearchParams, AbortController, WebSocket: FakeWebSocket, fetch,
    location: { protocol: "http:", host: "chart.test" },
    localStorage: { setItem() {}, getItem() { return null; } },
    setInterval: (fn, delay) => schedule(fn, delay, true),
    clearInterval: id => timers.delete(id),
    setTimeout: (fn, delay) => schedule(fn, delay, false),
    clearTimeout: id => timers.delete(id),
    __render: value => renders.push(jsonCopy(value)),
    __draw: value => frames.push(jsonCopy(value)),
  };
  vm.runInNewContext(SCRIPT.replace(marker, `
    renderQuote = () => globalThis.__render(state.quote);
    drawChart = () => globalThis.__draw(state.bars);
    globalThis.__chart = { state, el, startRealtime, stopRealtime, refreshChart,
      connectStream, loadChart, close, indicatorSeries, heikinAshi };
    return;
${marker}`), context, { filename: SCRIPT_PATH });
  const chart = context.__chart;
  Object.assign(chart.el, {
    live: {}, empty: {}, tooltip: { hidden: true }, modal: { hidden: false },
    indicatorModal: { hidden: true }, title: {},
    source: { value: "paper-test" }, venue: { value: "KRX" },
    interval: { value: "3m" }, type: { value: "CANDLE" },
    search: { value: "005930", setAttribute() {} },
    searchResults: { replaceChildren() {}, hidden: true },
  });
  Object.assign(chart.state, {
    code: "005930", name: "test instrument", source: { id: "paper-test", realtime: true },
    bars: initialBars(), quote: { price: 102, market_time: new Date(now).toISOString() },
    loadGeneration: 1, loadedGeneration: 1, viewCount: 2, viewEnd: 3,
  });
  async function settle() {
    // Drain fetch -> JSON -> Promise.allSettled -> refresh and load continuations.
    for (let index = 0; index < 20; index += 1) await Promise.resolve();
  }
  async function advance(milliseconds) {
    const target = now + milliseconds;
    while (true) {
      const next = [...timers.entries()].filter(([, timer]) => timer.at <= target)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      const [id, timer] = next;
      now = timer.at;
      if (timer.repeat) timer.at += timer.delay;
      else timers.delete(id);
      timer.callback();
      await settle();
    }
    now = target;
    await settle();
  }
  function quote(price = 110, overrides = {}) {
    return { price, instrument: { code: chart.state.code }, market_time: new Date(now).toISOString(), ...overrides };
  }
  function start() {
    chart.startRealtime();
    sockets.at(-1)?.open();
    return sockets.at(-1);
  }
  function pending(endpoint) { return requests.filter(item => !item.settled && item.url.pathname === endpoint); }
  return { ...chart, requests, sockets, timers, renders, frames, quote, start, advance, settle, pending, now: () => now };
}

test("an open, subscribed socket without quotes automatically refreshes both quote and bars", async () => {
  const chart = createChart();
  const socket = chart.start();
  socket.message({ event_type: "subscribed" });
  assert.equal(chart.state.lastStreamAt, 0, "subscription acknowledgement is not a live price");
  assert.doesNotMatch(chart.el.live.className, /\blive\b$/);
  await chart.advance(10_000);
  assert.deepEqual(chart.requests.map(item => item.url.pathname).sort(), ["/api/chart/bars", "/api/chart/quote"]);
  for (const request of chart.requests) {
    assert.equal(request.url.searchParams.get("code"), "005930");
    assert.equal(request.url.searchParams.get("source"), "paper-test");
    assert.equal(request.url.searchParams.get("venue"), "KRX");
    assert.equal(request.options.cache, "no-store");
  }
  const barsQuery = chart.pending("/api/chart/bars")[0].url.searchParams;
  assert.equal(barsQuery.get("interval"), "3m");
  assert.ok(Number(barsQuery.get("count")) >= 20 && Number(barsQuery.get("count")) <= 400,
    "automatic polling must obey the bars API's supported count range");
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(108) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [bar(BASE_TIME - 60_000, { close: 107, high: 109, volume: 300 })] });
  await chart.settle();
  assert.equal(chart.state.quote.price, 108);
  assert.equal(chart.state.bars.at(-1).close, 107);
  assert.match(chart.el.live.className, /\bfallback$/);
  assert.equal(chart.renders.at(-1).price, 108);
  assert.equal(chart.frames.at(-1).at(-1).close, 107);
});

test("a live quote immediately updates the in-progress candle and live indicator", () => {
  const chart = createChart();
  const socket = chart.start();
  socket.message({ event_type: "quote", quote: chart.quote(112) });
  assert.equal(chart.requests.length, 0, "WebSocket update must not wait for a REST request");
  assert.equal(chart.state.quote.price, 112);
  assert.equal(chart.state.bars.at(-1).close, 112);
  assert.equal(chart.state.bars.at(-1).high, 112);
  assert.equal(chart.state.bars.at(-1).volume, 100, "unknown tick volume must not be invented");
  assert.equal(chart.frames.at(-1).at(-1).close, 112);
  assert.match(chart.el.live.className, /\blive$/);
});

test("invalid, stale, delayed, wrong-instrument, and out-of-order ticks are ignored", async t => {
  const invalid = [
    ["zero price", { price: 0 }], ["negative price", { price: -1 }],
    ["non-numeric price", { price: "invalid" }], ["invalid timestamp", { market_time: "invalid" }],
    ["different instrument", { instrument: { code: "000660" } }],
    ["stale flag", { stale: true }], ["delayed flag", { delayed: true }],
    ["old timestamp", { market_time: new Date(BASE_TIME - 30_000).toISOString() }],
    ["future timestamp", { market_time: new Date(BASE_TIME + 60_000).toISOString() }],
    ["out-of-order timestamp", { market_time: new Date(BASE_TIME - 1_000).toISOString() }],
  ];
  for (const [name, overrides] of invalid) {
    await t.test(name, () => {
      const chart = createChart();
      const socket = chart.start();
      socket.message({ event_type: "quote", quote: chart.quote(111) });
      const before = jsonCopy({ bars: chart.state.bars, quote: chart.state.quote, at: chart.state.lastStreamAt });
      socket.message({ event_type: "quote", quote: chart.quote(999, overrides) });
      assert.deepEqual(jsonCopy({ bars: chart.state.bars, quote: chart.state.quote, at: chart.state.lastStreamAt }), before);
      assert.equal(chart.frames.length, 1);
    });
  }
});

test("REST replaces OHLCV and indicator inputs without turning receipt time into a candle", async () => {
  const chart = createChart();
  chart.start();
  const refresh = chart.refreshChart();
  const originalTimes = chart.state.bars.map(item => item.open_time);
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(900, { market_time: null, received_at: "2026-10-22T09:01:00+09:00" }) });
  const corrected = { ...chart.state.bars.at(-1), open: "101", high: "120", low: "90", close: "115", volume: "500" };
  chart.pending("/api/chart/bars")[0].respond({ bars: [corrected] });
  await refresh;
  assert.deepEqual(Array.from(chart.state.bars, item => item.open_time), originalTimes);
  assert.deepEqual(jsonCopy(chart.state.bars.at(-1)), { ...corrected, open: 101, high: 120, low: 90, close: 115, volume: 500 });
  assert.equal(chart.state.quote.price, 900, "the price strip may display a REST quote independently");
  const transformed = chart.heikinAshi(chart.state.bars);
  const series = type => chart.indicatorSeries({ type, period: 2 }, transformed, chart.state.bars)[0].values;
  assert.deepEqual(Array.from(series("VOLUME")), [80, 90, 500]);
  assert.deepEqual(Array.from(series("OBV")), [0, 90, 590]);
  assert.equal(series("ATR").at(-1), 19.5, "ATR uses the corrected raw range: (9 + 30) / 2");
  assert.equal(chart.frames.at(-1).at(-1).volume, 500);
});

test("timer polling never overlaps an outstanding refresh, and resumes after it completes", async () => {
  const chart = createChart();
  chart.start();
  await chart.advance(10_000);
  assert.equal(chart.requests.length, 2);
  await chart.advance(10_000);
  await chart.refreshChart();
  assert.equal(chart.requests.length, 2, "next timer tick and manual refresh share the outstanding request");
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote() });
  chart.pending("/api/chart/bars")[0].respond({ bars: [] });
  await chart.settle();
  assert.equal(chart.state.refreshInFlight, null);
  await chart.advance(10_000);
  assert.equal(chart.requests.length, 4);
});

test("a timed-out refresh releases the poll lock and automatically retries", async () => {
  const chart = createChart();
  chart.start();
  await chart.advance(25_000);
  assert.equal(chart.requests.length, 2);
  assert.equal(chart.state.refreshInFlight, null);
  assert.equal(chart.state.refreshFailed, true);
  await chart.advance(5_000);
  assert.equal(chart.requests.length, 4);
});

test("late refresh results cannot overwrite a newly selected instrument, interval, or source", async t => {
  for (const selection of ["instrument", "interval", "source"]) {
    await t.test(selection, async () => {
      const chart = createChart();
      chart.start();
      const oldRefresh = chart.refreshChart();
      const oldRequests = [...chart.requests];
      if (selection === "instrument") chart.state.code = "000660";
      if (selection === "interval") chart.el.interval.value = "1m";
      if (selection === "source") chart.el.source.value = "another-paper-source";
      const load = chart.loadChart();
      const newBars = [bar(BASE_TIME - 60_000, { open: 200, high: 210, low: 190, close: 205, volume: 25 })];
      const source = { id: chart.el.source.value, realtime: true };
      chart.pending("/api/chart/data")[0].respond({ source, quote: chart.quote(205), bars: newBars });
      await load;
      const newSocket = chart.state.socket;
      const newRefresh = chart.refreshChart();
      const newToken = chart.state.refreshInFlight;
      oldRequests[0].respond({ quote: { price: 1, market_time: new Date(BASE_TIME).toISOString() } });
      oldRequests[1].respond({ bars: [bar(BASE_TIME - 60_000, { close: 1 })] });
      await oldRefresh;
      assert.equal(chart.state.quote.price, 205);
      assert.equal(chart.state.bars.at(-1).close, 205);
      assert.equal(chart.state.socket, newSocket);
      assert.equal(chart.state.refreshInFlight, newToken, "old finally must not release the new request lock");
      chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(206) });
      chart.pending("/api/chart/bars")[0].respond({ bars: [] });
      await newRefresh;
      assert.equal(chart.state.quote.price, 206);
    });
  }
});

test("closing the chart discards late refresh results and cancels future polling", async () => {
  const chart = createChart();
  const socket = chart.start();
  const refresh = chart.refreshChart();
  const before = jsonCopy({ bars: chart.state.bars, quote: chart.state.quote });
  chart.close();
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(999) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [bar(BASE_TIME - 60_000, { close: 999 })] });
  await refresh;
  socket.message({ event_type: "quote", quote: chart.quote(888) });
  socket.finishClose();
  await chart.advance(60_000);
  assert.deepEqual(jsonCopy({ bars: chart.state.bars, quote: chart.state.quote }), before);
  assert.equal(chart.requests.length, 2);
  assert.equal(chart.state.fallbackTimer, null);
  assert.equal(chart.state.socket, null);
  assert.equal(chart.el.modal.hidden, true);
  assert.equal(chart.frames.length, 0);
});

test("an old socket's close, error, and quote callbacks cannot damage its replacement", () => {
  const chart = createChart();
  const oldSocket = chart.start();
  chart.connectStream(chart.state.loadGeneration);
  const currentSocket = chart.state.socket;
  currentSocket.open();
  currentSocket.message({ event_type: "quote", quote: chart.quote(115) });
  oldSocket.finishClose();
  oldSocket.onerror?.({});
  oldSocket.message({ event_type: "quote", quote: chart.quote(999) });
  assert.equal(chart.state.socket, currentSocket);
  assert.equal(currentSocket.readyState, 1);
  assert.equal(chart.state.quote.price, 115);
  assert.equal(chart.state.lastStreamAt, chart.now());
  assert.match(chart.el.live.className, /\blive$/);
  assert.equal(chart.requests.length, 0);
});

test("a live quote received while REST is pending survives an older REST snapshot", async () => {
  const chart = createChart();
  const socket = chart.start();
  const refresh = chart.refreshChart();
  await chart.advance(1_000);
  socket.message({ event_type: "quote", quote: chart.quote(115) });
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(103, { market_time: new Date(BASE_TIME).toISOString() }) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [bar(BASE_TIME - 60_000, { close: 103, high: 105, volume: 500 })] });
  await refresh;
  assert.equal(chart.state.quote.price, 115);
  assert.equal(chart.state.bars.at(-1).close, 115);
  assert.equal(chart.state.bars.at(-1).high, 115);
  assert.equal(chart.state.bars.at(-1).volume, 500);
  assert.match(chart.el.live.className, /\blive$/);
});

test("a live tick in the same millisecond as REST starts still owns the displayed price", async () => {
  const chart = createChart();
  const socket = chart.start();
  const refresh = chart.refreshChart();
  socket.message({ event_type: "quote", quote: chart.quote(115) });
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(103) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [bar(BASE_TIME - 60_000, { close: 103 })] });
  await refresh;
  assert.equal(chart.state.quote.price, 115);
  assert.equal(chart.renders.at(-1).price, 115);
  assert.equal(chart.state.bars.at(-1).close, 115);
});

test("a delayed REST snapshot retains every intervening tick's high and low, and broker volume", async () => {
  const chart = createChart();
  const socket = chart.start();
  const refresh = chart.refreshChart();
  for (const price of [100, 120, 80, 110]) {
    await chart.advance(1);
    socket.message({ event_type: "quote", quote: chart.quote(price) });
  }
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(102) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [
    bar(BASE_TIME - 60_000, { open: 101, high: 105, low: 99, close: 102, volume: 500 }),
  ] });
  await refresh;
  const last = chart.state.bars.at(-1);
  assert.deepEqual([last.open, last.high, last.low, last.close, last.volume], [101, 120, 80, 110, 500]);
  assert.equal(chart.state.quote.price, 110);
});

test("a delayed snapshot retains ticks on both sides of a candle boundary", async () => {
  const chart = createChart();
  await chart.advance(119_000); // 09:02:59, one second before the next 3-minute candle.
  const socket = chart.start();
  const refresh = chart.refreshChart();
  socket.message({ event_type: "quote", quote: chart.quote(120) });
  await chart.advance(1_000);
  socket.message({ event_type: "quote", quote: chart.quote(130) });
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(105) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [
    bar(BASE_TIME - 60_000, { close: 103, high: 105, volume: 500, complete: true }),
    bar(BASE_TIME + 120_000, { open: 125, high: 126, low: 124, close: 125, volume: 30 }),
  ] });
  await refresh;
  const previous = chart.state.bars.at(-2);
  const current = chart.state.bars.at(-1);
  assert.deepEqual([previous.high, previous.close, previous.volume], [120, 120, 500]);
  assert.deepEqual([current.open, current.high, current.close, current.volume], [125, 130, 130, 30]);
  assert.equal(chart.state.quote.price, 130);
  assert.equal(chart.state.bars.length, 4);
});

test("REST merging preserves a historical viewport, crosshair, and drag anchor", async () => {
  const chart = createChart();
  chart.state.viewEnd = 2;
  chart.state.crosshair = { index: 1, y: 40 };
  chart.state.drag = { startEnd: 2, startX: 40, step: 5 };
  const anchor = chart.state.bars[1].open_time;
  chart.start();
  const refresh = chart.refreshChart();
  chart.pending("/api/chart/quote")[0].respond({ quote: chart.quote(113) });
  chart.pending("/api/chart/bars")[0].respond({ bars: [
    bar(BASE_TIME - 10 * 60_000),
    bar(BASE_TIME - 60_000, { close: 110 }),
    bar(BASE_TIME + 2 * 60_000, { close: 113 }),
  ] });
  await refresh;
  assert.equal(chart.state.viewCount, 2);
  assert.equal(chart.state.bars[chart.state.viewEnd - 1].open_time, anchor);
  assert.equal(chart.state.bars[chart.state.crosshair.index].open_time, anchor);
  assert.equal(chart.state.viewEnd, 3);
  assert.equal(chart.state.drag.startEnd, 3);
  assert.equal(chart.state.crosshair.y, 40);
});

test("new live candles keep a scrolled viewport and move only a viewport following the latest", async t => {
  for (const following of [false, true]) {
    await t.test(following ? "following latest" : "viewing historical bars", async () => {
      const chart = createChart();
      chart.state.viewEnd = following ? 3 : 2;
      const socket = chart.start();
      await chart.advance(2 * 60_000);
      socket.message({ event_type: "quote", quote: chart.quote(112) });
      assert.equal(chart.state.bars.length, 4);
      assert.equal(chart.state.viewCount, 2);
      assert.equal(chart.state.viewEnd, following ? 4 : 2);
    });
  }
});
