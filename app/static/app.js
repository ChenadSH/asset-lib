'use strict';

const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, html) => { const e = document.createElement(t); if (c) e.className = c; if (html != null) e.innerHTML = html; return e; };

const state = {
  q: '',
  mode: 'name',
  kinds: new Set(),
  tops: new Set(),
  offset: 0,
  limit: 60,
  total: 0,
  loading: false,
  done: false,
  seq: 0,
  lastItems: [],
  imageSearching: false,
};

const KIND_LABEL = {
  raster: '图片', design: '设计源文件', raw: 'RAW', doc: 'PDF/PPT',
  video: '视频', audio: '音频', other: '其他',
};

/* ---------------- 工具 ---------------- */
function fmtSize(n) {
  if (!n && n !== 0) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)) + ' ' + u[i];
}
function fmtDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function hl(text, q) {
  const t = esc(text);
  if (!q || q.length < 1) return t;
  const idx = t.toLowerCase().indexOf(esc(q).toLowerCase());
  if (idx < 0) return t;
  const a = esc(q).length;
  return t.slice(0, idx) + '<mark>' + t.slice(idx, idx + a) + '</mark>' + t.slice(idx + a);
}
let toastTimer = null;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2200);
}

/* 带超时的 fetch：后台扫描/写库时接口可能被拖慢，绝不让加载状态永久卡死 */
async function apiFetch(url, ms = 12000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms);
  try { return await fetch(url, { signal: ac.signal }); }
  finally { clearTimeout(timer); }
}

/* ---------------- 状态 ---------------- */
let statusPolling = false;   // 上一轮状态轮询未结束时不再叠加，避免请求堆积
async function refreshStatus() {
  if (statusPolling) return;
  statusPolling = true;
  try {
    const s = await (await apiFetch('/api/stats', 6000)).json();
    const pill = $('#status'), txt = $('#statusText');
    const cov = s.coverage || {};
    const vp = cov.searchable ? Math.round(cov.vectorized / cov.searchable * 100) : 0;
    pill.className = 'status';
    if (!s.source_online) { pill.classList.add('off'); txt.textContent = 'E 盘离线 · 只读缓存'; }
    else if (s.scan.running) {
      pill.classList.add('busy');
      // 显示已进行时长，让用户看出扫描在推进而不是卡住
      const elapsed = s.scan.started_at ? Math.round(Date.now() / 1000 - s.scan.started_at) : 0;
      txt.textContent = '正在扫描 E 盘' + (elapsed > 3 ? ` · 已 ${fmtDur(elapsed)}` : '…');
    }
    else if (s.queue > 0) {
      pill.classList.add('busy');
      const pct = s.thumbs + s.queue > 0 ? Math.round(s.thumbs / (s.thumbs + s.queue) * 100) : 0;
      txt.textContent = `缩略图 ${pct}% · 向量 ${vp}% · 待处理 ${s.queue.toLocaleString()}`;
    } else { pill.classList.add('ok'); txt.textContent = `${s.thumbs.toLocaleString()} 张已就绪 · 向量 ${vp}%`; }
    $('#modeImage').disabled = false;
    const rb = $('#rescanBtn');
    if (s.scan.running) { rb.disabled = true; rb.textContent = '扫描中…'; }
    else { rb.disabled = false; rb.textContent = '重新扫描'; }
    return s;
  } catch (e) { /* 本轮失败忽略，下轮 5s 后重试 */
  } finally { statusPolling = false; }
}

