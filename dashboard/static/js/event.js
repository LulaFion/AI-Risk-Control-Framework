/* Page 2 — Why? */
async function loadEvent() {
  const ev = await API.get(`/api/events/${EVENT_ID}?lang=${LANG}`);

  document.getElementById('ev-title').textContent = ev.title;
  document.getElementById('ev-meta').innerHTML =
    `<span class="mono">${ev.id}</span> · ${ev.detected_at} · ${t('window')} ` +
    `${ev.window.start} → ${ev.window.end} · ${ev.operator} / ` +
    `<span class="mono">${ev.uid}</span> / ${ev.game_id} (${ev.currency})`;
  document.getElementById('ev-chips').innerHTML =
    `${typeChip(ev.risk_type)} ${sevChip(ev.severity)} ${statusChip(ev.status)}`;

  document.getElementById('ev-overview').innerHTML =
    Object.entries(ev.overview).map(([k, v]) =>
      `<div class="ov-item"><div class="k">${k}</div><div class="v num">${v}</div></div>`).join('');

  document.getElementById('ev-why').innerHTML = ev.why_detected.map((w) => `
    <div class="why-item">
      <span class="sig mono">${w.signal}</span><span class="fam">${w.family}</span>
      <span class="num" style="float:right">${t('value')} ${w.value}</span>
      <div class="desc">${w.description}</div>
      <div class="meth">${t('threshold')}: ${w.threshold} · ${w.method}</div>
    </div>`).join('');

  renderCharts(ev.evidence_charts || []);

  document.getElementById('ev-rounds').innerHTML = renderEvidenceRounds(ev.evidence_rounds);

  const arc = document.getElementById('ev-autoreport-card');
  if (ev.auto_report_md) {
    document.getElementById('ev-autoreport').innerHTML = md(ev.auto_report_md);
    if (arc) arc.style.display = '';
  } else if (arc) { arc.style.display = 'none'; }

  document.getElementById('ev-ai').innerHTML = md(ev.ai_investigation.summary_md);
  document.getElementById('ev-verified').innerHTML =
    ev.ai_investigation.verified.map((v) => `<div class="verified">✓ ${v}</div>`).join('');
  document.getElementById('ev-discrepancies').innerHTML =
    (ev.ai_investigation.discrepancies || []).map((d) =>
      `<div class="discrepancy">⚠ ${t('skeptic_disc')}: ${d}</div>`).join('');

  document.getElementById('ev-expl').innerHTML = ev.explanations.map((x) => `
    <div class="expl-row">
      <span class="plaus plaus-${x.plausibility}">${x.plausibility}</span>
      <span><b>${x.hypothesis}</b><br>
        <span style="color:var(--text-secondary);font-size:12.5px">${x.note}</span></span>
    </div>`).join('');

  document.getElementById('ev-actions').innerHTML = ev.recommended_actions.map((a) => `
    <div class="action-row"><span>${a.action}</span>
      <span class="own">${a.owner} · ${a.urgency}</span></div>`).join('');

  document.getElementById('ev-timeline').innerHTML = ev.status_history.map((h) => `
    <li><div class="t">${h.at} · ${h.by}</div>
      ${statusChip(h.status)} <span style="color:var(--text-secondary)">${h.note}</span></li>`).join('');

  document.getElementById('ev-related').innerHTML = (ev.related || []).length
    ? `<h2>${t('related')}</h2>` + ev.related.map((id) =>
        `<a class="backlink mono" href="/${LANG}/event/${id}">${id}</a><br>`).join('')
    : '';

  renderDisposition(ev);
}

// Human-review decision labels (bilingual)
const DECISIONS = {
  dismiss_benign:      { en: 'Dismiss — benign',            zh: '駁回 — 良性' },
  keep_monitoring:     { en: 'Keep monitoring',            zh: '維持監控' },
  enhanced_monitoring: { en: 'Enhanced monitoring',        zh: '加強監控' },
  needs_more_evidence: { en: 'Needs more evidence',        zh: '需更多證據' },
  escalate_to_board:   { en: 'Escalate to review board',   zh: '上報審查委員會' },
  confirmed_for_action:{ en: 'Confirmed for action',       zh: '確認需處置' },
};

function renderDisposition(ev) {
  // label the decision <option>s in the current language
  document.querySelectorAll('#rv-decision option').forEach((o) => {
    if (o.value && DECISIONS[o.value]) o.textContent = DECISIONS[o.value][LANG] || DECISIONS[o.value].en;
  });
  const d = ev.disposition;
  const box = document.getElementById('ev-disposition');
  if (d) {
    const lbl = (DECISIONS[d.decision] && DECISIONS[d.decision][LANG]) || d.decision_label || d.decision;
    box.innerHTML = `<div class="dispo-current">
      <b>${t('rv_recorded')}:</b> ${statusChip(ev.status)} <b>${lbl}</b>
      <div class="meth">${d.reviewer || ''} · ${d.at || ''}</div>
      ${d.notes ? `<div class="desc">${d.notes}</div>` : ''}</div>`;
    if (DECISIONS[d.decision]) document.getElementById('rv-decision').value = d.decision;
    if (d.reviewer) document.getElementById('rv-reviewer').value = d.reviewer;
    if (d.notes) document.getElementById('rv-notes').value = d.notes;
  } else {
    box.innerHTML = `<div class="empty">${t('rv_none')}</div>`;
  }
}

