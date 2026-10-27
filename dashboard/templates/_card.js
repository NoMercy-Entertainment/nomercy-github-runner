// One runner card and one fleet heading, as pure functions: data in, HTML out.
//
// Everything a card shows comes from design 14.1's payload (dashboard/cards.py).
// There is no test here of which forge or which platform a runner is on - a
// platform difference arrives as data: `capabilities` and its `annotations`,
// and `actions`, each of which says whether it is offered, whether it is
// enabled and, when it is not, why. tests/test_generic_card.py reads this file
// and fails on any such test, and runs these functions under node.
//
// Pure so they can be tested without a browser, and so the page can compare
// the HTML it would draw with what it drew last and leave an unchanged card
// alone - rebuilding every card every poll replays its entrance animation.

const CARD_FIELDS = ['runner_id', 'display_name', 'provider', 'platform',
  'architecture', 'labels', 'worker', 'runtime', 'state', 'job', 'cpu', 'memory',
  'storage', 'cache', 'reachable', 'readiness_note', 'last_seen_at', 'current_operation',
  'last_error', 'last_note', 'capabilities'];

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[ch]);
}

function fmtBytes(b) {
  if (b == null || isNaN(Number(b))) return 'unknown';
  const g = Number(b) / 1073741824;
  if (g >= 10) return Math.round(g) + ' GiB';
  if (g >= 0.01) return g.toFixed(2) + ' GiB';
  return (Number(b) / 1048576).toFixed(1) + ' MiB';
}

function fmtCores(n) {
  if (n == null || isNaN(Number(n))) return 'unknown';
  return Number.isInteger(Number(n)) ? String(Number(n)) : Number(n).toFixed(1);
}

// Cores in use always with one decimal: "0 / 8" and "0.0 / 8" from one beat
// to the next read as a change.
function fmtUsedCores(n) {
  if (n == null || isNaN(Number(n))) return 'unknown';
  return Number(n).toFixed(1);
}

function pctClass(p) { return p >= 90 ? 'crit' : p >= 70 ? 'warn' : ''; }

function known(v) { return v != null && !isNaN(Number(v)); }

// How old a reading is, in the fewest words: "45 s ago", "3 min ago".
function fmtAge(s) {
  if (!known(s)) return '';
  const n = Math.max(0, Math.round(Number(s)));
  return n < 90 ? n + ' s ago' : Math.round(n / 60) + ' min ago';
}

// One meter, drawn the same way for all four measures and every runner, in
// the same three fixed lines whatever is known (2026-10-27: a storage line
// that grew a second line, and CPU that flipped to "unknown", moved every
// card below it). Line one: the label - with the reading's age when it is
// the last known one, or the runner's own figure when the bar is a shared
// volume's - and the share of the boundary. Line two: used / total of what
// the bar shows, in one format. Line three: the bar, how full the boundary
// that would stop this runner is - a volume's fill when it is known.
// Anything not known reads "unknown" in its slot, never a confident zero.
function meter(key, label, m, fmt, unit, fmtUsed) {
  const volume = known(m.volumeTotal);
  const used = volume ? m.volumeUsed : m.used;
  const total = volume ? m.volumeTotal : m.total;
  const pct = known(used) && known(total) && Number(total) > 0
    ? 100 * Number(used) / Number(total) : null;
  const p = Math.max(0, Math.min(100, pct || 0));
  const sub = known(m.age) ? fmtAge(m.age)
    : volume && known(m.used) ? fmt(m.used) + ' own' : '';
  const detail = (fmtUsed || fmt)(used) + ' / ' + fmt(total) + unit + (m.shared ? ' shared' : '');
  return `<div class="meter" data-m="${esc(key)}">` +
    `<div class="mrow"><span class="mlabel">${esc(label)}` +
    `<span class="msub">${esc(sub)}</span></span>` +
    `<span class="mval">${esc(pct == null ? 'unknown' : pct.toFixed(1) + '%')}</span></div>` +
    `<div class="mdetail" title="${esc(detail + (sub ? ' · ' + sub : ''))}">${esc(detail)}</div>` +
    `<div class="track"><div class="fill ${pctClass(p)}" ` +
    `style="width:${p.toFixed(1)}%"></div></div></div>`;
}

