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
  'storage', 'cache', 'reachable', 'last_seen_at', 'current_operation',
  'last_error', 'last_note', 'capabilities'];

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[ch]);
}

function fmtBytes(b) {
  if (b == null || isNaN(Number(b))) return 'unknown';
  const g = Number(b) / 1e9;
  if (g >= 10) return Math.round(g) + ' GB';
  if (g >= 0.01) return g.toFixed(2) + ' GB';
  return (Number(b) / 1e6).toFixed(1) + ' MB';
}

function fmtCores(n) {
  if (n == null || isNaN(Number(n))) return 'unknown';
  return Number.isInteger(Number(n)) ? String(Number(n)) : Number(n).toFixed(1);
}

function pctClass(p) { return p >= 90 ? 'crit' : p >= 70 ? 'warn' : ''; }

function known(v) { return v != null && !isNaN(Number(v)); }

// One meter, drawn the same way for all four measures and every runner: the
// share of its boundary in the value slot, used / total in the detail, and a
// bar that is how full the boundary that would stop this runner is. A shared
// boundary says "shared"; a shared volume also gives the runner's own
// figure, since the bar is then the volume's fill. Anything not known right
// now reads "unknown" in its slot, never a confident zero.
function meter(key, label, m, fmt, unit) {
  const volume = known(m.volumeTotal);
  const used = volume ? m.volumeUsed : m.used;
  const total = volume ? m.volumeTotal : m.total;
  const pct = known(used) && known(total) && Number(total) > 0
    ? 100 * Number(used) / Number(total) : null;
  const p = Math.max(0, Math.min(100, pct || 0));
  const detail = (volume ? fmt(m.used) + ' own · ' : '') +
    fmt(used) + ' / ' + fmt(total) + unit + (m.shared ? ' shared' : '');
  return `<div class="meter" data-m="${esc(key)}">` +
    `<div class="mrow"><span>${esc(label)}</span>` +
    `<span class="mval">${esc(pct == null ? 'unknown' : pct.toFixed(1) + '%')}</span></div>` +
    `<div class="mdetail">${esc(detail)}</div>` +
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
                    total: cpu.total_cores, shared: cpu.shared}, fmtCores, ' cores'],
    ['memory', 'Memory', {used: mem.used_bytes, total: mem.total_bytes,
                          shared: mem.shared}, fmtBytes, ''],
    ['storage', 'Storage', {used: st.used_bytes, total: st.total_bytes, shared: st.shared,
                            volumeUsed: st.volume_used_bytes,
                            volumeTotal: st.volume_total_bytes}, fmtBytes, ''],
    ['cache', 'Cache', {used: ca.used_bytes, total: ca.total_bytes, shared: ca.shared,
                        volumeUsed: ca.volume_used_bytes,
                        volumeTotal: ca.volume_total_bytes}, fmtBytes, ''],
  ].map(([key, label, m, fmt, unit]) => meter(key, label, m, fmt, unit)).join('');
}

function actionButton(a, i) {
  if (!a.visible) return '';
  const title = a.enabled ? '' : ` title="${esc(a.reason)}"`;
  return `<button data-action="${i}" data-verb="${esc(a.verb)}" class="${esc(a.tone)}"` +
    (a.enabled ? '' : ' disabled aria-disabled="true"') + title + `>` +
    esc(a.label) + `</button>`;
}

