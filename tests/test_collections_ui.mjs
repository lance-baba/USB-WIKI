/* Collections 前端回归（jsdom，纯管理视图逻辑）
 * 运行：NODE_PATH=<workspace>/node_modules node tests/test_collections_ui.mjs
 * 覆盖：分类下拉填充 / 列表项 ⋯ 按钮 / 显示标题覆盖 / 类目标签 / 分类筛选 /
 *        回收站视图 / ⋯ 菜单（正常 3 项 · 回收站 1 项）
 * 只验证「组织 / 浏览」逻辑；不触碰检索与正文。
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
process.on("unhandledRejection", () => {});

const html = readFileSync(path.join(ROOT, "app/web/index.html"), "utf8");
const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
const doc = window.document;
if (window.Element && window.Element.prototype) window.Element.prototype.scrollIntoView = function () {};

const NOTES = [
  { doc_id: "d1", rel_path: "notes/a.md", display_source: "来源A", title: "标题A", chunks: 2 },
  { doc_id: "d2", rel_path: "notes/b.md", display_source: "来源B", title: "标题B", chunks: 1 },
];
const COLL = {
  collections: [{ id: "c1", name: "工程监测" }, { id: "c2", name: "投资研究" }],
  membership: { d1: ["c1"] },
  display_titles: { d1: "自定义标题A" },
  trash: [],
};
window.fetch = (url) => {
  const u = String(url);
  if (u.includes("/api/collections")) return Promise.resolve({ json: () => Promise.resolve({ code: 200, data: COLL }) });
  if (u.includes("/api/notes")) return Promise.resolve({ json: () => Promise.resolve({ code: 200, data: NOTES }) });
  return Promise.resolve({ json: () => Promise.resolve({ code: 200, data: {} }) });
};

const realQS = doc.querySelector.bind(doc);
const dummy = () => ({
  style: {}, dataset: {}, classList: { add() {}, remove() {}, contains() { return false; }, toggle() {}, replace() {} },
  children: [], childNodes: [], value: "", textContent: "", innerHTML: "", checked: false, disabled: false,
  addEventListener() {}, removeEventListener() {}, appendChild() { return this; }, removeChild() {},
  querySelector() { return null; }, querySelectorAll() { return []; },
  setAttribute() {}, getAttribute() { return null; }, focus() {}, select() {}, click() {},
});
doc.querySelector = (s) => realQS(s) || dummy();

const src = readFileSync(path.join(ROOT, "app/web/app.js"), "utf8");
const epilogue = "\n;globalThis.__coll = { renderNoteList, loadCollections, loadNotes,"
  + " docDisplayTitle, docCollections, isTrashed, openNoteMenu, _closePop, renderCollectionFilter,"
  + " editNoteCollections, editNoteTitle };\n";
vm.runInContext(src + epilogue, dom.getInternalVMContext(), { filename: "app.js" });
const T = window.__coll;
if (!T || typeof T.renderNoteList !== "function") {
  console.log("  FAIL  加载 app.js 失败（未导出函数）");
  process.exit(1);
}

const tick = () => new Promise(r => setTimeout(r, 60));
await tick();   // 等顶层 loadNotes() → loadCollections() → renderNoteList() 完成

// 1. 分类下拉填充
const sel = doc.querySelector("#noteCollectionFilter");
const optTexts = Array.from(sel.options).map(o => o.textContent);
ok("分类下拉填充（全部分类 + 自定义类目 + 回收站）",
  optTexts.includes("全部分类") && optTexts.some(t => t.includes("工程监测")) && optTexts.some(t => t.includes("回收站")),
  optTexts.join("|"));

// 2. 列表渲染
const listHtml = doc.querySelector("#noteList").innerHTML;
ok("列表项含 ⋯ 操作按钮", listHtml.includes('class="more"'));
ok("列表项显示自定义标题（display_titles 覆盖）", listHtml.includes("自定义标题A"));
ok("列表项显示类目标签", listHtml.includes('class="tag"') && listHtml.includes("工程监测"));

// 3. 按类目筛选
sel.value = "c1";
T.renderNoteList();
let items = Array.from(realQS_all("#noteList .list-item"));
ok("按类目 c1 筛选 → 只剩 1 条", items.length === 1, "len=" + items.length);
ok("筛选结果正是该类目文档", items[0] && items[0].dataset.path === "notes/a.md");

// 4. 回收站视图（trash 为空 → 提示）
sel.value = "__trash__";
T.renderNoteList();
ok("回收站视图（空）显示提示", doc.querySelector("#noteList").innerHTML.includes("回收站是空的"));

// 5. ⋯ 菜单（正常视图）
sel.value = "";
T.renderNoteList();
const moreBtn = doc.querySelector("#noteList .more");
T.openNoteMenu(moreBtn, NOTES[0], false);
const menu = doc.querySelector(".pop-layer .menu");
ok("⋯ 菜单含 3 个选项（编辑标题 / 分类 / 移入回收站）",
  !!menu && menu.querySelectorAll("button").length === 3, menu ? menu.textContent : "no-menu");
ok("⋯ 菜单含『移入回收站』", !!menu && menu.textContent.includes("移入回收站"));
T._closePop();

// 6. ⋯ 菜单（回收站视图 → 只有恢复）
T.openNoteMenu(moreBtn, NOTES[0], true);
const menu2 = doc.querySelector(".pop-layer .menu");
ok("回收站中 ⋯ 菜单只有『恢复』",
  !!menu2 && menu2.querySelectorAll("button").length === 1 && menu2.textContent.includes("恢复"));
T._closePop();

// 7. 分类弹层含「删除类目」入口（前端补齐的管理动作）
T.editNoteCollections(NOTES[0]);
await tick();
const nDel = realQS_all(".modal .ck-del").length;
ok("分类弹层每个类目都有『删除类目』按钮（2 类目 → 2 个）", nDel === 2, "n=" + nDel);
ok("分类弹层每个类目都有『重命名』按钮（2 类目 → 2 个）",
  realQS_all(".modal .ck-op").length === 2, "n=" + realQS_all(".modal .ck-op").length);
ok("分类弹层含『新建类目』入口", !!doc.querySelector(".modal #_newColl") || !!doc.querySelector("#_newColl"));
Array.from(doc.querySelectorAll(".modal-mask")).forEach(x => x.remove());
T._closePop();

function realQS_all(s) { return doc.querySelectorAll(s); }

console.log(`\n  Collections UI  TOTAL=${PASS + FAIL} PASS=${PASS} FAIL=${FAIL}`);
process.exit(FAIL ? 1 : 0);
