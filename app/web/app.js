
"use strict";
/* =========================================================================
 *  Wiki-USB 控制台（零框架 · 零 CDN）
 * ========================================================================= */
const $  = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const S = {
  ready: false,
  refs: [],          // 当前回答的引用字典
  rendered: "",      // 已渲染的文本（不含滞留缓冲）
  holding: "",       // 未闭合角标滞留缓冲
  full: "",          // 收到的完整文本
  q: "",             // 最近一次提问（证据跳转的高亮词源）
  busy: false,
};

/* ---------------------------- 工具 ---------------------------- */
function toast(msg, ms) {
  const d = document.createElement("div");
  d.textContent = msg;
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), ms || 2600);
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function api(path, opts) {
  return fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}))
    .then(r => r.json());
}

/* -------------------- 轻量 Markdown 渲染 -------------------- */
function mdToHtml(src) {
  const blocks = [];
  let text = String(src || "");
  // 代码块先摘出来，避免内部被行内规则污染
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push('<pre><code>' + esc(code.replace(/\n$/, "")) + "</code></pre>");
    return "\u0000BLK" + (blocks.length - 1) + "\u0000";
  });
  text = esc(text);
  // UX-2：把后端下发的命中哨兵（控制字符 \u0001/\u0002）转成 <mark>。
  // 必须放在 esc() **之后**：关键词本身已随全文一起转义，这里只把哨兵换成标签 ——
  // 既不破坏 HTML 转义，也不会在表格/路径里乱插标签。
  text = text.replace(/\u0001/g, "<mark>").replace(/\u0002/g, "</mark>");
  text = text.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  text = text.replace(/^#{1,6}\s*(.+)$/gm, "<h4>$1</h4>");
  text = text.replace(/^&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>");
  text = text.replace(/^\s*[-*]\s+(.+)$/gm, "<p>• $1</p>");
  text = text.replace(/^\s*(\d+)\.\s+(.+)$/gm, "<p>$1. $2</p>");
  // 引用角标 [^1] / [1]
  text = text.replace(/\[\^?(\d+)\]/g, (m, n) => {
    const ref = S.refs.find(r => String(r.id) === String(n));
    const tip = ref ? ((ref.display_source || ref.title) + "\n" + ref.path + "\n\n" + (ref.snippet || "")).slice(0, 320) : "引用 " + n;
    return '<span class="cite" data-ref="' + n + '" data-tip="' + esc(tip).replace(/\n/g, "&#10;") + '">' + n + "</span>";
  });
  text = text.replace(/\n{2,}/g, "</p><p>");
  text = text.replace(/\n/g, "<br>");
  text = "<p>" + text + "</p>";
  text = text.replace(/<p>\s*<\/p>/g, "");
  text = text.replace(/\u0000BLK(\d+)\u0000/g, (m, i) => blocks[+i]);
  return text;
}

/* -------- 未闭合角标缓冲：滞留在缓冲区，待闭合后再统一绘制 -------- */
function splitHold(s) {
  const i = s.lastIndexOf("[");
  if (i < 0) return [s, ""];
  const tail = s.slice(i);
  // 匹配结尾处「可能是角标但尚未闭合」的片段
  if (/^\[\^?\d{0,3}$/.test(tail)) return [s.slice(0, i), tail];
  return [s, ""];
}

/* ---------------------------- 状态 ---------------------------- */
function applyStatus(st) {
  const d = st && st.data ? st.data : st;
  S.ready = !!(d && d.ready);
  const cR = $("#chipReady"), cE = $("#chipEmbed"), cA = $("#chipAI"), cS = $("#chipSync");

  cR.textContent = S.ready ? "已就绪" : "初始化中…";
  cR.className = "chip " + (S.ready ? "on" : "off");

  if (!S.ready) return;

  const emb = d.embedding || {}, ai = d.ai || {}, syn = d.sync || {}, db = d.db || {};
  const vecOK = db.vec_ready && !emb.signature_mismatch;
  cE.textContent = vecOK ? "本地智能搜索：可用" : "本地智能搜索：仅关键词";
  cE.className = "chip " + (vecOK ? "on" : "warn");

  cA.textContent = "AI 对话：" + ({ ollama: "本地模型", api: "云端", offline: "尚未配置", error: "不可用" }[ai.resolved] || ai.provider_mode || "—");
  cA.className = "chip " + (ai.resolved === "ollama" || ai.resolved === "api" ? "on" : (ai.resolved === "error" ? "warn" : "off"));

  cS.textContent = "同步：" + (syn.running ? (syn.tracked + " 篇 · " + syn.interval + "s") : "未运行");
  cS.className = "chip " + (syn.running ? "on" : "off");

  renderAlerts(d);
  renderStats(d);
  renderEmptyGuide(d);
}

/* 知识库为空时，在问答页顶部直接给出「下一步该点哪里」 */
function renderEmptyGuide(d) {
  const box = $("#emptyGuide");
  if (!box || !d || !d.ready) return;
  if ((d.db && d.db.docs) > 0) { box.innerHTML = ""; return; }
  if (box.dataset.shown === "1") return;   // 只渲染一次，避免输入框被打断
  box.dataset.shown = "1";
  box.innerHTML =
    '<div class="empty-guide"><h5>知识库还是空的 —— 先加内容，问答才有东西可查</h5><ol>' +
    '<li>点顶部 <b>「剪藏」</b> 标签 →  把 <code>.md</code> 文件拖进虚线框，或粘贴一个网址抓取</li>' +
    '<li>也可以把 .md / .txt 笔记直接放进资料库的 <code>notes</code> 文件夹（安装版默认在 文档\\USB-WIKI-Data\\notes），15 秒内自动入库</li>' +
    '<li>入库后回到这里提问，回答会带 <b>[1]</b> 角标指向原文出处</li></ol>' +
    '<div style="margin-top:11px"><button class="btn primary" onclick="switchTab(\'capture\')">去添加第一份内容 →</button></div>' +
    "</div>";
}

function renderAlerts(d) {
  const box = $("#alerts");
  const items = [];
  if (d && d.db && d.db.signature_mismatch) {
    // 文案由后端生成：真配置变更会给「需重建」指引，临时降级会说明「索引已保留」
    items.push({ cls: "err", text: d.db.signature_mismatch });
  }
  (d && d.warnings ? d.warnings : []).forEach(w => items.push({ cls: "", text: w }));
  box.innerHTML = items.map((it, i) =>
    '<div class="alert ' + it.cls + '">' + esc(it.text) + '<button data-al="' + i + '">×</button></div>'
  ).join("");
  $$("#alerts .alert button").forEach(b => b.onclick = e => e.target.parentElement.remove());
}

/* 对话能力状态文案（后端 ai.state 是稳定枚举，前端只做展示映射）
   ⚠ 不在这里推荐任何具体模型 —— 产品尚未拍板推荐型号。 */
function chatStateText(ai) {
  if (!ai) return "—";
  if (ai.chat_ready) return "✅ 就绪" + (ai.selected_model ? "（" + ai.selected_model + "）" : "");
  const map = {
    no_runtime: "未检测到本地 Ollama",
    no_model: "Ollama 已启动，但本机尚无模型",
    selection_required: "本机已有模型，请选择用哪个对话",
    model_missing: "原配置的模型在本机已不存在",
  };
  return (map[ai.state] || "尚未配置") + (ai.reason ? "：" + ai.reason : "");
}

function fillAppVersion(d) {
  // 版本来自 /api/status（后端单一源 app/version.py），前端不自己硬编码
  const el = document.getElementById('appVer');
  if (el && d && d.app_version) el.textContent = 'v' + d.app_version;
}

function renderStats(d) {
  fillAppVersion(d);
  if (!d || !d.ready) return;
  const emb = d.embedding || {}, ai = d.ai || {}, db = d.db || {}, syn = d.sync || {};
  const rows = [
    ["控制台端口", d.port || "—"],
    ["运行时长", (d.uptime || 0) + " 秒"],
    ["文档 / 切片 / 父块", (db.docs || 0) + " / " + (db.chunks || 0) + " / " + (db.parents || 0)],
    ["向量召回", db.vec_ready && !emb.signature_mismatch ? "已启用" : "未启用"],
    ["嵌入源 / 维度", (emb.source || "—") + " / " + (emb.dim || "—")],
    ["嵌入签名", emb.signature ? (emb.signature.model + "@" + emb.signature.dim) : "—"],
    ["AI 模式 / 实际", (ai.provider_mode || "—") + " / " + (ai.resolved || "—")],
    ["对话能力", chatStateText(ai)],
    ["本机模型", (ai.installed_model_count || 0) + " 个" +
      (ai.selected_model ? "，已选 " + ai.selected_model : "，未选择")],
    ["Ollama 探测", ai.ollama_healthy ? "在线" : ("离线 (" + (ai.ollama_detail || "—") + ")")],
    ["API Key", ai.api_key_configured ? "已配置" : "未配置"],
    ["同步轮次 / 更新", (syn.cycles || 0) + " / " + ((syn.indexed || 0) + (syn.updated || 0))],
    ["冷启动耗时", (d.boot && d.boot.boot_ms ? d.boot.boot_ms : "—") + " ms"],
  ];
  $("#statKV").innerHTML = rows.map(r => "<b>" + esc(r[0]) + "</b><span>" + esc(r[1]) + "</span>").join("");
  renderNotes(d);
}

/* 系统自愈/自动适配的过程信息 —— 不是告警，放在设置页供排查时查证 */
function renderNotes(d) {
  const box = $("#runNotes");
  if (!box) return;
  const notes = (d && d.notes) || [];
  if (!notes.length) { box.innerHTML = '<div class="hint">（无）</div>'; return; }
  box.innerHTML = notes.map(n =>
    '<div class="note-line">· ' + esc(n) + "</div>").join("");
}

async function pollStatus(loop) {
  try {
    const st = await api("/api/status");
    applyStatus(st);
    if (loop) setTimeout(() => pollStatus(S.ready), S.ready ? 15000 : 900);
  } catch (e) {
    setTimeout(() => pollStatus(loop), 1500);
  }
}

/* ---------------------------- 问答 ---------------------------- */
function pushMsg(role, html) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  wrap.innerHTML = '<div class="who">' + (role === "user" ? "我" : "W") + '</div><div class="bubble"></div>';
  const bub = wrap.querySelector(".bubble");
  if (html != null) bub.innerHTML = html;
  $("#chatScroll").appendChild(wrap);
  $("#chatScroll").scrollTop = $("#chatScroll").scrollHeight;
  return bub;
}

function renderStream(bub) {
  const [safe] = splitHold(S.full);
  if (safe === S.rendered) return;
  S.rendered = safe;
  S.holding = S.full.slice(safe.length);
  bub.innerHTML = mdToHtml(safe) + '<span class="pending-dot"></span>';
  $("#chatScroll").scrollTop = $("#chatScroll").scrollHeight;
}

async function send() {
  const input = $("#chatInput");
  const q = input.value.trim();
  if (!q || S.busy) return;
  S.busy = true;
  $("#btnSend").disabled = true;

  pushMsg("user", mdToHtml(q));
  input.value = "";

  S.full = ""; S.rendered = ""; S.holding = ""; S.refs = [];
  S.q = q;                              // B：证据跳转时高亮查询关键词
  const bub = pushMsg("assistant", '<span class="hint">检索知识库…</span>');

  try {
    const resp = await fetch("/api/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q, history: [] }),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const dec = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = raw.split("\n").find(l => l.startsWith("data:"));
        if (!line) continue;
        handleFrame(JSON.parse(line.slice(5).trim()), bub);
      }
    }
    // 收尾：强制绘制滞留缓冲
    const [safe] = splitHold(S.full);
    bub.innerHTML = mdToHtml(S.full) + (S.refs.length ? refsHtml(S.refs) : "");
  } catch (e) {
    bub.innerHTML = '<div class="alert err" style="margin:0">请求失败：' + esc(e.message) + "</div>";
  } finally {
    S.busy = false;
    $("#btnSend").disabled = false;
    $("#chatScroll").scrollTop = $("#chatScroll").scrollHeight;
  }
}

