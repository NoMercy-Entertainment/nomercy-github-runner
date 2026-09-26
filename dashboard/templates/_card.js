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

function pctClass(p) { return p >= 90 ? 'crit' : p >= 70 ? 'warn' : ''; }

function meter(key, label, text, pct) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="meter" data-m="${esc(key)}">` +
    `<div class="mrow"><span>${esc(label)}</span>` +
    `<span class="mval">${esc(text)}</span></div>` +
    `<div class="track"><div class="fill ${pctClass(p)}" ` +
    `style="width:${p.toFixed(1)}%"></div></div></div>`;
}

// The four measures a card may carry. Each is null when the runner does not
// have it or it could not be measured, and then says so rather than drawing a
// confident zero.
function meters(c) {
  let out = '';
  const cpu = c.cpu || {};
  if (cpu.percent == null) {
    out += meter('cpu', 'CPU', 'unknown', 0);
  } else {
    const cores = Number(cpu.cores) || 0, host = Number(cpu.host_cores) || 0;
    const cap = cores || host;
    const text = cap
      ? Number(cpu.percent).toFixed(1) + '%  ·  ' +
        (Number(cpu.percent) / 100).toFixed(1) + ' / ' + cap + ' cores'
      : Number(cpu.percent).toFixed(1) + '%';
    out += meter('cpu', 'CPU', text, host ? Number(cpu.percent) / host
                                          : cap ? Number(cpu.percent) / cap : 0);
  }
  const mem = c.memory || {};
  if (mem.used_bytes == null) {
    out += meter('memory', 'Memory', 'unknown', 0);
  } else if (mem.limit_bytes) {
    const p = 100 * mem.used_bytes / mem.limit_bytes;
    out += meter('memory', 'Memory', p.toFixed(1) + '%  ·  ' +
                 fmtBytes(mem.used_bytes) + ' / ' + fmtBytes(mem.limit_bytes), p);
  } else {
    out += meter('memory', 'Memory', fmtBytes(mem.used_bytes), 0);
  }
  if (c.cache) {
    const used = c.cache.used_bytes, cap = c.cache.cap_bytes;
    out += used == null ? meter('cache', 'Cache', 'unknown', 0)
      : meter('cache', 'Cache', (cap ? (100 * used / cap).toFixed(1) + '%  ·  ' : '') +
              fmtBytes(used) + (cap ? ' / ' + fmtBytes(cap) : ''),
              cap ? 100 * used / cap : 0);
  }
  if (c.storage) {
    const used = c.storage.used_bytes, total = c.storage.total_bytes;
    out += meter('storage', 'Storage',
                 total ? (100 * used / total).toFixed(1) + '%  ·  ' +
                         fmtBytes(used) + ' / ' + fmtBytes(total)
                       : fmtBytes(used),
                 total ? 100 * used / total : 0);
  }
  return out;
}

function actionButton(a, i) {
  if (!a.visible) return '';
  const title = a.enabled ? '' : ` title="${esc(a.reason)}"`;
  return `<button data-action="${i}" class="${esc(a.tone)}"` +
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
  const job = c.state === 'draining' && !c.job ? 'draining - waiting for idle'
            : (c.job || 'no active job');
  const seen = c.reachable === false ? 'not reachable'
             : c.reachable == null ? 'reachability unknown' : 'reachable';
  const status = `<div class="creg cstatus">${esc(seen)}` +
    (c.last_seen_at ? ` · seen ${esc(String(c.last_seen_at).replace('T', ' ').replace('Z', ' UTC'))}` : '') +
    (c.current_operation ? ` · operation ${esc(c.current_operation)}` : '') +
    `</div>`;
  const error = c.last_error ? `<div class="cerr">${esc(c.last_error)}</div>` : '';
  // A note is something true that is not a failure - a runner registered
  // with labels of its own still works. It reads as a note, not in the
  // colour that means something is broken. When the note is registration
  // drift, the warning above already says it - the note does not repeat it.
  const note = (c.last_note && !c.label_drift) ? `<div class="cnote">${esc(c.last_note)}</div>` : '';
  const buttons = (c.actions || []).map(actionButton).join('');
  const metadata = `<details class="card-meta"><summary>Labels and details</summary>` +
    labels + (notes ? `<div class="annots">${notes}</div>` : '') + `</details>`;
  return `<div class="chead">${name}<span class="badge ${esc(c.state)}">` +
    `${esc(c.state)}</span></div>` +
    `<div class="creg">${esc(where)}</div>` +
    `<div class="cjob${c.job ? '' : ' none'}">${esc(job)}</div>` +
    `<div class="metrics">${meters(c)}</div>` + status + drift + note + error +
    metadata +
    (buttons ? `<details class="card-manage"><summary>Manage runner</summary>` +
      `<div class="actions">${buttons}</div></details>` : '');
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
  const labels = f.labels && f.labels.length
    ? `<div class="flabels">runs-on: ${f.labels.map(l => `<span class="chip">${esc(l)}</span>`).join('')}</div>`
    : '';
  const summary = f.summary || {};
  const running = summary.running == null ? '' :
    `<span class="fleet-ready">${summary.running} online</span>`;
  return `<div class="fleet-heading"><div><h2 class="fleet-head">${esc(f.title)}</h2>` +
    `<div class="fleet-summary"><span class="fcount">${count} runner${count === 1 ? '' : 's'}</span>${running}</div>` +
    `</div><div class="fleet-actions">${buttons}</div></div>` +
    labels + unavailable;
}

if (typeof module !== 'undefined') {
  module.exports = {CARD_FIELDS, cardHTML, fleetHeadHTML, meters, esc, fmtBytes};
}