// The four meters every card has, always, in this order. A CPU percent is
// summed over cores, so the cores in use are a hundredth of it, of the
// runner's own cores or, when it has none, of the machine's.
function meters(c) {
  const cpu = c.cpu || {}, mem = c.memory || {};
  const st = c.storage || {}, ca = c.cache || {};
  return [
    ['cpu', 'CPU', {used: known(cpu.percent) ? Number(cpu.percent) / 100 : null,
                    total: cpu.total_cores, shared: cpu.shared, age: cpu.age_s},
     fmtCores, ' cores', fmtUsedCores],
    ['memory', 'Memory', {used: mem.used_bytes, total: mem.total_bytes,
                          shared: mem.shared, age: mem.age_s}, fmtBytes, ''],
    ['storage', 'Storage', {used: st.used_bytes, total: st.total_bytes, shared: st.shared,
                            volumeUsed: st.volume_used_bytes,
                            volumeTotal: st.volume_total_bytes}, fmtBytes, ''],
    ['cache', 'Cache', {used: ca.used_bytes, total: ca.total_bytes, shared: ca.shared,
                        volumeUsed: ca.volume_used_bytes,
                        volumeTotal: ca.volume_total_bytes}, fmtBytes, ''],
  ].map(([key, label, m, fmt, unit, fmtUsed]) => meter(key, label, m, fmt, unit, fmtUsed)).join('');
}

function actionButton(a, i) {
  if (!a.visible) return '';
  const title = a.enabled ? '' : ` title="${esc(a.reason)}"`;
  return `<button data-action="${i}" data-verb="${esc(a.verb)}" class="${esc(a.tone)}"` +
    (a.enabled ? '' : ' disabled aria-disabled="true"') + title + `>` +
    esc(a.label) + `</button>`;
}

// The action grid has the same six slots on every card, whatever its state:
// a state that offers fewer actions leaves a slot empty instead of pulling
// the rows below up (2026-10-27). Each slot holds the first visible verb of
// its list; Repair, offered only on a failed runner, takes the drain slot,
// which a failed runner never offers.
const ACTION_SLOTS = [['drain', 'cancel_drain', 'repair'], ['start', 'stop'],
  ['restart'], ['clear_cache'], ['recreate'], ['remove']];

function actionGrid(actions) {
  const used = new Set();
  const slots = ACTION_SLOTS.map(verbs => {
    for (const verb of verbs) {
      const i = actions.findIndex(a => a.verb === verb && a.visible);
      if (i >= 0) { used.add(i); return actionButton(actions[i], i); }
    }
    return '<span class="aslot" aria-hidden="true"></span>';
  });
  // Anything else a payload offers is still drawn, after the six.
  actions.forEach((a, i) => {
    if (a.verb !== 'logs' && a.visible && !used.has(i)) slots.push(actionButton(a, i));
  });
  return slots.join('');
}

function displayName(c) {
  // The full name, number included: it is what the forge lists the runner
  // as, and what an alarm or a job's log names.
  return c.display_name;
}

