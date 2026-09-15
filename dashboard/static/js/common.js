/* shared helpers: i18n, fetch, chips, minimal markdown, Chart.js theme */

/* ---------------- i18n ---------------- */
const LANG = window.PAGE_LANG || 'en';

const I18N = {
  en: {
    nav_digest: 'Daily Digest', nav_history: 'History & Search',
    tagline: 'riskdet · evidence, never verdicts',
    footer: 'Live detection artifacts (riskdet). Final judgment rests 100% with human risk-control experts — this dashboard is investigation support, never enforcement.',
    digest_date: 'Digest date', load: 'Load',
    ai_digest: 'AI Daily Digest', decision_items: 'Standing decision items',
    observations: 'Platform / game observations', ai_review: 'AI review results',
    todos: 'To-do', none_this_cycle: 'None this cycle',
    caveats: 'Data caveats', caveats_sub: '(read first)',
    caveats_clean: '✓ Data quality: clean — no WARN/FAIL this cycle',
    today_by_sev: 'Today by severity', risk_events: 'Risk events',
    severity: 'Severity', status: 'Status', all: 'All',
    detected: 'Detected (UTC)', type: 'Type', event: 'Event',
    operator: 'Operator', key_metric: 'Key metric',
    on_this_date: 'on this date', no_events: 'No events match — clean data is allowed to be clean.',
    to_review: 'to review', monitoring_n: 'monitoring (backlog)', show_all: 'show all',
    human_review: 'Human Review', human_review_sub: 'reviewer decision — recorded, never enforced',
    rv_decision: 'Decision', rv_choose: '— choose —', rv_reviewer: 'Reviewer', rv_notes: 'Notes',
    rv_submit: 'Record decision', rv_recorded: 'Recorded', rv_none: 'No human decision yet.',
    rv_pick: 'Pick a decision first.', rv_saving: 'Saving…', rv_saved: 'Saved.', rv_error: 'Save failed.',
    players_screened: 'Players screened', events_day: 'Events (day)',
    critical: 'Critical', high: 'High', confirmed: 'Confirmed',
    sla_1h: 'SLA: within 1 hour', sla_day: 'SLA: same-day',
    new_n: 'new', downgraded_after: 'downgraded after skeptic',
    signals_cost: (s, c) => `${s} signals · $${c} scan cost`,
    new_changed: 'New & changed', vs_standing: (n) => `vs ${n} standing`,
    alerts_breakdown: (o, e, r) => `${o} opened · ${e} escalated · ${r} reopened`,
    seen_days: (n, a, b) => a === b ? `seen 1 day (${a})` : `seen ${n} days (${a} → ${b})`,
    expand_digest: 'Expand full digest ▾', collapse_digest: 'Collapse ▴',
    look_first: 'Look at this first:',
    search_filter: 'Search & filter', has_before: 'has it happened before?',
    free_text: 'Free text', risk_type: 'Risk type', from: 'From', to: 'To',
    search: 'Search', reset: 'Reset',
    events_over_time: 'Events over time', trend_sub: 'daily count · critical+high overlay',
    by_type: 'By risk type', historical: 'Historical events', matches: 'matches',
    no_hist: 'No historical events match this filter.',
    events_range: 'Events in range', confirm_rate: 'Confirm rate',
    of_resolved: 'of resolved cases', mtti: 'Mean time to investigate',
    mtti_sub: 'detection → skeptic verdict', open_cases: 'Open (new + investigating)',
    on_monitoring: 'on monitoring', top_operator: 'Top operator',
    events_in_range: 'events in range', crit_high: 'critical+high',
    back: '← Back', overview: 'Event overview',
    why: 'Why detected', why_sub: 'signals · thresholds · methods',
    evidence: 'Evidence', ev_rounds: 'Evidence rounds',
    ev_rounds_sub: 'game_seq_id · log availability',
    ai_inv: 'AI investigation', ai_inv_sub: 'skeptic-reviewed',
    explanations: 'Possible explanations',
    actions: 'Recommended actions', actions_sub: 'never enforcement',
    ev_status: 'Event status', related: 'Related events',
    value: 'value', threshold: 'threshold', no_rounds: 'No evidence rounds attached',
    exp_in: (n) => `expires in ${n}d`, expired_short: 'expired (>30d)',
    rounds_n: 'rounds', no_key: 'no round key',
    skeptic_disc: 'skeptic discrepancy', window: 'window',
    events_lbl: 'events', seconds_obs: 'seconds observed', observed: 'observed',
    suspected_zone: 'suspected zone', suspected: 'Suspected range',
    suspected_pts: (n) => `${n} flagged point(s)`, autoreport: 'Auto-detection report',
    reference: 'reference', pct_group: '% of group',
    trigger_pct: 'trigger % (within group)', nontrigger_pct: 'non-trigger % (within group)',
    no_chart: 'no chart', free_text_ph: 'uid, operator, game, event id…',
  },
  zh: {
    nav_digest: '每日摘要', nav_history: '歷史查詢',
    tagline: 'riskdet · 只提供證據,不做裁決',
    footer: '真實偵測產物(riskdet)。最終判斷 100% 屬於人類風控專家——本儀表板僅供調查支援,絕不執行處分。',
    digest_date: '摘要日期', load: '載入',
    ai_digest: 'AI 每日摘要', decision_items: '待決策事項',
    observations: '平台 / 遊戲觀察', ai_review: 'AI 複核結果',
    todos: '待辦事項', none_this_cycle: '本週期無',
    caveats: '資料警告', caveats_sub: '(請先閱讀)',
    caveats_clean: '✓ 資料品質正常 — 本週期無 WARN/FAIL',
    today_by_sev: '當日嚴重度分佈', risk_events: '風險事件',
    severity: '嚴重度', status: '狀態', all: '全部',
    detected: '偵測時間(UTC)', type: '類型', event: '事件',
    operator: '營運商', key_metric: '關鍵指標',
    on_this_date: '筆(當日)', no_events: '無符合事件——乾淨的資料本來就可以是乾淨的。',
    to_review: '筆待複核', monitoring_n: '筆監控(待辦)', show_all: '顯示全部',
    human_review: '人工複核', human_review_sub: '審閱者決定 — 僅記錄，絕不執行',
    rv_decision: '決定', rv_choose: '— 請選擇 —', rv_reviewer: '審閱者', rv_notes: '備註',
    rv_submit: '記錄決定', rv_recorded: '已記錄', rv_none: '尚無人工決定。',
    rv_pick: '請先選擇決定。', rv_saving: '儲存中…', rv_saved: '已儲存。', rv_error: '儲存失敗。',
    players_screened: '篩查玩家數', events_day: '當日事件',
    critical: '嚴重', high: '高', confirmed: '已確認',
    sla_1h: 'SLA:1 小時內', sla_day: 'SLA:當日',
    new_n: '新進', downgraded_after: '經質疑者降級',
    signals_cost: (s, c) => `${s} 個訊號 · 掃描費用 $${c}`,
    new_changed: '新增與異動', vs_standing: (n) => `相對 ${n} 件在案`,
    alerts_breakdown: (o, e, r) => `${o} 新開 · ${e} 升級 · ${r} 重啟`,
    seen_days: (n, a, b) => a === b ? `出現 1 天（${a}）` : `出現 ${n} 天（${a} → ${b}）`,
    expand_digest: '展開全文 ▾', collapse_digest: '收合 ▴',
    look_first: '最優先查看:',
    search_filter: '搜尋與篩選', has_before: '以前發生過嗎?',
    free_text: '關鍵字', risk_type: '風險類型', from: '起', to: '迄',
    search: '搜尋', reset: '重設',
    events_over_time: '事件時間趨勢', trend_sub: '每日件數 · 嚴重+高疊加',
    by_type: '依風險類型', historical: '歷史事件', matches: '筆符合',
    no_hist: '無符合篩選的歷史事件。',
    events_range: '期間事件數', confirm_rate: '確認率',
    of_resolved: '(已結案件中)', mtti: '平均調查時間',
    mtti_sub: '偵測 → 質疑者判定', open_cases: '未結(新進+調查中)',
    on_monitoring: '件監控中', top_operator: '事件最多營運商',
    events_in_range: '筆(期間內)', crit_high: '嚴重+高',
    back: '← 返回', overview: '事件總覽',
    why: '為何被偵測', why_sub: '訊號 · 門檻 · 方法',
    evidence: '證據', ev_rounds: '證據局',
    ev_rounds_sub: '局號 · 日誌可用性',
    ai_inv: 'AI 調查', ai_inv_sub: '經質疑者審查',
    explanations: '可能解釋',
    actions: '建議行動', actions_sub: '絕不含處罰',
    ev_status: '事件狀態', related: '相關事件',
    value: '數值', threshold: '門檻', no_rounds: '未附證據局',
    exp_in: (n) => `${n} 天後到期`, expired_short: '已過期（>30 天）',
    rounds_n: '回合', no_key: '無回合鍵',
    skeptic_disc: '質疑者發現的問題', window: '視窗',
    events_lbl: '事件數', seconds_obs: '觀測秒數', observed: '觀測值',
    suspected_zone: '疑似範圍', suspected: '疑似範圍',
    suspected_pts: (n) => `${n} 個疑似資料點`, autoreport: '自動偵測詳報',
    reference: '基準', pct_group: '組內占比 %',
    trigger_pct: '觸發局占比(組內)', nontrigger_pct: '未觸發局占比(組內)',
    no_chart: '無圖表', free_text_ph: 'uid、營運商、遊戲、事件編號…',
  },
};