function displayName(c) {
  const base = [c.provider, c.platform, c.architecture].join('-');
  return new RegExp('^' + base + '-[0-9]+$').test(c.display_name || '')
    ? base : c.display_name;
}

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
    : `<div class="clabels">` +
      (c.labels.length ? c.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('') : 'No labels configured') + `</div>`;
  const drift = c.label_drift ? `<div class="cwarn">${esc(c.label_drift)}</div>` : '';
  const notes = (c.annotations || []).map(
    t => `<span class="annot">${esc(t)}</span>`).join('');
  const draining = c.lifecycle_state === 'draining' || c.state === 'draining';
  const job = c.job || (draining ? 'draining' : 'no active job');
  const seen = c.reachable === false ? 'not reachable'
             : c.reachable == null ? 'reachability unknown' : 'reachable';
  const status = `<div class="creg cstatus"><span>${esc(seen)}</span>` +
    (c.last_seen_at ? `<span>seen ${esc(String(c.last_seen_at).replace('T', ' ').replace('Z', ' UTC'))}</span>` : '') +
    (c.current_operation ? `<span>operation ${esc(c.current_operation)}</span>` : '') +
    `</div>`;
  const error = c.last_error ? `<div class="cerr">${esc(c.last_error)}</div>` : '';
  // A note is something true that is not a failure - a runner registered
  // with labels of its own still works. It reads as a note, not in the
  // colour that means something is broken. When the note is registration
  // drift, the warning above already says it - the note does not repeat it.
  const note = (c.last_note && !c.label_drift) ? `<div class="cnote">${esc(c.last_note)}</div>` : '';
  const actions = c.actions || [];
  const logs = actions.map((a, i) => a.verb === 'logs' ? actionButton(a, i) : '').join('');
  // Keep the original index: click handlers look up actions in the payload.
  const order = ['drain', 'cancel_drain', 'start', 'stop', 'restart', 'clear_cache', 'recreate', 'remove'];
  const buttons = actions.map((a, i) => ({a, i})).filter(({a}) => a.verb !== 'logs')
    .sort((x, y) => order.indexOf(x.a.verb) - order.indexOf(y.a.verb))
    .map(({a, i}) => actionButton(a, i)).join('');
  const metadata = `<div class="card-meta" aria-label="Runner labels and capabilities">` +
    labels + (notes ? `<div class="annots">${notes}</div>` : '') + `</div>`;
  return `<div class="chead">${name}<span class="badge ${esc(c.state)}">` +
    `${esc(c.state)}</span></div>` +
    `<div class="creg">${esc(where)}</div>` +
    `<div class="cjob${c.job ? '' : ' none'}">${esc(job)}</div>` +
    `<div class="metrics">${meters(c)}</div>` + metadata + drift + note + error +
    `<div class="card-footer">${status}${logs}</div>` +
    (buttons ? `<div class="actions" role="group" aria-label="Manage runner">${buttons}</div>` : '');
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
  // The runner group this fleet's runners join, always - also when public
  // repositories may use it, which is when it matters most and is drawn in
  // the alert colour. Grey when it could not be read, with why on hover.
  const g = f.runner_group_policy;
  const group = !g ? ''
    : g.known
      ? `<div class="flabels">runner group: <span class="chip${g.allows_public_repositories ? ' danger' : ''} rgroup">` +
        `${esc(g.group)} · ${esc(g.visibility || 'visibility unknown')} · ` +
        `${g.allows_public_repositories ? 'public repositories allowed' : 'no public repositories'}</span></div>`
      : `<div class="flabels">runner group: <span class="chip unknown rgroup" title="${esc(g.why || '')}">` +
        `${g.group ? esc(g.group) + ' · ' : ''}runner group unknown</span></div>`;
  const labels = f.labels && f.labels.length
    ? `<div class="flabels">runs-on: ${f.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('')}</div>`
    : '';
  const summary = f.summary || {};
  const running = summary.running == null ? '' :
    `<span class="fleet-ready">${summary.running} online</span>`;
  return `<div class="fleet-heading"><div><h2 class="fleet-head">${esc(f.title)}</h2>` +
    `<div class="fleet-summary"><span class="fcount">${count} runner${count === 1 ? '' : 's'}</span>${running}</div>` +
    `</div><div class="fleet-actions">${buttons}</div></div>` +
    labels + group + unavailable + limits;
}

if (typeof module !== 'undefined') {
  module.exports = {CARD_FIELDS, cardHTML, fleetHeadHTML, meters, esc, fmtBytes};
}
