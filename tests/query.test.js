"use strict";

// 验证「只读 SQL 工作台」的前端行为：
// 本地校验（空 / 超长不发请求）、提交在途禁用、运行中只能取消、成功渲染
// 列名 + 前 100 行 + 截断标志、失败如实显示错误名 / 码、取消流向正确 URL、
// 400 如实显示、409（运行中再提交）与旧查询 ID（404）本地不猜测并立即以
// 服务端状态重核对（旧查询 ID 不自动新建）、新建进行中禁用、删除后冻结、
// 旧实验迟到响应不影响新实验、410 冻结。
// 响应顺序完全可控，不依赖真实时间与网络。
// 运行：node tests/query.test.js

const {
  loadApp,
  flush,
  statusBody,
  bootOldLive,
  settleNewCreated,
} = require("./harness");

let passed = 0;
let failed = 0;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}
function assertEq(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}）`);
  }
}
async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log("  ✓ " + name);
  } catch (e) {
    failed++;
    console.error("  ✗ " + name);
    console.error("      " + e.message);
  }
}

const RUNNING_VIEW = { id: "q1", status: "running", sql: "SELECT * FROM kv", cancel_requested: false };
const SUCCESS_VIEW = {
  id: "q1",
  status: "success",
  sql: "SELECT * FROM kv",
  columns: ["id", "val", "note"],
  rows: [
    [1, 100, "hello"],
    [2, "9007199254741093", null],
  ],
  truncated: false,
};
const FAILED_VIEW = {
  id: "q1",
  status: "failed",
  sql: "SELECT * FROM kv",
  error: { name: "SQLITE_ERROR", code: 1, message: "no such table: kv2" },
};
const CANCELLED_VIEW = { id: "q1", status: "cancelled", sql: "SELECT * FROM kv" };

// 提取结果表格：[[表头...], [行1...], ...]
function tableContent(ctx) {
  const result = ctx.el("query-result")._children;
  if (result.length === 0) return null;
  const table = result[0];
  const rows = [];
  for (const section of table._children) {
    for (const tr of section._children) {
      rows.push(tr._children.map((cell) => cell.textContent));
    }
  }
  return rows;
}

async function main() {
  await test("存活页控件可用；空 / 超长 SQL 不发请求并显示错误", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    assertEq(ctx.el("query-input").disabled, false, "SQL 输入可用");
    assertEq(ctx.el("query-submit").disabled, false, "提交按钮可用");
    assertEq(ctx.el("query-cancel").disabled, true, "无运行中查询时取消禁用");
    assertEq(ctx.el("query-state").textContent, "尚未提交查询", "初始状态文案");

    for (const bad of ["", "   ", "x".repeat(4001)]) {
      const before = ctx.calls.length;
      ctx.el("query-input").value = bad;
      ctx.el("query-submit").click();
      await flush();
      assertEq(ctx.calls.length, before, "非法输入不得发请求");
      assertEq(ctx.el("error-query").hidden, false, "非法输入显示错误");
    }
  });

  await test("提交成功：请求体、在途禁用、running 渲染；轮询带回成功渲染表格", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT * FROM kv";
    ctx.el("query-submit").click();
    await flush();

    const call = ctx.findCall("/OLD/query", "POST");
    assert(!!call, "应发出提交请求");
    assertEq(call.body, '{"sql":"SELECT * FROM kv"}', "请求体为 {sql}");
    assertEq(ctx.el("query-input").disabled, true, "在途时输入禁用");
    assertEq(ctx.el("query-submit").disabled, true, "在途时提交禁用");
    assertEq(ctx.el("query-cancel").disabled, true, "在途时取消禁用");

    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ ok: true, query: RUNNING_VIEW }) });
    await flush();
    assertEq(ctx.el("query-state").textContent, "查询 q1：运行中", "运行中文案");
    assert(ctx.el("query-state").classList.contains("running"), "运行中样式");
    assertEq(ctx.el("query-submit").disabled, true, "运行中提交禁用");
    assertEq(ctx.el("query-input").disabled, true, "运行中输入禁用");
    assertEq(ctx.el("query-cancel").disabled, false, "运行中取消可用");

    // 轮询带回成功视图：渲染列名、行（大整数字符串、NULL）与行数
    ctx.tickTimers(1);
    await flush();
    ctx.settle("/OLD/status", 200, statusBody("OLD", { query: SUCCESS_VIEW }));
    ctx.settle("/OLD/committed", 200, { committed: 111 });
    await flush();
    assertEq(ctx.el("query-state").textContent, "查询 q1：成功", "成功文案");
    const rows = tableContent(ctx);
    assertEq(rows.length, 3, "表头 + 两行");
    assertEq(rows[0].join(","), "id,val,note", "列名行");
    assertEq(rows[1].join(","), "1,100,hello", "第一行");
    assertEq(rows[2].join(","), "2,9007199254741093,NULL", "大整数字符串与 NULL");
    const foot = ctx.el("query-result")._children[1];
    assertEq(foot.textContent, "共 2 行", "未截断行数");
    assertEq(ctx.el("query-submit").disabled, false, "终结后提交恢复");
    assertEq(ctx.el("query-cancel").disabled, true, "终结后取消禁用");
  });

  await test("截断标志与失败视图如实渲染", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT x FROM c";
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 200, { ok: true, query: RUNNING_VIEW }, "POST");
    await flush();
    ctx.tickTimers(1);
    await flush();
    const truncatedView = Object.assign({}, SUCCESS_VIEW, {
      rows: SUCCESS_VIEW.rows,
      truncated: true,
    });
    ctx.settle("/OLD/status", 200, statusBody("OLD", { query: truncatedView }));
    ctx.settle("/OLD/committed", 200, { committed: 111 });
    await flush();
    assertEq(
      ctx.el("query-result")._children[1].textContent,
      "结果已截断：仅返回前 2 行",
      "截断标志渲染"
    );

    // 失败视图：错误名 / 错误码如实显示
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 200, { ok: true, query: RUNNING_VIEW }, "POST");
    await flush();
    ctx.tickTimers(1);
    await flush();
    ctx.settle("/OLD/status", 200, statusBody("OLD", { query: FAILED_VIEW }));
    ctx.settle("/OLD/committed", 200, { committed: 111 });
    await flush();
    assertEq(ctx.el("query-state").textContent, "查询 q1：失败", "失败文案");
    assert(
      ctx.el("query-result")._children[0].textContent.includes("SQLITE_ERROR（错误码 1）"),
      "失败显示错误名与错误码"
    );
  });

  await test("取消：流向正确 URL，已取消视图渲染；取消已确认后不再重复取消", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT * FROM kv";
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 200, { ok: true, query: RUNNING_VIEW }, "POST");
    await flush();

    ctx.el("query-cancel").click();
    await flush();
    const call = ctx.findCall("/OLD/query/q1/cancel", "POST");
    assert(!!call, "应发出取消请求（携带查询 ID）");
    assertEq(ctx.el("query-cancel").disabled, true, "取消在途时禁用");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ ok: true, query: CANCELLED_VIEW }) });
    await flush();
    assertEq(ctx.el("query-state").textContent, "查询 q1：已取消", "已取消文案");
    assert(
      ctx.el("query-result")._children[0].textContent.includes("查询已取消"),
      "已取消说明渲染"
    );
    assertEq(ctx.el("query-cancel").disabled, true, "已取消后取消禁用");
    assertEq(ctx.el("query-submit").disabled, false, "已取消后提交恢复");

    // 取消已确认（轮询先带回 cancel_requested）：取消按钮禁用、点击不发请求
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 200, { ok: true, query: RUNNING_VIEW }, "POST");
    await flush();
    ctx.tickTimers(1);
    await flush();
    ctx.settle("/OLD/status", 200, statusBody("OLD", {
      query: Object.assign({}, RUNNING_VIEW, { cancel_requested: true }),
    }));
    ctx.settle("/OLD/committed", 200, { committed: 111 });
    await flush();
    assert(
      ctx.el("query-state").textContent.includes("取消已确认"),
      "取消已确认文案"
    );
    assertEq(ctx.el("query-cancel").disabled, true, "取消已确认后取消禁用");
    const before = ctx.calls.length;
    ctx.el("query-cancel").click();
    await flush();
    assertEq(ctx.calls.length, before, "取消已确认后点击不发请求");
  });

  await test("400 校验拒绝：如实显示服务端原因，控件恢复", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "PRAGMA journal_mode";
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 400, { error: "forbidden keyword: pragma" }, "POST");
    await flush();
    assert(
      ctx.el("error-query").textContent.includes("forbidden keyword: pragma"),
      "400 显示服务端原因"
    );
    assertEq(ctx.el("query-submit").disabled, false, "400 后提交恢复");
    assertEq(ctx.el("query-state").textContent, "尚未提交查询", "400 不产生查询视图");
  });

  await test("409 运行中再提交：显示并立即以服务端状态重核对", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT 1";
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 409, {
      error: "a query is already running",
      query: RUNNING_VIEW,
    }, "POST");
    await flush();
    assert(
      ctx.el("error-query").textContent.includes("a query is already running"),
      "409 显示拒绝原因"
    );
    const statusCall = ctx.findCall("/OLD/status", "GET");
    assert(!!statusCall, "409 后应立即重新拉取状态");
    statusCall.settled = true;
    statusCall.resolve({
      status: 200,
      ok: true,
      json: async () => statusBody("OLD", { query: RUNNING_VIEW }),
    });
    await flush();
    assertEq(ctx.el("query-state").textContent, "查询 q1：运行中", "重核对后按服务端视图渲染");
    assertEq(ctx.el("query-submit").disabled, true, "运行中提交保持禁用");
    assertEq(ctx.el("query-cancel").disabled, false, "运行中取消可用");
  });

  await test("旧查询 ID 取消返回 404：不自动新建，显示并重核对", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT 1";
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 200, { ok: true, query: RUNNING_VIEW }, "POST");
    await flush();
    ctx.el("query-cancel").click();
    await flush();
    ctx.settle("/OLD/query/q1/cancel", 404, { error: "query not found" }, "POST");
    await flush();
    assert(!ctx.findCall("/api/experiments", "POST"), "旧查询 ID 404 不自动新建");
    assert(
      ctx.el("error-query").textContent.includes("query not found"),
      "404 显示原因"
    );
    const statusCall = ctx.findCall("/OLD/status", "GET");
    assert(!!statusCall, "404 后应立即重新拉取状态");
    statusCall.settled = true;
    statusCall.resolve({
      status: 200,
      ok: true,
      json: async () => statusBody("OLD", { query: null }),
    });
    await flush();
    assertEq(ctx.el("query-state").textContent, "尚未提交查询", "重核对后查询已被替换");
  });

  await test("新建进行中禁用工作台，成功后恢复并清空", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT 1";
    ctx.el("new-exp").click();
    await flush();
    assertEq(ctx.el("query-input").disabled, true, "新建中输入禁用");
    assertEq(ctx.el("query-submit").disabled, true, "新建中提交禁用");
    assertEq(ctx.el("query-cancel").disabled, true, "新建中取消禁用");
    const before = ctx.calls.length;
    ctx.el("query-submit").click();
    await flush();
    assertEq(ctx.calls.length, before, "新建中点击不发请求");

    await settleNewCreated(ctx);
    assertEq(ctx.el("query-input").disabled, false, "新建成功后输入恢复");
    assertEq(ctx.el("query-submit").disabled, false, "新建成功后提交恢复");
    assertEq(ctx.el("query-input").value, "", "新建后输入已清空");
    assertEq(ctx.el("query-state").textContent, "尚未提交查询", "新建后查询区重置");
  });

  await test("删除后冻结：工作台禁用且点击不发请求", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("delete-exp").click();
    await flush();
    ctx.settle("/api/experiments/OLD", 200, { id: "OLD", deleted: true }, "DELETE");
    await flush();
    assert(ctx.app().expFrozen, "删除后应冻结");
    assertEq(ctx.el("query-input").disabled, true, "冻结后输入禁用");
    assertEq(ctx.el("query-submit").disabled, true, "冻结后提交禁用");
    assertEq(ctx.el("query-cancel").disabled, true, "冻结后取消禁用");
    const before = ctx.calls.length;
    ctx.el("query-submit").click();
    ctx.el("query-cancel").click();
    await flush();
    assertEq(ctx.calls.length, before, "冻结后点击不发请求");
  });

  await test("旧实验的迟到查询响应不影响新实验", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT 1";
    ctx.el("query-submit").click();
    await flush();
    assert(!!ctx.findCall("/OLD/query", "POST"), "OLD 提交请求在途");

    // 提交在途期间手动新建 NEW 并完成
    ctx.el("new-exp").click();
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "已切换到新实验");

    // 迟到放行 OLD 的提交响应：不得污染 NEW 的展示与控件
    ctx.settle("/OLD/query", 200, { ok: true, query: RUNNING_VIEW }, "POST");
    await flush();
    assertEq(ctx.el("query-state").textContent, "尚未提交查询", "NEW 查询区不被旧响应污染");
    assertEq(ctx.el("query-submit").disabled, false, "NEW 提交保持可用");
    assertEq(ctx.el("error-query").hidden, true, "旧响应不显示错误");
  });

  await test("查询请求返回 410：冻结且不自动新建", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("query-input").value = "SELECT 1";
    ctx.el("query-submit").click();
    await flush();
    ctx.settle("/OLD/query", 410, { error: "experiment gone" }, "POST");
    await flush();
    assert(ctx.app().expFrozen, "410 后应冻结");
    assert(!ctx.findCall("/api/experiments", "POST"), "410 不自动新建");
    assertEq(ctx.el("query-submit").disabled, true, "410 后提交禁用");
    assertEq(ctx.el("query-cancel").disabled, true, "410 后取消禁用");
  });

  console.log(`\n${passed} 通过，${failed} 失败`);
  if (failed > 0) process.exit(1);
}

main();