async function loadFacets() {
  const f = await (await apiFetch('/api/facets', 10000)).json();
  const kc = $('#kindChips'), tc = $('#topChips');
  kc.innerHTML = ''; tc.innerHTML = '';
  f.kinds.filter(k => k[1] > 0).slice(0, 6).forEach(([k, n]) => {
    const b = el('button', 'chip', `${KIND_LABEL[k] || k}<span class="n">${n.toLocaleString()}</span>`);
    b.dataset.v = k;
    b.onclick = () => { toggleSet(state.kinds, k); b.classList.toggle('on'); reset(); };
    kc.appendChild(b);
  });
  f.tops.filter(t => t[1] > 0 && t[0]).slice(0, 6).forEach(([t, n]) => {
    const short = t.length > 24 ? t.slice(0, 23) + '…' : t;
    const b = el('button', 'chip', esc(short));
    b.dataset.v = t; b.title = t;
    b.onclick = () => { toggleSet(state.tops, t); b.classList.toggle('on'); reset(); };
    tc.appendChild(b);
  });
}

function toggleSet(set, v) { set.has(v) ? set.delete(v) : set.add(v); }

/* ---------------- 搜索 ---------------- */
let searchAbort = null;   // 进行中的搜索请求；新查询会取消它，避免旧响应写进新结果

function reset() {
  // 在画面模式点筛选项/改关键词时，自动退回名称检索，否则 search() 会被直接跳过
  if (state.mode === 'image' && !state.imageSearching) {
    clearQueryThumb(false);
    setMode('name');
  }
  state.offset = 0; state.done = false; state.lastItems = [];
  $('#grid').innerHTML = '';
  search();
}

async function search(append = false) {
  if (state.mode === 'image') return;   // 画面模式由 searchByImage 驱动，不走这里
  // 追加时防重；但新查询（改词/改筛选 reset）永远可以打断正在进行的旧请求
  if (append && (state.loading || state.done)) return;

  if (searchAbort) { try { searchAbort.abort(); } catch { /* ignore */ } }
  const ac = new AbortController();
  searchAbort = ac;
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ac.abort(); }, 15000);

  state.loading = true;
  const mySeq = ++state.seq;
  let ok = false;
  $('#loading').hidden = false;

  const params = new URLSearchParams();
  params.set('offset', state.offset);
  params.set('limit', state.limit);
  if (state.kinds.size) params.set('kind', [...state.kinds].join(','));
  if (state.tops.size) params.set('top', [...state.tops].join('||'));

  let url;
  params.set('q', state.q);
  url = '/api/search?' + params.toString();

  try {
    const r = await fetch(url, { signal: ac.signal });
    if (!r.ok) throw new Error('服务返回 ' + r.status);
    const d = await r.json();
    if (mySeq !== state.seq) return;

    if (!append) $('#grid').innerHTML = '';
    state.total = d.total;
    d.items.forEach(it => $('#grid').appendChild(renderCard(it)));

    state.offset += d.items.length;
    if (d.items.length < state.limit) state.done = true;

    $('#empty').hidden = !(state.total === 0);
    if (state.total === 0) setEmpty('没有匹配的素材', '试试更短的关键词，或者换个筛选条件');
    const modeTxt = d.mode === 'fts' ? '全文索引' : d.mode === 'like' ? '短词匹配' : '全部素材';
    const num = d.total.toLocaleString() + (d.capped ? '+' : '');
    const tail = d.capped && state.done ? ' · 已达检索上限，加关键词可缩小范围' : '';
    $('#metaLine').innerHTML = state.q
      ? `命中 <b>${num}</b> 条 · ${modeTxt} · ${d.took_ms} ms${tail}`
      : `共 <b>${num}</b> 条素材 · 按修改时间排序${tail}`;
    ok = true;
  } catch (e) {
    if (e.name === 'AbortError') {
      if (timedOut && mySeq === state.seq) toast('搜索超时：后台可能正在扫描/写库，请重试');
    } else {
      console.error(e);
      toast('搜索失败：' + (e.message || e));
    }
  } finally {
    clearTimeout(timer);
    if (mySeq === state.seq) {
      state.loading = false;
      searchAbort = null;
      $('#loading').hidden = true;
    }
    // 内容不足一屏时自动再拉一页；交叉观察器可能因元素一直在视口内而不再触发
    if (ok && mySeq === state.seq) setTimeout(maybeLoadMore, 60);
  }
}

