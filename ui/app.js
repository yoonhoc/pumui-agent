// 품의서·결의서 작업 화면. 문구는 /api/init 의 ui(config/ui.json)에서 받는다. 단계는 URL 해시에 둔다.
// 품의 단계는 이 파일, 결의 단계는 gyeolui.js가 MODES.gyeolui로 더한다(해시가 #g-로 시작하면 결의).
const S = { init: null, ui: null, error: null, picked: new Set(), set: 0, compare: null, chosen: null, reason: '',
  form: null, preview: null, previewState: 'idle', result: null, status: {}, busy: {} };
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const won = (n) => (n == null ? '' : Number(n).toLocaleString('ko-KR'));
const fmt = (s, o) => String(s).replace(/\{(\w+)\}/g, (_, k) => (o[k] ?? ''));
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const ICON = {
  ok: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  warn: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5l6 11H2z M8 6.5v3.5 M8 11.5v1" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
};

async function api(path, body) {
  let r;
  try {
    r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch (e) {  // 서버가 꺼졌거나 연결이 끊김: 무엇을 하면 되는지 알려 준다
    throw new Error(S.ui ? S.ui.offline : e.message);
  }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---------------------------------------------------------------- 업무(품의/결의)와 단계, 주소
// MODES[이름] = { keys(), steps(), wordmark(), title(), open(i), views: [[view, bind]...], after(at) }
const MODES = {};
const modeOf = () => (location.hash.startsWith('#g-') && MODES.gyeolui ? 'gyeolui' : 'pumui');
const M = () => MODES[modeOf()];
const keys = () => M().keys();
const quotes = () => S.init.quotes;
const byId = (id) => quotes().find((q) => q.id === id);
const picked = () => [...S.picked].map(byId).filter(Boolean);
const reviewDone = () => picked().length > 0 && picked().every((q) => !q.questions.length && !q.hold);
const chosenSaved = () => S.init.selection && S.init.selection.result_file === S.chosen;
const pumuiOpen = (i) => [true, S.picked.size > 0, reviewDone(), reviewDone() && chosenSaved(), reviewDone() && chosenSaved()][i];
const open = (i) => M().open(i);
function current() {
  const i = Math.max(0, keys().indexOf(location.hash.slice(1)));
  for (let j = i; j >= 0; j--) if (open(j)) return j;
  return 0;
}
const go = (i) => { location.hash = keys()[i]; };
window.addEventListener('hashchange', () => render(true));

function renderTabs() {
  const at = current(), mode = modeOf();
  $('#modes').innerHTML = Object.entries(MODES).map(([k, m]) =>
    `<a class="mode" href="#${m.keys()[0]}" ${k === mode ? 'aria-current="page"' : ''}>${esc(S.ui.modes[k])}</a>`).join('');
  $('#modes').setAttribute('aria-label', S.ui.modes.label);
  $('#wordmark').textContent = M().wordmark(); $('#wordmark').href = `#${keys()[0]}`; document.title = M().title();
  $('#tabs').innerHTML = M().steps().map((name, i) => open(i)
    ? `<a class="tab" href="#${keys()[i]}" ${i === at ? 'aria-current="step"' : ''}>${esc(name)}</a>`
    : `<span class="tab" aria-disabled="true">${esc(name)}</span>`).join('');
}
const status = (key) => S.status[key] ? `<p class="help ${S.status[key].error ? 'help--error' : ''}" role="status">${esc(S.status[key].text)}</p>` : '<p class="help" role="status"></p>';
const say = (key, text, error = false) => { S.status[key] = { text, error }; };
// 제목은 두 톤: 굵은 단계 이름 + 회색 설명 (Apple 구입 페이지 방식)
const head = (t) => `<div class="head"><h1>${esc(t.heading)}${t.lead ? `${esc(S.ui.title_sep)}<span class="sub">${esc(t.lead)}</span>` : ''}</h1></div>`;

// ---------------------------------------------------------------- 팝업 (확인 필요 설명)
// body는 [{text, muted}] 문단 목록, actions는 [{label, primary, run}] (run이 없으면 닫기만)
function popup(title, body, actions = []) {
  const d = $('#dlg');
  d.innerHTML = `<h2 id="dlg-title">${esc(title)}</h2>${body.map((b) => b.list
    ? `<ul>${b.list.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`
    : `<p class="${b.muted ? 'muted' : ''}">${esc(b.text)}</p>`).join('')}
    <div class="dlg__actions">${actions.map((x, i) => `<button type="button" class="key ${x.primary ? 'key--primary' : ''}" data-i="${i}">${esc(x.label)}</button>`).join('')}
      <button type="button" class="key" data-close>${esc(S.ui.close)}</button></div>`;
  $$('[data-i]', d).forEach((b) => (b.onclick = () => { d.close(); actions[Number(b.dataset.i)].run(); }));
  $('[data-close]', d).onclick = () => d.close();
  d.onclick = (e) => { if (e.target === d) d.close(); };  // 바깥(배경) 누르면 닫기
  if (typeof d.showModal === 'function') d.showModal(); else d.setAttribute('open', '');
  $('[data-close]', d).focus();
}

// ---------------------------------------------------------------- 1. 견적
function viewQuotes() {
  const t = S.ui.quotes, list = quotes().filter((q) => q.set === S.set);
  const sets = S.init.sets.length < 2 ? '<span></span>' : `<fieldset class="seg"><legend>${esc(t.set_legend)}</legend>${S.init.sets.map((name, i) =>
    `<input type="radio" name="qset" id="qset${i}" value="${i}" ${i === S.set ? 'checked' : ''}><label for="qset${i}">${esc(name)}</label>`).join('')}</fieldset>`;
  const upload = S.init.studio_ready
    ? `<label class="key key--file" ${S.busy.upload ? 'aria-busy="true"' : ''}>${esc(t.upload)}<input type="file" id="qupload" accept=".pdf,.png,.jpg,.jpeg" multiple></label>`
    : '';
  const stat = (q) => q.hold ? `<span class="status status--bad">${ICON.warn}${esc(t.hold)}</span>`
    : q.questions.length ? `<button type="button" class="status status--warn status--btn" data-issues="${esc(q.id)}">${ICON.warn}${esc(fmt(t.issues, { n: q.questions.length }))}</button>`
      : `<span class="status status--ok">${ICON.ok}${esc(t.ok)}</span>`;
  const row = (q) => `<tr data-id="${esc(q.id)}" class="${S.picked.has(q.id) ? 'is-on' : ''}">
      <td><input type="checkbox" aria-label="${esc(fmt(t.select, { vendor: q.vendor }))}" ${S.picked.has(q.id) ? 'checked' : ''}></td>
      <td class="vendor">${esc(q.vendor)}</td><td class="num">${won(q.total)}</td>
      <td class="hide-sm">${esc(q.quote_date || '')}</td><td class="num hide-sm">${esc(fmt(t.items, { n: q.items.length }))}</td><td>${stat(q)}</td></tr>`;
  // 같은 물품끼리 묶어서 보여 준다(묶음이 하나면 머리줄 없이)
  const byGroup = new Map();
  list.forEach((q) => { const g = groupOf(q.id); if (!byGroup.has(g.group)) byGroup.set(g.group, { label: g.label, items: [] }); byGroup.get(g.group).items.push(q); });
  const rows = [...byGroup.entries()].sort((a, b) => a[0] - b[0]).map(([n, g]) => (byGroup.size > 1
    ? `<tr class="grouphead"><th colspan="6"><span>${esc(fmt(t.group_head, { label: g.label || '-', n: g.items.length }))}</span><button type="button" class="linkish" data-group="${n}">${esc(t.group_pick)}</button></th></tr>`
    : '') + g.items.map(row).join('')).join('');
  const table = list.length
    ? `<div class="ledger-wrap"><table class="ledger"><thead><tr><th><span class="visually-hidden">${esc(S.ui.select_col)}</span></th>${t.cols.map((c, i) => `<th class="${[1, 3].includes(i) ? 'num' : ''} ${[2, 3].includes(i) ? 'hide-sm' : ''}">${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="note"><p>${esc(t.empty)}</p><p class="muted">${esc(t.empty_action)}</p></div>`;
  return `${head(t)}<div class="row spread" style="margin-bottom:var(--space-md)">${sets}<div class="row">${upload}</div></div>
    ${S.init.studio_ready ? `<p class="help">${esc(t.upload_hint)}</p>` : `<p class="note">${esc(t.studio_off)}</p>`}
    ${status('upload')}${intake()}${table}
    <div class="actions"><p class="help">${S.picked.size ? esc(fmt(t.picked, { n: S.picked.size })) : ''}</p>
      <button type="button" class="key key--primary" id="next" ${S.picked.size ? '' : 'disabled'}>${esc(t.next)}</button></div>`;
}
// 올리는 견적서: 왼쪽 미리보기, 오른쪽 Studio 단계 진행과 읽어 낸 값. 여러 장이면 동시에 읽고 목록으로 보여 준다.
const groupOf = (id) => (S.init.groups && S.init.groups[id]) || { group: 0, label: '' };
const UP = () => S.uploads || (S.uploads = []);
const viewed = () => UP().find((u) => u.id === S.upView) || UP().find((u) => !u.finished && !u.error) || UP()[UP().length - 1];
const upState = (u) => (u.error ? 'fail' : u.reject ? 'reject' : u.skipped ? 'skip' : u.finished ? 'done' : u.started ? 'now' : 'wait');
function nowStage(u) {
  const st = S.ui.quotes.intake.stages, done = new Set(u.done || []);
  return st.find(([key]) => !done.has(key)) || st[st.length - 1];
}
function intakeSteps(u) {
  const t = S.ui.quotes.intake, done = new Set(u.done || []), now = nowStage(u)[0];
  return t.stages.map(([key, label]) => {
    const st = done.has(key) ? 'done' : key === now && u.started && !u.finished ? (u.error ? 'fail' : 'now') : 'wait';
    const mark = st === 'done' ? ICON.ok : st === 'fail' ? ICON.warn : st === 'now' ? '<span class="spin keep-motion" aria-hidden="true"></span>' : '<span class="dot" aria-hidden="true"></span>';
    return `<li class="is-${st}">${mark}<span>${esc(label)}</span></li>`;
  }).join('');
}
function intakeFound(u) {
  const t = S.ui.quotes.intake, f = u.found || {};
  const val = { category: f.category, kind: f.kind, vendor: f.vendor, total: f.total != null ? `${won(f.total)}원` : null,
    items: f.items != null ? fmt(t.items, { n: f.items }) : null, decision: f.decision };
  return Object.entries(t.found).filter(([k]) => val[k]).map(([k, label]) => `<div><dt>${esc(label)}</dt><dd>${esc(val[k])}</dd></div>`).join('');
}
function intakeTitle(u) {
  const t = S.ui.quotes.intake;
  if (UP().length > 1) return fmt(t.count, { done: UP().filter((x) => x.finished).length, n: UP().length });
  return u.error ? t.failed : u.reject ? t.rejected : u.finished ? t.done : t.reading;
}
function intakeList() {
  const t = S.ui.quotes.intake, v = viewed();
  if (UP().length < 2) return '';
  return UP().map((u) => {
    const st = upState(u), f = u.found || {};
    const text = st === 'skip' || st === 'reject' ? fmt(t.row[st], { category: f.category || '' }) : st === 'done' ? fmt(t.row.done, { vendor: f.vendor || '', total: won(f.total || 0) })
      : st === 'now' ? fmt(t.row.now, { stage: nowStage(u)[1] }) : t.row[st];
    const mark = st === 'done' ? ICON.ok : st === 'fail' || st === 'reject' ? ICON.warn : st === 'now' ? '<span class="spin keep-motion" aria-hidden="true"></span>' : '<span class="dot" aria-hidden="true"></span>';
    return `<li><button type="button" class="intake__row is-${st}" data-up="${u.id}" ${u === v ? 'aria-current="true"' : ''}>${mark}<span class="intake__name">${esc(u.label || u.name)}</span><span class="intake__state">${esc(text)}</span></button></li>`;
  }).join('');
}
function intakeDoc(u) {
  const name = esc(fmt(S.ui.quotes.intake.preview, { name: u.label || u.name }));
  return u.kind === 'pdf' ? `<iframe src="${u.url}#toolbar=0&navpanes=0&view=FitH" title="${name}"></iframe>` : `<img src="${u.url}" alt="${name}">`;
}
function intake() {
  const u = viewed();
  if (!u) return '';
  const t = S.ui.quotes.intake, busy = UP().some((x) => !x.finished && !x.error);
  return `<section class="intake" aria-live="polite">
    <div class="intake__doc keep-motion ${!u.finished && !u.error ? 'is-reading' : ''}" id="intake-doc" data-for="${u.id}">${intakeDoc(u)}</div>
    <div class="intake__info">
      <div class="intake__head"><div><p class="intake__title" id="intake-title">${esc(intakeTitle(u))}</p>
        <p class="intake__file" id="intake-file">${esc(u.label || u.name)}</p></div>
        ${busy ? '' : `<button type="button" class="key" id="intake-close">${esc(t.close)}</button>`}</div>
      <ol class="intake__list" id="intake-list">${intakeList()}</ol>
      <ol class="intake__steps" id="intake-steps">${intakeSteps(u)}</ol>
      <dl class="intake__found" id="intake-found">${intakeFound(u)}</dl>
    </div></section>`;
}
function setHTML(sel, html) { const el = $(sel); if (el && el.innerHTML !== html) el.innerHTML = html; }  // 같으면 그대로 둬야 도는 표시가 처음으로 돌아가지 않는다
function updateIntake() {
  const u = viewed();
  if (!u || !$('#intake-steps')) return;
  const doc = $('#intake-doc');
  if (doc.dataset.for !== u.id) { doc.dataset.for = u.id; doc.innerHTML = intakeDoc(u); }
  doc.classList.toggle('is-reading', !u.finished && !u.error);
  setHTML('#intake-list', intakeList()); setHTML('#intake-steps', intakeSteps(u)); setHTML('#intake-found', intakeFound(u));
  $('#intake-title').textContent = intakeTitle(u); $('#intake-file').textContent = u.label || u.name;
  $$('[data-up]').forEach((b) => (b.onclick = () => { S.upView = b.dataset.up; updateIntake(); }));
}
const readB64 = (f) => new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result.split(',')[1]); r.onerror = no; r.readAsDataURL(f); });
async function readOne(u) {
  const t = S.ui.quotes;
  u.started = true; updateIntake();
  const poll = setInterval(async () => {
    try {
      const p = await api(`/api/quotes/progress?token=${encodeURIComponent(u.id)}`);
      if (!u.finished && !u.error) { u.done = p.done; u.found = p.found; updateIntake(); }
    } catch (e) { /* 다음 차례에 다시 묻는다 */ }
  }, 600);
  try {
    const r = await api('/api/quotes/upload', { name: u.name, data: await readB64(u.file), token: u.id });
    if (r.skipped) { u.skipped = r.skipped; u.finished = true; u.done = t.intake.stages.map(([key]) => key); u.found = { category: r.category }; clearInterval(poll); return; }
    S.init.quotes = S.init.quotes.filter((q) => q.id !== r.quote.id).concat(r.quote);
    if (r.groups) S.init.groups = r.groups;
    u.qid = r.quote.id; S.set = r.quote.set; S.compare = null;
    const st = r.quote.studio || {};
    u.done = t.intake.stages.map(([key]) => key); u.finished = true;
    u.found = { category: st.category, kind: st.kind, vendor: r.quote.vendor, total: r.quote.total, items: r.quote.items.length, decision: st.decision };
  } catch (e) { u.error = e.message; say('upload', fmt(S.ui.error, { msg: `${u.label || u.name}: ${e.message}` }), true); }
  clearInterval(poll);
}
async function uploadQuotes(files) {
  const t = S.ui.quotes, fresh = [...files];
  if (!fresh.length) return;
  const exts = S.init.upload_ext || ['.pdf', '.png', '.jpg', '.jpeg'];
  let ok = fresh.filter((f) => exts.some((x) => f.name.toLowerCase().endsWith(x)));
  // 여러 쪽 PDF는 쪽마다 나눠 한 장씩 읽는다(한 파일에 업체 견적서 여러 장이 들어오는 경우)
  const pages = [];
  for (const f of ok) {
    if (!/\.pdf$/i.test(f.name)) { pages.push(f); continue; }
    try {
      const r = await api('/api/quotes/split', { name: f.name, data: await readB64(f) });
      if (r.pages.length < 2) { pages.push(f); continue; }
      r.pages.forEach((p) => {
        const pf = new File([Uint8Array.from(atob(p.data), (c) => c.charCodeAt(0))], p.name, { type: 'application/pdf' });
        pf.from = f.name;  // 어느 파일에서 나온 쪽인지(파일 전체가 견적서가 아니면 다시 올리기를 요청한다)
        pf.label = fmt(t.page_name, { stem: f.name, n: p.page });  // 화면에는 '원래 파일 · n쪽'으로 보인다
        pages.push(pf);
      });
    } catch (e) { pages.push(f); }
  }
  ok = pages;
  if (ok.length < fresh.length) say('upload', fmt(t.upload_bad_ext, { ext: exts.join(', ') }), true);
  if (!UP().some((u) => !u.finished && !u.error)) { UP().forEach((u) => URL.revokeObjectURL(u.url)); S.uploads = []; S.upView = null; }  // 새 묶음
  const added = ok.map((f) => ({ id: (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random()}`, name: f.name, label: f.label || f.name, from: f.from || f.name, file: f,
    url: URL.createObjectURL(f), kind: /pdf$/i.test(f.type) || /\.pdf$/i.test(f.name) ? 'pdf' : 'img', done: [], found: {} }));
  UP().push(...added);
  if (!added.length) { render(); return; }
  if (S.busy.upload) { render(); return; }  // 이미 돌고 있는 일꾼이 이어서 읽는다
  S.busy.upload = true; if (!S.status.upload || !S.status.upload.error) S.status.upload = null; render();
  const next = () => UP().find((u) => !u.started);
  const worker = async () => { for (let u = next(); u; u = next()) { await readOne(u); render(); } };
  await Promise.all(Array.from({ length: Math.max(1, S.init.upload_workers || 1) }, worker));
  // 이번에 올린 견적이 한 품목이면 그 묶음을 고르고, 여러 품목이면 고르지 않고 알려 준다
  const got = UP().filter((u) => u.qid).map((u) => u.qid), gs = new Set(got.map((id) => groupOf(id).group));
  if (gs.size === 1) S.picked = new Set(quotes().filter((q) => groupOf(q.id).group === [...gs][0]).map((q) => q.id));
  else if (gs.size > 1) { S.picked = new Set(); say('upload', t.mixed_upload); }
  // 올린 파일 전체가 견적서로 읽히지 않았으면(표지 쪽만 빠진 경우는 제외) 다시 올리기를 요청한다
  const byFile = new Map();
  UP().forEach((u) => byFile.set(u.from, (byFile.get(u.from) || []).concat(u)));
  const rejected = [...byFile.entries()].filter(([, us]) => us.every((u) => u.skipped));
  rejected.forEach(([, us]) => us.forEach((u) => (u.reject = true)));
  S.busy.upload = false; render();
  if (rejected.length) {
    const r = t.reject;
    popup(r.title, [{ text: r.body }, { list: rejected.map(([name, us]) => fmt(r.item, { name, category: (us[0].found || {}).category || '' })) }, { text: r.check, muted: true }],
      [{ label: r.again, primary: true, run: () => { const up = $('#qupload'); if (up) up.click(); } }]);
  }
  // 모두 읽었으면 잠시 결과를 보여 준 뒤 패널을 접어 목록이 보이게 한다(실패·다시 올릴 파일이 있으면 그대로 둔다)
  const batch = S.uploads;
  if (batch.length && batch.every((u) => u.finished && !u.reject)) setTimeout(() => {
    if (S.uploads === batch && !S.busy.upload) { batch.forEach((u) => URL.revokeObjectURL(u.url)); S.uploads = []; S.upView = null; render(); }
  }, t.intake.hold_ms);
}
function bindQuotes() {
  $$('input[name="qset"]').forEach((el) => (el.onchange = () => { S.set = Number(el.value); render(); }));
  const up = $('#qupload'); if (up) up.onchange = (e) => uploadQuotes(e.target.files);
  const ic = $('#intake-close'); if (ic) ic.onclick = () => { UP().forEach((u) => URL.revokeObjectURL(u.url)); S.uploads = []; S.upView = null; render(); };
  $$('[data-up]').forEach((b) => (b.onclick = () => { S.upView = b.dataset.up; updateIntake(); }));
  // 파일을 화면에 끌어다 놓아도 올린다
  const main = $('#main');
  if (main && S.init.studio_ready && !main.dataset.drop) {
    main.dataset.drop = '1';
    // 끌어다 놓기: 품의 견적 선택 화면이면 견적서 업로드, 결의 증빙 선택 화면이면 증빙 파일 목록에 담는다
    const dropOk = () => (location.hash.slice(1) || S.ui.step_keys[0]) === keys()[0] && (modeOf() === 'pumui' || S.init.gyeolui.ready);
    main.addEventListener('dragover', (e) => { if (!dropOk()) return; e.preventDefault(); main.classList.add('is-drop'); });
    main.addEventListener('dragleave', (e) => { if (e.target === main) main.classList.remove('is-drop'); });
    main.addEventListener('drop', (e) => {
      if (!dropOk()) return; e.preventDefault(); main.classList.remove('is-drop');
      if (modeOf() === 'gyeolui') { S.g.files = S.g.files.concat([...e.dataTransfer.files]); S.status.gupload = null; render(); }
      else uploadQuotes(e.dataTransfer.files);
    });
  }
  $$('[data-issues]').forEach((b) => (b.onclick = (e) => {
    e.stopPropagation();  // 줄 선택은 바꾸지 않는다
    const t = S.ui.quotes, q = byId(b.dataset.issues);
    popup(fmt(t.issues_title, { vendor: q.vendor, n: q.questions.length }),
      [{ list: q.questions.map((x) => x.message) }, { text: t.issues_help, muted: true }],
      [{ label: t.issues_go, primary: true, run: () => { S.picked.add(q.id); S.compare = null; go(1); } }]);
  }));
  $$('[data-group]').forEach((b) => (b.onclick = () => {
    S.picked = new Set(quotes().filter((q) => q.set === S.set && groupOf(q.id).group === Number(b.dataset.group)).map((q) => q.id));
    S.compare = null; S.chosen = null; render();
  }));
  $$('.ledger tbody tr[data-id]').forEach((tr) => (tr.onclick = (e) => {
    const id = tr.dataset.id; S.picked.has(id) ? S.picked.delete(id) : S.picked.add(id);
    S.compare = null; S.chosen = null; render(); if (e.target.matches('input')) $(`tr[data-id="${CSS.escape(id)}"] input`).focus();
  }));
  $('#next').onclick = () => {  // 품목이 다른 견적이 섞여 있으면 비교할 묶음을 고르게 한다
    const t = S.ui.quotes, gs = [...new Set(picked().map((q) => groupOf(q.id).group))];
    if (gs.length < 2) return go(1);
    popup(t.mixed_title, [{ text: t.mixed_body }], gs.map((n, i) => {
      const ids = picked().filter((q) => groupOf(q.id).group === n).map((q) => q.id);
      return { label: fmt(t.group_head, { label: groupOf(ids[0]).label || '-', n: ids.length }), primary: i === 0,
        run: () => { S.picked = new Set(ids); S.compare = null; S.chosen = null; render(); go(1); } };
    }));
  };
}

// ---------------------------------------------------------------- 2. 검토
function viewReview() {
  const t = S.ui.review;
  const groups = picked().map((q) => {
    const done = !q.questions.length && !q.hold;
    const qs = q.questions.map((x, n) => `<div class="cut question">
        <p>${esc(x.message)}</p>${x.evidence ? `<p class="evidence">${esc(x.evidence)}</p>` : ''}
        <div class="tray" role="group" aria-label="${esc(t.options_label)}">${x.options.map((o, k) => `<button type="button" class="key" data-q="${esc(q.id)}" data-key="${esc(x.key)}" data-k="${k}">${esc(o.label)}</button>`).join('')}</div>
        <div class="row" data-input="${esc(x.key)}" hidden><div class="field" style="flex:1"><label for="in-${n}-${esc(q.id)}">${esc(t.input_label)}</label><input class="input" id="in-${n}-${esc(q.id)}"></div><button type="button" class="key">${esc(t.input_submit)}</button></div>
      </div>`).join('');
    return `<section class="group"><div class="row spread"><h2>${esc(fmt(t.group, { vendor: q.vendor, total: won(q.total) }))}</h2>
        ${done ? `<span class="status status--ok">${ICON.ok}${esc(t.done)}</span>` : ''}${q.hold ? `<span class="status status--bad">${ICON.warn}${esc(S.ui.quotes.hold)}</span>` : ''}</div>
      ${q.answered.length ? `<p class="small muted">${esc(fmt(t.answered, { n: q.answered.length }))}</p>` : ''}
      ${q.studio && q.studio.decision ? `<div class="studio"><p class="small"><span class="studio__tag">${esc(fmt(t.studio_decision, { decision: q.studio.decision }))}</span> <span class="muted">${esc(t.studio_help)}</span></p>${q.studio.note ? `<details class="studio__note"><summary>${esc(q.studio.branch || '')}</summary><p>${esc(q.studio.note)}</p></details>` : ''}</div>` : ''}
      ${q.model_flags.length && t.model_flags ? `<p class="small muted">${esc(fmt(t.model_flags, { flags: q.model_flags.join(', ') }))}</p>` : ''}${qs}</section>`;
  }).join('');
  const clear = picked().every((q) => !q.questions.length);
  return `${head(t)}${clear ? `<p class="note">${esc(t.all_clear)}</p>` : ''}${groups}${status('review')}
    <div class="actions"><button type="button" class="key key--primary" id="next" ${reviewDone() ? '' : 'disabled'}>${esc(t.next)}</button></div>`;
}
async function answer(id, key, value) {
  try {
    const q = await api('/api/answer', { id, key, value });
    S.init.quotes[S.init.quotes.findIndex((x) => x.id === id)] = q; S.compare = null; S.status.review = null; render();
  } catch (e) { say('review', fmt(S.ui.error, { msg: e.message }), true); render(); }
}
function bindReview() {
  $$('.tray .key').forEach((b) => (b.onclick = () => {
    const q = byId(b.dataset.q), x = q.questions.find((y) => y.key === b.dataset.key), o = x.options[Number(b.dataset.k)];
    if (o.value === '__INPUT__') {
      const row = $(`[data-input="${CSS.escape(x.key)}"]`); row.hidden = false; row.querySelector('input').focus();
      row.querySelector('button').onclick = () => answer(q.id, x.key, row.querySelector('input').value);  // 숫자 변환은 서버(harness.coerce)
      return;
    }
    b.setAttribute('aria-busy', 'true'); answer(q.id, x.key, o.value);
  }));
  $('#next').onclick = () => go(2);
}

// ---------------------------------------------------------------- 3. 업체 선정
function viewVendor() {
  const t = S.ui.vendor, c = S.compare;
  if (!c) return `${head(t)}<table class="ledger skeleton" aria-busy="true"><tbody>${'<tr><td><span></span></td><td><span></span></td><td><span></span></td></tr>'.repeat(3)}</tbody></table>`;
  const low = Math.min(...c.quotes.map((q) => q.total || Infinity));
  // 업체마다 고르는 타일 하나: 이름과 합계, 아래에 견적일과 최저가 대비. 추천은 위에 작은 글씨.
  const tiles = c.quotes.map((q, i) => {
    const rec = q.file === c.recommended, ex = c.excluded.includes(q.file), on = q.file === S.chosen;
    const same = (c.warnings || []).filter((w) => w.files.includes(q.file)).map((w) => (w.kind === 'same_lowest_total' ? t.same_low_tag : t.same_rep_tag));
    const tag = [rec ? esc(t.recommended) : ex ? esc(fmt(t.excluded, { why: q.issue_messages.join(', ') })) : '', same.map(esc).join(' · ')].filter(Boolean).join(' · ');
    return `<input type="radio" name="vendor" id="v${i}" value="${esc(q.file)}" ${on ? 'checked' : ''}>
      <label for="v${i}" class="tile ${ex ? 'is-off' : ''}">${tag ? `<span class="tag">${tag}</span>` : ''}
        <span class="name">${esc(q.vendor)}</span><span class="price">${won(q.total)}${esc(S.ui.won)}</span>
        <span class="meta">${esc(fmt(t.date, { date: q.quote_date || '' }))}</span>
        <span class="meta meta--right">${q.total === low ? esc(t.same) : esc(fmt(t.diff, { n: won(q.total - low) }))}</span></label>`;
  }).join('');
  const pick = c.quotes.find((q) => q.file === S.chosen) || {};
  const pq = byId(S.chosen);
  const summary = `<aside class="summary" aria-live="polite"><span class="label">${esc(t.summary_title)}</span>
      <span class="vendor">${esc(pick.vendor || '')}</span>
      ${pq ? `<span class="muted small">${esc(fmt(t.summary_items, { n: pq.items.length }))}</span>` : ''}
      <span class="label" style="margin-top:var(--space-md)">${esc(t.summary_total)}</span>
      <span class="total">${won(pick.total)}${esc(S.ui.won)}</span></aside>`;
  const isRec = S.chosen === c.recommended;
  return `${head(t)}${(c.warnings || []).map((w) => `<p class="note note--warn" role="alert">${esc(w.kind === 'same_lowest_total' ? fmt(t.same_low, { vendors: w.vendors.join(', '), total: won(w.total) }) : fmt(t.same_rep, { vendors: w.vendors.join(', '), rep: w.rep }))}</p>`).join('')}${c.quotes.length === 1 ? `<p class="note">${esc(t.single)}</p>` : ''}
    <div class="buy">${summary}<div class="stack">
      <fieldset class="tiles" style="border:0;padding:0;margin:0"><legend class="visually-hidden">${esc(t.heading)}</legend>${tiles}</fieldset>
      <div class="field" style="margin-top:var(--space-md)"><label for="reason">${esc(t.reason_label)}${isRec ? '' : ' ' + esc(S.ui.required)}</label>
        <textarea class="input" id="reason" aria-describedby="reason-help" ${isRec ? '' : 'aria-required="true"'}>${esc(S.reason)}</textarea>
        <p class="help" id="reason-help">${esc(isRec ? fmt(t.reason_auto, { auto: c.auto_reason }) : t.reason_required)}</p></div>
    </div></div>
    ${status('vendor')}
    <div class="actions"><button type="button" class="key key--primary" id="confirm">${esc(t.confirm)}</button>
      <button type="button" class="key" id="next" ${chosenSaved() ? '' : 'disabled'}>${esc(t.next)}</button></div>`;
}
async function loadCompare() {
  const ids = [...S.picked];
  S.compare = await api('/api/compare', { ids });
  if (!S.chosen || !ids.includes(S.chosen)) { S.chosen = S.compare.recommended || ids[0]; S.reason = ''; }
}
function bindVendor() {
  if (!S.compare) return;
  $$('input[name="vendor"]').forEach((el) => (el.onchange = () => {
    S.chosen = el.value; S.reason = $('#reason').value; S.status.vendor = null; render(); $(`#${el.id}`).focus();
  }));
  $('#reason').oninput = (e) => (S.reason = e.target.value);
  $('#confirm').onclick = async (e) => {
    e.target.setAttribute('aria-busy', 'true');
    try {
      S.init.selection = await api('/api/select', { ids: [...S.picked], pick: S.chosen, reason: S.reason });
      say('vendor', fmt(S.ui.vendor.chosen, { vendor: S.init.selection.vendor, reason: S.init.selection.reason }));
      go(3); return;  // 선정이 저장되면 바로 품의 정보로 넘어간다
    } catch (err) { say('vendor', err.message, true); $('#reason').setAttribute('aria-invalid', 'true'); }
    render(); if (S.status.vendor && S.status.vendor.error) $('#reason').focus();
  };
  $('#next').onclick = () => go(3);
}

// ---------------------------------------------------------------- 4. 구매 정보
function viewCase() {
  const t = S.ui.case, c = S.init.case;
  const field = ([k, label, type]) => type === 'funding'
    ? `<fieldset class="seg"><legend>${esc(label)}</legend>${S.init.funding_sources.map((f, i) => `<input type="radio" name="${k}" id="${k}${i}" value="${esc(f)}" ${c[k] === f ? 'checked' : ''}><label for="${k}${i}">${esc(f)}</label>`).join('')}</fieldset>`
    : `<div class="field"><label for="f-${k}">${esc(label)}</label><input class="input" id="f-${k}" name="${k}" type="${type}" autocomplete="off" value="${esc(c[k] || '')}"></div>`;
  return `${head(t)}<form id="caseform" class="fields">${t.fields.map(field).join('')}</form>${status('case')}
    <div class="actions"><button type="submit" form="caseform" class="key key--primary" id="save">${esc(t.save)}</button></div>`;
}
function bindCase() {
  $('#caseform').onsubmit = async (e) => {
    e.preventDefault(); $('#save').setAttribute('aria-busy', 'true');
    const c = { ...S.init.case };
    S.ui.case.fields.forEach(([k, , type]) => {
      if (type === 'funding') { const r = $(`input[name="${k}"]:checked`); if (r) c[k] = r.value; }
      else { const v = $(`input[name="${k}"]`).value.trim(); if (v) c[k] = v; else delete c[k]; }
    });
    try {
      const r = await api('/api/case', { case: c }); S.init.case = c; S.init.quotes = r.quotes; S.compare = null;
      say('case', S.ui.case.saved); go(reviewDone() ? 4 : 1);
    } catch (err) { say('case', fmt(S.ui.error, { msg: err.message }), true); render(); }
  };
}

// ---------------------------------------------------------------- 5. 품의서 (양식 판은 결의서도 같이 쓴다)
// st: 양식 상태 {form, preview, previewState, result, busy} (품의는 S, 결의는 S.g)
// o: {t: 문구, forms(), statusKey, missingHref, fill(): 작성 API, setForms(r), extra(r): 작성 뒤 덧붙일 html}
function formPanel(st, o) {
  const t = o.t, r = st.result;
  const groups = {}; o.forms().forEach((f) => (groups[f.group] = groups[f.group] || []).push(f));
  const fixed = o.forms().length === 1;  // 양식이 하나로 고정되어 있으면 고르는 칸·업로드 없이 바로 쓴다
  const list = Object.entries(groups).map(([g, fs]) => fs.map((f, i) => `<input type="radio" name="form" id="fm-${esc(g)}-${i}" value="${esc(f.path)}" ${st.form === f.path ? 'checked' : ''}>
      <label for="fm-${esc(g)}-${i}"><span>${esc(f.name)}</span><span class="small muted">${esc(g)}</span></label>`).join('')).join('');
  let doc, side = '';
  if (r && r.ok) {
    const rep = r.report;
    // 접을 수 있는 묶음: 확인할 것(칸 없음, 값 없음, 처음 보는 칸)은 펼치고 작성 내역은 접어 둔다
    // 묶음마다: 제목과 개수, 무엇을 하면 되는지 한 줄(help), 항목(html이면 그대로), 바로 갈 곳(action)
    const ul = (title, arr, open = true, help = '', action = '', raw = false) => arr.length ? `<details ${open ? 'open' : ''}><summary>${esc(title)} <span class="count">${arr.length}</span></summary>${help ? `<p class="help">${esc(help)}</p>` : ''}<ul>${arr.map((x) => `<li>${raw ? x : esc(x)}</li>`).join('')}</ul>${action}</details>` : '';
    const left = (rep.leftover || []).map((x, i) => `<button type="button" class="linkish" data-left="${i}">${esc(fmt(t.leftover_item, { where: x.where, values: x.values.join(', ') }))}</button>`);
    const dl = (S.ui.download || {})[rep.files.ext] || t.download_hwpx;  // 파일 종류별 내려받기 문구(ui.json download)
    doc = `<div class="doc__head"><h2>${esc(t.result)}</h2><a class="key key--primary" href="${rep.files.doc}" download>${esc(dl)}</a>${rep.files.pdf ? `<a class="key" href="${rep.files.pdf}" download>${esc(S.ui.download['.pdf'])}</a>` : ''}</div>
      <iframe title="${esc(t.result_title)}" src="${rep.files.html}"></iframe>`;
    side = `${o.extra ? o.extra(r) : ''}<div class="report">${ul(t.leftover, left, true, t.leftover_help, '', true)}${ul(t.missing, rep.missing.map((m) => m.label), true, t.missing_help, `<a class="key" href="${o.missingHref}">${esc(t.missing_action)}</a>`)}${ul(t.unplaced, rep.unplaced.map((u) => (u.value ? `${u.what}: ${u.value}` : `${u.what}, ${u.why}`)), true, t.unplaced_help)}${ul(t.unknown, rep.unknown_labels, true, t.unknown_help)}${ul(t.notes, rep.notes, false)}</div>`;
  } else if (r && !r.ok) {
    doc = `<div class="doc__head"><h2>${esc(t.blocked)}</h2></div><div class="report"><ul>${r.blocked.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>`;
  } else if (st.previewState === 'loading') {
    doc = `<div class="doc__head"><h2>${esc(t.preview_title)}</h2></div><div class="doc__empty" aria-busy="true">${esc(S.ui.loading)}</div>`;
  } else if (st.preview) {
    doc = `<div class="doc__head"><h2>${esc(t.preview_title)}</h2></div><iframe title="${esc(t.preview_title)}" src="${st.preview}"></iframe>`;
  } else {
    doc = `<div class="doc__empty">${esc(t.pick_first)}</div>`;
  }
  return `${head(t)}<div class="split"><div class="stack">
      ${fixed ? `<p class="help">${esc(fmt(t.fixed_form, { name: o.forms()[0].name }))}</p>` : `<fieldset class="seg" style="display:block"><legend>${esc(t.legend)}</legend><div class="formlist">${list}</div></fieldset>
      <label class="key key--file">${esc(t.upload)}<input type="file" id="fupload" accept="${esc(o.accept || '.hwpx')}"></label>
      <p class="help">${esc(t.upload_hint)}</p>`}
      ${status(o.statusKey)}
      <button type="button" class="key key--primary" id="fill" ${st.form ? '' : 'disabled aria-describedby="fill-why"'} ${st.busy.fill ? 'aria-busy="true"' : ''}>${esc(st.busy.fill ? t.filling : t.fill)}</button>
      ${st.form ? '' : `<p class="help" id="fill-why">${esc(t.fill_disabled)}</p>`}
      ${side}
    </div><section class="cut doc" aria-live="polite">${doc}</section></div>`;
}
async function previewPanel(st, o) {
  st.previewState = 'loading'; st.result = null; render();
  try { st.preview = (await api('/api/form_preview?form=' + encodeURIComponent(st.form))).url; st.previewState = 'idle'; }
  catch (e) { st.previewState = 'idle'; st.preview = null; say(o.statusKey, fmt(S.ui.error, { msg: e.message }), true); }
  render();
}
function bindPanel(st, o) {
  $$('[data-left]').forEach((b) => (b.onclick = () => {
    const t = o.t, x = st.result.report.leftover[Number(b.dataset.left)];
    popup(fmt(t.leftover_title, { where: x.where }),
      [{ text: fmt(t.leftover_what, { kind: x.kind, values: x.values.join(', ') }) }, { text: x.action }, { text: t.leftover_note, muted: true }]);
  }));
  $$('input[name="form"]').forEach((el) => (el.onchange = () => { st.form = el.value; S.status[o.statusKey] = null; previewPanel(st, o); }));
  const up = $('#fupload');
  if (up) up.onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try { const r = await api('/api/upload', { name: f.name, data: await readB64(f) }); o.setForms(r); st.form = r.path; previewPanel(st, o); }
    catch (err) { say(o.statusKey, fmt(S.ui.error, { msg: err.message }), true); render(); }
  };
  $('#fill').onclick = async () => {
    st.busy.fill = true; render();
    try { st.result = await o.fill(); S.status[o.statusKey] = null; if (o.done) o.done(st.result); }
    catch (err) { say(o.statusKey, fmt(S.ui.error, { msg: err.message }), true); }
    st.busy.fill = false; render();
  };
}
const setAllForms = (r) => { S.init.forms = r.forms; if (S.init.gyeolui) S.init.gyeolui.forms = r.gyeolui_forms; };
const pumuiPanel = () => ({
  t: S.ui.form, forms: () => S.init.forms, statusKey: 'form', missingHref: `#${keys()[3]}`, setForms: setAllForms,
  fill: () => api('/api/fill', { form: S.form, id: S.init.selection.result_file }),
  done: (r) => { if (r.handoff && S.init.gyeolui) S.init.gyeolui.handoff = r.handoff; },
  // 품의서를 다 쓰면 결의로 이어 가는 길을 보여 준다
  extra: (r) => (r.handoff && MODES.gyeolui ? `<div class="note"><p>${esc(S.ui.form.handoff_done)}</p><p style="margin-top:var(--space-sm)"><a class="key" href="#${MODES.gyeolui.keys()[0]}">${esc(S.ui.form.handoff_go)}</a></p></div>` : ''),
});
const viewForm = () => formPanel(S, pumuiPanel());
const bindForm = () => bindPanel(S, pumuiPanel());