function refsHtml(refs) {
  return '<div class="refs">' + refs.map(r =>
    '<span class="ref" data-ref="' + r.id + '" title="' +
      esc(r.path + "\n" + (r.snippet || "点击跳转到该笔记")) + '">[' + r.id + "] " +
      // UX-1：来源显示名优先（导入文件→源文件名；剪藏/笔记→标题）
      esc(r.display_source || r.title) + "</span>"
  ).join("") + "</div>";
}

function handleFrame(f, bub) {
  if (f.type === "references") {
    S.refs = f.refs || [];
  } else if (f.type === "meta") {
    (f.warnings || []).forEach(w => toast("⚠ " + w, 3600));
    const [safe] = splitHold(S.full);
    bub.innerHTML = mdToHtml(safe) + '<span class="pending-dot"></span>' +
      '<div class="hint" style="margin-top:6px">提供方：' + esc(f.provider || "—") +
      " · 检索路由：" + esc(f.route || "—") + "</div>";
  } else if (f.type === "notice") {
    toast("⚠ " + f.message, 3600);
  } else if (f.type === "delta") {
    S.full += f.content || "";
    renderStream(bub);   // 未闭合角标滞留，不提前绘制半截标记
  } else if (f.type === "error") {
    S.full += "\n\n> ⚠ " + (f.message || "模型服务异常");
    bub.innerHTML = mdToHtml(S.full);
  }
}

  /* ---------------------------- 剪藏 ---------------------------- */
  /* 抓取前先问后端「这个网页是不是已经保存过」。
     注意后端在真正抓取时**自己也会再查一次** —— 这里的预检只是为了让界面
     能在重复时给出选择，不是安全/正确性的依据。 */
  async function doCapture() {
    const url = $("#capUrl").value.trim();
    if (!url) return toast("请输入 URL");
    const box = $("#capResult");
    box.innerHTML = '<span class="hint"><span class="spin"></span> 正在检查这个网页是否已保存过…</span>';
    $("#btnCapture").disabled = true;
    let dup = null;
    try {
      const chk = await api("/api/capture/duplicate?url=" + encodeURIComponent(url));
      dup = (chk.data || {}).existing || null;
    } catch (e) {
      // 预检失败不阻断抓取：后端自己会判重，真重复时会返回 409
    }
    $("#btnCapture").disabled = false;
    if (dup) return renderDuplicateChoice(url, dup);
    return doCaptureWith(url, "abort");
  }

  /* 重复来源 URL 的四个选项。刻意不做「一律禁止重复」—— 网页会更新，
     用户有时确实要保存同一页面的不同版本，所以把决定权交回用户。 */
  function renderDuplicateChoice(url, dup) {
    const box = $("#capResult");
    box.innerHTML =
      '<div class="alert">⚠ 该网页已经保存过：' + esc(dup.title || dup.rel_path) + "</div>" +
      '<div class="hint" style="margin:6px 0">请选择处理方式</div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
      '<button class="btn" data-dup="open">打开已有</button>' +
      '<button class="btn primary" data-dup="update">更新已有</button>' +
      '<button class="btn" data-dup="new">另存为新版本</button>' +
      '<button class="btn ghost" data-dup="cancel">取消</button>' +
      "</div>";
    $$("#capResult [data-dup]").forEach(function (b) {
      b.onclick = function () {
        const act = b.dataset.dup;
        if (act === "cancel") {
          box.innerHTML = '<span class="hint">已取消，未做任何改动。</span>';
          return;
        }
        if (act === "open") return openNoteByPath(dup.rel_path);
        return doCaptureWith(url, act);
      };
    });
  }

  async function doCaptureWith(url, onDup) {
    const box = $("#capResult");
    box.innerHTML = '<span class="hint"><span class="spin"></span> 正在抓取并清洗正文…</span>';
    $("#btnCapture").disabled = true;
    try {
      const r = await api("/api/capture/url", {
        method: "POST",
        body: JSON.stringify({ url: url, on_duplicate: onDup }),
      });
    const d = r.data || {};
    // 系统自愈 / 自动降级的过程信息：按项目约定不打扰用户，只在结果区作说明
    const notesHtml = (d.notes && d.notes.length)
      ? '<div class="hint" style="margin-top:6px">' + d.notes.map(function (n) { return "· " + esc(n); }).join("<br>") + "</div>"
      : "";
    if (r.status === "partial_fallback") {
      box.innerHTML = '<div class="alert">⚠ ' + esc(r.message) + "</div>" +
        '<div class="hint">已保存：' + esc(d.file_path) + " · 快照：" + esc(d.snapshot_path || "—") + "</div>" + notesHtml;
      toast("该页面为前端动态渲染，仅保留快照", 4200);
    } else if (r.code === 200) {
      box.innerHTML = '<div class="alert info">✅ ' + esc(r.message || "抓取成功") + "</div>" +
        '<div class="hint">标题：' + esc(d.title) + " · 字数：" + d.char_count +
        " · 文件：" + esc(d.file_path) + " · 抽取器：" + esc(d.extractor || "—") + "</div>" + notesHtml;
      toast("已入库并建立索引");
    } else {
      box.innerHTML = '<div class="alert err">' + esc(r.message || "抓取失败") + "</div>";
    }
  } catch (e) {
    box.innerHTML = '<div class="alert err">' + esc(e.message) + "</div>";
  } finally {
    $("#btnCapture").disabled = false;
    loadNotes();
  }
}