/* 兜底续加载：sentinel 仍在视口附近且还有数据就继续拉 */
function maybeLoadMore() {
  if (state.mode !== 'name' || state.loading || state.done) return;
  const s = $('#sentinel');
  if (s && s.getBoundingClientRect().top < window.innerHeight + 600 && state.total > state.offset) {
    search(true);
  }
}

function renderCard(it) {
  const card = el('div', 'card');
  const tw = el('div', 'thumb');
  if (it.thumb) {
    const img = el('img');
    img.loading = 'lazy'; img.src = it.thumb; img.alt = it.name;
    img.onerror = () => { img.remove(); tw.appendChild(placeholder(it)); };
    tw.appendChild(img);
  } else {
    tw.appendChild(placeholder(it));
  }
  if (it.same_source) card.appendChild(el('div', 'badge src', '同源'));
  card.appendChild(tw);

  const cap = el('div', 'cap');
  cap.appendChild(el('div', 'nm', hl(it.name, state.q)));
  let sub = `${(it.ext || '').toUpperCase()} · ${fmtSize(it.size)}`;
  if (it.match === 'fingerprint') sub += ` · 指纹一致 d=${it.dist}`;
  else if (typeof it.score === 'number') sub += ` · 相似 ${Math.round(it.score * 100)}%`;
  cap.appendChild(el('div', 'sub', sub));
  card.appendChild(cap);

  card.onclick = () => openLightbox(it);
  return card;
}

function placeholder(it) {
  const p = el('div', 'ph');
  p.appendChild(el('div', 'ext', (it.ext || '?').toUpperCase()));
  const indexable = ['raster', 'design', 'raw', 'doc'].includes(it.kind);
  p.appendChild(el('div', null, indexable ? '缩略图生成中' : ''));
  return p;
}

/* ---------------- 灯箱 ---------------- */
let current = null;
function openLightbox(it) {
  current = it;
  $('#lbName').textContent = it.name;
  $('#lbPath').textContent = it.path;
  $('#lbMeta').textContent = [
    (it.ext || '').toUpperCase(), fmtSize(it.size), fmtDate(it.mtime),
    it.year ? it.year + ' 年' : '', it.top || ''
  ].filter(Boolean).join(' · ');
  const img = $('#lbImg');
  img.src = it.thumb || '';
  img.style.display = it.thumb ? 'block' : 'none';
  $('#lightbox').hidden = false;
}
function closeLightbox() { $('#lightbox').hidden = true; current = null; }

/* ---------------- 以图搜图 ---------------- */
let queryURL = null;

/* 搜索框里的「当前搜索图」+ 进度条 */
function showQueryThumb(file) {
  clearQueryThumb(false);
  queryURL = URL.createObjectURL(file);
  const img = $('#qthumbImg');
  img.src = queryURL;
  img.title = `${file.name} · ${fmtSize(file.size)}`;
  $('#qthumb').hidden = false;
  $('#qthumb').classList.add('busy');
  $('#q').value = '';
  state.q = '';
  $('#clearq').hidden = true;
  $('#qimgClear').hidden = false;
}

function clearQueryThumb(resetMode = true) {
  if (queryURL) { URL.revokeObjectURL(queryURL); queryURL = null; }
  $('#qthumb').hidden = true;
  $('#qthumb').classList.remove('busy');
  $('#qthumbImg').removeAttribute('src');
  $('#qimgClear').hidden = true;
  hideProgress();
  if (resetMode) setMode('name');
}

function setProgress(pct, text) {
  const bar = $('#qbar');
  bar.hidden = false;
  bar.classList.remove('done', 'err');
  $('#qbarFill').style.width = Math.max(3, Math.min(100, pct)) + '%';
  const p = $('#qprog');
  p.hidden = false;
  p.classList.remove('err');
  p.textContent = text || Math.round(pct) + '%';
  p.title = text || '';
}

