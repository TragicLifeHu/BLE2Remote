'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const DAYS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
const DAY_LONG = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const TMIN = 18, TMAX = 35, ARC = 395.8, CIRC = 527.8;

let S = {};            // heater state from /api/state
let SCH = null;        // schedule config from /api/schedule
let pendingSp = null;  // setpoint being adjusted locally (debounced)
let spTimer = null;
let lockDismissed = false;
let applyingUntil = 0;  // heater commits a new setpoint ~3 s after the command (display flashes)

// ---------------- api ----------------
async function api(path, method = 'GET', body) {
  const r = await fetch('api/' + path, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || r.statusText); e.data = data; e.status = r.status; throw e; }
  return data;
}
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.remove('out'); t.hidden = false;
  clearTimeout(toast.h);
  toast.h = setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => { if (t.classList.contains('out')) t.hidden = true; }, 220);
  }, 3000);
}
async function control(ch) {
  try { await api('control', 'POST', ch); lockDismissed = false; setTimeout(refresh, 900); setTimeout(refresh, 3500); }
  catch (e) { toast(e.message); refresh(); }
}

// ---------------- helpers ----------------
const pad = n => String(n).padStart(2, '0');
function parseLocal(s) { const [d, t] = s.split('T'); const [y, m, dd] = d.split('-').map(Number); const [h, mi] = t.split(':').map(Number); return new Date(y, m - 1, dd, h, mi); }
function bridgeNow() { return S.clock ? parseLocal(S.clock.now.slice(0, 16)) : new Date(); }
function whenLabel(s) {
  const d = parseLocal(s), now = bridgeNow();
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - day0) / 864e5);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (diff === 0) return `Today ${hm}`;
  if (diff === 1) return `Tomorrow ${hm}`;
  if (diff > 1 && diff < 7) return `${DAY_LONG[(d.getDay() + 6) % 7]} ${hm}`;
  return `${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} ${hm}`;
}
function actionText(a) {
  if (!a.power) return 'Turn off';
  const s = a.eco ? 'Power-save' : a.setpoint != null ? `Heat to ${a.setpoint}°` : 'Turn on';
  return a.hours ? `${s} for ${a.hours} h` : s;
}
function actionPill(a) {
  const cls = !a.power ? '' : a.eco ? 'eco' : 'heat';
  return `<span class="pill ${cls}">${esc(actionText(a))}</span>`;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function since(t) {
  const m = Math.floor((Date.now() / 1000 - t) / 60);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d`;
}
function bars(rssi) { return rssi == null ? 0 : rssi > -60 ? 4 : rssi > -70 ? 3 : rssi > -80 ? 2 : 1; }
function ago(t) { const s = Math.round(Date.now() / 1000 - t); if (s < 60) return 'just now'; if (s < 3600) return `${Math.round(s / 60)} min ago`; if (s < 86400) return `${Math.round(s / 3600)} h ago`; return `${Math.round(s / 86400)} d ago`; }

// ---------------- skeletons ----------------
const T0 = performance.now();
const SKELETON_MIN_MS = 550;   // short loads shouldn't blink the placeholders
function reveal(cls) {
  if (!document.body.classList.contains(cls)) return;
  const wait = Math.max(0, SKELETON_MIN_MS - (performance.now() - T0));
  setTimeout(() => document.body.classList.remove(cls), wait);
}

// ---------------- home ----------------
function setStateClass(st) {
  const b = document.body.classList;
  ['st-heat', 'st-eco', 'st-off', 'st-offline'].forEach(c => c !== 'st-' + st && b.remove(c));
  b.add('st-' + st);
}
function renderHome() {
  const body = document.body;
  const av = S.available;
  const unpaired = !S.paired && S.ble === 'unpaired';
  let st = 'offline', label = 'Offline';
  if (av) { st = !S.power ? 'off' : S.eco ? 'eco' : 'heat'; label = !S.power ? 'Off' : S.eco ? 'Power-save' : 'Heating'; }
  setStateClass(st);

  // status line
  let line;
  if (unpaired) line = 'No heater paired';
  else if (S.ble === 'disconnected') line = 'Bridge service unreachable';
  else if (S.ble === 'released') line = 'Bluetooth released for other controls';
  else if (!av) line = 'Heater offline' + (S.last_seen ? ` · seen ${ago(S.last_seen)}` : '');
  else line = `Connected · ${label}`;
  $('#h-status span:last-child').textContent = line;
  if (S.clock) $('#h-clock').textContent = S.clock.now.slice(11, 16);

  // banners
  $('#b-lock').hidden = !(av && S.rejected) || lockDismissed;
  const bl = $('#b-link'), bt = $('#b-link-text'), bb = $('#b-link-btn');
  bl.hidden = av; bb.hidden = true;
  if (unpaired) { bt.innerHTML = '<b>No heater paired yet.</b><br>Pair the bridge with your heater to get started.'; bb.textContent = 'Pair'; bb.hidden = false; bb.onclick = openPair; }
  else if (S.ble === 'released') { bt.innerHTML = '<b>Bluetooth is free for other controls.</b><br>The bridge reconnects automatically afterwards.'; bb.textContent = 'Reconnect now'; bb.hidden = false; bb.onclick = () => release(0); }
  else if (S.ble === 'disconnected') bt.innerHTML = '<b>Can’t reach the bridge’s Bluetooth service.</b><br>It restarts automatically; check the BLE link device if this persists.';
  else bt.innerHTML = '<b>Waiting for the heater…</b><br>It reconnects by itself when the heater has power and is in range. Is the other controls connected to it?';

  // dial
  const can = S.can || {};
  const sp = pendingSp ?? S.setpoint;
  const spEl = $('#sp'), spTxt = av && sp != null ? String(sp) : '--';
  if (spEl.textContent !== spTxt) {
    const dir = +spTxt > +spEl.textContent ? 'up' : 'down';
    const animate = spEl.textContent !== '--' && spTxt !== '--';
    spEl.textContent = spTxt;
    spEl.classList.remove('tick-up', 'tick-down');
    if (animate) { void spEl.offsetWidth; spEl.classList.add('tick-' + dir); }
  }
  $('#mode-label').textContent = av ? label : '—';
  const frac = av && S.power && sp != null ? (sp - TMIN) / (TMAX - TMIN) : 0;
  $('#dial-arc').style.strokeDasharray = `${Math.max(0.001, frac * ARC)} ${CIRC}`;
  $('#t-down').disabled = !can.setpoint || sp <= TMIN;
  $('#t-up').disabled = !can.setpoint || sp >= TMAX;
  let hint = ' ';
  if (applyingUntil > Date.now()) hint = 'Sent — the heater applies it after its display stops flashing (~3 s)';
  else if (av && !S.power) hint = 'Turn the heater on to change temperature';
  else if (av && S.eco) hint = 'Changing the temperature ends power-save';
  $('#dial-hint').textContent = hint;

  // power
  const pw = $('#power');
  pw.disabled = !av;
  $('span', pw).textContent = av && S.power ? 'Turn off' : 'Turn on';

  // tiles
  const ecoOn = av && S.power && S.eco;   // the heater keeps the eco flag while off; it only matters when on
  setTile('#tile-eco', ecoOn, ecoOn ? 'On' : 'Off', !can.eco);
  // while the heater is off its timer is an on-timer: it switches itself on when the timer runs out
  const onTimer = av && !S.power;
  $('#tile-timer .t-name').textContent = onTimer ? 'On-timer' : 'Off-timer';
  setTile('#tile-timer', av && S.timer_hours > 0,
    av && S.timer_hours ? (onTimer ? onTimerLabel() : `${S.timer_hours} h left${S.manual_timer ? ' · until schedule' : ''}`) : 'Off', !can.timer);
  setTile('#tile-voice', av && S.voice, av && S.voice ? 'On' : 'Off', !can.voice);

  // next event
  // device card
  const dc = $('#dev-card'), dv = S.device;
  dc.hidden = !dv;
  if (dv) {
    const conn = S.ble === 'connected';
    $('#dev-name').textContent = dv.name || 'Paired heater';
    const parts = [dv.mac ? `MAC ${dv.mac}` : `ID ${dv.id.slice(0, 8)}`];
    if (conn) {
      if (dv.rssi != null) parts.push(`${dv.rssi} dBm`);
      if (dv.connected_since) parts.push(`connected ${since(dv.connected_since)}`);
    } else {
      parts.push(S.ble === 'released' ? 'released for other controls' : 'offline' + (S.last_seen ? ` · seen ${ago(S.last_seen)}` : ''));
    }
    $('#dev-sub').innerHTML = parts.map(p => `<span class="nw">${esc(p)}</span>`).join(' · ');
    dc.classList.toggle('offline', !conn);
    const n = conn ? bars(dv.rssi) : 0;
    $$('#dev-bars i').forEach((b, i) => b.classList.toggle('on', i < n));
  }

  const ne = $('#next-eyebrow'), nt = $('#next-text'), np = $('#next-preview');
  np.hidden = true;
  if (S.paused_until) { ne.textContent = 'Schedules paused'; nt.textContent = `Resuming ${whenLabel(S.paused_until)}`; }
  else if (S.next) {
    ne.textContent = 'Next · ' + whenLabel(S.next.at);
    nt.textContent = `${S.next.name ? S.next.name + ' · ' : ''}${actionText(S.next.action)}`;
    if (S.next.preview) {
      np.hidden = false;
      np.className = 'preview ' + S.next.preview.effect;
      np.textContent = S.next.preview.text + (S.next.preview.effect === 'unknown' ? '' : ' · if nothing changes before then');
    }
  }
  else { ne.textContent = 'Schedule'; nt.textContent = 'Nothing scheduled — tap to add'; }
}
const hhmm = at => at.slice(11, 16);
function onTimerLabel() {
  const ot = S.on_timer;
  if (!ot) return `On in ${S.timer_hours} h`;
  return `On ~${hhmm(ot.start)}` + (ot.off_after ? ` · runs ${ot.off_after} h` : '');
}
function setTile(sel, on, val, disabled) {
  const t = $(sel); t.classList.toggle('on', !!on); t.disabled = !!disabled || !S.available;
  $('.t-val', t).textContent = val;
}
function bump(d) {
  if (!S.available || !S.power) return;
  const base = pendingSp ?? S.setpoint ?? 23;
  pendingSp = Math.min(TMAX, Math.max(TMIN, base + d));
  renderHome();
  clearTimeout(spTimer);
  spTimer = setTimeout(async () => {
    const v = pendingSp;
    applyingUntil = Date.now() + 4000;
    await control({ setpoint: v });
    renderHome();
    setTimeout(() => { if (pendingSp === v) pendingSp = null; renderHome(); }, 4000);
  }, 650);
}

// manual timer: off-timer while on, on-timer while off. The bridge answers 409 when the schedule
// interferes (off-timer: events inside the window; on-timer: events that would cancel it, or nothing
// that turns the heater off afterwards) and the user has to choose.
async function setTimer(h, mode, offAfter) {
  const body = { timer_hours: h };
  if (mode) body.timer_mode = mode;
  if (offAfter) body.off_after = offAfter;
  const onTimer = S.available && !S.power && h > 0;
  try {
    await api('control', 'POST', body);
    if (onTimer) toast(mode === 'timer' ? 'On-timer set — events that would cancel it are skipped' : 'On-timer set');
    else if (mode === 'timer') toast('Timer set — overlapping events skipped');
    else if (mode === 'schedule') toast('Timer set — the next schedule change will cancel it');
    setTimeout(refresh, 900); setTimeout(refresh, 3500);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.on_timer) return openOnTimerConfirm(h, e.data.on_timer);
    if (e.status === 409 && e.data && e.data.overlaps) return openTimerConfirm(h, e.data.overlaps);
    toast(e.message); refresh();
  }
}
function openOnTimerConfirm(h, info) {
  let mode = info.cancels.length ? 'timer' : 'ok';
  let offAfter = info.long ? null : 0;    // null = not chosen yet (required when nothing ends it soon)
  $('#ot-sub').textContent = `The heater switches itself on around ${hhmm(info.start)} (in ${h} h) with its last ` +
    'settings. It doesn’t switch itself off afterwards.';
  $('#ot-cancels').hidden = !info.cancels.length;
  $('#ot-cancels-lead').textContent = info.cancels.length === 1 ? 'this event turns it on' : 'these events turn it on';
  $('#ot-list').innerHTML = info.cancels.map(o =>
    `<div class="item"><span class="when">${whenLabel(o.at)}</span><span class="what">${esc(o.name ? o.name + ' · ' : '')}${esc(o.action)}</span></div>`).join('');
  const drawChoice = () => $$('#ot-choice .choice').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  $$('#ot-choice .choice').forEach(b => (b.onclick = () => { mode = b.dataset.mode; drawChoice(); }));
  drawChoice();
  const off = info.off;
  const safety = S.max_on_hours ? ` Only the ${S.max_on_hours} h safety limit would stop it sooner.` : '';
  const who = off && (off.name ? `“${off.name}”` : off.action);
  const then = !off ? '' : off.how === 'timer'
    ? `${who} takes over, and its timer ends heating ${whenLabel(off.at)}`
    : `${who} turns it off ${whenLabel(off.at)}`;
  if (!info.long) $('#ot-off').textContent = `Afterwards: ${then}.`;
  else if (off) $('#ot-off').innerHTML = `<b>It would heat for ${off.runs_h} h: ${esc(then)}.</b>${esc(safety)} ` +
    'Switch it off sooner, after it has run for:';
  else $('#ot-off').innerHTML = `<b>Nothing in your schedule turns it off afterwards.</b>${esc(safety)} ` +
    'Switch it off after it has run for:';
  $('#ot-chips').hidden = !info.long;
  const drawChips = () => {
    $('#ot-chips').innerHTML = [0, 1, 2, 3, 4, 6, 8, 12, 16, 24].map(x =>
      `<button data-h="${x}" class="${offAfter === x ? 'on' : ''}">${x ? x + ' h' : off ? 'Schedule' : 'Keep on'}</button>`).join('');
    $$('#ot-chips button').forEach(b => (b.onclick = () => { offAfter = +b.dataset.h; $('#ot-err').textContent = ''; drawChips(); }));
  };
  drawChips();
  $('#ot-err').textContent = '';
  $('#ot-cancel').onclick = closeSheets;
  $('#ot-go').onclick = () => {
    if (offAfter === null) { $('#ot-err').textContent = 'Pick how long it should run, or “Keep on”.'; return; }
    closeSheets(); setTimer(h, mode, offAfter);
  };
  openSheet('#sh-ontimer');
}
function openTimerConfirm(h, overlaps) {
  const end = new Date(bridgeNow().getTime() + h * 3600e3);
  $('#to-sub').textContent = `A ${h} h timer switches the heater off around ${pad(end.getHours())}:${pad(end.getMinutes())}. ` +
    `${overlaps.length === 1 ? 'This scheduled event falls' : 'These scheduled events fall'} inside that time:`;
  $('#to-list').innerHTML = overlaps.map(o =>
    `<div class="item"><span class="when">${whenLabel(o.at)}</span><span class="what">${esc(o.name ? o.name + ' · ' : '')}${esc(o.action)}</span></div>`).join('');
  $('#to-timer').onclick = () => { closeSheets(); setTimer(h, 'timer'); };
  $('#to-sched').onclick = () => { closeSheets(); setTimer(h, 'schedule'); };
  openSheet('#sh-timer-ok');
}

// ---------------- schedule ----------------
async function loadSchedule() {
  try { SCH = await api('schedule'); renderSchedule(); } catch (e) { toast(e.message); }
}
let logTab = 'runs';
function showLog(which) {
  logTab = which;
  $$('#log-seg button').forEach(b => b.classList.toggle('on', b.dataset.log === which));
  $('#runs').hidden = which !== 'runs';
  $('#activity').hidden = which !== 'activity';
  $('#log-sub').textContent = which === 'runs' ? 'Scheduled events · last 7 days' : 'Everything the bridge did or saw · latest 100';
  try { localStorage.setItem('logTab', which); } catch (e) { /* ignore */ }
  which === 'runs' ? loadRuns() : loadActivity();
}
const RESULT = {
  applied: 'Done', noop: 'No change', rejected: 'Ignored', error: 'Failed', running: 'Running…',
  skipped: 'Skipped', paused: 'Paused', missed: 'Missed', superseded: 'Replaced',
};
// Render a list only when its data changed. The first real render (replacing the skeletons)
// animates once; later updates swap in quietly. Prevents the double entrance animation.
function fill(el, sig, build) {
  if (el._sig === sig) return;
  const first = el._sig === undefined;
  el._sig = sig;
  build();
  if (first) {
    el.classList.remove('fresh'); void el.offsetWidth; el.classList.add('fresh');
    clearTimeout(el._ft); el._ft = setTimeout(() => el.classList.remove('fresh'), 900);
  }
}

async function loadRuns() {
  let runs;
  try { runs = await api('runs'); } catch (e) { return; }
  const el = $('#runs');
  fill(el, JSON.stringify(runs) + (S.clock ? S.clock.now.slice(0, 10) : ''), () => {
  el.innerHTML = runs.length ? '' : '<div class="empty">No scheduled runs in the last 7 days.</div>';
  let day = '';
  runs.forEach(r => {
    const d = r.at.slice(0, 10);
    if (d !== day) {
      day = d;
      const h = document.createElement('div');
      h.className = 'dayhead';
      h.textContent = whenLabel(r.at).replace(/ \d\d:\d\d$/, '');
      el.appendChild(h);
    }
    const m = [];
    if (r.delay_s > 60) m.push(`ran ${Math.round(r.delay_s / 60)} min late`);
    else if (r.delay_s != null && !['skipped', 'paused', 'missed', 'superseded'].includes(r.result)) m.push(`ran +${r.delay_s} s`);
    if (r.commands) m.push(`${r.commands} cmd${r.commands > 1 ? 's' : ''}`);
    if (r.duration_s) m.push(`confirmed in ${r.duration_s} s`);
    if (r.before && r.after && r.before !== r.after) m.push(`${r.before} → ${r.after}`);
    else if (r.detail) m.push(r.detail);
    const row = document.createElement('div');
    row.className = 'item run';
    row.innerHTML = `<div class="top-line"><span class="t">${r.at.slice(11, 16)}</span>
      <span class="what">${esc(r.name ? r.name + ' · ' : '')}${esc(r.action)}</span>
      <span class="badge ${esc(r.result)}">${RESULT[r.result] || esc(r.result)}</span></div>
      <div class="metrics">${esc(m.join(' · '))}</div>`;
    el.appendChild(row);
  });
  });
}
async function saveSchedule(next) {
  SCH = await api('schedule', 'PUT', { rules: next.rules, events: next.events, settings: next.settings });
  renderSchedule(); refresh();
}
function renderSchedule() {
  if (!SCH) return;
  reveal('sched-loading');
  $('#s-sub').textContent = `Bridge time ${SCH.now.replace('T', ' ')} · ${S.clock ? S.clock.tz : ''}`;
  const st = SCH.settings;
  $('#pause-toggle').checked = !!SCH.paused;
  $('#pause-until').hidden = !SCH.paused;
  if (SCH.paused) $('#pause-date').value = st.pause_until.slice(0, 10);
  $('#pause-sub').textContent = SCH.paused ? `Paused — resumes ${whenLabel(st.pause_until)}` : 'Holiday mode — nothing runs until you resume.';

  // conflicts: banner list + per-item flags
  const flags = {};
  const LV = { error: 'Clash', warn: 'Overlap', info: 'Note' };
  (SCH.conflicts || []).forEach(c => c.ids.forEach(id => { if (!flags[id] || c.level === 'error') flags[id] = c.level; }));
  fill($('#conflicts'), JSON.stringify(SCH.conflicts || []), () => {
    $('#conflicts').innerHTML = (SCH.conflicts || []).map(c =>
      `<div class="conflict ${c.level}"><div><b>${LV[c.level]}</b>${esc(c.text)}</div></div>`).join('');
  });
  const day = SCH.now.slice(0, 10);   // relative labels (Today/Tomorrow) change at midnight
  const flag = id => flags[id] && flags[id] !== 'info' ? `<span class="flag ${flags[id]}">⚠ ${LV[flags[id]]}</span>` : '';

  const rules = $('#rules');
  fill(rules, JSON.stringify([SCH.rules, flags]), () => {
  rules.innerHTML = SCH.rules.length ? '' : '<div class="empty">No weekly schedules yet. Add one to warm up before you get up, come home or go to bed.</div>';
  [...SCH.rules].sort((a, b) => a.time.localeCompare(b.time)).forEach(r => {
    const el = document.createElement('div');
    el.className = 'card rule' + (r.enabled ? '' : ' disabled');
    el.innerHTML = `<div class="time">${r.time}</div>
      <div class="meta"><div class="name">${esc(r.name || actionText(r.action))}${flag(r.id)}</div>
        <div class="daydots">${DAYS.map((d, i) => `<span class="${r.days.includes(i) ? 'on' : ''}">${d}</span>`).join('')}</div>
        <div style="margin-top:6px">${actionPill(r.action)}</div></div>
      <label class="switch" onclick="event.stopPropagation()"><input type="checkbox" ${r.enabled ? 'checked' : ''}><span></span></label>`;
    $('input', el).onchange = e => { r.enabled = e.target.checked; saveSchedule(SCH).catch(err => toast(err.message)); };
    el.onclick = () => openEditor('rule', r);
    rules.appendChild(el);
  });
  });

  const evs = $('#events');
  fill(evs, JSON.stringify([SCH.events, flags, day]), () => {
  evs.innerHTML = SCH.events.length ? '' : '<div class="empty">No one-time events. Handy for “warm up before I’m back from the trip”.</div>';
  [...SCH.events].sort((a, b) => a.at.localeCompare(b.at)).forEach(e => {
    const el = document.createElement('div');
    el.className = 'card rule' + (e.enabled ? '' : ' disabled');
    el.innerHTML = `<div class="meta"><div class="name">${esc(e.name || actionText(e.action))}${flag(e.id)}</div>
      <div class="sub">${whenLabel(e.at)}</div><div style="margin-top:6px">${actionPill(e.action)}</div></div>
      <svg class="chev"><use href="#i-chev"/></svg>`;
    el.onclick = () => openEditor('event', e);
    evs.appendChild(el);
  });
  });

  const up = $('#upcoming');
  const items = SCH.upcoming.slice(0, 12);
  fill(up, JSON.stringify([items, day]), () => {
  up.innerHTML = items.length ? '' : '<div class="empty">Nothing in the next 8 days.</div>';
  items.forEach(u => {
    const el = document.createElement('button');
    el.className = 'item' + (u.skipped || u.paused ? ' skipped' : '');
    el.innerHTML = `<span class="when">${whenLabel(u.at)}</span><span class="what">${esc(u.name ? u.name + ' · ' : '')}${esc(actionText(u.action))}</span>
      <span class="skiptag">${u.paused ? 'paused' : u.skipped ? 'skipped · undo' : 'skip'}</span>`;
    el.disabled = u.paused;
    el.onclick = async () => { try { SCH = await api('schedule/skip', 'POST', { key: u.key, skip: !u.skipped }); renderSchedule(); refresh(); } catch (e) { toast(e.message); } };
    up.appendChild(el);
  });
  });
  renderSettings();
}

// editor sheet
let ED = null;
function openEditor(kind, item) {
  const now = bridgeNow();
  const tomorrow = new Date(now.getTime() + 864e5);
  ED = item ? JSON.parse(JSON.stringify(item)) : {
    name: '', enabled: true, days: [0, 1, 2, 3, 4], time: '07:00',
    at: `${tomorrow.getFullYear()}-${pad(tomorrow.getMonth() + 1)}-${pad(tomorrow.getDate())}T07:00`,
    action: { power: true, setpoint: 22, eco: false, hours: 0 },
  };
  if (ED.action.hours == null) ED.action.hours = 0;
  ED.kind = kind; ED.isNew = !item;
  if (ED.action.setpoint == null) ED.action.setpoint = 22;
  if (!ED.days) ED.days = [0, 1, 2, 3, 4];
  if (!ED.time) ED.time = '07:00';
  if (!ED.at) ED.at = `${tomorrow.getFullYear()}-${pad(tomorrow.getMonth() + 1)}-${pad(tomorrow.getDate())}T07:00`;
  $('#ed-title').textContent = item ? 'Edit schedule' : 'New schedule';
  $('#ed-kind').hidden = !!item;
  $('#ed-del').hidden = !item;
  $('#ed-name').value = ED.name || '';
  $('#ed-time').value = ED.time;
  $('#ed-at').value = ED.at;
  $('#ed-err').textContent = '';
  drawEditor();
  openSheet('#sh-edit');
}
function edAct() { return !ED.action.power ? 'off' : ED.action.eco ? 'eco' : 'heat'; }
function drawEditor() {
  $$('#ed-kind button').forEach(b => b.classList.toggle('on', b.dataset.kind === ED.kind));
  $('#ed-weekly').hidden = ED.kind !== 'rule';
  $('#ed-once').hidden = ED.kind !== 'event';
  $('#ed-days').innerHTML = DAYS.map((d, i) => `<button data-d="${i}" class="${ED.days.includes(i) ? 'on' : ''}" aria-label="${DAY_LONG[i]}">${d}</button>`).join('');
  $$('#ed-days button').forEach(b => b.onclick = () => { const d = +b.dataset.d; ED.days = ED.days.includes(d) ? ED.days.filter(x => x !== d) : [...ED.days, d].sort(); drawEditor(); });
  $$('#ed-action button').forEach(b => b.classList.toggle('on', b.dataset.act === edAct()));
  $('#ed-temp').hidden = edAct() !== 'heat';
  $('#ed-tval').textContent = `${ED.action.setpoint}°C`;
  $('#ed-dur').hidden = edAct() === 'off';
  const hs = $('#ed-hours');
  if (!hs.options.length) {
    hs.innerHTML = '<option value="0">Until the next event</option>' +
      Array.from({ length: 24 }, (_, i) => `<option value="${i + 1}">${i + 1} hour${i ? 's' : ''}</option>`).join('');
    hs.onchange = () => { ED.action.hours = +hs.value; drawEditor(); };
  }
  hs.value = ED.action.hours || 0;
  const maxOn = SCH && SCH.settings.max_on_hours ? SCH.settings.max_on_hours : 0;
  $('#ed-dur-hint').textContent = ED.action.hours
    ? 'Sets the heater’s own off-timer, so it switches off even if the bridge is down.' +
      (maxOn && ED.action.hours > maxOn ? ` Note: your ${maxOn} h maximum on-time will turn it off first.` : '')
    : 'The heater’s own timer is left as it is.';
}
async function saveEditor() {
  ED.name = $('#ed-name').value.trim();
  ED.time = $('#ed-time').value;
  ED.at = $('#ed-at').value.slice(0, 16);
  const a = edAct();
  const action = { power: a !== 'off', eco: a === 'eco', setpoint: a === 'heat' ? ED.action.setpoint : null,
                   hours: a === 'off' ? 0 : (ED.action.hours || 0) };
  if (ED.kind === 'rule' && !ED.days.length) { $('#ed-err').textContent = 'Pick at least one day.'; return; }
  if (ED.kind === 'rule' && !ED.time) { $('#ed-err').textContent = 'Pick a time.'; return; }
  if (ED.kind === 'event' && !ED.at) { $('#ed-err').textContent = 'Pick a date and time.'; return; }
  const next = JSON.parse(JSON.stringify(SCH));
  const item = ED.kind === 'rule'
    ? { id: ED.id, name: ED.name, enabled: ED.enabled !== false, days: ED.days, time: ED.time, action }
    : { id: ED.id, name: ED.name, enabled: ED.enabled !== false, at: ED.at, action };
  const list = ED.kind === 'rule' ? next.rules : next.events;
  const i = list.findIndex(x => x.id && x.id === ED.id);
  if (i >= 0) list[i] = item; else list.push(item);
  try {
    await saveSchedule(next);
    closeSheets();
    const w = (SCH.conflicts || []).filter(c => c.level === 'warn').length;
    toast(w ? `Saved — check the overlap warning${w > 1 ? 's' : ''} above` : 'Schedule saved');
  } catch (e) { $('#ed-err').textContent = e.message; }
}
async function deleteEditor() {
  const next = JSON.parse(JSON.stringify(SCH));
  if (ED.kind === 'rule') next.rules = next.rules.filter(r => r.id !== ED.id);
  else next.events = next.events.filter(e => e.id !== ED.id);
  try { await saveSchedule(next); closeSheets(); toast('Deleted'); } catch (e) { $('#ed-err').textContent = e.message; }
}

// ---------------- settings ----------------
function renderSettings() {
  $('#set-ver').textContent = S.version ? `Bridge v${S.version} · ${S.clock ? S.clock.tz : ''}` : '';
  const paired = !!S.paired;
  const dv = S.device || {};
  $('#pair-title').textContent = paired ? (dv.name || 'Paired heater') : 'Not paired';
  $('#pair-sub').textContent = paired
    ? [dv.mac ? `MAC ${dv.mac}` : `ID ${S.paired.slice(0, 8)}`, S.available ? 'connected' : S.ble,
       dv.rssi != null ? `${dv.rssi} dBm` : null].filter(Boolean).join(' · ')
    : 'Pair the bridge with your heater.';
  $('#pair-btn').textContent = paired ? 'Pair a different heater' : 'Pair a heater';
  $('#forget-btn').hidden = !paired;
  if (!SCH) return;
  const st = SCH.settings;
  const mo = $('#max-on');
  if (!mo.options.length) {
    mo.innerHTML = '<option value="0">No limit</option>' + Array.from({ length: 24 }, (_, i) => `<option value="${i + 1}">${i + 1} h</option>`).join('');
  }
  if (document.activeElement !== mo) mo.value = st.max_on_hours;
  const cu = $('#catchup');
  if (document.activeElement !== cu) cu.value = [0, 15, 30, 60, 120].includes(st.catchup_minutes) ? st.catchup_minutes : 30;
}
async function setSetting(k, v) {
  const next = JSON.parse(JSON.stringify(SCH));
  next.settings[k] = v;
  try { await saveSchedule(next); toast('Saved'); } catch (e) { toast(e.message); }
}
async function loadActivity() {
  try {
    const items = await api('activity');
    const el = $('#activity');
    fill(el, JSON.stringify(items.slice(0, 40)), () => {
    el.innerHTML = items.length ? '' : '<div class="empty">Nothing yet.</div>';
    items.slice(0, 40).forEach(a => {
      const d = new Date(a.t * 1000);
      const row = document.createElement('div');
      row.className = 'item';
      row.innerHTML = `<span class="when">${d.toLocaleDateString('en', { month: 'short', day: 'numeric' })} ${pad(d.getHours())}:${pad(d.getMinutes())}</span>
        <span class="what"><span class="act-kind ${esc(a.kind.split(' ')[0])}">${esc(a.kind)}</span><br>${esc(a.text)}</span>`;
      el.appendChild(row);
    });
    });
  } catch (e) { /* ignore */ }
}
async function release(min) {
  try { await api('release', 'POST', { minutes: min }); toast(min ? `Bluetooth free for ${min} min` : 'Reconnecting…'); setTimeout(refresh, 800); }
  catch (e) { toast(e.message); }
}

// pairing
let pairPoll = null;
async function openPair() {
  openSheet('#sh-pair');
  $('#pair-list').innerHTML = '';
  try { await api('pair/scan', 'POST', { seconds: 15 }); } catch (e) { toast(e.message); }
  clearInterval(pairPoll);
  pairPoll = setInterval(drawPair, 1000);
  drawPair();
}
async function drawPair() {
  let r;
  try { r = await api('pair'); } catch (e) { return; }
  const list = $('#pair-list');
  const verify = 'These devices use the same kind of Bluetooth module as the heater — the bridge can’t tell what product each one is. ' +
    'Compare the MAC with the one on the heater’s settings page in the original platform.';
  $('#pair-hint').textContent = r.active
    ? 'Looking for devices nearby… Keep the BLE link device close to the heater. If another phone is connected to the heater, close the other controls there.'
    : r.devices.length ? 'Scan finished. ' + verify
    : 'Nothing found. Make sure the heater has power and nothing else is connected to it, then scan again.';
  if (!r.active) clearInterval(pairPoll);
  list.innerHTML = '';
  r.devices.forEach(d => {
    const bars = d.rssi > -60 ? 4 : d.rssi > -70 ? 3 : d.rssi > -80 ? 2 : 1;
    const el = document.createElement('button');
    el.className = 'card dev' + (d.previously_paired ? ' match' : '');
    const tag = d.id === r.paired ? '<span class="pill eco">Paired</span>'
      : d.previously_paired ? '<span class="pill">Previously paired</span>'
      : '<svg class="chev"><use href="#i-chev"/></svg>';
    el.innerHTML = `<div class="bars">${[1, 2, 3, 4].map(i => `<i class="${i <= bars ? 'on' : ''}" style="height:${i * 4}px"></i>`).join('')}</div>
      <div class="grow"><b>${esc(d.name || 'Unnamed device')}</b><div class="sub">${d.mac ? 'MAC ' + esc(d.mac) : 'MAC unknown · ' + esc(d.id.slice(0, 8))} · ${d.rssi} dBm</div></div>
      ${tag}`;
    el.onclick = async () => {
      try { await api('pair', 'POST', { id: d.id }); clearInterval(pairPoll); closeSheets(); toast('Paired — connecting…'); setTimeout(refresh, 1500); }
      catch (e) { toast(e.message); }
    };
    list.appendChild(el);
  });
}

// ---------------- sheets & nav ----------------
function openSheet(sel) {
  $$('.sheet').forEach(s => { s.hidden = true; s.classList.remove('closing'); });
  const sc = $('#scrim'); sc.classList.remove('closing'); sc.hidden = false;
  $(sel).hidden = false;
  document.body.classList.add('sheet-open');
}
function closeSheets() {
  clearInterval(pairPoll);
  const els = [...$$('.sheet'), $('#scrim')].filter(e => !e.hidden);
  if (!els.length) return;
  els.forEach(e => e.classList.add('closing'));
  setTimeout(() => {
    els.forEach(e => { if (e.classList.contains('closing')) { e.hidden = true; e.classList.remove('closing'); } });
    if ($$('.sheet').every(s => s.hidden)) document.body.classList.remove('sheet-open');
  }, 220);
}
function go(view) {
  $$('.view').forEach(v => {
    const show = v.dataset.view === view;
    if (show && v.hidden) {
      v.classList.remove('enter'); void v.offsetWidth; v.classList.add('enter');
      clearTimeout(v._enterT); v._enterT = setTimeout(() => v.classList.remove('enter'), 900);
    }
    v.hidden = !show;
  });
  $$('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.go === view));
  if (view === 'schedule' || view === 'settings') loadSchedule();
  if (view === 'log') showLog(logTab);
  try { localStorage.setItem('tab', view); } catch (e) { /* private mode */ }
  window.scrollTo(0, 0);
}

// ---------------- wiring ----------------
function wire() {
  $$('[data-go]').forEach(b => (b.onclick = () => go(b.dataset.go)));
  $('#t-down').onclick = () => bump(-1);
  $('#t-up').onclick = () => bump(1);
  $('#power').onclick = () => control({ power: !S.power });
  $('#tile-eco').onclick = () => control({ eco: !S.eco });
  $('#tile-voice').onclick = () => control({ voice: !S.voice });
  $('#tile-timer').onclick = () => {
    const off = !S.power;
    $('#sh-timer h3').textContent = off ? 'On-timer' : 'Off-timer';
    $('#sh-timer .sub').textContent = off
      ? 'The heater is off. It switches itself on after this many hours, using its last settings.'
      : 'The heater switches itself off after this many hours.';
    $('#timer-chips').innerHTML = [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24].map(h =>
      `<button data-h="${h}" class="${(S.timer_hours || 0) === h ? 'on' : ''}">${h ? h + ' h' : 'Off'}</button>`).join('');
    $$('#timer-chips button').forEach(b => (b.onclick = () => { closeSheets(); setTimer(+b.dataset.h); }));
    openSheet('#sh-timer');
  };
  $$('[data-dismiss]').forEach(b => (b.onclick = () => { lockDismissed = true; $('#' + b.dataset.dismiss).hidden = true; }));
  $('#scrim').onclick = closeSheets;
  $('#to-cancel').onclick = closeSheets;

  $('#s-add').onclick = () => openEditor('rule', null);
  $$('#ed-kind button').forEach(b => (b.onclick = () => { ED.kind = b.dataset.kind; drawEditor(); }));
  $$('#ed-action button').forEach(b => (b.onclick = () => {
    const a = b.dataset.act; ED.action.power = a !== 'off'; ED.action.eco = a === 'eco'; drawEditor();
  }));
  $$('.presets button').forEach(b => (b.onclick = () => { ED.days = b.dataset.days.split(',').map(Number); drawEditor(); }));
  $('#ed-tdown').onclick = () => { ED.action.setpoint = Math.max(TMIN, ED.action.setpoint - 1); drawEditor(); };
  $('#ed-tup').onclick = () => { ED.action.setpoint = Math.min(TMAX, ED.action.setpoint + 1); drawEditor(); };
  $('#ed-save').onclick = saveEditor;
  $('#ed-cancel').onclick = closeSheets;
  $('#ed-del').onclick = deleteEditor;

  $('#pause-toggle').onchange = e => {
    if (e.target.checked) {
      const d = bridgeNow(); d.setDate(d.getDate() + 7);
      setSetting('pause_until', `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T00:00`);
    } else setSetting('pause_until', null);
  };
  $('#pause-date').onchange = e => { if (e.target.value) setSetting('pause_until', e.target.value + 'T00:00'); };
  $('#max-on').onchange = e => setSetting('max_on_hours', +e.target.value);
  $('#catchup').onchange = e => setSetting('catchup_minutes', +e.target.value);
  $$('[data-release]').forEach(b => (b.onclick = () => release(+b.dataset.release)));
  $('#pair-btn').onclick = openPair;
  $('#forget-btn').onclick = async () => {
    if (!confirm('Forget this heater? The bridge will disconnect until you pair again.')) return;
    try { await api('pair/forget', 'POST', {}); toast('Heater forgotten'); refresh(); } catch (e) { toast(e.message); }
  };
  $('#pair-rescan').onclick = openPair;
  $$('#log-seg button').forEach(b => (b.onclick = () => showLog(b.dataset.log)));
  try { logTab = localStorage.getItem('logTab') || 'runs'; } catch (e) { /* ignore */ }
  $('#pair-close').onclick = closeSheets;
}

async function refresh() {
  try {
    S = await api('state');
    renderHome();
    reveal('loading');
    if (!$('#v-settings').hidden) renderSettings();
  } catch (e) {
    S = { available: false, ble: 'disconnected' };
    $('#h-status span:last-child').textContent = 'Bridge unreachable';
    setStateClass('offline');
    reveal('loading');
  }
}

wire();
let start = 'home';
try { start = localStorage.getItem('tab') || 'home'; } catch (e) { /* ignore */ }
go(start);
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 2500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); if (!$('#v-schedule').hidden) loadSchedule(); } });
setInterval(() => { if (!document.hidden && !$('#v-log').hidden) showLog(logTab); }, 15000);