/* data-value display maps (filters still send EN values to the API) */
const SEV_ZH = { Critical: '嚴重', High: '高', Medium: '中', Low: '低' };
const STATUS_ZH = {
  new: '新進', queued: '已排入第4層', investigating: '調查中', skeptic_review: '質疑審查',
  confirmed: '已確認', downgraded: '已降級', rejected: '已駁回',
  monitoring: '監控中', closed: '已結案',
};
const TYPE_ZH = {
  'Abnormal RTP': '異常 RTP', 'Advantage Play': '優勢玩家',
  'Feature Buy Abuse': '購買功能濫用', 'Game RTP Shift': '遊戲 RTP 偏移',
  'Suspicious Betting Pattern': '可疑下注模式', 'Bot/Automation': '機器人/自動化',
  'Settlement Anomaly': '結算異常', 'Balance Reconciliation': '餘額對帳',
  'API Anomaly': 'API 異常', 'Game/Platform Defect': '遊戲／平台缺陷',
};

function t(key, ...args) {
  const v = (I18N[LANG] && I18N[LANG][key]) ?? I18N.en[key] ?? key;
  return typeof v === 'function' ? v(...args) : v;
}
function sevLabel(s) { return LANG === 'zh' ? (SEV_ZH[s] || s) : s; }
function statusLabel(s) { return LANG === 'zh' ? (STATUS_ZH[s] || s) : s.replace('_', ' '); }
function typeLabel(tp) { return LANG === 'zh' ? (TYPE_ZH[tp] || tp) : tp; }

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach((el) => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll('.lang-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.lang === LANG);
  });
  document.documentElement.lang = LANG === 'zh' ? 'zh-Hant' : 'en';
}
function localizeValueOptions() {
  document.querySelectorAll('select#f-sev option[value]').forEach((o) => {
    if (o.value) o.textContent = sevLabel(o.value);
  });
  document.querySelectorAll('select#f-status option[value]').forEach((o) => {
    if (o.value) o.textContent = statusLabel(o.value);
  });
  document.querySelectorAll('select#f-type option[value]').forEach((o) => {
    if (o.value) o.textContent = typeLabel(o.value);
  });
}
document.addEventListener('DOMContentLoaded', () => { applyI18n(); localizeValueOptions(); });

