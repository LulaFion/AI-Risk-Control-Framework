/* Page 3 — Has it happened before? */
let trendChart = null, typeChart = null;

function params() {
  const v = (id) => document.getElementById(id).value;
  return { q: v('f-q'), type: v('f-type'), severity: v('f-sev'),
           status: v('f-status'), from: v('f-from'), to: v('f-to') };
}

async function search() {
  const p = params();
  const qs = new URLSearchParams();
  Object.entries(p).forEach(([k, val]) => { if (val) qs.set(k, val); });
  qs.set('lang', LANG);

  const [rows, stats] = await Promise.all([
    API.get(`/api/events?${qs}`),
    API.get(`/api/stats?from=${p.from}&to=${p.to}`),
  ]);

  // stats KPIs
  const total = Object.values(stats.by_severity).reduce((a, b) => a + b, 0);
  document.getElementById('stat-kpis').innerHTML = `
    <div class="card kpi blue"><div class="label">${t('events_range')}</div>
      <div class="value num">${total}</div>
      <div class="delta">${stats.by_severity.Critical + stats.by_severity.High} ${t('crit_high')}</div></div>
    <div class="card kpi"><div class="label">${t('confirm_rate')}</div>
      <div class="value num">${(stats.confirm_rate * 100).toFixed(1)}%</div>
      <div class="delta">${t('of_resolved')}</div></div>
    <div class="card kpi"><div class="label">${t('mtti')}</div>
      <div class="value num">${stats.mtti_hours}h</div>
      <div class="delta">${t('mtti_sub')}</div></div>
    <div class="card kpi ${stats.by_status.new ? 'amber' : 'green'}">
      <div class="label">${t('open_cases')}</div>
      <div class="value num">${(stats.by_status.new || 0) + (stats.by_status.investigating || 0)}</div>
      <div class="delta">${stats.by_status.monitoring || 0} ${t('on_monitoring')}</div></div>
    <div class="card kpi"><div class="label">${t('top_operator')}</div>
      <div class="value" style="font-size:18px">${stats.by_operator[0]?.operator || '—'}</div>
      <div class="delta">${stats.by_operator[0]?.count || 0} ${t('events_in_range')}</div></div>`;

  // trend chart
  const tctx = document.getElementById('trend-chart');
  if (trendChart) trendChart.destroy();
  trendChart = new Chart(tctx, { type: 'bar',
    data: { labels: stats.daily.map((d) => d.date.slice(5)),
      datasets: [
        { label: t('events_lbl'), data: stats.daily.map((d) => d.events),
          backgroundColor: 'rgba(88,166,255,.5)', borderRadius: 3 },
        { label: t('crit_high'), type: 'line',
          data: stats.daily.map((d) => d.critical_high),
          borderColor: PALETTE.red, pointRadius: 0, borderWidth: 2, tension: .3 }] },
    options: { maintainAspectRatio: false,
      scales: { x: { ticks: { maxTicksLimit: 12 } } } } });

  // type chart
  const entries = Object.entries(stats.by_type).filter(([, v2]) => v2 > 0)
    .sort((a, b) => b[1] - a[1]);
  const yctx = document.getElementById('type-chart');
  if (typeChart) typeChart.destroy();
  typeChart = new Chart(yctx, { type: 'bar',
    data: { labels: entries.map(([k]) => k),
      datasets: [{ label: t('events_lbl'), data: entries.map(([, v2]) => v2),
        backgroundColor: PALETTE.blue, borderRadius: 4 }] },
    options: { indexAxis: 'y', maintainAspectRatio: false,
      plugins: { legend: { display: false } } } });

  // table
  document.getElementById('hist-count').textContent = `${rows.length} ${t('matches')}`;
  document.getElementById('hist-body').innerHTML = rows.length
    ? rows.map(eventRow).join('')
    : `<tr><td colspan="7" class="empty">${t('no_hist')}</td></tr>`;
}

document.getElementById('btn-search').addEventListener('click', search);
document.getElementById('btn-reset').addEventListener('click', () => {
  ['f-q', 'f-type', 'f-sev', 'f-status'].forEach((id) => {
    document.getElementById(id).value = '';
  });
  search();
});
document.getElementById('f-q').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') search();
});
search();
