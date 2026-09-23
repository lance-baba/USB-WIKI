/* Citation Precision Highlight 回归（纯 Citation UX patch）
 * 运行：
 *   NODE_PATH=<workspace>/node_modules node tests/test_citation_highlight.mjs
 * 覆盖 spec A–F：
 *   A. 回答几乎逐字引用原文 → 整句黄色命中（exact）
 *   B. 回答轻度改写        → overlap 命中正确原句
 *   C. 同 parent 多个「19日」→ 选与 clicked claim 最相关的一句
 *   D. 一句跨 <b>/<span>/文本节点 → 仍可命中（不丢字）
 *   E. 匹配失败 → 正常降级（terms + 整块），citation 仍定位正确 parent
 *   F. 连续两轮后点第一轮 [1] → 只用第一轮自己的 claim/ref（turn-scoped）
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
let PASS = 0, FAIL = 0;
const ok = (name, cond, detail = "") => {
  if (cond) { PASS++; console.log("  PASS  " + name); }
  else { FAIL++; console.log("  FAIL  " + name + (detail ? "  [" + detail + "]" : "")); }
};

process.on("unhandledRejection", () => {});   // 顶层 init 的异步失败不应打断测试

/* ---------------- 加载真实 app.js 到 jsdom ---------------- */
const html = readFileSync(path.join(ROOT, "app/web/index.html"), "utf8");
const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
const doc = window.document;
// jsdom 未实现 scrollIntoView（真实浏览器有）；补桩，否则集成用例会误报
if (window.Element && window.Element.prototype) window.Element.prototype.scrollIntoView = function () {};

// fetch 桩：/api/notes/evidence 返回证据块；其余返回空成功
const NOTE_TEXT = "在冷空气方面，预计9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温。广州未来7天最低气温持续在25度以上，而且19-21日甚至还报出最高气温可能达到35℃。";
window.fetch = (url) => {
  if (String(url).includes("/api/notes/evidence")) {
    return Promise.resolve({ json: () => Promise.resolve({ code: 200, data: {
      content: NOTE_TEXT, source_start_line: 1, source_end_line: 40 } }) });
  }
  return Promise.resolve({ json: () => Promise.resolve({ code: 200, data: {} }) });
};

// 缺失元素兜底：index.html 若缺某 id，顶层 `$("#x").onclick=` 不应崩
const realQS = doc.querySelector.bind(doc);
const dummy = () => ({ style: {}, dataset: {},
  classList: { add() {}, remove() {}, contains() { return false; }, toggle() {}, replace() {} },
  children: [], childNodes: [], value: "", textContent: "", innerHTML: "", checked: false, disabled: false,
  addEventListener() {}, removeEventListener() {}, appendChild() { return this; }, removeChild() {},
  querySelector() { return null; }, querySelectorAll() { return []; },
  setAttribute() {}, getAttribute() { return null; }, focus() {}, select() {}, click() {} });
doc.querySelector = (s) => realQS(s) || dummy();

const src = readFileSync(path.join(ROOT, "app/web/app.js"), "utf8");
const epilogue = "\n;globalThis.__cph = { _claimTextOf, _claimNorm, _sentSpans, _claimTokens,"
  + " _matchEvidenceSentence, _wrapRange, jumpToEvidence, _evidencePhrases };\n";
vm.runInContext(src + epilogue, dom.getInternalVMContext(), { filename: "app.js" });
const T = window.__cph;
if (!T || typeof T._matchEvidenceSentence !== "function") {
  console.log("  FAIL  加载 app.js 失败（未导出函数）");
  process.exit(1);
}

/* ---------------- A / B / C / E：纯匹配逻辑 ---------------- */
console.log("\n== A/B/C/E：region 内 claim 匹配 ==");
const SENT = "9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温";

// A. 逐字引用 → exact
{
  const blocks = [SENT + "。"];
  const hit = T._matchEvidenceSentence(blocks, SENT);
  ok("A 逐字引用 → exact 整句命中",
    !!hit && hit.mode === "exact" && blocks[0].slice(hit.start, hit.end) === SENT,
    hit ? hit.mode : "null");
}
// B. 轻度改写（插入「又」）→ overlap 命中原句
{
  const blocks = [SENT + "。"];
  const claim = "9月19日新一轮冷空气又将开启，届时东北多地将会出现8度以上的降温";
  const hit = T._matchEvidenceSentence(blocks, claim);
  ok("B 轻度改写 → overlap 命中正确原句",
    !!hit && hit.mode === "overlap" && blocks[0].slice(hit.start, hit.end) === SENT,
    hit ? hit.mode + ":" + hit.score.toFixed(2) : "null");
}
// C. 同 parent 多个「19日」→ 选与 claim 最相关的一句
{
  const guangzhou = "广州未来7天最低气温持续在25度以上，而且19-21日甚至还报出最高气温可能达到35℃";
  const cold = "预计9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温";
  const blocks = [guangzhou + "。" + cold + "。"];
  const claim = "9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温";
  const hit = T._matchEvidenceSentence(blocks, claim);
  const slice = hit ? blocks[0].slice(hit.start, hit.end) : "";
  ok("C 多「19日」→ 选含冷空气/降温的那句",
    !!hit && slice.includes("冷空气") && !slice.includes("广州"), slice.slice(0, 24));
}
// E-1. 无关 claim → 无匹配（降级信号）
{
  const blocks = ["今天东京股市大涨，日经指数上涨2%。"];
  const hit = T._matchEvidenceSentence(blocks, "9月19日新一轮冷空气将开启");
  ok("E-1 无关 claim → 返回 null（触发降级）", hit === null, hit ? JSON.stringify(hit) : "");
}