async function doSaveNote() {
  const title = $("#noteTitle").value.trim(), content = $("#noteBody").value.trim();
  if (!content) return toast("正文不能为空");
  $("#btnSaveNote").disabled = true;
  try {
    const r = await api("/api/notes/save", { method: "POST", body: JSON.stringify({ title, content }) });
    $("#saveHint").textContent = r.code === 200 ? "✅ " + r.message + " · " + (r.data.file_path || "") : "⚠ " + r.message;
    if (r.code === 200) { $("#noteTitle").value = ""; $("#noteBody").value = ""; loadNotes(); }
  } finally { $("#btnSaveNote").disabled = false; }
}

/* ------------------- 拖拽 / 选择文件导入知识库 ------------------- */
/* 由后端 /api/import/formats 提供能力清单，前端不硬编码，避免两边漂移 */
const IMPORT = { exts: [], loaded: false };

async function loadImportFormats() {
  try {
    const r = await api("/api/import/formats");
    const d = r.data || {};
    IMPORT.exts = d.exts || [];
    IMPORT.loaded = true;
    const picker = $("#fileInput");
    if (picker && IMPORT.exts.length) picker.setAttribute("accept", IMPORT.exts.join(","));
    const hint = $("#formatHint");
    if (hint) {
      const cats = (d.categories || []).filter(c => (c.exts || []).length);
      hint.innerHTML = cats.map(c =>
        "<b>" + esc(c.name) + "</b>　" + c.exts.map(e => "<code>" + esc(e) + "</code>").join(" ")
      ).join("<br>") +
        "<br><span style='color:var(--warn)'>暂不支持：" +
        esc((d.unsupported.legacy_office || []).join(" ")) + " " +
        esc((d.unsupported.image || []).join(" ")) + " " +
        esc((d.unsupported.av || []).slice(0, 4).join(" ")) +
        " —— " + esc(d.unsupported.note) + "</span>" +
        (d.pdf_backend ? "" : "<br><span style='color:var(--warn)'>PDF 解析库未安装：执行 <code>python setup_runtime_windows.py --with-pdf</code> 后可用。</span>");
    }
  } catch (e) { /* 拿不到就退化为「不限制 accept」，由后端给出明确错误 */ }
}

function isBinaryExt(name) {
  const ext = ("." + (name.split(".").pop() || "")).toLowerCase();
  // 文本族前端直读（可顺带处理 GBK）；其余一律走 base64，避免二进制被 UTF-8 破坏
  const TEXTY = [".md", ".markdown", ".mdown", ".txt", ".log", ".ini", ".cfg", ".conf",
    ".env", ".properties", ".rst", ".org", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml", ".toml", ".xml", ".srt", ".vtt", ".ass", ".ssa", ".lrc",
    ".html", ".htm", ".xhtml", ".rtf"];
  if (TEXTY.includes(ext)) return false;
  return true;
}

async function readTextSmart(file) {
  const buf = await file.arrayBuffer();
  let text = new TextDecoder("utf-8").decode(buf);
  // 中文环境常见 GBK 编码：UTF-8 解出替换符就换 GB18030 再试
  if (text.includes("\ufffd")) {
    try {
      const alt = new TextDecoder("gb18030").decode(buf);
      if (!alt.includes("\ufffd")) return alt;
    } catch (e) { /* 浏览器不支持则保持 UTF-8 结果 */ }
  }
  return text;
}

function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const CHUNK = 0x8000;                 // 分块拼接，避免超长字符串导致栈溢出
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

async function importFiles(files) {
  const zone = $("#dropZone"), out = $("#dzResult");
  if (!zone || !out) return;
  const payload = [];
  const skipped = [];
  for (const f of files) {
    if (f.size > 20 * 1024 * 1024) { skipped.push(`${f.name}（超过 20MB）`); continue; }
    const ext = ("." + (f.name.split(".").pop() || "")).toLowerCase();
    if (IMPORT.loaded && IMPORT.exts.length && !IMPORT.exts.includes(ext)) {
      skipped.push(`${f.name}（不支持 ${ext}）`);
      continue;
    }
    if (isBinaryExt(f.name)) {
      payload.push({ filename: f.name, content_base64: bufToBase64(await f.arrayBuffer()) });
    } else {
      payload.push({ filename: f.name, content: await readTextSmart(f) });
    }
  }
  if (!payload.length) {
    out.innerHTML = '<span class="bad">没有可导入的文件</span>' +
      (skipped.length ? "<br>已跳过：" + esc(skipped.join("、")) : "");
    return;
  }

  zone.classList.add("busy");
  const mainLabel = zone.querySelector(".dz-main");
  mainLabel.textContent = `正在转换并索引 ${payload.length} 个文件…`;
  try {
    const r = await api("/api/notes/import", {
      method: "POST", body: JSON.stringify({ files: payload }),
    });
    const d = r.data || {};
    const rows = (d.results || []).map(x =>
      (x.ok ? '<span class="ok">✅ ' + esc(x.filename) + "</span>"
            : '<span class="bad">❌ ' + esc(x.filename) + "</span>") +
      " —— " + esc(x.message) + (x.ok ? "（" + (x.char_count || 0) + " 字）" : ""));
    out.innerHTML = rows.join("<br>") +
      (skipped.length ? '<br><span class="bad">已跳过：' + esc(skipped.join("、")) + "</span>" : "");
    if (d.ok) toast(`✅ 成功导入 ${d.ok} 个文件，已转成 Markdown 并建索引`);
    else if (d.total) toast("⚠ 全部导入失败，请看下方原因", 4000);
    await loadNotes();
  } catch (e) {
    out.innerHTML = '<span class="bad">导入失败：' + esc(e.message) + "</span>";
  } finally {
    zone.classList.remove("busy");
    mainLabel.textContent = "把文件拖到这里";
  }
}

function initDropZone() {
  const zone = $("#dropZone"), picker = $("#fileInput");
  if (!zone || !picker) return;
  ["dragenter", "dragover"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.add("over"); }));
  ["dragleave", "dragend", "drop"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", e => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length) importFiles(files);
  });
  picker.addEventListener("change", e => {
    const files = Array.from(e.target.files || []);
    if (files.length) importFiles(files);
    e.target.value = "";
  });
  // 阻止把文件拖到页面其他位置时浏览器直接打开它
  ["dragover", "drop"].forEach(ev =>
    window.addEventListener(ev, e => { if (e.target.closest("#dropZone") === null) e.preventDefault(); }));
}

/* ---------------------------- 笔记 ---------------------------- */
let noteCache = [];
async function loadNotes() {
  const r = await api("/api/notes?limit=500");
  noteCache = r.data || [];
  $("#docCount").textContent = noteCache.length;
  renderNoteList();
}
function renderNoteList() {
  const kw = $("#noteFilter").value.trim().toLowerCase();
  // UX-1：列表显示与过滤都用 display_source（导入文件→源文件名），
  //       内部 title 仍保留在索引里（检索/提示词用），只是不再当「来源名」展示。
  const list = noteCache.filter(n =>
    !kw || (n.display_source || "").toLowerCase().includes(kw)
    || (n.title || "").toLowerCase().includes(kw) || (n.rel_path || "").toLowerCase().includes(kw));
  $("#noteList").innerHTML = list.map(n =>
    '<div class="list-item" data-path="' + esc(n.rel_path) + '">' +
      '<div class="t">' + esc(n.display_source || n.title || "(无标题)") + "</div>" +
      '<div class="m"><span>' + esc(n.rel_path) + "</span>" +
      (n.status === "partial_fallback" ? '<span class="pill warn">快照</span>' : "") +
      "<span>" + (n.chunks || 0) + " 切片</span></div>" +
    "</div>").join("") || '<div class="hint">暂无文档，去「剪藏」页录入一篇吧。</div>';

  $$("#noteList .list-item").forEach(el => el.onclick = () => openNoteItem(el));
}

/* 打开一条笔记（列表项已渲染时使用）。抽成具名函数，供问答引用跳转复用。
 * jump 非空时（B）：证据跳转 —— 打开后按 parent_id 定位证据块并高亮。 */