// Every card is the same lines in the same order, each a fixed height:
// heading, where, job (two lines at most), four meters, labels, one message
// line pair, three status lines, the action grid. What is not there leaves
// its line empty; long text is cut with its whole in the tooltip.
function cardHTML(c) {
  const shownName = displayName(c);
  const name = c.href
    ? `<a class="cname" href="${esc(c.href)}">${esc(shownName)}</a>`
    : `<span class="cname">${esc(shownName)}</span>`;
  const where = ['worker ' + (c.worker || '?'), c.runtime,
                 [c.platform, c.architecture].filter(Boolean).join('/'),
                 c.registration, c.uptime].filter(Boolean).join(' · ');
  const labels = c.labels == null
    ? `<div class="clabels">Labels unavailable</div>`
    : `<div class="clabels" title="${esc(c.labels.join(', '))}">` +
      (c.labels.length ? c.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('') : 'No labels configured') + `</div>`;
  const notes = (c.annotations || []).map(
    t => `<span class="annot">${esc(t)}</span>`).join('');
  const draining = c.lifecycle_state === 'draining' || c.state === 'draining';
  const job = c.job || (c.state === 'busy' ? 'runner is busy - job details unavailable'
    : draining ? 'draining' : 'no active job');
  const seen = c.reachable === false ? 'not reachable'
             : c.reachable == null ? 'reachability unknown' : 'reachable';
  const third = [c.current_operation ? 'operation ' + c.current_operation : '',
                 c.readiness_note || ''].filter(Boolean).join(' · ');
  const line = text => `<span class="sline" title="${esc(text)}">${esc(text)}</span>`;
  const status = `<div class="creg cstatus">${line(seen)}` +
    line(c.last_seen_at ? 'seen ' + String(c.last_seen_at).replace('T', ' ').replace('Z', ' UTC') : '') +
    line(third) + `</div>`;
  // One message slot, always there: the last error, a registration drift
  // warning, and a note - something true that is not a failure, in the
  // colour of a note. When the note is registration drift, the warning
  // already says it - the note does not repeat it.
  const messages = [];
  if (c.last_error) messages.push(`<span class="cerr">${esc(c.last_error)}</span>`);
  if (c.label_drift) messages.push(`<span class="cwarn">${esc(c.label_drift)}</span>`);
  if (c.last_note && !c.label_drift) messages.push(`<span class="cnote">${esc(c.last_note)}</span>`);
  const whole = [c.last_error, c.label_drift, c.label_drift ? null : c.last_note]
    .filter(Boolean).join(' · ');
  const message = `<div class="cmsg" title="${esc(whole)}">${messages.join(' ')}</div>`;
  const actions = c.actions || [];
  const logs = actions.map((a, i) => a.verb === 'logs' ? actionButton(a, i) : '').join('');
  const buttons = actions.some(a => a.verb !== 'logs')
    ? `<div class="actions" role="group" aria-label="Manage runner">${actionGrid(actions)}</div>` : '';
  const metadata = `<div class="card-meta" aria-label="Runner labels and capabilities">` +
    labels + `<div class="annots" title="${esc((c.annotations || []).join(', '))}">${notes}</div></div>`;
  return `<div class="chead">${name}<span class="badge ${esc(c.state)}">` +
    `${esc(c.state)}</span></div>` +
    `<div class="creg cwhere" title="${esc(where)}">${esc(where)}</div>` +
    `<div class="cjob${c.job ? '' : ' none'}" title="${esc(job)}"><span>${esc(job)}</span></div>` +
    `<div class="metrics">${meters(c)}</div>` + metadata + message +
    `<div class="card-footer">${status}${logs}</div>` + buttons;
}