/* ---------------- D：跨节点整句高亮 ---------------- */
console.log("\n== D：一句跨 <b>/<span>/文本节点 ==");
{
  const box = doc.createElement("div");
  box.innerHTML = "预计<b>9月19日</b>新一轮冷空气将开启，届时<span>东北多地将会</span>出现8度以上的降温。";
  const before = box.textContent;
  const hit = T._matchEvidenceSentence([before], SENT);
  ok("D-0 跨节点文本仍能匹配到整句", !!hit, hit ? hit.mode : "null");
  if (hit) {
    const m = T._wrapRange(box, hit.start, hit.end, "ev-claim");
    const marks = box.querySelectorAll("mark.ev-claim");
    const marked = Array.from(marks).map(x => x.textContent).join("");
    ok("D-1 生成 mark.ev-claim", !!m && marks.length > 0);
    ok("D-2 高亮文字 == 命中整句", marked === before.slice(hit.start, hit.end),
      JSON.stringify(marked.slice(0, 20)));
    ok("D-3 不丢字（textContent 不变）", box.textContent === before);
  } else { ok("D-1/2/3 跨节点", false, "no hit"); }
}

/* ---------------- E-2 / F-2：jumpToEvidence 端到端（含降级） ---------------- */
console.log("\n== E-2/F：jumpToEvidence 集成（降级 + 两轮隔离） ==");
let view = doc.getElementById("noteView");
if (!view) { view = doc.createElement("div"); view.id = "noteView"; doc.body.appendChild(view); }
const noteHtml = '<div data-src-start="1" data-src-end="40"><p>' + NOTE_TEXT + "</p></div>";
const blocks = () => Array.from(view.children).filter(b => b.dataset.srcStart);
const jumpBase = { parent_id: "p1", snippet: "", source_start_line: 1, source_end_line: 40 };

// E-2：claim/snippet 都匹配不到 → 降级为 terms + 整块（不退步）
{
  view.innerHTML = noteHtml;
  await T.jumpToEvidence("x.md", Object.assign({}, jumpBase, { claim: "完全无关的一句话内容" }),
    null, ["冷空气", "降温"]);
  ok("E-2a 降级：无 ev-claim", view.querySelectorAll("mark.ev-claim").length === 0);
  ok("E-2b 降级：region 仍高亮 + terms 弱标", blocks().some(b => b.classList.contains("ev-hl"))
    && view.querySelectorAll("mark.ev-ev").length > 0);
  ok("E-2c 降级：citation 仍定位到正确 parent（有 region 块）", blocks().length === 1);
}

// F-2：第一轮 claim（冷空气）→ 第二轮 claim（广州）→ 只保留第二轮
{
  view.innerHTML = noteHtml;
  await T.jumpToEvidence("x.md", Object.assign({}, jumpBase,
    { claim: "9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温" }), null, []);
  const m1 = view.querySelectorAll("mark.ev-claim");
  const t1 = m1.length ? m1[0].textContent : "";
  await T.jumpToEvidence("x.md", Object.assign({}, jumpBase,
    { claim: "广州未来7天最低气温持续在25度以上" }), null, []);
  const m2 = view.querySelectorAll("mark.ev-claim");
  const t2 = m2.length ? m2[0].textContent : "";
  ok("F-2a 第一轮高亮冷空气句", t1.includes("冷空气"));
  ok("F-2b 第二轮高亮广州句", t2.includes("广州"));
  ok("F-2c 切换后旧高亮被清除（仅剩 1 处）", m2.length === 1, "count=" + m2.length);
}

/* ---------------- F-1：claim 提取turn-scoped ---------------- */
console.log("\n== F-1：claim 从被点击角标所在段落取得（turn-scoped） ==");
{
  const b1 = doc.createElement("p");
  b1.innerHTML = "9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温 <span class=\"cite\" data-ref=\"1\">1</span>";
  const b2 = doc.createElement("p");
  b2.innerHTML = "广州未来7天最低气温持续在25度以上 <span class=\"cite\" data-ref=\"1\">1</span>";
  const c1 = T._claimTextOf(b1.querySelector(".cite"));
  const c2 = T._claimTextOf(b2.querySelector(".cite"));
  ok("F-1a 第一轮 claim == 角标之前正文（不含角标自身）",
    c1.trim() === "9月19日新一轮冷空气将开启，届时东北多地将会出现8度以上的降温", JSON.stringify(c1));
  ok("F-1b 第二轮 claim == 角标之前正文（不含角标自身）",
    c2.trim() === "广州未来7天最低气温持续在25度以上", JSON.stringify(c2));
  ok("F-1c 两轮互不串号", c1 !== c2 && !c2.includes("冷空气"));
}

console.log("\n" + "=".repeat(52));
console.log("TOTAL pass=" + PASS + " fail=" + FAIL);
process.exit(FAIL ? 1 : 0);