async function openNoteItem(el, jump) {
    $$("#noteList .list-item").forEach(x => x.classList.remove("active"));
    el.classList.add("active");
    $("#noteTitleBar").textContent = el.querySelector(".t").textContent;
    const r = await api("/api/notes/content?path=" + encodeURIComponent(el.dataset.path));
    if (r.code !== 200) {
      NOTE.payload = null;
      $("#noteSeg").hidden = true;
      $("#noteView").innerHTML = '<span class="empty">读取失败：' + esc(r.message || "") + "</span>";
      return;
    }
    NOTE.payload = r.data;
    const orig = r.data.original;
    $("#noteSeg").hidden = false;
    const btn = $("#segOriginal");
    btn.disabled = !orig;
    btn.title = orig
      ? ("查看" + noteLabels(orig).original + "：" + orig.name + "（" + fmtSize(orig.size) + "）")
      : "该笔记没有留存原件（纯 Markdown / 纯文本导入）";
    if (jump) {
      // 证据跳转只在「渲染」视图里定位（B4：不做 PDF/DOCX 页面坐标）。
      // 若该笔记默认开「原版」，先切回渲染视图再定位。
      NOTE.view = "rendered";
      setNoteView(NOTE.view);
      jumpToEvidence(el.dataset.path, jump);
      return;
    }
    NOTE.view = orig ? "original" : "rendered";
    setNoteView(NOTE.view);
}

/* 按路径打开笔记：供问答内文角标 / 底部来源 chips 跳转使用 */
async function openNoteByPath(path, jump) {
  if (!path) return;
  switchTab("notes");
  await loadNotes();                    // 列表异步渲染，必须等它出来
  const find = () => $$("#noteList .list-item").find(x => x.dataset.path === path);
  let el = find();
  if (!el && $("#noteFilter") && $("#noteFilter").value) {
    $("#noteFilter").value = "";        // 可能被过滤词挡住 → 清空后重试
    await loadNotes();
    el = find();
  }
  if (!el) { toast("找不到该笔记（可能已被删除）", 3200); return; }
  el.scrollIntoView({ block: "center" });
  openNoteItem(el, jump);
}

