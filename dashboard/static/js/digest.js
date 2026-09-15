/* Page 1 — What happened? (bi-language via URL prefix; LANG from common.js) */
let sevChart = null;
let lastTotals = null;   // stash day totals so the events label can show the backlog
const DIGEST_MAX_PX = 320;

// Cap the AI Daily Digest height; show an expand/collapse toggle only when the
// content actually overflows, so the Risk Events table stays within easy reach.
function setupDigestCollapse() {
  const body = document.getElementById('digest-body');
  const toggle = document.getElementById('digest-toggle');
  if (!body || !toggle) return;
  body.classList.add('collapsible');
  body.classList.remove('collapsed');
  if (body.scrollHeight > DIGEST_MAX_PX + 48) {
    body.classList.add('collapsed');
    toggle.style.display = '';
    toggle.textContent = t('expand_digest');
    toggle.onclick = () => {
      const collapsed = body.classList.toggle('collapsed');
      toggle.textContent = collapsed ? t('expand_digest') : t('collapse_digest');
      if (collapsed) body.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    };
  } else {
    toggle.style.display = 'none';
  }
}


async function load() {
  const date = document.getElementById('f-date').value;
  const [summary, digest] = await Promise.all([
    API.get(`/api/summary?date=${date}&lang=${LANG}`),
    API.get(`/api/digest?date=${date}&lang=${LANG}`),
  ]);

  const t0 = summary.totals;
  lastTotals = t0;
  document.getElementById('kpis').innerHTML = `
    <div class="card kpi"><div class="label">${t('players_screened')}</div>
      <div class="value num">${summary.players_screened.toLocaleString()}</div>
      <div class="delta">${t('signals_cost', summary.signals.toLocaleString(), summary.queries_cost_usd)}</div></div>
    <div class="card kpi ${t0.events ? 'blue' : ''}"><div class="label">${t('events_day')}</div>
      <div class="value num">${t0.events}</div><div class="delta">${t0.new} ${t('new_n')}</div></div>
    ${t0.alerts !== undefined ? `
    <div class="card kpi ${t0.alerts ? 'blue' : 'green'}"><div class="label">${t('new_changed')}</div>
      <div class="value num">${t0.alerts}</div>
      <div class="delta">${t('alerts_breakdown', t0.opened, t0.escalated, t0.reopened)}</div></div>` : ''}
    <div class="card kpi ${t0.critical ? 'red' : ''}"><div class="label">${sevLabel('Critical')}</div>
      <div class="value num">${t0.critical}</div><div class="delta">${t('sla_1h')}</div></div>
    <div class="card kpi ${t0.high ? 'amber' : ''}"><div class="label">${sevLabel('High')}</div>
      <div class="value num">${t0.high}</div><div class="delta">${t('sla_day')}</div></div>
    <div class="card kpi ${t0.confirmed ? 'red' : 'green'}"><div class="label">${t('confirmed')}</div>
      <div class="value num">${t0.confirmed}</div>
      <div class="delta">${t0.downgraded} ${t('downgraded_after')}</div></div>`;

  // Structured, human-readable summary only. The box never renders the full
  // report body, appendix, raw evidence, or file names -- full detail lives in
  // the Risk Events drill-down below.
  document.getElementById('digest-when').textContent = digest.generated_at;
  document.getElementById('digest-headline').textContent = digest.headline;
  document.getElementById('digest-stats').textContent = digest.stats || '';
  document.getElementById('look-first').innerHTML =
    `<b>${t('look_first')}</b> ${digest.look_first}`;

  const list = (items, cls) => (items && items.length)
    ? items.map((x) => `<div class="${cls}">${x}</div>`).join('')
    : `<div class="empty">${t('none_this_cycle')}</div>`;
  // AI review items read better with the case id, verdict and next-step on
  // their own lines (labels handled for both English and Chinese).
  const fmtReview = (x) => x
    .replace(/^\s*(.+?)\s*—\s*/, '<b>$1</b> — ')
    .replace(/\s*((?:AI verdict|AI\s*判定|AI\s*裁定|Next|下一步|後續)\s*[:：])/g,
             '<br><b>$1</b> ');
  document.getElementById('decision-items').innerHTML = list(digest.decision_items, 'decision-item');
  document.getElementById('observations').innerHTML = list(digest.observations, 'digest-line');
  document.getElementById('ai-review').innerHTML = (digest.ai_review && digest.ai_review.length)
    ? digest.ai_review.map((x) => `<div class="ai-review-item">${fmtReview(x)}</div>`).join('')
    : `<div class="empty">${t('none_this_cycle')}</div>`;
  document.getElementById('todos').innerHTML = list(digest.todos, 'todo-item');

  document.getElementById('caveats').innerHTML = summary.data_caveats.length
    ? summary.data_caveats.map((c) => `<div class="caveat-line">⚠ ${c}</div>`).join('')
    : `<div class="caveat-clean">${t('caveats_clean')}</div>`;

  const ctx = document.getElementById('sev-chart');
  if (sevChart) sevChart.destroy();
  const labels = Object.keys(summary.by_severity);
  sevChart = new Chart(ctx, {
    type: 'doughnut',
    data: { labels: labels.map(sevLabel),
      datasets: [{ data: labels.map((l) => summary.by_severity[l]),
        backgroundColor: labels.map((l) => SEV_COLORS[l]),
        borderColor: css('--bg-card'), borderWidth: 2 }] },
    options: { maintainAspectRatio: false, cutout: '62%',
      plugins: { legend: { position: 'right' } } },
  });

  await loadEvents();
}

async function loadEvents() {
  const date = document.getElementById('f-date').value;
  const sev = document.getElementById('f-sev').value;
  const st = document.getElementById('f-status').value;
  const qs = new URLSearchParams({ from: date, to: date });
  if (sev) qs.set('severity', sev);
  if (st) qs.set('status', st);
  // Default view = actionable (human_review tier). Monitor-tier is routine
  // backlog and is only shown once the user picks a Severity/Status filter.
  const actionable = !sev && !st;
  if (actionable) qs.set('actionable', '1');
  qs.set('lang', LANG);
  const rows = await API.get(`/api/events?${qs}`);
  const cnt = document.getElementById('events-count');
  if (actionable && lastTotals) {
    const backlog = Math.max(0, lastTotals.events - rows.length);
    cnt.textContent = `${rows.length} ${t('to_review')} · ${backlog} ${t('monitoring_n')}`;
  } else {
    cnt.textContent = `${rows.length} ${t('on_this_date')}`;
  }
  document.getElementById('events-body').innerHTML = rows.length
    ? rows.map(eventRow).join('')
    : `<tr><td colspan="7" class="empty">${t('no_events')}</td></tr>`;
}

document.getElementById('btn-load').addEventListener('click', load);
document.getElementById('f-sev').addEventListener('change', loadEvents);
document.getElementById('f-status').addEventListener('change', loadEvents);
load();
