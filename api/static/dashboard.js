/* Relay dashboard. Read-only except the operator's own replies (posted as the
   'operator' role, optionally answering a needs_operator ask). Polls /api/dashboard/state every 4 s and
   renders with DOM nodes + textContent only: message content never goes
   through innerHTML. Design: Claude Design canvas "Relay dashboard
   redesign", brief of 2026-09-25. */
(function () {
  'use strict';

  const POLL_MS = 4000;
  const BACKOFF = [4000, 8000, 16000, 30000];
  const NEW_MS = 30000;
  const FEED_MAX = 80;
  // Ids are assigned at INSERT but visible at COMMIT, so a lower id can
  // appear after a higher one. Each poll re-asks for this many ids below
  // the highest seen; ingest skips ones it already has.
  const ID_OVERLAP = 20;
  const FOLD_OVER = 10;   // threads longer than this fold their middle
  const FOLD_TAIL = 6;    // messages shown in full at the end
  // The viewer's own timezone, not a hardcoded one -- this is a portable
  // public release, not a single operator's fixed location.
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const STAGES = ['a', 'r', 'n', 'p'];
  const STAGE_LABEL = { a: 'Acked', r: 'Read', n: 'Nudged', p: 'Pending' };
  const STAGE_HINT = {
    a: 'thumbs-up, nothing to add', r: 'read it',
    n: 'told it has mail, not read yet', p: 'not notified yet',
  };

  // Signals. Real bodies open with "sender -> recipients:" and often a
  // "re 633/634." before the point, so HOLD / ALL CLEAR / HOLD LIFTED are
  // matched at the start of any sentence, not only the start of the body.
  // "acknowledging the HOLD in 636" is mid-sentence, so not a hold.
  // \s* after a newline used to be unbounded, which is O(n) work at each
  // of O(n) starting positions for a body of many blank lines -- capped to
  // spaces/tabs only, bounded, so a huge run of newlines can't blow this
  // up (a body of many further newlines just restarts the match at the
  // next \n instead, same net result).
  const SENT = '(?:^|[.!?][ \\t]{1,40}|\\n[ \\t]{0,40})';
  const HOLD_RE = new RegExp(SENT + '(?:Please\\s+)?(HOLD)\\b(?!\\s+LIFTED)');
  const CLEAR_RE = new RegExp(SENT + '(ALL CLEAR|HOLD LIFTED)\\b');
  // "Needs you" shows only what a session explicitly flagged with
  // needs_operator {kind, why} (design decision, 2026-09-25: "No guessing").
  const SEC_RE = /^security finding:\s*/i;
  const ASK_LABEL = { decision: 'Decision', approval: 'Approval', security: 'Security', bug: 'Bug', other: 'Needs you' };
  const INL = new RegExp('(' + [
    '`[^`\\n]+`',
    '\\bPR #\\d+',
    '(?<![\\w/#])#\\d+\\b', '\\b[Tt]hread #?\\d+\\b',
    '\\b(?=[0-9a-f]*\\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}\\b',
    '\\bALL CLEAR\\b', '\\bHOLD LIFTED\\b', '\\bHOLD\\b',
  ].join('|') + ')', 'g');

  // ---------- state ----------
  const S = {
    roles: {}, groups: {}, threads: {}, msgs: new Map(), byThread: {}, dl: {}, asks: {}, msgCounts: {},
    lastId: 0, serverTime: null, loaded: false, rolesKey: '', seenKey: '', threadsKey: '',
  };
  const UI = {
    status: 'active', q: '', sec: false, unread: false, role: '', feedMode: 'said',
    tid: null, focus: null, folds: {}, dlOpen: {}, newAt: {}, pop: false,
    listDirty: false, readerDirty: false, readerNew: [], feedNew: 0,
    drafts: {}, answering: {}, composerTid: null, posting: false, postNote: null,
    ntOpen: false, nt: null,
  };
  const CONN = { state: 'init', fails: 0, lastOk: null, lastData: null, nextAt: null, attempt: 0, timer: null };
  let D = { threads: {}, attn: [], resolved: [], unread: {}, activeN: 0 };

  // ---------- helpers ----------
  const $ = (id) => document.getElementById(id);

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    if (props) {
      for (const k in props) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const c of kids.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  // Constant, trusted SVG markup only.
  const ICONS = {
    pause: '<svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="4" width="5" height="16" rx="1"/><rect x="14" y="4" width="5" height="16" rx="1"/></svg>',
    q: '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.5v.7M12 17h.01"/></svg>',
    shield: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>',
    check: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    checkOk: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    checkCircle: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M7 12.5l3.5 3.5L17 9"/></svg>',
    link: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
    down: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg>',
    up: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
    chevDown: '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>',
    chevUp: '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg>',
    x: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  };
  function icon(name) {
    const t = document.createElement('template');
    t.innerHTML = ICONS[name];
    return t.content.firstChild;
  }
  const pip = (s) => h('span', { class: 'rl-pip rl-pip-' + s });
  const askTag = (kind) => h('span', { class: 'rl-tag rl-tag-ask' + (kind === 'security' ? ' rl-tag-ask-sec' : '') },
    icon(kind === 'security' ? 'shield' : 'q'), (kind === 'other' ? 'Needs you' : 'Needs you · ' + ASK_LABEL[kind]));

  // ---------- time ----------
  const fHM = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const fExact = new Intl.DateTimeFormat('en-GB', {
    timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'short',
  });
  const fDayKey = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  const fDate = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short' });
  const fTzName = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, timeZoneName: 'short' });

  const dayKey = (d) => fDayKey.format(d);
  const isToday = (d) => dayKey(d) === dayKey(new Date());
  const hm = (d) => fHM.format(d);
  const exact = (d) => fExact.format(d);
  const tzName = (d) => (fTzName.formatToParts(d).find((p) => p.type === 'timeZoneName') || {}).value || '';

  function rel(d) {
    const s = (Date.now() - d) / 1000;
    if (s < 45) return 'just now';
    const m = Math.round(s / 60);
    if (m < 60) return m + ' min ago';
    if (isToday(d)) return Math.floor(m / 60) + ' h ago';
    if (dayKey(d) === dayKey(new Date(Date.now() - 864e5))) return 'yesterday ' + hm(d);
    return Math.max(2, Math.round(s / 86400)) + ' days ago';
  }
  function short(d) {
    const s = (Date.now() - d) / 1000;
    if (s < 60) return 'now';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm';
    const hr = Math.floor(m / 60);
    if (hr < 24) return hr + 'h';
    return Math.floor(hr / 24) + 'd';
  }
  function dur(ms) {
    const m = Math.round(ms / 60000);
    if (m < 60) return m + ' min';
    const hr = Math.floor(m / 60);
    if (hr < 48) return hr + ' h' + (m % 60 && hr < 10 ? ' ' + (m % 60) + ' min' : '');
    return Math.floor(hr / 24) + ' d';
  }
  function when(d) { return isToday(d) ? hm(d) : fDate.format(d) + ' ' + hm(d); }
  // A time element that refreshTimes() keeps current.
  function tspan(d, fmt, cls) {
    return h('span', { class: cls || 'rl-meta', title: exact(d), 'data-ts': d.getTime(), 'data-fmt': fmt },
      fmt === 'short' ? short(d) : rel(d));
  }
  function refreshTimes() {
    document.querySelectorAll('[data-ts]').forEach((el) => {
      const d = new Date(+el.getAttribute('data-ts'));
      el.textContent = el.getAttribute('data-fmt') === 'short' ? short(d) : rel(d);
    });
  }

  // ---------- text ----------
  const flat = (s) => s.replace(/\s+/g, ' ').trim();
  // Markdown -> plain text, for excerpts and HOLD/ALL CLEAR detection.
  function stripMd(s) {
    return s
      .replace(/^```.*$/gm, '')
      .replace(/^\s{0,3}#{1,4}\s+/gm, '')
      .replace(/^\s*>\s?/gm, '')
      .replace(/^\s*([-*+]|\d+[.)])\s+/gm, '')
      .replace(/\[([^\]\n]+)\]\([^)\s]+\)/g, '$1')
      .replace(/\*\*([^*\n]+?)\*\*/g, '$1')
      .replace(/(^|[^\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])/g, '$1$2')
      .replace(/`([^`\n]+)`/g, '$1');
  }
  function knownRole(r) { return !!S.roles[r] || Object.values(S.groups).some((g) => g.includes(r)); }
  // Length of the leading "sender -> recipients:" address, if any.
  function addrLen(body) {
    const m = /^\s*([a-z][\w-]*)(\s*(?:->|→)\s*[^:\n]{1,200})?:\s*/.exec(body);
    return m && knownRole(m[1]) ? m[0].length : 0;
  }
  function kwIndex(re, text) {
    const m = re.exec(text);
    if (!m) return null;
    const kw = m[m.length - 1];
    return { i: m.index + m[0].lastIndexOf(kw), len: kw.length };
  }
  const isStale = (r) => !!(S.roles[r] && S.roles[r].status === 'stale');
  const topicOf = (t) => (D.threads[t.id] && D.threads[t.id].isSec ? t.topic.replace(SEC_RE, '') : t.topic);

  // ---------- derive ----------
  function derive() {
    const today = dayKey(new Date());
    const dt = {};
    const attnAsk = [], resolved = [];
    const answerOf = {};
    for (const mid in S.asks) { const a = S.asks[mid]; if (a.answeredBy) answerOf[a.answeredBy] = +mid; }
    for (const tid in S.threads) {
      const t = S.threads[tid];
      const msgs = (S.byThread[tid] || []).map((id) => S.msgs.get(id));
      const active = t.status === 'active';
      const x = {
        msgs, active, members: S.groups[t.gid] || [], isSec: SEC_RE.test(t.topic),
        last: msgs[msgs.length - 1] || null, openHolds: [], behind: new Set(),
        // Server-reported true total -- may exceed msgs.length if older
        // messages weren't loaded (the 1000-message cap on first load).
        msgCount: S.msgCounts[tid] != null ? S.msgCounts[tid] : msgs.length,
      };
      x.lastAt = x.last ? x.last.at : t.created;
      msgs.forEach((m) => {
        m.clearedBy = null; m.clears = null;
        m.ask = S.asks[m.id] || null;
        m.askOpen = !!(m.ask && !m.ask.answeredBy && !m.ask.cleared);
        m.answers = answerOf[m.id] || null;
        if (m.plain == null) {
          m.alen = addrLen(m.body);
          m.plain = m.body.slice(m.alen);
          const text = stripMd(m.plain);
          m.flat = flat(text);
          m.holdHit = kwIndex(HOLD_RE, text);
          m.clearHit = kwIndex(CLEAR_RE, text);
          m.isHold = !!m.holdHit;
          m.isClear = !!m.clearHit;
        }
      });
      msgs.forEach((m, i) => {
        if (!m.isHold) return;
        const c = msgs.slice(i + 1).find((y) => y.isClear);
        if (c) { m.clearedBy = c; if (!c.clears) c.clears = m; }
      });
      if (active) x.openHolds = msgs.filter((m) => m.isHold && !m.clearedBy);
      msgs.forEach((m) => {
        const dl = S.dl[m.id] || {};
        for (const r in dl) if ((dl[r] === 'p' || dl[r] === 'n') && !isStale(r) && x.members.includes(r)) x.behind.add(r);
        if (m.ask) {
          const ans = m.ask.answeredBy ? S.msgs.get(m.ask.answeredBy) : null;
          const done = ans ? ans.at : m.ask.cleared ? new Date(m.ask.cleared) : null;
          if (done && dayKey(done) === today) resolved.push({ t, m, done, ans });
        }
      });
      x.asks = msgs.filter((m) => m.askOpen);
      // An archived thread is finished; its unanswered asks stay tagged in
      // the thread but don't count as needing the operator.
      if (active) x.asks.forEach((m) => attnAsk.push({ kind: 'ask', t, m }));
      dt[tid] = x;
    }
    attnAsk.sort((a, b) => a.m.id - b.m.id);
    resolved.sort((a, b) => b.done - a.done);
    // Only current group members: /pending can't show mail from a group a
    // role has left, so counting it would be a backlog nobody can clear.
    const unread = {};
    for (const mid in S.dl) {
      const m = S.msgs.get(+mid);
      const x = m && dt[m.tid];
      if (!x) continue;
      const dl = S.dl[mid];
      for (const r in dl) if ((dl[r] === 'p' || dl[r] === 'n') && x.members.includes(r)) unread[r] = (unread[r] || 0) + 1;
    }
    D = {
      threads: dt, attn: attnAsk, resolved, unread,
      activeN: Object.values(S.threads).filter((t) => t.status === 'active').length,
    };
  }

  // ---------- ingest ----------
  function ingest(j) {
    const first = !S.loaded;
    const touched = new Set();
    const fresh = [];

    const roles = {};
    j.roles.forEach((r) => {
      roles[r.role] = {
        role: r.role, session: r.session_name, status: r.status, bound: new Date(r.bound_at),
        seen: r.last_seen_at ? new Date(r.last_seen_at) : null, seenWhat: r.last_seen_action || '',
        actAt: r.last_action_at ? new Date(r.last_action_at) : null,
        hb: r.heartbeat_at ? new Date(r.heartbeat_at) : null, hbInt: r.heartbeat_interval || 20,
      };
    });
    // Last seen changes on every mail-watch poll, so it is kept out of the
    // key that triggers a full re-render and updated in place instead.
    const rolesKey = JSON.stringify(j.roles.map((r) => [r.role, r.session_name, r.status, r.bound_at]));
    const rolesChanged = rolesKey !== S.rolesKey;
    const seenKey = JSON.stringify(j.roles.map((r) => [r.role, r.last_seen_at, r.last_seen_action, r.heartbeat_at, r.heartbeat_interval]));
    const seenChanged = seenKey !== S.seenKey;
    S.roles = roles; S.rolesKey = rolesKey; S.seenKey = seenKey;
    S.groups = j.groups;
    // Always sent whole (like threads/groups) -- true totals, even for a
    // thread whose messages are all older than the 1000-message load cap.
    if (j.message_counts) S.msgCounts = j.message_counts;

    const threadsKey = JSON.stringify(j.threads) + JSON.stringify(j.groups);
    const threadsChanged = threadsKey !== S.threadsKey;
    if (threadsChanged) {
      const live = new Set(j.threads.map((t) => t.thread_id));
      // threads (like groups) are always sent whole, so any thread_id we
      // know about that's missing here was hard-deleted (DELETE /groups) --
      // prune it and its messages/asks, or they'd linger in an open
      // dashboard tab forever (state only ever grew otherwise).
      for (const tid in S.threads) {
        if (live.has(+tid)) continue;
        for (const mid of S.byThread[tid] || []) { S.msgs.delete(mid); delete S.asks[mid]; delete S.dl[mid]; }
        delete S.byThread[tid];
        delete S.threads[tid];
        touched.add(tid);
      }
      j.threads.forEach((t) => {
        const old = S.threads[t.thread_id];
        if (old && (old.status !== t.status || old.topic !== t.topic)) touched.add(String(t.thread_id));
        S.threads[t.thread_id] = {
          id: t.thread_id, gid: t.group_id, topic: t.topic, status: t.status, created: new Date(t.created_at),
        };
      });
      S.threadsKey = threadsKey;
    }

    j.messages.forEach((m) => {
      if (S.msgs.has(m.message_id)) return;
      const msg = { id: m.message_id, tid: m.thread_id, from: m.sender_role, body: m.body, at: new Date(m.created_at), plain: null };
      S.msgs.set(msg.id, msg);
      (S.byThread[msg.tid] = S.byThread[msg.tid] || []).push(msg.id);
      if (msg.id > S.lastId) S.lastId = msg.id;
      touched.add(String(msg.tid));
      fresh.push(msg);
      if (!first) UI.newAt[msg.id] = Date.now();
    });
    j.deliveries.forEach(([mid, role, st]) => {
      const dl = (S.dl[mid] = S.dl[mid] || {});
      if (dl[role] !== st) {
        dl[role] = st;
        const m = S.msgs.get(mid);
        if (m) touched.add(String(m.tid));
      }
    });
    // The relay sends every flagged message on a full load, then only open
    // ones and ones answered/cleared since the last poll: merge by id.
    let asksChanged = false;
    (j.attention || []).forEach((a) => {
      const next = { kind: a.attn_kind, why: a.attn_why, answeredBy: a.attn_answered_by, cleared: a.attn_cleared_at };
      if (JSON.stringify(S.asks[a.message_id]) === JSON.stringify(next)) return;
      S.asks[a.message_id] = next;
      asksChanged = true;
      const m = S.msgs.get(a.message_id);
      if (m) touched.add(String(m.tid));
    });
    for (const tid in S.byThread) S.byThread[tid].sort((a, b) => a - b);
    S.serverTime = j.server_time;
    S.loaded = true;

    if (!first && !touched.size && !rolesChanged && !threadsChanged && !asksChanged) {
      if (seenChanged) updateSeen();
      return;
    }
    derive();
    if (first) {
      initialRoute();
      renderAll();
      return;
    }
    renderAttn();
    renderList(false);
    renderRoles();
    renderFeed(fresh.length);
    if (UI.tid != null && touched.has(String(UI.tid))) {
      updateReader(fresh.filter((m) => String(m.tid) === String(UI.tid)));
    }
  }

  // ---------- polling ----------
  function schedule(ms) {
    clearTimeout(CONN.timer);
    CONN.nextAt = Date.now() + ms;
    CONN.timer = setTimeout(poll, ms);
  }
  async function poll() {
    clearTimeout(CONN.timer);
    if (CONN.state === 'lost') { CONN.state = 'reco'; CONN.attempt++; renderTop(); }
    const qs = S.loaded ? '?since_id=' + Math.max(0, S.lastId - ID_OVERLAP) + '&since_ts=' + encodeURIComponent(S.serverTime) : '';
    const ctl = new AbortController();
    const kill = setTimeout(() => ctl.abort(), 8000);
    let j;
    try {
      const r = await fetch('/api/dashboard/state' + qs, { cache: 'no-store', signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      j = await r.json();
    } catch (e) {
      // Only an actual network/HTTP failure counts as connection loss --
      // an exception below, in ingest/render, is a client bug and
      // shouldn't be indistinguishable from "Connection lost" forever.
      clearTimeout(kill);
      CONN.fails++;
      if (CONN.fails >= 2) CONN.state = 'lost';
      schedule(BACKOFF[Math.min(CONN.fails - 1, BACKOFF.length - 1)]);
      renderTop();
      return;
    }
    clearTimeout(kill);
    const wasDown = CONN.state === 'reco' || CONN.state === 'lost';
    CONN.fails = 0; CONN.attempt = 0; CONN.state = 'live'; CONN.lastOk = Date.now();
    try {
      ingest(j);
    } catch (e) {
      console.error('dashboard: ingest failed (data was fetched fine)', e);
    }
    if (wasDown) renderTop();
    schedule(POLL_MS);
    renderTop();
  }

  // ---------- top bar ----------
  // Rebuilt only when the state changes (so "Retry now" survives the 1 s
  // tick); in between, just the detail text is refreshed.
  let topState = null;
  function renderTop() {
    const conn = $('conn');
    const st = CONN.state === 'init' ? 'live' : CONN.state;
    const shown = CONN.lastOk ? hm(new Date(CONN.lastOk)) : '—';
    let detail;
    if (st === 'live') {
      const since = CONN.lastOk ? Math.max(0, Math.round((Date.now() - CONN.lastOk) / 1000)) : null;
      detail = (since != null ? 'updated ' + since + ' s ago · ' : '') + 'polls every 4 s';
      conn.title = CONN.lastOk ? 'Last successful poll ' + exact(new Date(CONN.lastOk)) : '';
    } else if (st === 'lost') {
      const next = Math.max(0, Math.ceil((CONN.nextAt - Date.now()) / 1000));
      const ago = CONN.lastOk ? ' (' + rel(new Date(CONN.lastOk)) + ')' : '';
      detail = 'Showing data from ' + shown + ago + ' · ' + CONN.fails + ' polls failed · next retry in ' + next + ' s';
      conn.title = '';
    } else {
      detail = 'attempt ' + CONN.attempt + ' · showing data from ' + shown;
    }
    const key = st + (st === 'live' && !CONN.lastOk ? '0' : '');
    if (key !== topState) {
      topState = key;
      $('top').classList.toggle('top-lost', st === 'lost');
      conn.replaceChildren();
      if (st === 'live') {
        conn.append(h('span', { class: 'rl-dot rl-dot-live' }),
          h('span', { class: 'conn-strong', text: CONN.lastOk ? 'Live' : 'Connecting…' }),
          h('span', { class: 'rl-meta', id: 'conn-detail' }));
      } else if (st === 'lost') {
        conn.append(h('span', { class: 'rl-dot rl-dot-lost' }),
          h('span', { class: 'conn-lost-t', text: 'Connection lost' }),
          h('span', { class: 'conn-lost-m', id: 'conn-detail' }),
          h('button', { class: 'rl-btn', onclick: () => poll() }, 'Retry now'));
      } else {
        conn.append(h('span', { class: 'rl-dot rl-dot-reco' }),
          h('span', { class: 'conn-strong', text: 'Reconnecting…' }),
          h('span', { class: 'rl-meta', id: 'conn-detail' }));
      }
    }
    $('conn-detail').textContent = detail;
  }
  function renderClock() {
    const d = new Date();
    const clock = $('clock');
    clock.textContent = fDate.format(d) + ' · ' + hm(d) + ' ' + tzName(d);
    clock.title = 'All times ' + TZ;
  }

  // ---------- needs-you strip ----------
  function attnCard(a) {
    const { t, m } = a;
    return h('button', {
      class: 'rl-card rl-card-ask' + (m.ask.kind === 'security' ? ' rl-card-ask-sec' : ''),
      title: exact(m.at), onclick: () => openThread(t.id, m.id),
    },
    h('div', { class: 'card-top' }, askTag(m.ask.kind), h('span', { class: 'card-id', text: '#' + t.id }),
      h('span', { class: 'rl-meta ellipsis' }, m.from + ' · ', tspan(m.at, 'rel', ''))),
    h('div', { class: 'rl-clamp2 card-ex card-why', text: m.ask.why }));
  }
  function renderAttn() {
    const el = $('attn');
    el.replaceChildren();
    const n = D.attn.length;
    document.title = n ? '(' + n + ') Agora' : 'Agora';
    const res = h('div', { class: 'attn-res' },
      h('div', { class: 'rl-label', style: 'padding-left:8px', text: 'Answered today' + (D.resolved.length > 1 ? ' · ' + D.resolved.length : '') }),
      h('div', { class: 'attn-res-list' },
        D.resolved.length ? D.resolved.map((r) => h('button', { class: 'rl-res', title: r.m.ask.why, onclick: () => openThread(r.t.id, r.ans ? r.ans.id : r.m.id) },
          h('span', { class: 'res-line' }, icon('checkOk'), h('span', { class: 'res-strike', text: ASK_LABEL[r.m.ask.kind] }),
            h('span', { text: (r.ans ? 'answered' : 'handled') + ' in #' + r.t.id })),
          h('span', { class: 'rl-meta', style: 'padding-left:19px',
            text: hm(r.m.at) + ' → ' + hm(r.done) + ' · ' + dur(r.done - r.m.at) + (r.ans ? ' · #' + r.ans.id : ' · no reply') })))
          : h('div', { class: 'rl-meta', style: 'padding:6px 8px', text: 'Nothing yet today' })));
    if (!n) {
      el.append(h('div', { class: 'attn-clear' }, icon('checkCircle'),
        h('div', { style: 'display:flex;flex-direction:column;gap:3px;min-width:0' },
          h('div', { class: 'attn-clear-h', text: 'Nothing needs you right now' }),
          h('div', { class: 'rl-meta', style: 'font-size:12.5px',
            text: 'No session has flagged anything for you · checked ' + D.activeN + ' active threads at ' + hm(new Date()) }))), res);
      return;
    }
    const byKind = {};
    D.attn.forEach((a) => { byKind[a.m.ask.kind] = (byKind[a.m.ask.kind] || 0) + 1; });
    const bits = Object.keys(ASK_LABEL).filter((k) => byKind[k]).map((k) => byKind[k] + ' ' + ASK_LABEL[k].toLowerCase());
    const cards = h('div', { class: 'attn-cards' }, D.attn.slice(0, 3).map(attnCard));
    if (n > 3) {
      cards.append(h('button', { class: 'rl-btn attn-more', 'aria-expanded': UI.pop ? 'true' : 'false',
        onclick: (e) => { e.stopPropagation(); UI.pop = !UI.pop; renderAttn(); } }, '+' + (n - 3) + ' more'));
    }
    el.append(h('div', { class: 'attn-sum' },
      h('div', { class: 'rl-label', style: 'color:var(--attn-ink)', text: 'Needs you' }),
      h('div', { class: 'attn-count', text: String(n) }),
      h('div', { class: 'attn-break', text: bits.join(' · ') })), cards, res);
    if (UI.pop && n > 3) {
      el.append(h('div', { class: 'attn-pop rl-scroll', role: 'dialog', 'aria-label': 'Everything that needs you' },
        D.attn.map((a) => {
          return h('button', { class: 'pop-row', onclick: () => { UI.pop = false; openThread(a.t.id, a.m.id); } },
            askTag(a.m.ask.kind), h('span', { class: 'card-id', text: '#' + a.t.id }),
            h('span', { class: 'rl-ex', text: a.m.ask.why }));
        })));
    }
  }

  // ---------- thread list ----------
  function listHot() {
    const nav = document.querySelector('.list-pane');
    return nav.matches(':hover') || nav.contains(document.activeElement);
  }
  function filteredThreads() {
    const ql = UI.q.trim().toLowerCase().replace(/^#/, '');
    const all = Object.values(S.threads);
    const base = all.filter((t) => {
      const x = D.threads[t.id];
      if (ql && !(String(t.id) === ql || t.topic.toLowerCase().includes(ql) || x.members.some((r) => r.includes(ql)))) return false;
      if (UI.sec && !x.isSec) return false;
      if (UI.unread && !x.behind.size) return false;
      if (UI.role && !x.members.includes(UI.role)) return false;
      return true;
    });
    const counts = {
      active: base.filter((t) => t.status === 'active').length,
      archived: base.filter((t) => t.status === 'archived').length,
      all: base.length,
    };
    const list = base.filter((t) => UI.status === 'all' || t.status === UI.status)
      .sort((a, b) => D.threads[b.id].lastAt - D.threads[a.id].lastAt);
    return { list, counts };
  }
  function renderList(force) {
    if (!force && listHot()) { UI.listDirty = true; return; }
    UI.listDirty = false;
    const { list, counts } = filteredThreads();
    const seg = $('status-seg');
    seg.replaceChildren(...['active', 'archived', 'all'].map((k) => h('button', {
      class: 'rl-segb' + (UI.status === k ? ' rl-segb-on' : ''), 'aria-pressed': UI.status === k ? 'true' : 'false',
      onclick: () => { UI.status = k; renderList(true); },
    }, k[0].toUpperCase() + k.slice(1) + ' ' + counts[k])));

    const roles = Object.keys(S.roles).sort();
    const sel = h('select', { class: 'rl-select', 'aria-label': 'Filter by member role',
      onchange: (e) => setRole(e.target.value) },
      h('option', { value: '', text: 'any' }),
      roles.map((r) => h('option', { value: r, text: r })));
    sel.value = UI.role;
    $('chips').replaceChildren(
      h('button', { class: 'rl-chip' + (UI.sec ? ' rl-chip-on' : ''), 'aria-pressed': UI.sec ? 'true' : 'false',
        onclick: () => { UI.sec = !UI.sec; renderList(true); } }, icon('shield'), 'Security'),
      h('button', { class: 'rl-chip' + (UI.unread ? ' rl-chip-on' : ''), 'aria-pressed': UI.unread ? 'true' : 'false',
        onclick: () => { UI.unread = !UI.unread; renderList(true); } }, pip('n'), 'Not yet read'),
      h('label', { class: 'rl-chip' + (UI.role ? ' rl-chip-on' : ''), style: 'padding-right:4px' }, 'Role', sel));

    const filters = [];
    if (UI.q.trim()) filters.push('“' + UI.q.trim() + '”');
    if (UI.sec) filters.push('security');
    if (UI.unread) filters.push('not yet read');
    if (UI.role) filters.push(UI.role);
    $('list-count').textContent = list.length + ' shown' + (filters.length ? ' · ' + filters.join(', ') : '');
    $('list-foot').textContent = 'Sorted by last activity · ' +
      (UI.status === 'active' ? 'archived hidden' : UI.status === 'archived' ? 'archived only' : 'active and archived');

    const totalRoles = Object.keys(S.roles).length;
    const box = $('list');
    const top = box.scrollTop;
    box.replaceChildren();
    if (!list.length) {
      box.append(h('div', { class: 'list-empty' }, h('div', { text: 'No threads match these filters.' }),
        h('button', { class: 'rl-btn', onclick: clearFilters }, 'Clear filters')));
      return;
    }
    list.forEach((t) => {
      const x = D.threads[t.id];
      const selected = String(t.id) === String(UI.tid);
      const n = x.msgCount;
      const tags = [];
      if (x.openHolds.length) tags.push(h('span', { class: 'rl-tag rl-tag-hold' }, icon('pause'), 'Hold'));
      if (x.asks.length) tags.push(h('span', { class: 'rl-tag rl-tag-ask' }, 'Asks you' + (x.asks.length > 1 ? ' · ' + x.asks.length : '')));
      if (x.isSec) tags.push(x.active
        ? h('span', { class: 'rl-tag rl-tag-sec' }, icon('shield'), 'Finding · open')
        : h('span', { class: 'rl-tag rl-tag-muted' }, icon('shield'), 'Finding · closed'));
      if (!x.active && !x.isSec) tags.push(h('span', { class: 'rl-tag rl-tag-muted', text: 'Archived' }));
      const who = x.members.length >= totalRoles && totalRoles > 2 ? 'all roles' : x.members.length + ' roles';
      box.append(h('button', {
        class: 'rl-trow' + (selected ? ' rl-trow-sel' : '') + (x.active ? '' : ' rl-trow-arch'),
        'aria-current': selected ? 'page' : null, onclick: () => openThread(t.id, null),
      },
      h('div', { class: 'trow-1' }, h('span', { class: 'rl-tid', text: '#' + t.id }),
        h('span', { class: 'rl-ttopic', text: topicOf(t) }), tspan(x.lastAt, 'short', 'rl-meta trow-time')),
      h('div', { class: 'trow-2' }, tags, h('span', { class: 'rl-meta', text: who + ' · ' + n + ' msg' + (n === 1 ? '' : 's') }),
        x.behind.size ? h('span', { class: 'behind', title: [...x.behind].sort().join(', ') }, pip('n'),
          x.behind.size + ' role' + (x.behind.size === 1 ? " hasn't" : "s haven't") + ' read') : null)));
    });
    box.scrollTop = top;
  }
  function clearFilters() {
    UI.q = ''; UI.sec = false; UI.unread = false; UI.status = 'active';
    $('q').value = '';
    setRole('');
  }

  // ---------- new thread (the operator starts one from the dashboard) ----------
  // Same two mutating calls the composer already uses (POST as sender_role
  // "operator", X-Relay-Client + X-Operator-Token headers), just chained: find/create
  // a group for the chosen members, then a thread in it, then the first
  // message. No needs_operator checkbox, matching the reply composer.
  function openNewThread() {
    UI.ntOpen = true;
    UI.nt = { members: new Set(), group: null, topic: '', body: '', posting: false, error: null };
    renderNewThread();
  }
  function closeNewThread() {
    UI.ntOpen = false; UI.nt = null;
    renderNewThread();
  }
  function toggleNtMember(role) {
    if (UI.nt.members.has(role)) UI.nt.members.delete(role); else UI.nt.members.add(role);
    renderNewThread();
  }
  async function findOrCreateGroup() {
    if (!UI.nt.members.size) return;
    UI.nt.posting = true; UI.nt.error = null; renderNewThread();
    try {
      const r = await fetch('/groups', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Relay-Client': 'dashboard' },
        body: JSON.stringify({ members: [...UI.nt.members] }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : 'HTTP ' + r.status);
      UI.nt.group = { id: j.group_id, existing: j.existing };
    } catch (e) {
      UI.nt.error = 'Could not find/create group: ' + e.message;
    } finally {
      UI.nt.posting = false; renderNewThread();
    }
  }
  async function postNewThread() {
    const topic = UI.nt.topic.trim();
    const body = UI.nt.body.trim();
    if (!topic || !body || UI.nt.posting) return;
    UI.nt.posting = true; UI.nt.error = null; renderNewThread();
    try {
      // Reusing threadId here is what makes a retry after a failed first
      // message (e.g. missing token) not create a second, empty thread.
      // It's only safe to reuse while group/topic are unchanged, which is
      // why Back and editing the topic both clear it (see renderNewThread).
      // Clearing it that way does leave the original empty thread behind
      // (Back/topic-edit abandon it, they don't delete or archive it) --
      // harmless, and the agoranomos can archive it from the dashboard.
      let threadId = UI.nt.threadId;
      if (!threadId) {
        const tr = await fetch('/groups/' + UI.nt.group.id + '/threads', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Relay-Client': 'dashboard' },
          body: JSON.stringify({ topic }),
        });
        const tj = await tr.json().catch(() => ({}));
        if (!tr.ok) throw new Error(tj.detail ? (typeof tj.detail === 'string' ? tj.detail : JSON.stringify(tj.detail)) : 'HTTP ' + tr.status);
        threadId = tj.thread_id;
        UI.nt.threadId = threadId;
      }
      const mr = await fetch('/threads/' + threadId + '/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Relay-Client': 'dashboard', 'X-Operator-Token': operatorToken() },
        body: JSON.stringify({ sender_role: 'operator', body }),
      });
      const mj = await mr.json().catch(() => ({}));
      const detail = mj.detail ? (typeof mj.detail === 'string' ? mj.detail : JSON.stringify(mj.detail)) : null;
      if (mr.status === 403 && detail && detail.includes('X-Operator-Token')) {
        throw new Error(detail + ' -- use "set token" in a thread\'s reply box, then try again');
      }
      if (!mr.ok) throw new Error(detail || 'HTTP ' + mr.status);
      closeNewThread();
      await poll();
      openThread(threadId, null);
    } catch (e) {
      UI.nt.error = 'Not posted: ' + e.message;
      UI.nt.posting = false; renderNewThread();
    }
  }
  function renderNewThread() {
    const head = $('list-head');
    const old = head.querySelector('.newthread-pop');
    if (old) old.remove();
    $('new-thread-btn').setAttribute('aria-expanded', UI.ntOpen ? 'true' : 'false');
    if (!UI.ntOpen) return;
    const nt = UI.nt;
    const roles = Object.keys(S.roles).sort();
    const pop = h('div', { class: 'newthread-pop', role: 'dialog', 'aria-label': 'Start a new thread' });
    pop.append(h('div', { class: 'nt-h', text: 'New thread' }));
    if (!nt.group) {
      pop.append(
        h('div', { class: 'nt-group-line', text: 'Who should be in this thread?' }),
        h('div', { class: 'nt-members' }, roles.map((r) => h('label', { class: 'nt-member' },
          h('input', { type: 'checkbox', checked: nt.members.has(r) ? '' : null,
            onchange: () => toggleNtMember(r) }),
          h('span', { text: r })))),
        nt.error ? h('div', { class: 'composer-err', text: nt.error }) : null,
        h('div', { class: 'nt-row' },
          h('button', { class: 'rl-btn', onclick: closeNewThread }, 'Cancel'),
          h('button', { class: 'rl-btn rl-btn-primary', disabled: !nt.members.size || nt.posting, onclick: findOrCreateGroup },
            nt.posting ? 'Finding…' : 'Find/create group')));
    } else {
      pop.append(
        h('div', { class: 'nt-group-line',
          text: 'Group ' + nt.group.id + ' (' + (nt.group.existing ? 'existing' : 'new') + ') -- ' + [...nt.members].sort().join(', ') }),
        h('input', { class: 'nt-input', placeholder: 'Topic', value: nt.topic,
          oninput: (e) => { nt.topic = e.target.value; nt.threadId = null; } }),
        h('textarea', { class: 'nt-ta', placeholder: 'First message (posts as operator)', 'aria-label': 'First message',
          oninput: (e) => { nt.body = e.target.value; } }, nt.body),
        nt.error ? h('div', { class: 'composer-err', text: nt.error }) : null,
        h('div', { class: 'nt-row' },
          h('button', { class: 'rl-btn', onclick: () => { nt.group = null; nt.threadId = null; nt.error = null; renderNewThread(); } }, 'Back'),
          h('button', { class: 'rl-btn rl-btn-primary', disabled: nt.posting, onclick: postNewThread },
            nt.posting ? 'Posting…' : 'Post')));
    }
    head.append(pop);
  }

  // ---------- reader ----------
  function inline(parent, text, m) {
    text.split(INL).forEach((x, i) => {
      if (!x) return;
      if (i % 2 === 0) { parent.append(x); return; }
      if (x[0] === '`') { parent.append(h('code', { class: 'rl-code', text: x.slice(1, -1) })); return; }
      if (/^PR #\d+$/.test(x)) { parent.append(h('span', { class: 'rl-ref', text: x })); return; }
      if (/^#\d+$/.test(x)) {
        // Sessions write #N for message numbers (message ids are global).
        const target = S.msgs.get(+x.slice(1));
        if (target && target.id !== m.id) {
          const tt = S.threads[target.tid];
          parent.append(h('button', { class: 'rl-tref', text: x,
            title: 'Message ' + x + ' from ' + target.from + (tt ? ' in thread #' + tt.id + ': ' + tt.topic : ''),
            onclick: () => openThread(target.tid, target.id) }));
        } else parent.append(h('span', { class: 'rl-ref', text: x }));
        return;
      }
      if (/^[Tt]hread #?\d+$/.test(x)) {
        const id = x.match(/\d+/)[0];
        if (S.threads[id] && String(id) !== String(UI.tid)) {
          parent.append(h('button', { class: 'rl-tref', text: x, title: S.threads[id].topic, onclick: () => openThread(+id, null) }));
        } else parent.append(x);
        return;
      }
      if (/^[0-9a-f]{7,12}$/.test(x)) { parent.append(h('span', { class: 'rl-sha', text: x })); return; }
      if (x === 'HOLD') { parent.append(m.isHold ? h('span', { class: 'rl-kw', text: x }) : x); return; }
      parent.append(m.isClear ? h('span', { class: 'rl-kw', text: x }) : x);
    });
  }
  // ---------- Markdown (sessions post Markdown with links; design decision, 2026-09-26) ----------
  // Built from DOM nodes only; message text never goes through innerHTML, so
  // raw HTML in a body shows as text. Links: http(s) or site-relative only.
  // Site-relative paths may not start // or /\ (browsers treat both as
  // another host). Tested on the raw string, so encoded or padded schemes
  // (jav&#x61;script:, \tjavascript:) never match and stay plain text.
  const SAFE_URL = /^(https?:\/\/|\/(?![\/\\]))/i;
  const MD_INL = /(`[^`\n]+`)|\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^*\n]+?)\*\*|(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])|(https?:\/\/[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'"])/g;
  const mdLink = (url, text) => h('a', { class: 'rl-link', href: url, target: '_blank', rel: 'noopener noreferrer', title: url, text });
  function mdInline(parent, text, m) {
    const re = new RegExp(MD_INL.source, 'g');
    let last = 0, r;
    while ((r = re.exec(text))) {
      if (r.index > last) inline(parent, text.slice(last, r.index), m);
      if (r[1]) parent.append(h('code', { class: 'rl-code', text: r[1].slice(1, -1) }));
      else if (r[2]) { if (SAFE_URL.test(r[3])) parent.append(mdLink(r[3], r[2])); else parent.append(r[0]); }
      else if (r[4]) { const b = h('strong'); mdInline(b, r[4], m); parent.append(b); }
      else if (r[5]) { const e = h('em'); mdInline(e, r[5], m); parent.append(e); }
      else if (r[6]) parent.append(mdLink(r[6], r[6]));
      last = re.lastIndex;
    }
    if (last < text.length) inline(parent, text.slice(last), m);
  }
  const LIST_RE = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
  function renderBody(m) {
    const wrap = h('div', { class: 'rl-body rl-md' });
    const addr = m.alen ? m.body.slice(0, m.alen) : '';
    let addrUsed = !addr;
    const lines = m.body.slice(m.alen).split('\n');
    const para = [];
    const flushPara = () => {
      if (!para.length) return;
      const text = para.join('\n');
      para.length = 0;
      if (/^\$ /.test(text)) { wrap.append(h('pre', { class: 'rl-pre', text })); return; }
      const p = h('div', { class: 'rl-para' });
      if (!addrUsed) { p.append(h('span', { class: 'rl-addr', text: addr })); addrUsed = true; }
      mdInline(p, text, m);
      wrap.append(p);
    };
    for (let i = 0; i < lines.length;) {
      const ln = lines[i];
      if (/^\s*```/.test(ln)) {
        flushPara();
        const buf = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) buf.push(lines[i]);
        i++;
        wrap.append(h('pre', { class: 'rl-pre', text: buf.join('\n') }));
        continue;
      }
      if (!ln.trim()) { flushPara(); i++; continue; }
      const hd = /^\s{0,3}(#{1,4})\s+(.+)$/.exec(ln);
      if (hd) {
        flushPara();
        const el = h('div', { class: 'rl-mdh rl-mdh' + hd[1].length, role: 'heading', 'aria-level': String(hd[1].length + 2) });
        mdInline(el, hd[2], m);
        wrap.append(el);
        i++;
        continue;
      }
      if (!para.length && LIST_RE.test(ln)) {
        const ordered = /^\s*\d/.test(ln);
        const list = h(ordered ? 'ol' : 'ul', { class: 'rl-list' });
        for (; i < lines.length && LIST_RE.test(lines[i]); i++) {
          const li = h('li');
          mdInline(li, LIST_RE.exec(lines[i])[2], m);
          list.append(li);
        }
        wrap.append(list);
        continue;
      }
      if (!para.length && /^\s*>/.test(ln)) {
        const q = [];
        for (; i < lines.length && /^\s*>/.test(lines[i]); i++) q.push(lines[i].replace(/^\s*>\s?/, ''));
        const bq = h('blockquote', { class: 'rl-quote' });
        mdInline(bq, q.join('\n'), m);
        wrap.append(bq);
        continue;
      }
      para.push(ln);
      i++;
    }
    flushPara();
    if (!addrUsed) wrap.prepend(h('div', { class: 'rl-para' }, h('span', { class: 'rl-addr', text: addr })));
    return wrap;
  }
  function delivery(m) {
    const dl = S.dl[m.id] || {};
    const by = { a: [], r: [], n: [], p: [] };
    Object.keys(dl).sort().forEach((r) => by[dl[r]].push(r));
    const total = Object.keys(dl).length;
    const read = by.a.length + by.r.length;
    const notYet = by.n.length + by.p.length;
    const text = total ? read + '/' + total + ' read' + (notYet ? ' · ' + notYet + ' not yet' : '') : 'no recipients';
    const pips = total <= 16 ? STAGES.flatMap((s) => by[s].map(() => pip(s))) : [];
    return { by, total, text, pips };
  }
  function msgArticle(t, x, m) {
    const d = delivery(m);
    const open = !!UI.dlOpen[m.id];
    const isNew = UI.newAt[m.id] && Date.now() - UI.newAt[m.id] < NEW_MS;
    const holdOpen = m.isHold && !m.clearedBy && x.active;
    const tags = [];
    if (holdOpen) tags.push(h('span', { class: 'rl-tag rl-tag-hold' }, icon('pause'), 'Open hold'));
    if (m.isHold && m.clearedBy) tags.push(h('span', { class: 'rl-tag rl-tag-ok', title: exact(m.clearedBy.at) }, icon('check'),
      'Hold cleared ' + hm(m.clearedBy.at) + ' by #' + m.clearedBy.id + ' · ' + dur(m.clearedBy.at - m.at)));
    if (m.isClear && m.clears) tags.push(h('span', { class: 'rl-tag rl-tag-ok', text: stripMd(m.plain).match(CLEAR_RE)[1] + ' · lifts hold #' + m.clears.id }));
    if (m.askOpen) tags.push(askTag(m.ask.kind));
    if (m.ask && m.ask.answeredBy) tags.push(h('span', { class: 'rl-tag rl-tag-ok' }, icon('check'), 'Answered by #' + m.ask.answeredBy));
    if (m.ask && !m.ask.answeredBy && m.ask.cleared) tags.push(h('span', { class: 'rl-tag rl-tag-ok' }, icon('check'), 'Handled'));
    if (m.answers) tags.push(h('span', { class: 'rl-tag rl-tag-act', text: 'Answers #' + m.answers }));
    if (isNew) tags.push(h('span', { class: 'rl-tag rl-tag-new', text: 'New' }));
    const cls = 'rl-msg' + (holdOpen || m.askOpen ? ' rl-msg-hold' : '') + (m.from === 'operator' ? ' rl-msg-operator' : '') + (isNew ? ' rl-msg-new' : '') +
      (String(UI.focus) === String(m.id) ? ' rl-msg-focus' : '');
    const art = h('article', { id: 'm' + m.id, class: cls, 'data-mid': m.id },
      h('div', { class: 'rl-mhead' },
        h('span', { class: 'rl-sender', text: m.from }),
        h('span', { class: 'rl-mono rl-meta', text: '#' + m.id }),
        tspan(m.at, 'rel'),
        tags,
        h('div', { class: 'grow' }),
        h('button', { class: 'rl-dl', 'aria-expanded': open ? 'true' : 'false', 'aria-label': 'Delivery: ' + d.text,
          onclick: () => { UI.dlOpen[m.id] = !open; rerenderReader('keep'); } },
          d.pips.length ? h('span', { class: 'rl-pips' }, d.pips) : null, h('span', { text: d.text }), icon(open ? 'chevUp' : 'chevDown'))),
      m.ask ? h('div', { class: 'rl-ask' + (m.askOpen ? '' : ' rl-ask-done') },
        h('span', { class: 'rl-label', style: 'color:inherit', text: 'Asks you' }),
        h('span', { class: 'rl-ask-why', text: m.ask.why }),
        m.askOpen && x.active ? h('button', { class: 'rl-btn rl-btn-primary', onclick: () => startAnswer(m) }, 'Answer') : null,
        m.askOpen ? h('button', { class: 'rl-btn', onclick: (e) => clearAsk(m, e.currentTarget) }, 'Mark handled') : null) : null,
      renderBody(m));
    if (open) {
      art.append(h('div', { class: 'rl-dldetail' }, STAGES.map((s) => h('div', { class: 'dl-col' },
        h('div', { class: 'dl-h' }, pip(s), h('span', { text: STAGE_LABEL[s] }), h('span', { class: 'rl-mono rl-meta', text: String(d.by[s].length) })),
        h('div', { class: 'rl-meta dl-hint', text: STAGE_HINT[s] }),
        d.by[s].length ? d.by[s].map((r) => h('span', { class: 'rl-nm' + (isStale(r) ? ' rl-nm-stale' : ''), text: r + (isStale(r) ? ' · stale' : '') }))
          : h('span', { class: 'rl-nm rl-nm-stale', text: '—' })))));
    }
    return art;
  }
  function readerView() {
    const main = $('reader');
    let view = $('reader-view');
    if (!view) {
      view = h('div', { id: 'reader-view', class: 'reader-view' });
      main.replaceChildren(view, h('div', { id: 'composer', class: 'composer' }));
    }
    return view;
  }
  function renderReader(scroll) {
    const main = readerView();
    main.replaceChildren();
    if (String(UI.composerTid) !== String(UI.tid)) renderComposer();
    UI.readerDirty = false;
    const t = UI.tid != null ? S.threads[UI.tid] : null;
    if (!t) {
      main.append(h('div', { class: 'reader-empty', text: UI.tid != null ? 'Thread #' + UI.tid + ' not found.' : 'Pick a thread on the left.' }));
      return;
    }
    const x = D.threads[t.id];
    const totalRoles = Object.keys(S.roles).length;
    const allRoles = x.members.length >= totalRoles && totalRoles > 2;
    const last = x.last;
    const head = h('div', { class: 'rhead' },
      h('div', { class: 'rhead-row' },
        h('span', { class: 'rl-mono rl-meta', text: '/dashboard/threads/' + t.id }),
        h('button', { class: 'rl-btn rl-btn-ghost', 'aria-label': 'Copy thread link', onclick: (e) => copyLink(e.currentTarget) }, icon('link'), h('span', { text: 'Copy link' })),
        h('div', { class: 'grow' }),
        h('button', { class: 'rl-btn', onclick: () => scrollReader('bottom', true) }, icon('down'), 'Latest')),
      h('h1', { class: 'rtitle', text: topicOf(t) }),
      h('div', { class: 'rhead-row' },
        x.active ? h('span', { class: 'rl-tag rl-tag-act', text: 'Active' }) : h('span', { class: 'rl-tag rl-tag-muted', text: 'Archived' }),
        x.isSec ? (x.active
          ? h('span', { class: 'rl-tag rl-tag-sec' }, icon('shield'), 'Security finding · stays open until the owner confirms')
          : h('span', { class: 'rl-tag rl-tag-muted' }, icon('shield'), 'Security finding · closed')) : null,
        h('span', { class: 'rl-meta' }, '#' + t.id + ' · group ' + t.gid + ' · ' + x.msgCount + ' message' + (x.msgCount === 1 ? '' : 's') +
          ' · started ' + when(t.created) + (last ? ' · last post ' : ''), last ? tspan(last.at, 'rel', '') : null)),
      h('div', { class: 'rhead-row', style: 'gap:5px' },
        h('span', { class: 'rl-meta', style: 'margin-right:3px', text: allRoles ? 'Members · all ' + x.members.length + ' roles' : 'Members' }),
        x.members.map((r) => h('span', {
          class: 'rl-mchip' + (isStale(r) ? ' rl-mchip-stale' : '') + (r === UI.role ? ' rl-mchip-sel' : ''),
          title: S.roles[r] ? r + ': ' + (isStale(r) ? 'stale binding (' + S.roles[r].session + ')' : S.roles[r].session) : r + ': never registered',
          text: r,
        }))));

    const tl = h('div', { class: 'timeline rl-scroll', id: 'timeline', onscroll: onReaderScroll });
    const msgs = x.msgs;
    let tailFrom = 0;
    if (msgs.length > FOLD_OVER) {
      tailFrom = msgs.length - FOLD_TAIL;
      const fi = UI.focus != null ? msgs.findIndex((m) => String(m.id) === String(UI.focus)) : -1;
      if (fi > 0 && fi < tailFrom) tailFrom = fi;
    }
    if (tailFrom > 1) {
      tl.append(msgArticle(t, x, msgs[0]));
      const hidden = msgs.slice(1, tailFrom);
      const open = !!UI.folds[t.id];
      const fold = h('div', { class: 'rl-fold' },
        h('button', { class: 'rl-foldbtn', 'aria-expanded': open ? 'true' : 'false',
          onclick: () => { UI.folds[t.id] = !open; rerenderReader('keep'); } },
          icon(open ? 'chevUp' : 'chevDown'),
          h('span', { style: 'font-weight:600;color:var(--text)', text: hidden.length + ' earlier messages' }),
          h('span', { class: 'rl-meta', text: when(hidden[0].at) + ' – ' + when(hidden[hidden.length - 1].at) + (open ? ' · hide' : ' · show') })));
      if (open) {
        fold.append(h('div', { class: 'fold-lines' }, hidden.map((m) => h('button', { class: 'rl-fline',
          onclick: () => { UI.focus = m.id; replaceHash(m.id); rerenderReader('focus'); } },
          h('span', { class: 'rl-mono rl-meta', text: '#' + m.id }),
          h('span', { class: 'rl-sender ellipsis', style: 'font-size:12px;font-weight:600', text: m.from }),
          h('span', { class: 'rl-meta', title: exact(m.at), text: when(m.at) }),
          h('span', { class: 'rl-ex', text: m.flat })))));
      }
      tl.append(fold);
      msgs.slice(tailFrom).forEach((m) => tl.append(msgArticle(t, x, m)));
    } else {
      msgs.forEach((m) => tl.append(msgArticle(t, x, m)));
    }
    if (!msgs.length) tl.append(h('div', { class: 'reader-empty',
      text: x.msgCount ? x.msgCount + ' older message' + (x.msgCount === 1 ? '' : 's') + ' not loaded (outside the 1000-message cap).' : 'No messages yet.' }));
    tl.append(h('div', { class: 'end-note', text: x.active ? 'End of thread · new posts appear here as they arrive' : 'End of thread · archived' }));
    const pill = h('button', { class: 'rl-pill', id: 'reader-pill', hidden: true, onclick: () => scrollReader('bottom', true) });
    main.append(head, tl, pill);
    if (scroll) scrollReader(scroll);
  }
  function rerenderReader(mode) {
    const tl = $('timeline');
    const top = tl ? tl.scrollTop : 0;
    renderReader(null);
    if (mode === 'keep') { const n = $('timeline'); if (n) n.scrollTop = top; } else scrollReader(mode);
  }
  function scrollReader(mode, smooth) {
    const tl = $('timeline');
    if (!tl) return;
    if (mode === 'focus' && UI.focus != null) {
      const el = document.getElementById('m' + UI.focus);
      if (el) { el.scrollIntoView({ block: 'center' }); return; }
    }
    if (mode === 'bottom' || mode === 'focus') {
      tl.scrollTo({ top: tl.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
      UI.readerNew = [];
      const p = $('reader-pill'); if (p) p.hidden = true;
    }
  }
  function nearBottom(tl) { return tl.scrollHeight - tl.scrollTop - tl.clientHeight < 48; }
  function onReaderScroll() {
    const tl = $('timeline');
    if (tl && nearBottom(tl) && UI.readerNew.length) {
      UI.readerNew = [];
      const p = $('reader-pill'); if (p) p.hidden = true;
    }
  }
  function selectionInReader() {
    const sel = window.getSelection();
    const tl = $('timeline');
    return sel && !sel.isCollapsed && tl && tl.contains(sel.anchorNode);
  }
  function updateReader(freshInThread) {
    const tl = $('timeline');
    if (selectionInReader()) { UI.readerDirty = true; UI.readerNew.push(...freshInThread); return; }
    const pinned = !tl || nearBottom(tl);
    const top = tl ? tl.scrollTop : 0;
    renderReader(null);
    const n = $('timeline');
    if (!n) return;
    if (pinned) { n.scrollTop = n.scrollHeight; UI.readerNew = []; return; }
    n.scrollTop = top;
    UI.readerNew.push(...freshInThread);
    if (UI.readerNew.length) {
      const froms = [...new Set(UI.readerNew.map((m) => m.from))];
      const p = $('reader-pill');
      p.replaceChildren(icon('down'), UI.readerNew.length + ' new message' + (UI.readerNew.length > 1 ? 's' : '') +
        ' from ' + (froms.length === 1 ? froms[0] : froms.length + ' roles'));
      p.hidden = false;
    }
  }
  function copyLink(btn) {
    const url = location.origin + '/dashboard/threads/' + UI.tid;
    const done = (ok) => { btn.lastChild.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(() => { btn.lastChild.textContent = 'Copy link'; }, 1500); };
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => done(true), () => done(false));
    else done(false);
  }

  // ---------- composer (the operator's replies) ----------
  function renderComposer() {
    const box = $('composer');
    if (!box) return;
    UI.composerTid = UI.tid;
    box.replaceChildren();
    const t = UI.tid != null ? S.threads[UI.tid] : null;
    if (!t) { box.hidden = true; return; }
    box.hidden = false;
    if (t.status !== 'active') {
      box.append(h('div', { class: 'rl-meta', text: 'Archived thread: it takes no new messages.' }));
      return;
    }
    const ans = UI.answering[t.id] ? S.msgs.get(UI.answering[t.id]) : null;
    if (ans) {
      box.append(h('div', { class: 'composer-ans' },
        h('span', { class: 'rl-meta', text: 'Answering' }),
        h('span', { class: 'card-id', text: '#' + ans.id }),
        h('span', { class: 'rl-meta', text: ans.from + ' · ' }),
        h('span', { class: 'rl-ex', text: ans.ask ? ans.ask.why : ans.flat }),
        h('button', { class: 'rl-btn', style: 'padding:0 7px', 'aria-label': 'Stop answering',
          onclick: () => { delete UI.answering[t.id]; renderComposer(); } }, icon('x'))));
    }
    const ta = h('textarea', { class: 'composer-ta', id: 'composer-ta', rows: '3', 'aria-label': 'Reply to thread #' + t.id + ' as operator',
      placeholder: 'Reply to #' + t.id + ' as operator  (Ctrl+Enter to post)' });
    ta.value = UI.drafts[t.id] || '';
    ta.addEventListener('input', () => { UI.drafts[t.id] = ta.value; });
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); postReply(); } });
    const note = UI.postNote && String(UI.postNote.tid) === String(t.id) ? UI.postNote : null;
    box.append(ta, h('div', { class: 'composer-row' },
      note ? h('span', { class: note.ok ? 'composer-ok' : 'composer-err' }, note.text,
        note.copy && !note.copied ? h('code', { class: 'rl-code composer-copy', text: note.copy }) : null)
        : h('span', { class: 'rl-meta', text: 'Posts as operator. The fan-out note is copied for you to paste into your coordinator role\'s thread.' }),
      h('div', { class: 'grow' }),
      h('button', { class: 'rl-btn', style: 'margin-right:6px', onclick: setOperatorToken, title: 'Set or clear the relay operator-token (kept in this tab only)' }, 'set token'),
      h('button', { class: 'rl-btn rl-btn-primary', id: 'composer-post', onclick: postReply, disabled: UI.posting }, UI.posting ? 'Posting…' : 'Post')));
  }
  function startAnswer(m) {
    UI.answering[m.tid] = m.id;
    UI.postNote = null;
    renderComposer();
    const ta = $('composer-ta');
    if (ta) ta.focus();
  }
  // The relay restricts sender_role="operator" to callers holding this token
  // (security review finding, approved 2026-09-28) once OPERATOR_TOKEN is set on
  // the relay -- kept in sessionStorage only (this tab, this session,
  // never written to disk), never in localStorage or a cookie.
  //
  // Never prompt automatically from inside a post -- window.prompt() opens
  // a native dialog that blocks page JS until answered, which hangs any
  // CDP-driven automation with no dialog handler (ui_qa.mjs included) and
  // is bad UX for a real user besides: nobody wants a surprise dialog on
  // every post before they've configured a token. Only the explicit
  // "set token" button below ever calls it, and posting just uses
  // whatever's already in sessionStorage (or nothing, which is exactly
  // right while the relay's OPERATOR_TOKEN is unset -- soft rollout).
  function operatorToken() {
    try { return sessionStorage.getItem('relay_operator_token') || ''; } catch (e) { return ''; }
  }
  function setOperatorToken() {
    try {
      const t = window.prompt(
        'Agora operator-token (kept only in this browser tab -- never saved to disk). ' +
        'Leave blank to clear it.'
      );
      if (t === null) return;
      sessionStorage.setItem('relay_operator_token', t);
      UI.postNote = null;
      renderComposer();
    } catch (e) { /* sessionStorage unavailable -- nothing to do */ }
  }
  async function postReply() {
    const tid = UI.tid;
    const body = (UI.drafts[tid] || '').trim();
    if (!body || UI.posting) return;
    UI.posting = true; renderComposer();
    const answers = UI.answering[tid] || null;
    try {
      const r = await fetch('/threads/' + tid + '/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Relay-Client': 'dashboard', 'X-Operator-Token': operatorToken() },
        body: JSON.stringify({ sender_role: 'operator', body, answers }),
      });
      const j = await r.json().catch(() => ({}));
      const detail = j.detail ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : null;
      // A 403 can come from either check in the relay's CSRF guard -- the
      // operator-token check, or the Origin/Host check (e.g. the dashboard
      // reached via a host not in RELAY_ALLOWED_HOSTS). Only point at
      // "set token" when the detail actually says it's the token; a
      // Host/Origin mismatch needs a different fix and showing the real
      // server detail is what lets that get diagnosed at all.
      if (r.status === 403 && detail && detail.includes('X-Operator-Token')) {
        throw new Error(detail + ' -- use "set token" below and try again');
      }
      if (!r.ok) throw new Error(detail || 'HTTP ' + r.status);
      const copy = 'operator: posted message ' + j.message_id + ' to thread ' + tid + ' (group ' + j.group_id + ')' +
        (answers ? ', answering #' + answers : '');
      let copied = false;
      try { await navigator.clipboard.writeText(copy); copied = true; } catch (e) { copied = false; }
      delete UI.drafts[tid]; delete UI.answering[tid];
      UI.postNote = { tid, ok: true, copy, copied,
        text: 'Posted #' + j.message_id + '. ' + (copied ? 'Fan-out note copied: paste it into coordinator-claude.' : 'Copy this into coordinator-claude: ') };
    } catch (e) {
      UI.postNote = { tid, ok: false, text: 'Not posted: ' + e.message + '. Your text is kept.' };
    } finally {
      UI.posting = false;
      renderComposer();
      poll();
    }
  }
  async function clearAsk(m, btn) {
    btn.disabled = true;
    try {
      const r = await fetch('/messages/' + m.id + '/attention/clear', { method: 'POST', headers: { 'X-Relay-Client': 'dashboard', 'X-Operator-Token': operatorToken() } });
      if (!r.ok) throw new Error('HTTP ' + r.status);
    } catch (e) { btn.disabled = false; btn.textContent = 'Failed, retry'; return; }
    poll();
  }

  // ---------- feed ----------
  function feedSource() {
    const all = [...S.msgs.values()].sort((a, b) => b.id - a.id);
    if (!UI.role) return all.slice(0, FEED_MAX);
    if (UI.feedMode === 'said') return all.filter((m) => m.from === UI.role && isToday(m.at)).slice(0, FEED_MAX);
    return all.filter((m) => { const st = (S.dl[m.id] || {})[UI.role]; return st === 'p' || st === 'n'; }).slice(0, FEED_MAX);
  }
  function renderFeed(freshCount) {
    $('feed-sub').textContent = UI.role
      ? (UI.feedMode === 'said' ? 'posted by ' + UI.role + ' today' : 'waiting for ' + UI.role + ' to read')
      : 'all threads · newest first';
    const focusBar = $('feed-focus');
    focusBar.replaceChildren();
    focusBar.className = UI.role ? 'feed-focus' : '';
    if (UI.role) {
      focusBar.append(
        h('span', { class: 'rl-mono', style: 'font-weight:700;font-size:12.5px', text: UI.role }),
        h('div', { class: 'rl-seg grow' },
          ['said', 'wait'].map((k) => h('button', { class: 'rl-segb' + (UI.feedMode === k ? ' rl-segb-on' : ''),
            'aria-pressed': UI.feedMode === k ? 'true' : 'false', onclick: () => { UI.feedMode = k; renderFeed(0); } },
          k === 'said' ? 'Posted today' : 'Not yet read'))),
        h('button', { class: 'rl-btn', style: 'padding:0 7px', 'aria-label': 'Clear role focus', onclick: () => setRole('') }, icon('x')));
    }
    const box = $('feed');
    const keep = box.scrollTop > 4;
    const oldH = box.scrollHeight;
    const oldTop = box.scrollTop;
    const rows = feedSource();
    box.replaceChildren();
    if (!rows.length) {
      box.append(h('div', { class: 'feed-empty', text: UI.role
        ? (UI.feedMode === 'said' ? UI.role + ' has not posted today.' : 'Nothing waiting for ' + UI.role + '.')
        : 'No posts yet.' }));
    }
    rows.forEach((m) => {
      const t = S.threads[m.tid];
      const x = t ? D.threads[t.id] : null;
      const d = delivery(m);
      const isNew = UI.newAt[m.id] && Date.now() - UI.newAt[m.id] < NEW_MS;
      box.append(h('button', {
        class: 'rl-frow' + (isNew ? ' rl-frow-new' : '') + (String(UI.focus) === String(m.id) ? ' rl-frow-sel' : ''),
        'data-mid': m.id, title: exact(m.at) + (t ? ' · ' + t.topic : ''), onclick: () => openThread(m.tid, m.id),
      },
      h('div', { class: 'frow-1' },
        h('span', { class: 'rl-sender', style: 'font-size:12px', text: m.from }),
        h('span', { class: 'rl-meta', text: 'in' }),
        h('span', { class: 'frow-tid', text: '#' + m.tid }),
        m.isHold && !m.clearedBy && x && x.active ? h('span', { class: 'rl-tag rl-tag-hold', text: 'Hold' }) : null,
        m.askOpen ? h('span', { class: 'rl-tag rl-tag-ask', text: 'Asks you' }) : null,
        isNew ? h('span', { class: 'rl-tag rl-tag-new', text: 'New' }) : null,
        h('div', { class: 'grow' }),
        h('span', { class: 'rl-pips', title: d.text }, d.pips),
        tspan(m.at, 'short', 'rl-meta frow-time')),
      h('div', { class: 'rl-ex', text: m.flat })));
    });
    const pill = $('feed-pill');
    if (keep && freshCount) {
      box.scrollTop = oldTop + (box.scrollHeight - oldH);
      UI.feedNew += freshCount;
    } else if (keep) {
      box.scrollTop = oldTop;
    } else {
      UI.feedNew = 0;
    }
    pill.hidden = !UI.feedNew;
    if (UI.feedNew) pill.replaceChildren(icon('up'), UI.feedNew + ' new post' + (UI.feedNew > 1 ? 's' : ''));
  }

  // ---------- roles ----------
  // Rebuild the Roles panel only when something it shows (other than last
  // seen / watch state, which updateSeen patches in place) has changed, so
  // routine traffic doesn't drop hover or swallow clicks there.
  let rolesRenderKey = '';
  function renderRoles() {
    const key = JSON.stringify([UI.role, Object.values(S.roles).map((r) => [r.role, r.session, r.status, +r.bound, D.unread[r.role] || 0])]);
    if (key === rolesRenderKey && document.querySelector('#roles [data-role]')) { updateSeen(); return; }
    rolesRenderKey = key;
    const all = Object.values(S.roles).sort((a, b) => a.role.localeCompare(b.role));
    const live = all.filter((r) => r.status !== 'stale');
    const stale = all.filter((r) => r.status === 'stale');
    $('roles-sub').textContent = rolesSub();
    const row = (r) => {
      const u = D.unread[r.role] || 0;
      const sel = UI.role === r.role;
      const reg = isToday(r.bound) ? 'today ' + hm(r.bound) : rel(r.bound).replace(/^yesterday .*/, 'yesterday');
      return h('button', {
        class: 'rl-rrow rl-rgrid' + (r.status === 'stale' ? ' rl-rrow-stale' : '') + (sel ? ' rl-rrow-sel' : ''),
        'aria-pressed': sel ? 'true' : 'false',
        title: roleTitle(r), 'data-role': r.role,
        onclick: () => { UI.feedMode = u ? 'wait' : 'said'; setRole(sel ? '' : r.role); },
      },
      h('span', { class: 'role-name' }, watchPip(r), h('span', { text: r.role })),
      r.seen ? tspan(r.seen, 'short', 'role-last') : h('span', { class: 'role-last', text: 'never' }),
      h('span', { class: 'role-reg', text: reg }),
      h('span', { class: 'rl-count' + (u ? ' rl-count-on' : ''), text: u ? String(u) : '—' }));
    };
    const box = $('roles');
    const top = box.scrollTop;
    box.replaceChildren(...live.map(row));
    if (stale.length) box.append(h('div', { class: 'rl-label stale-label', text: 'Stale bindings' }), ...stale.map(row));
    box.scrollTop = top;
  }
  // Mail-watch state from its heartbeat. 'busy': heartbeats stopped because
  // the watch exited and the session is handling that mail (it has acted
  // since the last heartbeat); 'off': stopped with no sign of life.
  const BUSY_MS = 10 * 60 * 1000;
  function watchState(r) {
    if (!r.hb) return 'none';
    const now = Date.now();
    if (now - r.hb < 3 * r.hbInt * 1000) return 'on';
    if (r.actAt && r.actAt > r.hb && now - r.actAt < BUSY_MS) return 'busy';
    return 'off';
  }
  const WATCH_TEXT = {
    on: (r) => 'Mail watch running · heartbeat ' + rel(r.hb),
    busy: (r) => 'Mail watch paused while the session handles mail · last action ' + rel(r.actAt),
    off: (r) => 'Mail watch STOPPED · last heartbeat ' + rel(r.hb) + ' (' + exact(r.hb) + ')',
    none: () => 'No mail watch has reported for this role',
  };
  function watchPip(r) {
    const st = watchState(r);
    return h('span', { class: 'role-watch role-watch-' + st, role: 'img', 'aria-label': WATCH_TEXT[st](r), title: WATCH_TEXT[st](r) });
  }
  function rolesSub() {
    const all = Object.values(S.roles);
    const live = all.filter((r) => r.status !== 'stale');
    const n = { on: 0, busy: 0, off: 0 };
    live.forEach((r) => { const st = watchState(r); if (st in n) n[st]++; });
    return live.length + ' registered · ' + (n.on + n.busy) + ' watching' +
      (n.off ? ' · ' + n.off + ' watch stopped' : '') + ' · ' + (all.length - live.length) + ' stale';
  }
  function updateSeen() {
    $('roles-sub').textContent = rolesSub();
    document.querySelectorAll('#roles [data-role]').forEach((row) => {
      const r = S.roles[row.getAttribute('data-role')];
      const cell = row.querySelector('.role-last');
      if (!r || !cell) return;
      if (r.seen) {
        cell.setAttribute('data-ts', r.seen.getTime());
        cell.setAttribute('data-fmt', 'short');
        cell.textContent = short(r.seen);
        cell.title = exact(r.seen);
      } else cell.textContent = 'never';
      row.title = roleTitle(r);
      const pipEl = row.querySelector('.role-watch');
      if (pipEl) pipEl.replaceWith(watchPip(r));
    });
  }
  function roleTitle(r) {
    return 'Session ' + (r.session || '—') + ' · registered ' + exact(r.bound) +
      (r.seen ? ' · last seen ' + exact(r.seen) : ' · never seen') +
      (r.actAt ? ' · last action ' + exact(r.actAt) + ' (' + r.seenWhat + ')' : '') +
      ' · ' + WATCH_TEXT[watchState(r)](r) +
      '. Last seen includes mail-watch heartbeats.';
  }
  function setRole(role) {
    UI.role = role;
    if (!role) UI.feedMode = 'said';
    renderList(true); renderFeed(0); renderRoles(); rerenderReader('keep');
  }

  // ---------- routing ----------
  function parseLocation() {
    const pm = /^\/dashboard\/threads\/(\d+)\/?$/.exec(location.pathname);
    const hm_ = /^#m(\d+)$/.exec(location.hash);
    return { tid: pm ? +pm[1] : null, focus: hm_ ? +hm_[1] : null };
  }
  function initialRoute() {
    const loc = parseLocation();
    if (loc.tid != null) { UI.tid = loc.tid; UI.focus = loc.focus; return; }
    if (loc.focus != null && S.msgs.has(loc.focus)) { UI.tid = S.msgs.get(loc.focus).tid; UI.focus = loc.focus; return; }
    const a = D.attn[0];
    if (a) { UI.tid = a.t.id; UI.focus = a.m ? a.m.id : null; return; }
    const recent = Object.values(S.threads).sort((p, q) => D.threads[q.id].lastAt - D.threads[p.id].lastAt)[0];
    UI.tid = recent ? recent.id : null;
  }
  function replaceHash(mid) {
    history.replaceState(null, '', '/dashboard/threads/' + UI.tid + (mid != null ? '#m' + mid : ''));
  }
  function openThread(tid, focus, fromPop) {
    if (String(UI.tid) !== String(tid)) UI.postNote = null;
    UI.tid = tid; UI.focus = focus; UI.readerNew = [];
    if (!fromPop) history.pushState(null, '', '/dashboard/threads/' + tid + (focus != null ? '#m' + focus : ''));
    renderList(true);
    renderFeed(0);
    renderReader(focus != null ? 'focus' : 'bottom');
    if (UI.pop) { UI.pop = false; renderAttn(); }
  }

  function renderAll() {
    renderTop(); renderClock(); renderAttn(); renderList(true); renderRoles(); renderFeed(0);
    renderReader(UI.focus != null ? 'focus' : 'bottom');
  }

  // ---------- events ----------
  function wire() {
    const q = $('q');
    q.addEventListener('input', () => { UI.q = q.value; renderList(true); });
    q.addEventListener('keydown', (e) => { if (e.key === 'Escape') { q.value = ''; UI.q = ''; renderList(true); q.blur(); } });
    document.addEventListener('keydown', (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey) { e.preventDefault(); q.focus(); q.select(); }
      if (e.key === 'Escape' && UI.pop) { UI.pop = false; renderAttn(); }
      if (e.key === 'Escape' && UI.ntOpen) closeNewThread();
    });
    document.addEventListener('click', (e) => {
      if (UI.pop && !e.target.closest('.attn-pop')) { UI.pop = false; renderAttn(); }
      if (UI.ntOpen && !e.target.closest('.newthread-pop') && e.target.id !== 'new-thread-btn') closeNewThread();
    });
    $('new-thread-btn').addEventListener('click', () => { if (UI.ntOpen) closeNewThread(); else openNewThread(); });
    const nav = document.querySelector('.list-pane');
    const flush = () => setTimeout(() => { if (UI.listDirty && !listHot()) renderList(true); }, 0);
    nav.addEventListener('mouseleave', flush);
    nav.addEventListener('focusout', flush);
    document.addEventListener('selectionchange', () => {
      if (UI.readerDirty && !selectionInReader()) { const fresh = UI.readerNew.splice(0); updateReader(fresh); }
    });
    const feed = $('feed');
    feed.addEventListener('scroll', () => {
      if (feed.scrollTop <= 4 && UI.feedNew) { UI.feedNew = 0; $('feed-pill').hidden = true; }
    });
    $('feed-pill').addEventListener('click', () => { feed.scrollTo({ top: 0, behavior: 'smooth' }); UI.feedNew = 0; $('feed-pill').hidden = true; });
    const rail = document.querySelector('.rail');
    rail.dataset.tab = 'live';
    $('rail-tabs').addEventListener('click', (e) => {
      const b = e.target.closest('[data-tab]');
      if (!b) return;
      rail.dataset.tab = b.dataset.tab;
      $('rail-tabs').querySelectorAll('[data-tab]').forEach((x) => {
        const on = x === b;
        x.classList.toggle('rl-segb-on', on); x.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    });
    window.addEventListener('popstate', () => {
      const loc = parseLocation();
      if (loc.tid != null) { openThread(loc.tid, loc.focus, true); return; }
      initialRoute();   // plain /dashboard: same default thread as a fresh load
      if (UI.tid != null) openThread(UI.tid, UI.focus, true);
    });
    setInterval(() => { renderTop(); renderClock(); }, 1000);
    setInterval(() => { refreshTimes(); updateSeen(); }, 30000);
    setInterval(expireNew, 5000);
  }
  // Draggable dividers. Sizes are a per-viewer convenience kept in
  // localStorage; everything works without it.
  const SPLITS = {
    list: { v: '--list-w', def: 340, min: 240, max: 640, axis: 'x', sign: 1 },
    rail: { v: '--rail-w', def: 380, min: 280, max: 720, axis: 'x', sign: -1 },
    roles: { v: '--roles-h', def: 400, min: 120, max: 900, axis: 'y', sign: -1 },
  };
  function loadSizes() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem('relay.sizes') || '{}') || {}; } catch (e) { saved = {}; }
    for (const k in SPLITS) if (typeof saved[k] === 'number') setSize(k, saved[k], false);
  }
  function setSize(k, px, save) {
    const sp = SPLITS[k];
    let v = Math.round(Math.min(sp.max, Math.max(sp.min, px)));
    if (sp.axis === 'x') {
      // keep the reader at least 560px wide
      const other = k === 'list' ? curSize('rail') : curSize('list');
      v = Math.min(v, Math.max(sp.min, window.innerWidth - other - 560 - 12));
    }
    document.documentElement.style.setProperty(sp.v, v + 'px');
    const el = document.querySelector('[data-split="' + k + '"]');
    if (el) el.setAttribute('aria-valuenow', String(v));
    if (save) {
      try {
        const all = JSON.parse(localStorage.getItem('relay.sizes') || '{}') || {};
        all[k] = v;
        localStorage.setItem('relay.sizes', JSON.stringify(all));
      } catch (e) { /* storage unavailable: size just isn't remembered */ }
    }
  }
  function curSize(k) {
    const v = parseInt(getComputedStyle(document.documentElement).getPropertyValue(SPLITS[k].v), 10);
    return isNaN(v) ? SPLITS[k].def : v;
  }
  function wireSplitters() {
    document.querySelectorAll('[data-split]').forEach((el) => {
      const k = el.dataset.split;
      const sp = SPLITS[k];
      el.setAttribute('aria-valuemin', String(sp.min));
      el.setAttribute('aria-valuemax', String(sp.max));
      el.setAttribute('aria-valuenow', String(curSize(k)));
      el.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        el.setPointerCapture(e.pointerId);
        const start = sp.axis === 'x' ? e.clientX : e.clientY;
        const base = curSize(k);
        document.body.classList.add('rl-dragging', sp.axis === 'x' ? 'rl-drag-x' : 'rl-drag-y');
        const move = (ev) => setSize(k, base + sp.sign * ((sp.axis === 'x' ? ev.clientX : ev.clientY) - start), false);
        const up = () => {
          el.removeEventListener('pointermove', move);
          el.removeEventListener('pointerup', up);
          el.removeEventListener('pointercancel', up);
          document.body.classList.remove('rl-dragging', 'rl-drag-x', 'rl-drag-y');
          setSize(k, curSize(k), true);
        };
        el.addEventListener('pointermove', move);
        el.addEventListener('pointerup', up);
        el.addEventListener('pointercancel', up);
      });
      el.addEventListener('dblclick', () => setSize(k, sp.def, true));
      el.addEventListener('keydown', (e) => {
        const grow = sp.axis === 'x' ? { ArrowRight: 1, ArrowLeft: -1 } : { ArrowDown: 1, ArrowUp: -1 };
        if (!(e.key in grow)) return;
        e.preventDefault();
        setSize(k, curSize(k) + sp.sign * grow[e.key] * (e.shiftKey ? 64 : 16), true);
      });
    });
  }

  function expireNew() {
    const now = Date.now();
    for (const id in UI.newAt) {
      if (now - UI.newAt[id] < NEW_MS) continue;
      delete UI.newAt[id];
      document.querySelectorAll('[data-mid="' + id + '"]').forEach((el) => {
        el.classList.remove('rl-frow-new', 'rl-msg-new');
        el.querySelectorAll('.rl-tag-new').forEach((t) => t.remove());
      });
    }
  }

  loadSizes();
  wireSplitters();
  wire();
  renderTop();
  renderClock();
  poll();
})();