/* -------- B：证据精确定位（parent_id 为主、snippet 文本兜底） -------- */
function _normTxt(s) {
  return String(s || "").replace(/\s+/g, "").replace(/[#>*`|]/g, "").toLowerCase();
}
function _evBlocks(box) {
  // 渲染视图的顶层块级元素（docToHtml 的输出结构：p/h*/ul/ol/blockquote/table/pre/hr）
  return Array.from(box.children).filter(el =>
    /^(P|H1|H2|H3|H4|H5|H6|UL|OL|BLOCKQUOTE|TABLE|PRE|HR|DIV)$/.test(el.tagName));
}
/* 在元素内的文本节点上包高亮标签（不碰任何标签/属性；每个节点从最早命中开始，递归处理尾部） */
function _markNode(node, phrases, tag, cls) {
  const text = node.nodeValue;
  if (!text || !text.trim()) return;
  let best = null;
  for (const t of phrases) {
    if (!t) continue;
    const i = text.toLowerCase().indexOf(t);
    if (i >= 0 && (!best || i < best.i)) best = { i, t };
  }
  if (!best) return;
  const frag = document.createDocumentFragment();
  if (best.i > 0) frag.appendChild(document.createTextNode(text.slice(0, best.i)));
  const mk = document.createElement(tag || "mark");
  if (cls) mk.className = cls;
  mk.textContent = text.slice(best.i, best.i + best.t.length);
  frag.appendChild(mk);
  const tail = document.createTextNode(text.slice(best.i + best.t.length));
  frag.appendChild(tail);
  node.parentNode.replaceChild(frag, node);
  _markNode(tail, phrases, tag, cls);     // tail 严格更短 → 必然终止
}
/* 证据句子：从 ref.snippet（后端已给 query-centered 摘录）里取真正出现在正文中的片段。
   注意剔除后端下发的**高亮哨兵**（\u0001/\u0002）—— 它们是给聊天气泡渲染 <mark> 用的，
   留着会让这里做精确子串匹配时永远对不上正文。 */
function _evidencePhrases(snippet) {
  const clean = String(snippet || "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
  const parts = [];
  // 先按省略号/换行切，再按句读切细：摘录常把「小节标题 + 正文」粘成一段，
  // 不切细就永远匹配不到正文（正文里标题是独立的一行）。
  clean.split(/[…\n]|\.\.\./).forEach(seg => {
    seg.split(/[，。；：,;]/).forEach(piece => {
      const t = piece.replace(/\s+/g, " ").trim();
      if (t.length >= 8) parts.push(t);
    });
  });
  // 按**摘录顺序**取（后端摘录以命中词为中心 → 首片就是真正支持回答的那句），
  // 不按长度排序：长片段往往落在句子尾部，会把高亮打到无关的从句上。
  return parts.slice(0, 3);
}
async function jumpToEvidence(path, jump) {
  const box = $("#noteView");
  if (!box || !jump) return;
  // 1) 取证据块原文（parent_id → /api/notes/evidence；失败则退 snippet）
  let ev = null;
  if (jump.parent_id) {
    try {
      const r = await api("/api/notes/evidence?path=" + encodeURIComponent(path) +
        "&parent_id=" + encodeURIComponent(jump.parent_id));
      if (r.code === 200) ev = r.data;
    } catch (e) { /* 走 snippet 兜底 */ }
  }
  const src = (ev && ev.content) || jump.snippet || "";
  if (!src) { toast("该引用缺少定位信息，已打开原文", 3000); return; }

  // 2) 清掉上一次的高亮（noteView 里的 <mark> 全部是本功能写入的）
  box.querySelectorAll("mark").forEach(m => {
    const p = m.parentNode;
    while (m.firstChild) p.insertBefore(m.firstChild, m);
    m.remove();
    if (p.normalize) p.normalize();
  });
  box.querySelectorAll(".ev-hl,.ev-hl-fade").forEach(el =>
    el.classList.remove("ev-hl", "ev-hl-fade"));

  // 3) 文本定位：parent 原文的行 → 渲染块。parent_id 拿不到内容时才退 snippet。
  const blocks = _evBlocks(box);
  const texts = blocks.map(b => _normTxt(b.textContent));
  const lines = src.split("\n").map(s => _normTxt(s)).filter(s => s.length >= 6);
  let start = -1;
  if (lines.length) {
    for (let i = 0; i < blocks.length && start < 0; i++) {
      for (const ln of lines) {
        if (texts[i].indexOf(ln) >= 0) { start = i; break; }      // 块包含行
        if (texts[i].length >= 8 && ln.indexOf(texts[i]) === 0) { start = i; break; } // 行起点
      }
    }
    if (start < 0) {                                              // 首行前缀兜底
      const head = lines[0].slice(0, 12);
      for (let i = 0; i < blocks.length; i++)
        if (head.length >= 6 && texts[i].indexOf(head) >= 0) { start = i; break; }
    }
  }
  if (start < 0) { toast("未能精确定位证据块，已打开原文", 3000); return; }

  // 4) 向后扩展到约覆盖整个 parent（上限 15 块，防止异常数据导致高亮半篇文档）
  const targetLen = _normTxt(src).length;
  let end = start, acc = 0;
  for (let j = start; j < blocks.length && j <= start + 15; j++) {
    end = j; acc += texts[j].length;
    if (acc >= targetLen) break;
  }
  const region = blocks.slice(start, end + 1);
  region.forEach(b => b.classList.add("ev-hl"));
  // 视觉语义（本轮修正）：章节上下文 = 淡蓝块（3.5s 后淡化）；
  // 真正支持回答的**证据句**（ref.snippet 的正文片段）= 黄色高亮并保留；
  // 查询词不再做永久 mark —— 否则用户满屏都是「Qwen-Image-2.1 / 功能」的黄块。
  const phrases = _evidencePhrases(jump.snippet);
  if (phrases.length) {
    region.forEach(b => {
      const walker = document.createTreeWalker(b, NodeFilter.SHOW_TEXT, null);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(n => _markNode(n, phrases, "mark", "ev-ev"));
    });
  }
  region[0].scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(() => region.forEach(b => b.classList.add("ev-hl-fade")), 3500);
}

/* -------- 笔记视图：渲染 / 原版 / 源码 -------- */
const NOTE = { payload: null, view: "rendered" };
/* View 产品语义（本轮修正）：三个视图 = 阅读 / 原件（或网页快照）/ Markdown。
   「源码」是误称 —— 它其实是转换后的 Markdown，不是 PDF/DOCX/HTML 的真源码。 */
function noteLabels(orig) {
  const isWeb = !!(orig && (orig.ext === ".html" || orig.ext === ".htm"));
  return {
    rendered: "阅读",
    original: isWeb ? "网页快照" : "原件",
    raw: "Markdown",
  };
}
/* 阅读视图不显示内部 metadata：剥掉开头的 YAML frontmatter 块（磁盘上的真相源不动） */
function stripFrontmatter(src) {
  const m = String(src || "").match(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---\r?\n?/);
  return m ? String(src).slice(m[0].length) : String(src || "");
}
/* 从 Markdown 头部读一个 frontmatter 字段（只读展示用） */
function frontmatterValue(src, key) {
  const m = String(src || "").match(/^\uFEFF?---\r?\n([\s\S]*?)\r?\n---/);
  if (!m) return "";
  const hit = m[1].match(new RegExp("^" + key + ":\\s*\"?([^\"\\n]+?)\"?\\s*$", "m"));
  return hit ? hit[1].trim() : "";
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function setNoteView(view) {
  const d = NOTE.payload;
  if (!d) return;
  const orig = d.original;
  if (view === "original" && !orig) view = "rendered";
  NOTE.view = view;
  const labels = noteLabels(orig);
  $$("#noteSeg button").forEach(b => {
    b.textContent = labels[b.dataset.view] || b.textContent;
    b.classList.toggle("on", b.dataset.view === view);
  });
  const box = $("#noteView");
  box.className = "doc-md";

  if (view === "raw") {
    // Markdown 视图：完整显示转换后的 Markdown（含 frontmatter，便于核对）
    box.innerHTML = '<pre class="doc" style="white-space:pre-wrap;margin:0">' + esc(d.content) + "</pre>";
    return;
  }

  if (view === "original") {
    const isWeb = orig.ext === ".html" || orig.ext === ".htm";
    const url = "/api/notes/original?path=" + encodeURIComponent(d.path);
    // 「在新标签打开」实际打开的是本地离线快照 → 文案必须说清楚（网页剪藏）；
    // PDF/Office 的它就是原件本身，保留「在新标签打开」。
    const openLabel = isWeb ? "新标签打开快照" : "在新标签打开";
    const srcUrl = frontmatterValue(d.content, "source_url");
    const visit = (isWeb && /^https?:\/\//i.test(srcUrl))
      ? '　<a href="' + esc(srcUrl) + '" target="_blank" rel="noopener noreferrer">访问原网页</a>'
      : "";
    const meta = '<div class="doc-meta">' + labels.original + "：" + esc(orig.name) +
      "（" + fmtSize(orig.size) + "）" + visit +
      '　<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + openLabel + "</a>" +
      '　<a href="' + url + '?download=1" download>下载原件</a></div>';
    const isPdf = orig.ext === ".pdf";

    // 剪藏页加宽度切换；PDF 用浏览器自带查看器（自带缩放），无需切换
    const tools = isWeb
      ? '<div class="frame-tools">宽度：'
        + '<button type="button" data-m="fit" class="on">适应宽度</button>'
        + '<button type="button" data-m="raw">实际大小</button>'
        + '<span>剪藏页已禁用脚本，需登录/展开等交互时请用「下载原件」在浏览器打开</span></div>'
      : "";

    box.className = "doc-md doc-original";   // 让 #noteView 成为填满面板的 flex 列
    box.innerHTML = meta + tools +
      '<div class="frame-wrap fit"><iframe class="doc-frame" title="原版预览"></iframe></div>';
    const wrap = box.querySelector(".frame-wrap");
    const frame = wrap.querySelector("iframe");

    if (isPdf) {
      // 浏览器内置 PDF 查看器 —— 与直接打开 PDF 完全一致的观感。
      // 不参与下面的 1280px 虚拟宽度缩放：查看器自带缩放/适配，transform-scale
      // 只会把工具栏和页面一起压小（窄面板下小到不可用）。
      frame.style.width = "100%";
      frame.style.height = "100%";
      frame.src = url + "#toolbar=1&view=FitH";
      // 某些环境（浏览器关闭了内置 PDF 查看器 / 旧内核）iframe 里看不到 PDF ——
      // 「在新标签打开」就是兜底（C2），同一 URL 浏览器会用自带查看器整页打开。
      return;
    } else if (isWeb) {
      // 剪藏的原网页：沙箱 iframe 还原版式。sandbox="" 屏蔽脚本/表单/弹窗，
      // 因为抓来的第三方 HTML 属于不可信内容 —— 这条安全底线不能放宽
      frame.setAttribute("sandbox", "");
      frame.setAttribute("referrerpolicy", "no-referrer");
      frame.src = url;
    } else if (/^\.(png|jpe?g|gif|webp|bmp|svg)$/.test(orig.ext)) {
      wrap.outerHTML = '<img src="' + url + '" alt="' + esc(orig.name) + '" style="max-width:100%;border-radius:9px">';
      return;
    } else {
      wrap.outerHTML = '<div class="doc-dl">该格式无法内嵌预览。<br><br>' +
        '<a class="btn" href="' + url + '?download=1" download>下载原件</a></div>';
      return;
    }

    // 剪藏页适配：X / 微博这类站点是桌面固定宽度布局，塞进窄面板必被横向裁半。
    // 方案：按 1280px 的桌面宽度渲染，再等比缩放到面板宽 —— 整页横向永远完整可见。
    // 沙箱（无 allow-same-origin）拿不到内容高度，因此高度按「缩放后恰好填满」反推，
    // 页面更长时由 iframe 自己出滚动条，全程只有一根滚动条。
    const VIRTUAL_W = 1280;
    let mode = "fit";
    const layout = () => {
      const w = wrap.clientWidth, h = wrap.clientHeight;
      if (!w || !h) return;
      if (mode === "fit") {
        // 原则：窄面板可以缩小，宽面板**绝不放大超过原尺寸**（>1280px 时保持 1:1）
        const k = Math.min(1, w / VIRTUAL_W);
        frame.style.width = VIRTUAL_W + "px";
        frame.style.height = Math.max(400, Math.floor(h / k)) + "px";
        frame.style.transformOrigin = "0 0";
        frame.style.transform = "scale(" + k + ")";
      } else {
        frame.style.width = "100%";
        frame.style.height = "100%";
        frame.style.transform = "none";
      }
    };
    layout();
    if (window.__frameRO) window.__frameRO.disconnect();
    window.__frameRO = new ResizeObserver(layout);
    window.__frameRO.observe(wrap);
    box.querySelectorAll(".frame-tools button").forEach(b =>
      b.addEventListener("click", () => {
        mode = b.dataset.m;
        box.querySelectorAll(".frame-tools button").forEach(x => x.classList.toggle("on", x === b));
        wrap.classList.toggle("fit", mode === "fit");
        wrap.classList.toggle("raw", mode !== "fit");
        layout();
      }));
    return;
  }

  // 阅读视图：剥掉内部 metadata（frontmatter），只渲染 Markdown 正文
  box.innerHTML = docToHtml(stripFrontmatter(d.content));
}

/* -------- 文档级 Markdown 渲染（比聊天气泡版更完整：表格/多级标题/列表） -------- */
function docToHtml(src) {
  const fences = [];
  let text = String(src || "").replace(/\r\n/g, "\n");
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    fences.push("<pre><code>" + esc(code.replace(/\n$/, "")) + "</code></pre>");
    return "\u0000F" + (fences.length - 1) + "\u0000";
  });

  const lines = esc(text).split("\n");
  const out = [];
  let list = null, para = [], quote = [];

  const flushPara = () => { if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; } };
  const flushList = () => { if (list) { out.push("<" + list.tag + ">" + list.items.join("") + "</" + list.tag + ">"); list = null; } };
  const flushQuote = () => { if (quote.length) { out.push("<blockquote>" + quote.join("<br>") + "</blockquote>"); quote = []; } };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); };

  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    const t = ln.trim();

    if (!t) { flushAll(); continue; }
    if (/^\u0000F\d+\u0000$/.test(t)) { flushAll(); out.push(t); continue; }

    // 表格：表头 + 分隔行 + 数据行
    if (t.startsWith("|") && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flushAll();
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(t);
      let j = i + 2, rows = [];
      while (j < lines.length && lines[j].trim().startsWith("|")) { rows.push(cells(lines[j].trim())); j++; }
      out.push("<table><thead><tr>" + head.map(c => "<th>" + c + "</th>").join("") +
        "</tr></thead><tbody>" +
        rows.map(r => "<tr>" + r.map(c => "<td>" + c + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      i = j - 1;
      continue;
    }

    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushAll(); const lv = h[1].length; out.push("<h" + lv + ">" + inline(h[2]) + "</h" + lv + ">"); continue; }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { flushAll(); out.push("<hr>"); continue; }

    const q = t.match(/^&gt;\s?(.*)$/);
    if (q) { flushPara(); flushList(); quote.push(inline(q[1])); continue; }

    const ul = t.match(/^[-*+]\s+(.*)$/);
    const ol = t.match(/^(\d+)[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara(); flushQuote();
      const tag = ul ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push("<li>" + inline(ul ? ul[1] : ol[2]) + "</li>");
      continue;
    }

    flushList(); flushQuote();
    para.push(inline(t));
  }
  flushAll();
  return out.join("").replace(/\u0000F(\d+)\u0000/g, (m, i) => fences[+i]) ||
    '<span class="empty">（空文档）</span>';
}

function inline(s) {
  return s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1">')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/~~([^~\n]+)~~/g, "<s>$1</s>");
}

async function loadGraph() {
  const minDocs = Math.max(1, Math.min(20, parseInt($("#topicMin").value || "2", 10) || 2));
  $("#topicStats").innerHTML = '<span class="spin"></span> 正在归类…';
  try {
    const r = await api("/api/topics?min_docs=" + minDocs);
    renderTopics(r.data || {});
  } catch (e) {
    $("#topicStats").textContent = "归类失败：" + e.message;
    $("#topicBox").innerHTML = "";
  }
}

/* 主题分组渲染。刻意不画节点图 —— 实测本项目语料是「剪藏一批互不相关页面」，
   节点图必然是一堆孤岛（12 篇分成 9 个分量）；自动归题 + 按题浏览才贴合场景。 */
function renderTopics(d) {
  const topics = d.topics || [];
  const st = d.stats || {};
  const ungrouped = d.ungrouped || [];
  $("#topicStats").textContent =
    "笔记 " + (st.docs || 0) + " 篇 · 主题 " + (st.topics || 0) + " 个 · 已归题 " +
    (st.grouped_docs || 0) + " 篇 · 尚未归题 " + (st.ungrouped_docs || 0) + " 篇";

  let html = "";
  if (!topics.length) {
    html += '<div class="hint" style="padding:14px 4px">' +
      "还没有出现'多篇笔记共有的关键词'——<b>主题分组要等你围绕同一主题积累若干篇笔记才会长出来</b>。" +
      "<br><br>可在上方把阈值调成 <b>1</b>，先看每篇笔记各自的关键词（当作自动标签索引）。</div>";
  }
  topics.forEach(function (t) {
    html += '<div style="margin-bottom:12px">' +
      '<div style="font-weight:600;font-size:13px;margin-bottom:5px">' +
      esc(t.term) + ' <span class="pill">' + t.count + ' 篇</span></div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
      t.docs.map(function (x) {
        return '<span class="ref" data-path="' + esc(x.path) + '" title="' + esc(x.path) + '">' +
               esc(x.title.slice(0, 28)) + '</span>';
      }).join("") + '</div></div>';
  });
  if (ungrouped.length) {
    html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:13px;margin-bottom:5px">' +
      '尚未归题 <span class="pill">' + ungrouped.length + ' 篇</span></div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
      ungrouped.map(function (x) {
        return '<span class="ref" data-path="' + esc(x.path) + '" title="' + esc(x.path) + '">' +
               esc(x.title.slice(0, 28)) + '</span>';
      }).join("") + '</div></div>';
  }
  $("#topicBox").innerHTML = html;
  $$("#topicBox .ref").forEach(function (el) {
    el.onclick = function () { openNoteByPath(el.dataset.path); };
  });
}

/* ---------------------------- 设置 ---------------------------- */
/* schema 驱动渲染：common=true 的组默认展开，其余折叠进「高级设置」。
   config.ini 保持完整不删项 —— 只是把「看不懂的」收起来，并给每一项配人话说明。 */
const CFG_SCHEMA = [
  {
    sec: "AI", title: "AI 对话模型", common: true, test: "ai",
    desc: "决定回答由谁生成。什么都不改也能正常用 —— 会自动降级成本地检索摘要，不会崩。",
    fields: [
      { key: "provider", label: "AI 模式", type: "select",
        tip: "「自动」最省心：优先用本地 Ollama（免费、离线），不行再走云端 API，都没有就纯离线检索。",
        options: [["auto", "自动（推荐）"], ["ollama", "只用本地 Ollama"], ["api", "只用云端 API"], ["offline", "纯离线，不调用 AI"]] },
      { key: "ollama_host", label: "Ollama 服务地址", type: "text", placeholder: "http://127.0.0.1:11434",
        tip: "Ollama 装在本机就用默认值，别改。" },
      { key: "ollama_chat_model", label: "本地对话模型", type: "ollama-model",
        tip: "从你本机已安装的 Ollama 模型里挑一个。程序不会替你预设或自动选择任何模型；" +
             "本机还没有模型时请先用 Ollama 自行安装，然后点「刷新模型列表」。" },
      { key: "api_base_url", label: "云端接口地址", type: "preset-text", placeholder: "https://api.deepseek.com/v1",
        tip: "选一家常用服务商，或自己填兼容 OpenAI 协议的地址。",
        options: [["https://api.deepseek.com/v1", "DeepSeek"], ["https://dashscope.aliyuncs.com/compatible-mode/v1", "通义千问"], ["https://api.moonshot.cn/v1", "Kimi 月之暗面"], ["https://open.bigmodel.cn/api/paas/v4", "智谱 GLM"], ["https://api.openai.com/v1", "OpenAI"]] },
      { key: "api_key", label: "云端 API Key", type: "password",
        tip: "留空就不用云端。注意：这文件明文存在 U 盘上，别在借出去的电脑上留高余额 Key。" },
      { key: "api_chat_model", label: "云端对话模型", type: "datalist", placeholder: "deepseek-chat",
        tip: "填服务商文档里的模型名，比如 deepseek-chat。",
        options: ["deepseek-chat", "deepseek-reasoner", "qwen-plus", "qwen-turbo", "moonshot-v1-8k", "glm-4-flash", "gpt-4o-mini"] },
    ],
  },
  {
    sec: "GRAPH", title: "知识星图", common: true,
    desc: "只控制「星图」页的连线密度，怎么调都不会出错。",
    fields: [
      { key: "semantic_threshold", label: "语义连线阈值", type: "range", min: 0.70, max: 0.95, step: 0.01,
        tip: "越低连线越多（容易变毛线团），越高越清爽。节点很密就往右拉。" },
    ],
  },
  {
    sec: "AI", title: "向量检索（决定「怎么找」）", common: false,
    desc: "把问题变成向量做语义匹配。这一组程序会自己适配，基本不用碰。",
    fields: [
      { key: "embedding_source", label: "向量嵌入来源", type: "select",
        tip: "local_onnx 用随包自带的本地模型（bge-small-zh-v1.5，开箱可用、不联网）；"
             + "ollama 借本机 Ollama 算；api 用云端算。本机没有嵌入能力时会自动降级为纯词法检索，不会崩。",
        options: [["local_onnx", "本地 ONNX"], ["ollama", "本地 Ollama"], ["api", "云端 API"]] },
      { key: "embedding_model_name", label: "嵌入模型名", type: "ollama-model",
        tip: "用 Ollama 做嵌入时，从下拉里选（推荐 nomic-embed-text / bge-m3 这类 embedding 专用模型，别选 chat 模型）。用 local_onnx / api 时会自动变回手动填写。" },
      { key: "embedding_dim", label: "向量维度", type: "number", min: 64, max: 4096,
        tip: "用 ollama / api 时程序会自动探测并覆盖这个值，不用填。只有 local_onnx 才需要手填。改错会让向量检索被自动禁用。" },
      { key: "top_k_parents", label: "送入 AI 的段落数", type: "number", min: 1, max: 20,
        tip: "一次给大模型看几段背景。越多信息越全，但更慢、更费 token。默认 5。" },
      { key: "recall_candidates", label: "每路召回候选数", type: "number", min: 5, max: 100,
        tip: "词法和向量各捞多少个候选来融合排序。默认 20，不用动。" },
    ],
  },
  {
    sec: "CRAWLER", title: "网页抓取（剪藏行为）", common: false,
    desc: "只影响「剪藏」页抓网页的表现。",
    fields: [
      { key: "min_body_chars", label: "正文降级门限（字）", type: "number", min: 0,
        tip: "抓下来的正文少于这个字数，就判定为动态页面，自动改成「存快照 + 提示手动粘贴」。默认 150。" },
      { key: "enable_headless_chrome", label: "动态页渲染抓取", type: "select",
        tip: "开启后遇到动态页面会调用本机 Chrome 渲染（更慢更重）。默认关闭 —— 反爬页面不硬刚。",
        options: [["0", "关闭（推荐）"], ["1", "开启"]] },
      { key: "request_timeout", label: "请求超时（秒）", type: "number", min: 5, max: 120,
        tip: "默认 20 秒。" },
      { key: "snapshot_chars", label: "降级快照保留字数", type: "number", min: 100,
        tip: "默认 1000。" },
    ],
  },
  {
    sec: "SYSTEM", title: "系统（端口与后台扫描）", common: false,
    desc: "一般完全不用改。",
    fields: [
      { key: "port", label: "服务起始端口", type: "number", min: 1024, max: 65535,
        tip: "被占用时程序会自动 +1 往后找，最多试 10 个，所以这里基本不用改。" },
      { key: "host", label: "监听地址", type: "text",
        tip: "保持 127.0.0.1 —— 只有本机能访问。别改成 0.0.0.0，那会把整个知识库暴露给局域网。" },
      { key: "sync_interval", label: "外部文件扫描间隔（秒）", type: "number", min: 3,
        tip: "你用 Obsidian / Typora 改了笔记后，多久被同步进索引。默认 15 秒。" },
      { key: "log_level", label: "日志级别", type: "select",
        options: [["INFO", "INFO（默认）"], ["DEBUG", "DEBUG（排查问题用）"], ["WARNING", "WARNING"], ["ERROR", "ERROR"]] },
    ],
  },
];
const SENSITIVE = new Set(["AI.api_key"]);
let CFG_ORIGINAL = {};   // 记住加载时的值，用于检测「嵌入模型变了要重建索引」

function cfgVal(cfg, sec, key) {
  const v = cfg && cfg[sec] ? cfg[sec][key] : undefined;
  return (v === undefined || v === null) ? "" : v;
}

function cfgControl(sec, f, val) {
  const a = 'data-sec="' + esc(sec) + '" data-key="' + esc(f.key) + '"';
  switch (f.type) {
    case "select":
      return "<select " + a + ">" + f.options.map(o =>
        '<option value="' + esc(o[0]) + '"' + (String(val) === String(o[0]) ? " selected" : "") + ">" +
        esc(o[1]) + "</option>").join("") + "</select>";
    case "preset-text":
    case "datalist":
      return '<div class="ctl-inline"><input type="text" ' + a + ' list="dl_' + sec + "_" + f.key +
        '" value="' + esc(val) + '" placeholder="' + esc(f.placeholder || "") + '"><datalist id="dl_' +
        sec + "_" + f.key + '">' + (f.options || []).map(o =>
          Array.isArray(o) ? '<option value="' + esc(o[0]) + '">' + esc(o[1]) + "</option>"
                           : '<option value="' + esc(o) + '"></option>').join("") + "</datalist></div>";
    case "password":
      return '<input type="password" ' + a + ' value="' + esc(val) +
        '" placeholder="留空则不使用云端 API" autocomplete="off">';
    case "number":
      return '<input type="number" ' + a + ' value="' + esc(val) + '" style="max-width:140px"' +
        (f.min != null ? ' min="' + f.min + '"' : "") + (f.max != null ? ' max="' + f.max + '"' : "") + ">";
    case "range":
      return '<div class="ctl-inline"><input type="range" ' + a + ' min="' + f.min + '" max="' + f.max +
        '" step="' + f.step + '" value="' + esc(val) +
        '" oninput="this.closest(\'.field\').querySelector(\'.rangeval\').textContent=(+this.value).toFixed(2)">' +
        '<span class="rangeval">' + (+val).toFixed(2) + "</span></div>";
    case "ollama-model":
      // 对话模型：选中即持久化（P0-A），不要求再点「测试」/重启/刷新浏览器。
      // 嵌入模型复用同一控件，但不需要即时落盘，故只对 ollama_chat_model 挂 onchange。
      const selOnChange = f.key === "ollama_chat_model" ? ' onchange="onSelectChatModel(this)"' : "";
      return '<div class="ctl-inline ollama-ctl"><select ' + a + ' class="ollama-sel"' + selOnChange + '>' +
        '<option value="' + esc(val) + '">' + esc(val || "加载中…") + "</option></select>" +
        '<button class="btn" type="button" title="重新读取本机已安装的 Ollama 模型" onclick="refreshOllamaModels()">刷新模型列表</button></div>' +
        '<div class="tip ollama-hint">正在读取本地 Ollama 模型…</div>';
    default:
      return '<input type="text" ' + a + ' value="' + esc(val) + '" placeholder="' + esc(f.placeholder || "") + '">';
  }
}

function cfgGroup(g, cfg) {
  const body = g.fields.map(f => '<div class="field">' +
    '<div class="frow"><label>' + esc(f.label) + "</label></div>" +
    cfgControl(g.sec, f, cfgVal(cfg, g.sec, f.key)) +
    (f.tip ? '<div class="tip">' + esc(f.tip) + "</div>" : "") + "</div>").join("");
  const testbar = g.test === "ai"
    ? '<div class="testbar">' +
      '<button class="btn" type="button" title="会先自动保存当前表单，再真实跑一次调用" onclick="testAI(\'ollama\')">测试本地 Ollama</button>' +
      '<button class="btn" type="button" title="会先自动保存当前表单，再真实跑一次调用" onclick="testAI(\'api\')">测试云端 API</button>' +
      '<div class="test-out" id="testOut"></div></div>'
    : "";
  return '<div class="cfg-group"><h4>' + esc(g.title) + "</h4>" +
    (g.desc ? '<div class="gdesc">' + esc(g.desc) + "</div>" : "") + body + testbar + "</div>";
}

async function loadConfig() {
  const r = await api("/api/config");
  const cfg = r.data || {};
  CFG_ORIGINAL = JSON.parse(JSON.stringify(cfg));

  const seen = new Set();
  const common = [], advanced = [];
  CFG_SCHEMA.forEach(g => {
    g.fields.forEach(f => seen.add(g.sec + "." + f.key));
    (g.common ? common : advanced).push(cfgGroup(g, cfg));
  });
  // config.ini 里存在但 schema 未覆盖的键 —— 兜底渲染，保证保存时不丢
  const extra = [];
  Object.keys(cfg).forEach(sec => {
    const hidden = Object.keys(cfg[sec]).filter(k => !seen.has(sec + "." + k));
    if (!hidden.length) return;
    extra.push('<div class="cfg-group"><h4>' + esc(sec) + " · 其他</h4>" + hidden.map(k =>
      '<div class="field"><div class="frow"><label>' + esc(k) + "</label></div>" +
      '<input type="text" data-sec="' + esc(sec) + '" data-key="' + esc(k) +
      '" value="' + esc(cfg[sec][k]) + '"></div>').join("") + "</div>");
  });

  $("#cfgForm").innerHTML = common.join("");
  $("#cfgAdvanced").innerHTML = advanced.join("") + extra.join("");
  refreshOllamaModels();
}

/* Ollama 模型下拉（对话模型 + 嵌入模型通用）：在线就列出本机已装模型；
   不在线退化成手填，绝不丢用户已填的值 */
async function refreshOllamaModels() {
  const sels = $$(".ollama-sel");
  if (!sels.length) return;
  sels.forEach(s => { const h = s.closest(".ollama-ctl").parentElement.querySelector(".ollama-hint");
                      if (h) h.textContent = "正在读取本地 Ollama 模型…"; });
  let d = null;
  try {
    const r = await api("/api/ollama/models");
    d = r.data || {};
  } catch (e) {
    sels.forEach(sel => {
      const h = sel.closest(".ollama-ctl").parentElement.querySelector(".ollama-hint");
      if (h) h.textContent = "读取失败：" + e.message;
    });
    return;
  }

  sels.forEach(sel => {
    const current = String(sel.value || "").trim();
    const field = sel.closest(".ollama-ctl").parentElement;
    const hint = field.querySelector(".ollama-hint");
    if (!d.available) {
      const inp = document.createElement("input");
      inp.type = "text";
      inp.dataset.sec = sel.dataset.sec;
      inp.dataset.key = sel.dataset.key;
      inp.value = current;
      inp.placeholder = "填写你本机已安装的模型名";
      sel.replaceWith(inp);
      if (hint) hint.innerHTML = "⚠ " + esc(d.detail || "未检测到本地 Ollama") +
        " —— 已切换为手动填写。装好 Ollama 后点「刷新模型列表」可恢复下拉。";
      return;
    }
    const models = d.models || [];
    const bits = m => [m.params, m.size_text].filter(Boolean).join(" · ");
    const installed = models.some(m => m.name === current);
    sel.innerHTML = '<option value="">— 未选择（请从下方本机模型里挑一个）—</option>' +
      (current && !installed
        ? '<option value="' + esc(current) + '">' + esc(current) + "（原配置，本机未找到）</option>"
        : "") +
      models.map(m =>
        '<option value="' + esc(m.name) + '">' + esc(m.name) +
        (bits(m) ? "　" + esc(bits(m)) : "") + "</option>").join("");
    // ⚠ **绝不自动选中 models[0]**：用户没选就保持「未选择」。
    //   本机模型可能是 embedding 模型、vision 模型，或资源要求过高的模型 ——
    //   替用户挑一个等于偷偷替他拍板。选了就在保存时显式持久化。
    sel.value = current;
    if (hint) {
      if (!models.length) {
        hint.innerHTML = "⚠ Ollama 在线，但本机尚未安装任何模型 —— " +
          "请先用 Ollama 装好模型，再点「刷新模型列表」。";
      } else if (!current) {
        hint.innerHTML = "发现 " + models.length + " 个本机模型，请选择一个作为本地对话模型" +
          "（程序不会自动替你选）。";
      } else if (!installed) {
        hint.innerHTML = "⚠ 原配置的「" + esc(current) + "」在本机已不存在，请重新选择。";
      } else {
        hint.innerHTML = "✅ 当前模型：" + esc(current) + "　（Ollama：" + esc(d.host) + "）";
      }
    }
  });
}

// P0-A：从「本地对话模型」下拉框选择模型 → 立即持久化 AI.ollama_chat_model，
// 刷新 AI readiness，状态立即变 ready，下一次问答直接用 —— 无需点「测试」/重启/刷新页面。
// 「测试本地 Ollama」仅保留为诊断按钮，不再承担「保存选择」的职责。
async function onSelectChatModel(sel) {
  const val = sel ? sel.value : "";
  try {
    const r = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ data: { AI: { ollama_chat_model: val } } }),
    });
    if (r.code !== 200) { toast("⚠ 模型选择保存失败：" + (r.message || "")); return; }
    // 同步 CFG_ORIGINAL，避免后续「保存配置」把它覆盖回旧值
    if (!CFG_ORIGINAL.AI) CFG_ORIGINAL.AI = {};
    CFG_ORIGINAL.AI.ollama_chat_model = val;
    // 立即刷新后端就绪状态（只读缓存、不发网络请求），UI 状态马上更新
    await pollStatus(false);
    const hint = sel && sel.closest(".ollama-ctl") && sel.closest(".ollama-ctl").parentElement
      ? sel.closest(".ollama-ctl").parentElement.querySelector(".ollama-hint") : null;
    if (hint) {
      hint.innerHTML = val
        ? ("✅ 已选择本地模型：" + esc(val) + "（已保存，可直接提问）")
        : "已清空本地模型选择";
    }
    toast(val ? ("已选择并保存本地模型：" + val) : "已清空本地模型选择");
  } catch (e) {
    toast("⚠ 模型选择保存失败：" + e.message);
  }
}

