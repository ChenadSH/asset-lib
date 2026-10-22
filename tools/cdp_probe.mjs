#!/usr/bin/env node
/**
 * 无头浏览器探针：不依赖 Puppeteer/Playwright，直接用 CDP 验证前端。
 *
 * 用法：
 *   1) 先启动带调试端口的浏览器
 *      msedge --headless=new --remote-debugging-port=9223 \
 *             --user-data-dir=/tmp/cdp-profile http://127.0.0.1:8765/
 *   2) node tools/cdp_probe.mjs [url] [scrollRounds]
 *
 * 会做：等待首屏 → 反复滚到底部并统计卡片数 → 检查 #loading 是否隐藏 → 截图到 data/tmp/probe.png
 */
const PORT = Number(process.env.CDP_PORT || 9223);
const TARGET_URL = process.argv[2] || 'http://127.0.0.1:8765/';
const ROUNDS = Number(process.argv[3] || 4);

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function findTarget() {
  for (let i = 0; i < 24; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      const page = list.find(t => t.type === 'page' && t.url.startsWith(new URL(TARGET_URL).origin));
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* 浏览器还没起来 */ }
    await sleep(500);
  }
  throw new Error(`连不上 CDP ${PORT}，请确认浏览器已带 --remote-debugging-port=${PORT} 启动`);
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    ws.onopen = () => resolve({
      send(method, params = {}) {
        return new Promise((res, rej) => {
          const mid = ++id;
          pending.set(mid, { res, rej });
          ws.send(JSON.stringify({ id: mid, method, params }));
        });
      },
      close: () => ws.close(),
    });
    ws.onerror = reject;
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { res, rej } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      }
    };
  });
}

const cdp = await connect(await findTarget());
await cdp.send('Runtime.enable');
await cdp.send('Page.enable');

const evaluate = async expr => (await cdp.send('Runtime.evaluate', {
  expression: expr, returnByValue: true, awaitPromise: true,
})).result.value;

await sleep(4000);   // 等首屏（模型未加载时首屏也可能是 2~3 秒）

console.log(`目标页面：${TARGET_URL}`);
console.log(`首屏卡片：${await evaluate(`document.querySelectorAll('#grid .card').length`)}`);

for (let i = 1; i <= ROUNDS; i++) {
  const before = await evaluate(`document.querySelectorAll('#grid .card').length`);
  await evaluate('window.scrollTo(0, document.body.scrollHeight)');
  await sleep(2500);
  const after = await evaluate(`document.querySelectorAll('#grid .card').length`);
  const hidden = await evaluate(`document.getElementById('loading').hidden`);
  const st = await evaluate(`JSON.stringify({offset: state.offset, total: state.total, done: state.done, loading: state.loading})`);
  const verdict = after > before ? '✓ 增长' : (await evaluate(`state.done`) ? '· 已到末尾' : '✗ 未增长');
  console.log(`第 ${i} 轮：${before} → ${after} ${verdict} | #loading.hidden=${hidden} | ${st}`);
  if (after === before && await evaluate('state.done')) break;
}

const { writeFileSync, mkdirSync } = await import('node:fs');
mkdirSync('tmp', { recursive: true });
const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
writeFileSync('tmp/probe.png', Buffer.from(shot.data, 'base64'));
console.log('截图已保存：tmp/probe.png');

cdp.close();
process.exit(0);