/* ---------------- shared ---------------- */
const API = {
  get: async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  },
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function sevChip(s) { return `<span class="chip sev-${s}">${sevLabel(s)}</span>`; }
function statusChip(s) { return `<span class="chip st-${s}">${statusLabel(s)}</span>`; }
function typeChip(tp) { return `<span class="chip type-chip">${typeLabel(tp)}</span>`; }

// Minimal, dependency-free Markdown -> HTML (CSP blocks external libs).
// Handles headings, tables, ordered/unordered lists, hr, blockquote, and the
// inline set (bold/italic/code). Enough for the composed daily digest + case md.
function mdInline(s) {
  return s
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/`([^`]+?)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+?)\*/g, '<em>$1</em>');
}
function mdTableRow(line, cell) {
  const cells = line.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  return '<tr>' + cells.map((c) => `<${cell}>${mdInline(c)}</${cell}>`).join('') + `</tr>`;
}
// Join hard-wrapped source lines into one flowing string. Source reports wrap at
// ~40-80 chars; without joining, every line became its own <p> and the text read
// as short choppy lines that never filled the width. CJK boundaries join with no
// space (Chinese has no word separators); other boundaries join with a space.
function mdJoin(lines) {
  let s = '';
  const cjk = (c) => c && c.charCodeAt(0) > 0x2e7f;   // CJK / full-width punctuation
  for (const ln of lines) {
    const t = ln.trim();
    if (!t) continue;
    if (!s) { s = t; continue; }
    s += (cjk(s[s.length - 1]) && cjk(t[0])) ? t : ' ' + t;
  }
  return s;
}
function md(text) {
  const lines = (text || '').split('\n');
  const out = [];
  let i = 0;
  let para = [];                 // buffered plain-text paragraph lines
  let list = null;              // {type:'ul'|'ol', items:[[line,...],...]}
  const flushPara = () => {
    if (para.length) { out.push(`<p>${mdInline(mdJoin(para))}</p>`); para = []; }
  };
  const flushList = () => {
    if (!list) return;
    out.push(`<${list.type} class="md-list">`);
    for (const it of list.items) out.push(`<li>${mdInline(mdJoin(it))}</li>`);
    out.push(`</${list.type}>`);
    list = null;
  };
  const flushAll = () => { flushPara(); flushList(); };
  while (i < lines.length) {
    const line = lines[i];
    // table: header row followed by a |---|---| separator
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushAll();
      out.push('<div class="md-tablewrap"><table class="md-table"><thead>'
               + mdTableRow(line, 'th') + '</thead><tbody>');
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        out.push(mdTableRow(lines[i], 'td')); i++;
      }
      out.push('</tbody></table></div>');
      continue;
    }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushAll(); out.push(`<h${m[1].length} class="md-h">${mdInline(m[2])}</h${m[1].length}>`);
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      flushAll(); out.push('<hr class="md-hr">');
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      flushPara();
      if (!list || list.type !== 'ul') { flushList(); list = { type: 'ul', items: [] }; }
      list.items.push([m[1]]);
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      flushPara();
      if (!list || list.type !== 'ol') { flushList(); list = { type: 'ol', items: [] }; }
      list.items.push([m[1]]);
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      flushAll(); out.push(`<blockquote class="md-quote">${mdInline(m[1])}</blockquote>`);
    } else if (line.trim() === '') {
      flushAll();
    } else if (list && para.length === 0) {
      list.items[list.items.length - 1].push(line);   // continuation of current list item
    } else {
      para.push(line);                                // continuation of current paragraph
    }
    i++;
  }
  flushAll();
  return out.join('\n');
}

