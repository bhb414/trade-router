const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8");
const start = source.indexOf("async function exportDiagnostics(");
const end = source.indexOf("\nfunction activateSettingsTab(", start);
assert.ok(start > 0 && end > start);

function harness(response) {
  const nodes = Object.fromEntries(["diagnostics-export-button", "diagnostics-hours", "diagnostics-status"].map(id => [id, { disabled: false, textContent: "", value: "72" }]));
  const calls = [], downloads = [], notices = [], timers = [], revoked = [];
  const context = vm.createContext({
    AbortController,
    document: {
      querySelector: selector => nodes[selector.slice(1)],
      body: { append: () => {} },
      createElement: () => ({ click() { downloads.push(this.download); }, remove() {} }),
    },
    URL: { createObjectURL: () => "blob:local-diagnostic", revokeObjectURL: value => revoked.push(value) },
    window: { setTimeout: (callback, ms) => { timers.push({ callback, ms }); return timers.length; }, clearTimeout: () => {} },
    fetch: async (...args) => { calls.push(args); return typeof response === "function" ? response(...args) : response; },
    notify: (...args) => notices.push(args),
  });
  vm.runInContext(source.slice(start, end), context);
  return { ...context, nodes, calls, downloads, notices, timers, revoked };
}
function zipResponse(filename = 'trade-router-diagnostics-20261022-120000.zip') {
  return { ok: true, headers: { get: key => key === "Content-Type" ? "application/zip" : `attachment; filename="${filename}"` }, blob: async () => ({ size: 100 }) };
}

test("diagnostics download stays local and sends only the selected time range", async () => {
  const h = harness(zipResponse());
  h.nodes["diagnostics-hours"].value = "24";
  await h.exportDiagnostics();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "/api/system/diagnostics/export?hours=24");
  assert.equal(h.calls[0][1].credentials, "same-origin");
  assert.equal(h.calls[0][1].body, undefined);
  assert.deepEqual(h.downloads, ["trade-router-diagnostics-20261022-120000.zip"]);
  assert.match(h.nodes["diagnostics-status"].textContent, /문제가 생긴 시각/);
  assert.equal(h.nodes["diagnostics-export-button"].disabled, false);
  h.timers.find(t => t.ms === 30000).callback();
  assert.deepEqual(h.revoked, ["blob:local-diagnostic"]);
});

test("a failed export cannot download an error response or expose its raw contents", async () => {
  const h = harness({ ok: false, text: async () => "SECRET-ACCOUNT-NUMBER" });
  await h.exportDiagnostics();
  assert.equal(h.downloads.length, 0);
  assert.equal(h.notices.length, 1);
  assert.doesNotMatch(h.nodes["diagnostics-status"].textContent, /SECRET/);
  assert.equal(h.nodes["diagnostics-hours"].disabled, false);
});

test("invalid range and unsafe server filenames fall back to safe defaults", async () => {
  const h = harness(zipResponse("../../private-account.txt"));
  h.nodes["diagnostics-hours"].value = "all&secret=true";
  await h.exportDiagnostics();
  assert.equal(h.calls[0][0], "/api/system/diagnostics/export?hours=72");
  assert.deepEqual(h.downloads, ["trade-router-diagnostics.zip"]);
});

test("repeat clicks cannot start a second export while one is preparing", async () => {
  let finish;
  const h = harness(() => new Promise(resolve => { finish = resolve; }));
  const pending = h.exportDiagnostics();
  await h.exportDiagnostics();
  assert.equal(h.calls.length, 1);
  finish(zipResponse());
  await pending;
  assert.equal(h.downloads.length, 1);
});

test("timeout resets controls and suggests a smaller time range", async () => {
  const h = harness(async () => { throw Object.assign(new Error("private detail"), { name: "AbortError" }); });
  await h.exportDiagnostics();
  assert.match(h.nodes["diagnostics-status"].textContent, /기록 기간을 줄여/);
  assert.equal(h.nodes["diagnostics-export-button"].disabled, false);
  assert.equal(h.downloads.length, 0);
});