async function submitDisposition() {
  const decision = document.getElementById('rv-decision').value;
  const msg = document.getElementById('rv-msg');
  if (!decision) { msg.textContent = t('rv_pick'); return; }
  msg.textContent = t('rv_saving');
  try {
    const res = await fetch(`/api/events/${EVENT_ID}/disposition?lang=${LANG}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        decision,
        reviewer: document.getElementById('rv-reviewer').value,
        notes: document.getElementById('rv-notes').value }) });
    if (!res.ok) throw new Error('save failed');
    const ev = await res.json();
    document.getElementById('ev-chips').innerHTML =
      `${typeChip(ev.risk_type)} ${sevChip(ev.severity)} ${statusChip(ev.status)}`;
    document.getElementById('ev-timeline').innerHTML = ev.status_history.map((h) => `
      <li><div class="t">${h.at} · ${h.by}</div>
        ${statusChip(h.status)} <span style="color:var(--text-secondary)">${h.note}</span></li>`).join('');
    renderDisposition(ev);
    msg.textContent = t('rv_saved');
  } catch (e) { msg.textContent = t('rv_error'); }
}
document.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'rv-submit') submitDisposition();
});

// One evidence chart per fired risk family; each gets its own titled canvas.
function renderCharts(specs) {
  const host = document.getElementById('ev-charts');
  host.innerHTML = '';
  if (!specs.length) {
    host.innerHTML = `<div class="empty">${t('no_chart')}</div>`;
    return;
  }
  specs.forEach((spec, i) => {
    const wrap = document.createElement('div');
    if (i > 0) wrap.className = 'section-gap';
    const sr = spec.suspect_range
      ? ` · <b>${t('suspected')}:</b> ${t('suspected_pts', spec.suspect_range.n)} (${spec.suspect_range.start}${spec.suspect_range.start !== spec.suspect_range.end ? ' → ' + spec.suspect_range.end : ''})`
      : '';
    wrap.innerHTML =
      `<h3 class="chart-h">${spec.title || ''}</h3>` +
      `<div class="chart-box"><canvas id="ev-chart-${i}"></canvas></div>` +
      `<div class="chart-note">${spec.note || ''}${sr}</div>`;
    host.appendChild(wrap);
    renderChart(spec, document.getElementById(`ev-chart-${i}`));
  });
}

function renderChart(spec, ctx) {
  if (!spec) return;
  const yTitle = (txt) => txt ? { title: { display: true, text: txt } } : {};
  const refLine = (v) => ({
    type: 'line', label: t('reference'), data: spec.labels.map(() => v),
    borderColor: css('--text-muted'), borderDash: [6, 4], borderWidth: 1.5,
    pointRadius: 0, fill: false,
  });

  if (spec.kind === 'line') {
    const susp = spec.suspect || [];
    const hasSusp = susp.some(Boolean);
    // per-point emphasis on suspected days
    const ptColor = spec.labels.map((_, i) => susp[i] ? PALETTE.red : PALETTE.blue);
    const ptR = spec.labels.map((_, i) => susp[i] ? 5 : 3);
    // shaded vertical band over the suspected range (a filled dataset that is
    // non-null only on suspected days, so the fill demarcates the flagged region)
    const vals = spec.series.filter((v) => v != null);
    const bandMax = (Math.max(...vals, spec.ref != null ? spec.ref : 0) || 1) * 1.12;
    const band = spec.labels.map((_, i) => susp[i] ? bandMax : null);
    const ds = [];
    if (hasSusp) ds.push({ label: t('suspected_zone'), data: band,
      backgroundColor: 'rgba(248,81,73,.13)', borderColor: 'transparent',
      pointRadius: 0, fill: 'origin', stepped: true, spanGaps: false });
    ds.push({ label: t('observed'), data: spec.series, borderColor: PALETTE.blue,
      backgroundColor: 'rgba(88,166,255,.12)', tension: .25,
      pointRadius: ptR, pointBackgroundColor: ptColor, fill: true });
    if (spec.ref != null) ds.push(refLine(spec.ref));
    new Chart(ctx, { type: 'line',
      data: { labels: spec.labels, datasets: ds },
      options: { maintainAspectRatio: false,
        scales: { y: yTitle(spec.ylabel) } } });
  } else if (spec.kind === 'bars') {
    const susp = spec.suspect || [];
    const colors = spec.series.map((v, i) =>
      susp[i] || (spec.ref != null && v > spec.ref) || (spec.sqrt && v > 1000)
        ? PALETTE.amber : PALETTE.blue);
    new Chart(ctx, { type: 'bar',
      data: { labels: spec.labels, datasets: [
        { label: t('value'), data: spec.series, backgroundColor: colors,
          borderRadius: 4 },
        ...(spec.ref != null ? [refLine(spec.ref)] : [])] },
      options: { maintainAspectRatio: false,
        scales: { y: spec.sqrt ? { type: 'logarithmic', ...yTitle(spec.ylabel) }
                                : yTitle(spec.ylabel) } } });
  } else if (spec.kind === 'split') {
    new Chart(ctx, { type: 'bar',
      data: { labels: spec.labels, datasets: [
        { label: spec.legend_a || t('nontrigger_pct'), data: spec.nontrigger,
          backgroundColor: PALETTE.blue, borderRadius: 4 },
        { label: spec.legend_b || t('trigger_pct'), data: spec.trigger,
          backgroundColor: PALETTE.amber, borderRadius: 4 }] },
      options: { maintainAspectRatio: false,
        scales: { y: yTitle(spec.ylabel || t('pct_group')) } } });
  } else if (spec.kind === 'persec') {
    const labels = Object.keys(spec.mult).map((k) => LANG === 'zh' ? `每秒 ${k} 局` : `${k} round(s)/sec`);
    new Chart(ctx, { type: 'bar',
      data: { labels, datasets: [{ label: t('seconds_obs'),
        data: Object.values(spec.mult),
        backgroundColor: labels.map((_, i) => i >= 2 ? PALETTE.amber : PALETTE.blue),
        borderRadius: 4 }] },
      options: { indexAxis: 'y', maintainAspectRatio: false } });
  }
}

// Evidence rounds as time BURSTS: rounds within 20 min collapse into one
// session, shown with count + span + retention countdown, then compact rows
// (account · short id · time). Temporal clustering and pull-urgency at a glance.
function renderEvidenceRounds(rounds) {
  if (!rounds || !rounds.length) return `<div class="empty">${t('no_rounds')}</div>`;
  const RET = 30 * 864e5, NOW = Date.now();
  const parsed = rounds.map((r) => {
    const ts = (r.time && r.time !== '?' && r.time !== '—') ? Date.parse(r.time) : NaN;
    const acct = ((r.note || '').match(/account (\S+)/) || [])[1] || '';
    return { gsid: r.gsid || '', time: r.time || '?', ts, acct,
             expired: (r.log_status || '').startsWith('expired'),
             na: (r.log_status || '') === 'n/a' };
  });
  parsed.sort((a, b) => (isNaN(a.ts) ? 1 : 0) - (isNaN(b.ts) ? 1 : 0) || a.ts - b.ts);
  const GAP = 20 * 60000, bursts = [];
  parsed.forEach((r) => {
    const last = bursts[bursts.length - 1];
    if (last && !isNaN(r.ts) && !isNaN(last.end) && (r.ts - last.end) <= GAP) {
      last.rows.push(r); last.end = r.ts;
    } else { bursts.push({ start: r.ts, end: r.ts, rows: [r] }); }
  });
  const t16 = (ms) => new Date(ms).toISOString().replace('T', ' ').slice(0, 16) + 'Z';
  const hhmm = (ms) => new Date(ms).toISOString().slice(11, 16) + 'Z';
  return bursts.map((b) => {
    const span = isNaN(b.start) ? '—'
      : (b.start === b.end ? t16(b.start) : `${t16(b.start)} – ${hhmm(b.end)}`);
    const avail = b.rows.filter((r) => !r.expired && !r.na && !isNaN(r.ts));
    let ret;
    if (avail.length) {
      const d = Math.max(0, Math.min(...avail.map((r) => Math.floor((RET - (NOW - r.ts)) / 864e5))));
      ret = `<span class="avail">✓ ${t('exp_in', d)}</span>`;
    } else if (b.rows.some((r) => r.expired)) {
      ret = `<span class="expired">✗ ${t('expired_short')}</span>`;
    } else { ret = `<span class="round-na">${t('no_key')}</span>`; }
    const rows = b.rows.map((r) =>
      `<div class="round-row">${r.acct ? `<span class="mono acct">${r.acct}</span> · ` : ''}` +
      `<span class="mono">${r.gsid ? r.gsid.slice(0, 8) + '…' : '—'}</span> · ${r.time}</div>`).join('');
    return `<div class="burst"><div class="burst-head">▸ ${span} · ` +
           `${b.rows.length} ${t('rounds_n')} · ${ret}</div>${rows}</div>`;
  }).join('');
}

loadEvent();
