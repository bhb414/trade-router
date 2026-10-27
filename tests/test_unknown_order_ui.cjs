const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const staticRoot = path.join(
  __dirname,
  "../apps/studio/src/trade_router_studio/static",
);
const source = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");

test("unknown order recovery reads the wrapped service response", () => {
  assert.match(source, /const reconciledOrder = result\.order \|\| row/);
  assert.match(source, /result\.resolved \|\| reconciledOrder\.status/);
  assert.match(source, /\/api\/runtime\/orders\/\$\{encodeURIComponent\(row\.id\)\}\/reconcile/);
});

test("manual recovery requires explicit HTS confirmations", () => {
  assert.match(source, /confirmation: "HTS_LINK_CONFIRMED"/);
  assert.match(source, /confirmation: "HTS_NOT_ACCEPTED"/);
  assert.match(html, /HTS에서 해당 주문이 접수되지 않았음을 직접 확인했습니다/);
  assert.match(html, /실제로 접수된 주문을 미접수로 처리하면 중복 주문/);
});

test("unknown orders expose a recovery action without automatic resubmission", () => {
  assert.match(source, /row\.status === "UNKNOWN_RECONCILING"/);
  assert.match(source, /resolve\.textContent = "주문 확인·해결"/);
  assert.match(source, /http_status_unknown: "증권사 서버 오류로 접수 결과를 확정하지 못함"/);
  assert.match(source, /technicalParts\.push\(`오류 \$\{metadata\.exception_type\}`\)/);
  assert.match(source, /technicalParts\.push\(`WinError \$\{winerror\}`\)/);
  assert.match(source, /technicalParts\.push\(`HTTP \$\{httpStatus\}`\)/);
  assert.match(source, /technicalParts\.push\(`자동 대조 \$\{attempts\}회`\)/);
  assert.doesNotMatch(source, /reconcileSelectedOrder[\s\S]{0,1200}submit_order/);
});

test("account-wide unresolved orders stay reachable from every strategy", () => {
  assert.match(source, /unknown_only: "true"/);
  assert.match(source, /accountUnknownQuery\.set\("account_alias", accountAlias\)/);
  assert.match(source, /const rows = \[\.\.\.accountUnknown, \.\.\.strategyRows\]/);
  assert.match(source, /seen\.has\(row\.id\)/);
  assert.match(source, /다른 전략에서 발생한 확인 필요 주문/);
  assert.match(source, /계좌: \$\{row\.account_alias/);
  assert.match(source, /await refreshStrategySummary\(\);[\s\S]*await Promise\.all\(\[refreshLogs\(\), refreshOrders\(\)\]\)/);
  assert.match(source, /state\.strategySummary\?\.strategy_id[\s\S]*\|\| state\.strategyPermission\?\.strategy_id/);
});

test("uncertain amendment recovery exposes a verified child-order link", () => {
  assert.match(source, /const modificationKind = row\.provider_order_id/);
  assert.match(source, /정정·취소 미접수 확인/);
  assert.match(source, /원주문 상태로 복원/);
  assert.match(source, /const uncertainAmendment = modificationKind === "amend"/);
  assert.match(source, /새 정정 주문번호와 연결/);
  assert.match(source, /elements\.orderResolutionLinkSection\.hidden = uncertainModification && !uncertainAmendment/);
  assert.match(source, /요청한 \$\{uncertainAmendment \? "정정" : "취소"\}/);
  assert.match(source, /이번 대조 후보 \$\{state\.orderResolutionCandidates\.length\}건/);
  assert.match(source, /orderResolutionCandidateLine\(candidate\)/);
  assert.match(source, /openOrderResolution\(reconciledOrder, result\.candidates \|\| \[\]\)/);
  assert.match(html, /id="order-resolution-link-section"/);
  assert.match(html, /id="order-resolution-link-title"/);
});