function finishProgress(text) {
  setProgress(100, text || '完成');
  $('#qbar').classList.add('done');
  $('#qthumb').classList.remove('busy');
  setTimeout(() => { if (!state.imageSearching) hideProgress(); }, 1800);
}

function failProgress(text) {
  $('#qbar').hidden = false;
  $('#qbar').classList.remove('done');
  $('#qbar').classList.add('err');
  $('#qbarFill').style.width = '100%';
  const p = $('#qprog');
  p.hidden = false;
  p.classList.add('err');
  p.textContent = text || '失败';
  $('#qthumb').classList.remove('busy');
}

function hideProgress() {
  $('#qbar').hidden = true;
  $('#qbar').classList.remove('done', 'err');
  $('#qbarFill').style.width = '0%';
  const p = $('#qprog');
  p.hidden = true; p.textContent = ''; p.classList.remove('err');
}

/* 读 NDJSON 流 */
async function readEvents(resp, onEvent) {
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

function covNote(cov) {
  if (!cov || !cov.searchable) return '';
  const vp = Math.round(cov.vectorized / cov.searchable * 100);
  if (vp >= 95) return '';
  const fp = Math.round(cov.fingerprinted / cov.searchable * 100);
  return `<div class="covline">向量索引覆盖 ${vp}%（${cov.vectorized.toLocaleString()} / ${cov.searchable.toLocaleString()}）`
    + ` · 指纹覆盖 ${fp}%。未覆盖的素材这次还搜不到，后台编码正在推进。</div>`;
}

async function searchByImage(file) {
  if (!file) return;
  state.seq++;                       // 作废进行中的文本搜索
  const mySeq = state.seq;
  state.imageSearching = true;
  state.mode = 'image';
  setMode('image');
  showQueryThumb(file);
  if (!$('#qthumb').hidden) $('#q').placeholder = file.name;

  $('#grid').innerHTML = '';
  $('#empty').hidden = true;
  $('#metaLine').innerHTML = '正在分析这张图…';
  setProgress(3, '准备中');

  const fd = new FormData();
  fd.append('file', file, file.name);
  const t0 = performance.now();

  try {
    const resp = await fetch('/api/similar/stream?limit=60', { method: 'POST', body: fd });
    if (!resp.ok || !resp.body) {
      const t = await resp.text().catch(() => '');
      throw new Error(`服务返回 ${resp.status}${t ? '：' + t.slice(0, 120) : ''}`);
    }
    let got = false;
    await readEvents(resp, ev => {
      if (mySeq !== state.seq) return;          // 期间用户又发了新搜索，丢弃旧结果
      if (ev.stage === 'error') throw new Error(ev.text || '解析失败');
      if (ev.stage === 'done') { got = true; renderImageResult(ev.result, performance.now() - t0); }
      else setProgress(ev.pct || 0, ev.text);
    });
    if (!got && mySeq === state.seq) throw new Error('没有收到结果');
  } catch (e) {
    if (mySeq !== state.seq) return;
    console.error(e);
    failProgress('搜索失败');
    $('#metaLine').innerHTML = `<span class="cov">图搜失败：${esc(e.message)}</span>`;
  } finally {
    if (mySeq === state.seq) state.imageSearching = false;
  }
}

function setEmpty(title, sub) {
  const box = $('#empty');
  box.querySelector('.et').textContent = title;
  box.querySelector('.es').textContent = sub;
  box.hidden = false;
}

function renderImageResult(d, clientMs) {
  state.total = d.total;
  state.done = true;
  state.mode = 'image';
  $('#grid').innerHTML = '';
  d.items.forEach(it => $('#grid').appendChild(renderCard(it)));
  $('#empty').hidden = true;

  const b = d.breakdown_ms || {};
  const parts = [`<b>${d.total}</b> 条结果`];
  if (d.same_source) parts.push(`<span class="cov">同源 ${d.same_source} 张</span>`);
  parts.push(`端到端 ${Math.round(clientMs)} ms`);
  if (b.fingerprint != null) parts.push(`指纹 ${b.fingerprint} ms`);
  if (b.search != null) parts.push(`检索 ${b.search} ms`);
  $('#metaLine').innerHTML = parts.join('<span class="sep">·</span>') + covNote(d.coverage);

  if (d.total === 0) {
    const cov = d.coverage || {};
    const vp = cov.searchable ? Math.round(cov.vectorized / cov.searchable * 100) : 0;
    setEmpty('没有找到相似素材',
      `向量索引目前覆盖 ${vp}%（${(cov.vectorized || 0).toLocaleString()} / ${(cov.searchable || 0).toLocaleString()}）。`
      + '如果这张图来自素材库，可能还没轮到它编码，稍后再试；也可以先用名称搜索定位。');
  }

  finishProgress(`${d.total} 条 · ${Math.round(clientMs)} ms`);
}

/* ---------------- 事件绑定 ---------------- */
let debounce = null;
$('#q').addEventListener('input', e => {
  const v = e.target.value;
  $('#clearq').hidden = !v;
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    state.q = v.trim();
    if (state.mode === 'image') { clearQueryThumb(false); setMode('name'); }
    reset();
  }, 250);
});
$('#clearq').onclick = () => {
  $('#q').value = ''; $('#clearq').hidden = true;
  state.q = ''; reset();
};
$('#qimgClear').onclick = () => {
  clearQueryThumb(true);
  $('#metaLine').innerHTML = '';
  reset();
};

