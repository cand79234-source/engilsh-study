/* 在 Node 里跑真实前端脚本，验证「复制单题 / 复制全部」的文案完整性。
   用法：node copy_test.js <index.html 路径> <后端真实响应 fixture.json> <输出.json>

   不做完整 DOM，只桩出脚本真正用到的那几个接口。
   复制走 navigator.clipboard.writeText，把文本截下来交给 Python 断言。 */
const fs = require('fs');
const vm = require('vm');

const htmlPath = process.argv[2];
const fxPath = process.argv[3];
const outPath = process.argv[4];
const fx = JSON.parse(fs.readFileSync(fxPath, 'utf8'));

const html = fs.readFileSync(htmlPath, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('找不到 <script> 块'); process.exit(1); }
// 去掉启动时的自动渲染（我们没有真实后端可打）
let code = m[1].replace(/^\s*render\('learn'\);\s*$/m, '');
// vm 里 const/let 不会挂到 sandbox 对象上，脚本末尾显式导出要用的东西
code += `
;__X = {
  state: state,
  copyOne: copyOne, copyAll: copyAll, copyAllText: copyAllText,
  attemptCopyText: attemptCopyText, submitSentence: submitSentence,
  histOf: histOf, renderAttempts: renderAttempts, planProgress: planProgress,
  planKeys: planKeys, esc: esc
};
`;

/* ---------- 极简 DOM 桩 ---------- */
const els = {};
function mkEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', disabled: false,
    style: { cssText: '', display: '' }, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    focus() {}, select() {}, setSelectionRange() {}, remove() {},
    setAttribute() {}, getAttribute() { return null; },
    querySelectorAll() { return []; }, closest() { return null; },
    insertAdjacentHTML(pos, h) { this.innerHTML += h; },
    appendChild() {}, offsetHeight: 0,
  };
}
const document = {
  getElementById(id) { if (!els[id]) els[id] = mkEl(id); return els[id]; },
  querySelector(sel) { if (!els['Q' + sel]) els['Q' + sel] = mkEl(sel); return els['Q' + sel]; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return mkEl('tmp'); },
  body: { appendChild() {} },
};

/* ---------- fetch 桩：按句子回真实的后端批改结果 ---------- */
const captured = [];
const callLog = [];
function fakeFetch(url, opt) {
  callLog.push(url);
  if (url.indexOf('/api/sentence/check') === 0) {
    const body = JSON.parse(opt.body);
    const key = body.sentence.trim();
    const r = fx.checks[key];
    if (!r) return Promise.resolve({ json: () => ({ error: 'fixture 里没有这句：' + key }) });
    return Promise.resolve({ json: () => r });
  }
  if (url.indexOf('/api/sentence/attempts') === 0) {
    return Promise.resolve({ json: () => fx.attempts || { groups: [] } });
  }
  return Promise.resolve({ json: () => ({}) });
}

const ctx = {
  document, fetch: fakeFetch, console,
  navigator: { clipboard: { writeText: (t) => { captured.push(t); return Promise.resolve(); } } },
  window: { isSecureContext: true },
  setTimeout, clearTimeout, requestAnimationFrame: (f) => f(),
  Date, JSON, Math, String, Number, Object, Array, RegExp, Error, Promise,
  parseInt, parseFloat, encodeURIComponent, decodeURIComponent, isNaN,
};
ctx.globalThis = ctx;
ctx.__X = null;
vm.createContext(ctx);

try {
  vm.runInContext(code, ctx, { filename: 'index.html<script>' });
} catch (e) {
  console.error('前端脚本执行失败：', e && e.stack || e);
  process.exit(2);
}

/* ---------- 用 fixture 里的真实计划渲染，再提交两次作答 ---------- */
(async () => {
  const out = { captured: [], errors: [] };
  try {
    const X = ctx.__X;
  X.state.today = fx.today;
    X.state.tkMeta = {};
    const p = fx.today.sentence_plan;
    p.basic.forEach((b, i) => {
      const tk = 'basic:' + i;
      X.state.tkMeta[tk] = { word: b.word || '', task: b.task || '', label: b.task || '' };
    });
    (p.upgrade || []).forEach((u, i) => {
      X.state.tkMeta['up:' + i] = { word: u.word || '', task: u.task || '' };
    });
    (p.combo || []).forEach((c, i) => {
      X.state.tkMeta['combo:' + i] = {
        word: (c.words || []).map(w => w.word).join(' '), task: c.scene || '' };
    });

    X.state.hist = {};
    for (const step of (fx.steps || [])) {
      const inp = document.getElementById('in_' + step.task_key);
      inp.value = step.sentence;
      await X.submitSentence(step.task_key);
      out.errors.push({ task_key: step.task_key, sentence: step.sentence,
                        hist: X.histOf(step.task_key).length });
    }

    // 复制单题（第 1 次 / 最新一次 各来一份）
    for (const c of (fx.copy_one || [])) {
      captured.length = 0;
      X.copyOne(c.task_key, c.which);
      out.captured.push({ name: c.name, text: captured[0] || '' });
    }
    // 复制全部
    captured.length = 0;
    X.copyAll();
    out.captured.push({ name: 'copyAll', text: captured[0] || '' });

    out.callLog = callLog;
    out.histKeys = Object.keys(X.state.hist || {});
  } catch (e) {
    out.fatal = String(e && e.stack || e);
  }
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
})();
