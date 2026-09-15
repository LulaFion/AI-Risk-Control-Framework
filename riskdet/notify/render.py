"""Message rendering for Telegram.

HTML, not MarkdownV2, on purpose: case ids are `P_{parent}_{game}_{uid}`
(`riskdet/paths.py:45-46`) and every `_` in MarkdownV2 must be backslash-escaped
or the API returns HTTP 400 "can't parse entities". HTML needs only
`html.escape`, which is far harder to get wrong.

Messages stay short enough to read on a lock screen. They carry an identifier and
a link -- never evidence, amounts, or a verdict. The reviewer pulls the detail
from the dashboard behind authentication.
"""
from __future__ import annotations

import html
from typing import Any

_EMOJI = {"Critical": "\U0001F534",   # red circle
          "High": "\U0001F7E0",       # orange circle
          "Medium": "\U0001F7E1",     # yellow circle
          "Low": "\U000026AA"}        # white circle

_ACTION_VERB = {"opened": "opened", "escalated": "escalated",
                "reopened": "reopened after close"}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else "-"), quote=False)


def telegram_html(p: dict) -> str:
    """One alert -> one message body."""
    sev = str(p.get("severity", "?"))
    emoji = _EMOJI.get(sev, "\U000026AB")
    action = _ACTION_VERB.get(str(p.get("action", "")), str(p.get("action", "")))

    lines = [f"{emoji} <b>{_e(sev.upper())}</b> · {_e(action)}",
             f"<code>{_e(p.get('case_id'))}</code>"]

    sla = f"SLA {_e(p.get('sla_label'))}"
    if p.get("sla_due_utc"):
        sla += f" (by {_e(p['sla_due_utc'])})"
    lines.append(f"tier: {_e(p.get('escalation'))} · {sla}")

    fams = p.get("families") or []
    if fams:
        lines.append(f"families: {_e(', '.join(str(f) for f in fams))}")

    lines.append(f"scan: {_e(p.get('scan'))} ({_e(p.get('source'))})")

    if p.get("note"):
        lines.append(_e(p["note"]))

    if p.get("l4_route"):
        lines.append("• auto-queued for AI investigation")

    if p.get("link"):
        lines.append(f"▸ <a href=\"{html.escape(str(p['link']), quote=True)}\">"
                     f"Open case</a>")

    return "\n".join(lines)


def telegram_rollup_html(payloads: list[dict], *, scan: str, source: str,
                         dashboard_url: str, lang: str) -> str:
    """Anti-storm summary.

    A single ETL duplicate wave can open thousands of DUP_ROUND cases in one
    scan, and the case-state gate cannot help on FIRST occurrence -- they are all
    genuinely new. Sending one summary is the right failure mode: the operator
    learns something is very wrong without 1,842 notifications.
    """
    by_sev: dict[str, int] = {}
    for p in payloads:
        sev = str(p.get("severity", "?"))
        by_sev[sev] = by_sev.get(sev, 0) + 1

    order = [s for s in ("Critical", "High", "Medium", "Low") if s in by_sev]
    order += [s for s in sorted(by_sev) if s not in order]
    breakdown = ", ".join(f"{by_sev[s]} {s}" for s in order)
    emoji = _EMOJI.get(order[0] if order else "", "\U000026AB")

    lines = [f"{emoji} <b>{len(payloads)} new alerts</b> in one scan",
             f"{_e(breakdown)}",
             f"scan: {_e(scan)} ({_e(source)})",
             "<i>burst suppressed to one message — review the queue</i>"]
    if dashboard_url:
        url = html.escape(f"{dashboard_url}/{lang}/", quote=True)
        lines.append(f"▸ <a href=\"{url}\">Open review queue</a>")
    return "\n".join(lines)