async function testAI(target) {
  const out = $("#testOut");
  out.className = "test-out loading";
  out.textContent = target === "ollama"
    ? "正在调用本地 Ollama（首次要加载模型，可能等几秒）…"
    : "正在调用云端 API…";
  await saveConfig(true);   // 测的是你刚改的值，先落盘
  const r = await api("/api/ai/test", { method: "POST", body: JSON.stringify({ target }) });
  const d = r.data || {};
  out.className = "test-out " + (d.ok ? "ok" : "bad");
  out.textContent = (d.ok ? "✅ " : "❌ ") + (d.detail || r.message || "未知结果");
}

async function saveConfig(silent) {
  const data = {};
  $$("#cfgForm [data-sec], #cfgAdvanced [data-sec]").forEach(i => {
    const sec = i.dataset.sec;
    data[sec] = data[sec] || {};
    data[sec][i.dataset.key] = i.value;
  });
  const r = await api("/api/config", { method: "POST", body: JSON.stringify({ data }) });
  if (r.code !== 200) { if (!silent) toast("⚠ " + r.message); return false; }

  // ⚠ 必须在覆盖 CFG_ORIGINAL 之前比对，否则永远检测不到变化
  const embKeys = ["embedding_source", "embedding_model_name", "embedding_dim"];
  const prev = CFG_ORIGINAL.AI || {};
  const changed = embKeys.some(k => String((data.AI || {})[k]) !== String(prev[k]));
  CFG_ORIGINAL = JSON.parse(JSON.stringify(data));

  if (changed) {
    toast("⚠ 嵌入模型相关设置已变更，请点下方「重建向量索引」，否则向量检索会被禁用", 6000);
  }
  if (!silent) toast("✅ 配置已保存（重启程序后完全生效）");
  return true;
}