// ---------------------------------------------------------------- 그리기
// 양식 미리보기(A4 폭)를 미리보기 칸 폭에 맞춰 줄인다. 화면이 좁아도 가로로 잘리지 않게.
function fitFrame(f) {
  try {
    const d = f.contentDocument; if (!d || !d.body) return;
    d.documentElement.style.zoom = '';
    const w = d.documentElement.scrollWidth;
    d.documentElement.style.zoom = String(Math.min(1, (f.clientWidth - 2) / w));
  } catch (e) { /* 다른 출처면 그대로 둔다 */ }
}
const fitFrames = () => $$('.doc iframe').forEach((f) => { f.onload = () => fitFrame(f); fitFrame(f); });
// 상단 바 실제 높이(글꼴, 줄바꿈에 따라 다름)를 CSS 변수로 넘긴다: 견적 목록 칸 높이 계산용
const barH = () => document.documentElement.style.setProperty('--bar-h', `${$('.bar').offsetHeight}px`);
if (window.ResizeObserver) new ResizeObserver(barH).observe(document.querySelector('.bar'));
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { barH(); $$('.doc iframe').forEach(fitFrame); }, 100); });

MODES.pumui = {
  keys: () => S.ui.step_keys, steps: () => S.ui.steps, wordmark: () => S.ui.wordmark, title: () => S.ui.title,
  open: pumuiOpen,
  views: [[viewQuotes, bindQuotes], [viewReview, bindReview], [viewVendor, bindVendor], [viewCase, bindCase], [viewForm, bindForm]],
  // 업체 선정 단계는 비교 결과를 받아 온 뒤 다시 그린다
  after: async (at, redraw) => {
    // 품의서 단계: 양식이 하나로 고정되어 있으면 바로 골라 미리보기를 띄운다
    if (at === 4 && !S.form && S.init.forms.length === 1) { S.form = S.init.forms[0].path; await previewPanel(S, pumuiPanel()); return; }
    if (at !== 2 || S.compare) return;
    try { await loadCompare(); } catch (e) { say('vendor', fmt(S.ui.error, { msg: e.message }), true); S.compare = { quotes: [], excluded: [], recommended: null, auto_reason: '' }; }
    redraw();
  },
};
async function render(fromNav = false) {
  if (S.error) {
    $('#main').innerHTML = `<div class="note note--error"><p>${esc(S.error)}</p></div><div class="actions"><button type="button" class="key key--primary" id="retry">${esc(S.ui ? S.ui.retry : '다시 불러오기')}</button></div>`;
    $('#retry').onclick = start; return;
  }
  const at = current();
  if (keys()[at] !== location.hash.slice(1)) { history.replaceState(null, '', '#' + keys()[at]); }
  renderTabs();
  const [view, bind] = M().views[at];
  const draw = () => { $('#main').innerHTML = view(); bind(); fitFrames(); };
  $('#main').classList.toggle('fit', at === 0);
  draw();
  if (M().after) await M().after(at, draw);
  if (fromNav) $('#main').focus({ preventScroll: true });
}

async function start() {
  S.error = null; $('#main').innerHTML = '<p class="muted" aria-busy="true">불러오는 중</p>';
  try {
    S.init = await api('/api/init'); S.ui = S.init.ui;
    if (MODES.gyeolui && MODES.gyeolui.init) MODES.gyeolui.init();
    S.set = Math.max(0, S.init.sets.findIndex((_, i) => S.init.quotes.some((q) => q.set === i)));
    if (S.init.selection) { S.chosen = S.init.selection.result_file; }
  } catch (e) { S.error = fmt(S.ui ? S.ui.load_error : '작업 화면을 불러오지 못했습니다: {msg}', { msg: e.message }); }
  render();
}
// 결의 화면(gyeolui.js)이 MODES에 붙은 뒤 시작한다
window.addEventListener('DOMContentLoaded', start);
