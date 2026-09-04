/**
 * H1 前端回归：api() 自动带 X-Auth-Token，401 时弹框+重试。
 * 用 jsdom + 假 fetch 跑，验证 wrapper 行为。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', '..', 'frontend', 'index.html'), 'utf8');
// 取出 <script>...</script> 第一段（包含 api 封装）
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('no <script> found'); process.exit(1); }
const scriptText = m[1];
// 只截取 api 相关片段（从 EOS_TOKEN_KEY 到 api 结尾的右花括号后面多取 5 行保险）
const start = scriptText.indexOf('EOS_TOKEN_KEY');
const endSnippet = scriptText.indexOf("function esc(s)");
const snippet = scriptText.slice(0, endSnippet);
if (start < 0 || endSnippet < 0) {
  console.error('snippet anchors not found', { start, endSnippet });
  process.exit(1);
}

let pass = 0, fail = 0;
function t(name, ok, detail='') {
  const mark = ok ? 'PASS' : 'FAIL';
  console.log(`[${mark}] ${name} ${detail}`);
  ok ? pass++ : fail++;
}

async function withDom(token, fakeFetch) {
  // 起 jsdom
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { runScripts: 'outside-only' });
  // 用一个独立对象当 localStorage，避免 jsdom Storage 抛错
  const ls = {
    _s: {},
    getItem(k){ return Object.prototype.hasOwnProperty.call(this._s, k) ? this._s[k] : null; },
    setItem(k,v){ this._s[k] = String(v); },
    removeItem(k){ delete this._s[k]; }
  };
  // 把我们的 ls 装到 dom.window 上
  Object.defineProperty(dom.window, 'localStorage', { value: ls, configurable: true });
  dom.window.prompt = () => token === null ? null : token;
  // 把 fetch/prompt 显式注入到 snippet 闭包
  // eslint-disable-next-line no-new-func
  const fn = new dom.window.Function(
    'fetch', 'localStorage', 'prompt',
    snippet + '\n;return {api, apiRaw, getEosToken, setEosToken, promptEosToken};'
  );
  const exports = fn(fakeFetch, ls, dom.window.prompt);
  return { dom, ...exports };
}

(async () => {
  // 场景 A：服务端不要求口令（永远 200），不带 token，请求成功
  {
    const seen = [];
    const fakeFetch = async (url, opt) => {
      seen.push({ url, hdrs: opt.headers });
      return { status: 200, json: async () => ({ ok: true, url }) };
    };
    const { api, getEosToken } = await withDom(null, fakeFetch);
    const r = await api('/today');
    t('A: 200 with no token', r.ok === true, JSON.stringify(r));
    t('A: fetch called once', seen.length === 1, `seen=${seen.length}`);
    t('A: no X-Auth-Token header', !seen[0].hdrs['X-Auth-Token'], JSON.stringify(seen[0].hdrs));
  }

  // 场景 B：401 + 用户输入正确 token → 重试成功，localStorage 写入了
  {
    const seen = [];
    let calls = 0;
    const fakeFetch = async (url, opt) => {
      calls++;
      seen.push({ url, hdrs: opt.headers });
      if (calls === 1) return { status: 401, json: async () => ({ ok: false, error: 'unauth' }) };
      return { status: 200, json: async () => ({ ok: true }) };
    };
    const { api, getEosToken } = await withDom('my-pass', fakeFetch);
    const r = await api('/today');
    t('B: retry succeeds', r.ok === true, JSON.stringify(r));
    t('B: fetched twice', calls === 2, `calls=${calls}`);
    t('B: second call has X-Auth-Token', seen[1].hdrs['X-Auth-Token'] === 'my-pass', JSON.stringify(seen[1].hdrs));
    t('B: localStorage has token', getEosToken() === 'my-pass', `got=${getEosToken()}`);
  }

  // 场景 C：401 + 用户输入错误 token → 第二次还 401 → 清掉 token，返错
  {
    const seen = [];
    let calls = 0;
    const fakeFetch = async (url, opt) => {
      calls++;
      seen.push({ url, hdrs: opt.headers });
      return { status: 401, json: async () => ({ ok: false, error: 'unauth' }) };
    };
    const { api, getEosToken } = await withDom('WRONG', fakeFetch);
    const r = await api('/today');
    t('C: still 401 returned', r.ok === false, JSON.stringify(r));
    t('C: fetched twice', calls === 2, `calls=${calls}`);
    t('C: token cleared after wrong', getEosToken() === '', `got=${getEosToken()}`);
  }

  // 场景 D：401 + 用户取消 → 不重试
  {
    let calls = 0;
    const fakeFetch = async (url, opt) => {
      calls++;
      return { status: 401, json: async () => ({ ok: false, error: 'unauth' }) };
    };
    const { api, getEosToken } = await withDom(null /* cancel */, fakeFetch);
    const r = await api('/today');
    t('D: cancel returns original 401', r.error && r.error.includes('unauth'), JSON.stringify(r));
    t('D: fetched once', calls === 1, `calls=${calls}`);
    t('D: no token saved', getEosToken() === '', `got=${getEosToken()}`);
  }

  // 场景 E：localStorage 已有 token，请求头自动带上
  {
    const seen = [];
    const fakeFetch = async (url, opt) => {
      seen.push(opt.headers);
      return { status: 200, json: async () => ({ ok: true }) };
    };
    const { dom, setEosToken, api } = await withDom(null, fakeFetch);
    setEosToken('persisted');
    await api('/today');
    t('E: stored token sent in header', seen[0]['X-Auth-Token'] === 'persisted', JSON.stringify(seen[0]));
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) process.exit(1);
})();