function eventRow(e) {
  return `<tr onclick="location.href='/${LANG}/event/${e.id}'">
    <td class="num">${e.detected_at.replace('T', ' ').replace('Z', '').slice(0, 16)}</td>
    <td>${sevChip(e.severity)}</td>
    <td>${typeChip(e.risk_type)}</td>
    <td class="wrap"><span class="mono">${e.id}</span><br>
        <span style="color:var(--text-secondary)">${e.title}</span>
        ${e.occurrences > 1 ? `<br><span style="color:var(--text-muted);font-size:11px">↻ ${t('seen_days', e.occurrences, e.first_seen, e.last_seen)}</span>` : ''}</td>
    <td>${e.operator}</td>
    <td class="keymetric">${e.headline_metric}</td>
    <td>${statusChip(e.status)}</td>
  </tr>`;
}

if (window.Chart) {
  Chart.defaults.color = css('--text-secondary');
  Chart.defaults.borderColor = 'rgba(99,134,187,0.12)';
  Chart.defaults.font.family = "'Plus Jakarta Sans', system-ui, sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
}
const PALETTE = {
  blue: '#58a6ff', red: '#f85149', amber: '#d29922',
  green: '#3fb950', purple: '#bc8cff', grey: '#8b949e',
};
const SEV_COLORS = { Critical: PALETTE.red, High: PALETTE.amber,
                     Medium: PALETTE.blue, Low: PALETTE.grey };