function setMode(m) {
  state.mode = m;
  document.querySelectorAll('.mode').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
}
document.querySelectorAll('.mode').forEach(b => {
  b.onclick = () => {
    if (b.dataset.mode === 'image') { $('#fileInput').click(); return; }
    if (state.mode === 'image') clearQueryThumb(false);
    setMode('name');
    state.done = false;
    reset();
  };
});

$('#fileInput').onchange = e => { if (e.target.files[0]) searchByImage(e.target.files[0]); e.target.value = ''; };

/* ---------------- 重新扫描（确认弹窗 + 预估时间） ---------------- */
function fmtDur(s) {
  s = Math.round(s);
  if (s < 60) return s + ' 秒';
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m} 分 ${r} 秒` : `${m} 分钟`;
}

const rescanModal = $('#rescanModal');
function closeRescanModal() { rescanModal.hidden = true; }
rescanModal.querySelectorAll('[data-mclose]').forEach(b => b.onclick = closeRescanModal);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !rescanModal.hidden) closeRescanModal(); });

async function openRescanModal() {
  rescanModal.hidden = false;
  const estText = $('#rescanEstText');
  const confirmBtn = $('#rescanConfirm');
  confirmBtn.disabled = false;
  estText.textContent = '正在读取历史扫描数据…';
  try {
    const est = await (await apiFetch('/api/rescan/estimate', 8000)).json();
    if (est.running) {
      estText.innerHTML = '扫描<b>正在进行中</b>，请等本轮结束后再试。';
      confirmBtn.disabled = true;
      return;
    }
    if (est.last_total_s) {
      // 有真实历史数据：以上次实测为准
      const when = est.last_at ? new Date(est.last_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
      const sub = [
        `新增 ${est.last_new ?? 0} · 变更 ${est.last_changed ?? 0} · 失效 ${est.last_missing ?? 0}`,
        when ? `上次扫描：${when}` : '',
      ].filter(Boolean).join('　·　');
      estText.innerHTML =
        `预计耗时约 <b>${fmtDur(est.last_total_s)}</b>（按上次实测 ${fmtDur(est.last_total_s)} 估算，` +
        `其中遍历磁盘 ${fmtDur(est.last_walk_s || 0)}）。` +
        `<span class="m-sub">${esc(sub)}</span>`;
    } else {
      // 没有历史数据：按文件数粗估（遍历约 1500 文件/秒 + 比对入库）
      const files = est.files || 0;
      const estS = Math.max(90, Math.ceil(files / 1500) + 60);
      estText.innerHTML =
        `预计耗时约 <b>${fmtDur(estS)}</b>（首次无历史数据，按索引中 ${files.toLocaleString()} 个文件粗估；` +
        `实际视磁盘负载浮动，完成后会显示真实用时）。`;
    }
  } catch {
    estText.innerHTML = '无法读取历史扫描数据，预计耗时约 <b>5 分钟左右</b>。';
  }
}

$('#rescanBtn').onclick = openRescanModal;

$('#rescanConfirm').onclick = async () => {
  const btn = $('#rescanConfirm');
  btn.disabled = true;
  btn.textContent = '正在启动…';
  try {
    const r = await (await fetch('/api/rescan', { method: 'POST' })).json();
    closeRescanModal();
    toast(r.ok ? '已开始后台扫描，完成后状态栏会自动更新' : (r.message || '触发失败'));
    if (r.ok) setTimeout(refreshStatus, 1500);
  } catch {
    toast('触发失败，请检查服务是否在线');
  } finally {
    btn.disabled = false;
    btn.textContent = '开始扫描';
  }
};

$('#lbReveal').onclick = async () => {
  if (!current) return;
  const r = await (await fetch('/api/reveal/' + current.id, { method: 'POST' })).json();
  if (!r.ok) toast(r.message || '打开失败');
};
$('#lbCopy').onclick = async () => {
  if (!current) return;
  try { await navigator.clipboard.writeText(current.path); toast('路径已复制'); }
  catch { toast('复制失败，请手动选择'); }
};
$('#lbSimilar').onclick = async () => {
  if (!current) return;
  const c = current;
  closeLightbox();
  state.seq++;
  state.imageSearching = true;
  state.mode = 'image';
  setMode('image');

  if (queryURL) { URL.revokeObjectURL(queryURL); queryURL = null; }
  const img = $('#qthumbImg');
  if (c.thumb) { img.src = c.thumb; img.title = c.name; }
  else img.removeAttribute('src');
  $('#qthumb').hidden = !c.thumb;
  $('#qthumb').classList.add('busy');
  $('#qimgClear').hidden = false;
  $('#q').value = '';
  $('#q').placeholder = c.name;

  $('#grid').innerHTML = '';
  $('#empty').hidden = true;
  setProgress(25, '找同源与相似…');
  const t0 = performance.now();
  try {
    const r = await fetch('/api/similar/' + c.id + '?limit=60');
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || '服务返回 ' + r.status);
    const d = await r.json();
    setProgress(88, '整理结果…');
    renderImageResult(d, performance.now() - t0);
  } catch (e) {
    failProgress('失败');
    $('#metaLine').innerHTML = `<span class="cov">找相似失败：${esc(e.message)}</span>`;
  } finally {
    state.imageSearching = false;
  }
};
document.querySelectorAll('[data-close]').forEach(n => n.onclick = closeLightbox);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });

/* 拖拽 */
let dragDepth = 0;
window.addEventListener('dragenter', e => {
  if (![...e.dataTransfer.types].includes('Files')) return;
  dragDepth++; $('#dropzone').classList.add('on');
});
window.addEventListener('dragleave', e => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) $('#dropzone').classList.remove('on');
});
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; $('#dropzone').classList.remove('on');
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) searchByImage(f);
});

/* 无限滚动 */
new IntersectionObserver(entries => {
  if (entries[0].isIntersecting && !state.done && !state.loading && state.mode === 'name') {
    search(true);
  }
}, { rootMargin: '800px' }).observe($('#sentinel'));

// 交叉观察器的兜底：滚动时再确认一次，避免 sentinel 一直在视口内导致不再触发
window.addEventListener('scroll', () => maybeLoadMore(), { passive: true });

/* 启动 */
(async function init() {
  await refreshStatus();
  await loadFacets();
  reset();
  setInterval(refreshStatus, 5000);
})();