async function rebuild(vec) {
  const log = $("#rebuildLog");
  log.innerHTML = '<span class="spin"></span> 已在后台启动…';
  const r = await api(vec ? "/api/system/rebuild-vectors" : "/api/system/rebuild-index", {
    method: "POST", body: "{}",
  });
  toast(r.message || "任务已启动");
  setTimeout(async () => {
    await loadNotes(); await loadGraph(); await pollStatus(false);
    log.textContent = "任务已提交后台执行，可继续使用其他功能。";
  }, 2500);
}

/* ---------------------------- 标签页 ---------------------------- */
function switchTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === "panel-" + name));
  if (name === "graph") setTimeout(loadGraph, 40);
  if (name === "notes") loadNotes();
  if (name === "settings") { loadConfig(); pollStatus(false); }
}

/* 问答引用跳转（B）：
 * 正文角标 [N]（.cite）= **证据**：打开笔记 → 按 parent_id 定位证据块 → 滚动 + 高亮；
 * 底部来源 chips（.ref）= **文档**：只打开整篇资料，不强制跳某个 Parent。
 * 语义区分：数字 = 证据，来源名 = 文档。 */
document.addEventListener("click", function (ev) {
  const chip = ev.target && ev.target.closest ? ev.target.closest(".cite,.ref") : null;
  if (!chip) return;
  const id = chip.dataset.ref;
  const ref = (S.refs || []).find(function (r) { return String(r.id) === String(id); });
  if (!ref) { toast("该引用没有对应的笔记路径", 2600); return; }
  const isCite = chip.classList.contains("cite");
  openNoteByPath(ref.path, isCite ? {
    parent_id: ref.parent_id || "",
    snippet: ref.snippet || ""
  } : null);
});