// Who may use this fleet's runners, in plain words, and whether the runners
// themselves refuse a pull request from an outside fork - always, for every
// fleet whose forge has runner groups.
//
// `g` is the runner group as GitHub last told it (runner_groups.py; `says`
// is the sentence). `og` counts the fleet's runners by what each one's own
// unit last reported of its job-started hook (api_v2.origin_guard_of):
// proven (carries the check, unchanged, and its last job read its event),
// unproven (carries it, no job since), unread (its last job's event could
// not be read), missing (no check, or a changed one), unknown (no recent
// report). Green: every runner proven. Red: public repositories may use the
// runners and a runner is missing or unknown. Amber: anything else - no
// public repositories, no runners, or runners that carry the check but have
// not proven it. Grey: the group could not be read and not every runner is
// proven.
function runnerGroupHTML(g, og) {
  if (!g) return '';
  const n = (k) => (og ? Number(og[k]) || 0 : 0);
  const runners = n('runners');
  const covered = n('proven') + n('unproven') + n('unread');
  const proven = runners > 0 && n('proven') === runners;
  const tone = proven ? 'safe'
    : !g.known ? 'unknown'
    : runners === 0 || covered === runners ? 'warn'
    : g.allows_public_repositories ? 'danger' : 'warn';
  const who = g.known
    ? `Runner group ${esc(g.group)}: ${esc(g.says || '')}`
    : `Runner group ${g.group ? esc(g.group) : 'unknown'}: who may use these runners could not be read`;
  const forks = `Pull requests from forks outside ${esc(g.org || 'the organisation')} and trusted owners`;
  let guard;
  if (proven) {
    guard = `${forks} are refused by the runner`;
  } else if (runners === 0) {
    guard = 'No runners to check yet: whether outside pull requests are refused is said once one runs';
  } else if (covered < runners) {
    if (n('missing')) {
      guard = covered === 0
        ? `${forks} are not refused by these runners yet: recreate the ${n('missing')} runners missing the check`
        : `${forks} are refused by ${covered} of ${runners} runners; recreate the ${n('missing')} runners missing the check`;
      if (n('unknown')) guard += ` (${n('unknown')} could not be checked lately)`;
    } else {
      guard = `${forks}: protection is currently unverified (${n('unknown')} could not be checked lately)`;
    }
  } else {
    const why = [];
    if (n('unproven')) why.push(`${n('unproven')} of ${runners} have not run a job with it yet`);
    if (n('unread')) why.push(`the last job on ${n('unread')} of ${runners} could not read its event`);
    guard = `These runners carry the check against outside forks, but ${why.join('; ')}`;
  }
  const notes = og && og.notes && og.notes.length ? ` title="${esc(og.notes.join('; '))}"` : '';
  const why = !g.known && g.why ? ` title="${esc(g.why)}"` : '';
  return `<div class="flabels"><span class="chip ${tone} rgroup"${why}>${who}` +
    `<span class="rguard"${notes}>${guard}</span></span></div>`;
}

function fleetHeadHTML(f) {
  const count = (f.runners || []).length;
  const buttons = (f.actions || []).map((a, i) => {
    if (!a.visible) return '';
    const title = a.enabled ? '' : ` title="${esc(a.reason)}"`;
    return `<button data-fleet-action="${i}" class="${esc(a.tone)}"` +
      (a.enabled ? '' : ' disabled aria-disabled="true"') + title + `>` +
      esc(a.label) + `</button>`;
  }).join('');
  const unavailable = f.available ? ''
    : `<div class="hint unavailable">${esc(f.reason || 'unavailable')}</div>`;
  // Why "+ Add runner" is off when the fleet lacks a CPU or memory limit -
  // said on the heading too, not only in a button's tooltip.
  const limits = f.limits_problem
    ? `<div class="cwarn flimits">${esc(f.limits_problem)}</div>` : '';
  const group = runnerGroupHTML(f.runner_group_policy, f.origin_guard);
  const labels = f.labels && f.labels.length
    ? `<div class="flabels">runs-on: ${f.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('')}</div>`
    : '';
  const summary = f.summary || {};
  const running = summary.online == null ? '' :
    `<span class="fleet-ready">${summary.online} online</span>`;
  // "3 of 3 runners" when the fleet has a ceiling (Settings, max_capacity).
  const fcount = f.max_capacity == null
    ? `${count} runner${count === 1 ? '' : 's'}`
    : `${count} of ${f.max_capacity} runner${f.max_capacity === 1 ? '' : 's'}`;
  return `<div class="fleet-heading"><div><h2 class="fleet-head">${esc(f.title)}</h2>` +
    `<div class="fleet-summary"><span class="fcount">${fcount}</span>${running}</div>` +
    `</div><div class="fleet-actions">${buttons}</div></div>` +
    labels + group + unavailable + limits;
}

if (typeof module !== 'undefined') {
  module.exports = {CARD_FIELDS, cardHTML, fleetHeadHTML, meters, esc, fmtBytes};
}