/* ---------------------------- 绑定 ---------------------------- */
$$(".tab").forEach(t => t.onclick = () => switchTab(t.dataset.tab));
$("#btnSend").onclick = send;
$("#chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#btnCapture").onclick = doCapture;
$("#capUrl").addEventListener("keydown", e => { if (e.key === "Enter") doCapture(); });
$("#btnSaveNote").onclick = doSaveNote;
$("#noteFilter").oninput = renderNoteList;
$("#btnCfgSave").onclick = () => saveConfig(false);
$("#btnCfgReload").onclick = loadConfig;
$("#btnRebuild").onclick = () => rebuild(false);
$("#btnRebuildVec").onclick = () => rebuild(true);
$("#btnGraphReload").onclick = loadGraph;
$("#topicMin").onchange = loadGraph;
$("#btnRefresh").onclick = async () => { await pollStatus(false); await loadNotes(); toast("状态已刷新"); };

$("#btnShutdown").onclick = async () => {
  if (!confirm("将执行 WAL 检查点并释放数据库锁，确保 U 盘可直接拔出。\n\n确定要安全退出吗？")) return;
  try {
    await api("/api/system/shutdown", { method: "POST", body: "{}" });
    document.body.innerHTML =
      '<div style="display:grid;place-items:center;height:100vh;font:15px/1.8 sans-serif;color:#0f172a;text-align:center">' +
      "<div><h2 style='margin:0 0 8px'>已完成安全退出</h2>" +
      "<p style='color:#64748b'>WAL 已刷回主库、临时文件已清理，现在可以安全拔出 U 盘。</p></div></div>";
    setTimeout(() => window.close(), 1200);
  } catch (e) { toast("退出请求失败：" + e.message); }
};

/* ---------------------------- 启动 ---------------------------- */
initDropZone();
loadImportFormats();
loadConfig();
loadNotes();
pollStatus(true);

$$("#noteSeg button").forEach(b => {
  b.onclick = () => { if (!b.disabled) setNoteView(b.dataset.view); };
});
