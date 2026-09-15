"""AI Risk Control Dashboard — Flask app (UI with mock data).

Bi-language by URL prefix:
  /zh/...  Traditional Chinese      /en/...  English
  /        -> redirect /zh/         (zh first per operator preference)

Pages (per language):
  /<lang>/               Page 1: Daily Digest + Risk Events  (What happened?)
  /<lang>/event/<id>     Page 2: Event Detail                (Why?)
  /<lang>/history        Page 3: Historical / Search         (Has it happened before?)

All data flows through dashboard.providers (mock today; real riskdet/BigQuery/
AI later — contract in providers/base.py). APIs are language-neutral except
/api/digest, which accepts ?lang= for the AI-written text.

Run:
    C:/Users/user/AppData/Local/Programs/Python/Python313/python.exe dashboard/app.py
    -> http://127.0.0.1:5050
"""

from __future__ import annotations

import datetime as dt

from flask import Flask, abort, jsonify, redirect, render_template, request

from providers import RISK_TYPES, SEVERITIES, STATUSES, get_provider

app = Flask(__name__)
provider = get_provider()

def _default_date() -> str:
    """Latest scan date the provider actually has, so the UI opens on real data."""
    win = getattr(provider, "_window", None)
    if win and win[1]:
        return win[1][:10]
    return "2026-08-19"

DEFAULT_DATE = _default_date()
LANGS = ("zh", "en")


def _lang_or_404(lang: str) -> str:
    if lang not in LANGS:
        abort(404)
    return lang


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
@app.route("/")
def root():
    return redirect("/zh/")


@app.route("/event/<event_id>")
def legacy_event(event_id: str):
    return redirect(f"/zh/event/{event_id}")


@app.route("/history")
def legacy_history():
    return redirect("/zh/history")


@app.route("/<lang>/")
def page_digest(lang: str):
    return render_template("digest.html", lang=_lang_or_404(lang),
                           default_date=DEFAULT_DATE)


@app.route("/<lang>/event/<event_id>")
def page_event(lang: str, event_id: str):
    _lang_or_404(lang)
    if provider.get_event(event_id) is None:
        abort(404)
    return render_template("event.html", lang=lang, event_id=event_id)


@app.route("/<lang>/history")
def page_history(lang: str):
    return render_template("history.html", lang=_lang_or_404(lang),
                           risk_types=RISK_TYPES, severities=SEVERITIES,
                           statuses=STATUSES, default_date=DEFAULT_DATE)


# --------------------------------------------------------------------------- #
# api
# --------------------------------------------------------------------------- #
@app.route("/api/summary")
def api_summary():
    return jsonify(provider.daily_summary(
        request.args.get("date", DEFAULT_DATE),
        lang=request.args.get("lang", "en")))


@app.route("/api/digest")
def api_digest():
    return jsonify(provider.daily_digest(
        request.args.get("date", DEFAULT_DATE),
        lang=request.args.get("lang", "en")))


@app.route("/api/events")
def api_events():
    a = request.args
    return jsonify(provider.list_events(
        date_from=a.get("from"), date_to=a.get("to"),
        severity=a.get("severity") or None,
        risk_type=a.get("type") or None,
        status=a.get("status") or None,
        operator=a.get("operator") or None,
        q=a.get("q") or None,
        actionable=a.get("actionable") in ("1", "true", "True"),
        limit=int(a.get("limit", 200)),
        lang=a.get("lang", "en")))


@app.route("/api/alerts")
def api_alerts():
    """De-duplicated alert queue for a scan day (state gate): only opened /
    escalated / reopened cases, not the full standing cumulative list."""
    if not hasattr(provider, "alert_queue"):
        return jsonify([])
    return jsonify(provider.alert_queue(
        request.args.get("date", DEFAULT_DATE),
        lang=request.args.get("lang", "en")))


@app.route("/api/events/<event_id>")
def api_event(event_id: str):
    ev = provider.get_event(event_id, lang=request.args.get("lang", "en"))
    if ev is None:
        abort(404)
    return jsonify(ev)


# Human-review disposition: record the reviewer's decision (audit trail). The
# system never acts on it -- enforcement remains 100% human.
_VALID_DECISIONS = {"dismiss_benign", "keep_monitoring", "enhanced_monitoring",
                    "needs_more_evidence", "escalate_to_board", "confirmed_for_action"}


@app.route("/api/events/<event_id>/disposition", methods=["POST"])
def api_disposition(event_id: str):
    if not hasattr(provider, "record_disposition"):
        abort(501)
    body = request.get_json(silent=True) or {}
    decision = (body.get("decision") or "").strip()
    if decision not in _VALID_DECISIONS:
        return jsonify({"error": "invalid decision"}), 400
    ev = provider.record_disposition(
        event_id, decision=decision,
        reviewer=(body.get("reviewer") or "unknown").strip()[:64] or "unknown",
        notes=(body.get("notes") or "").strip()[:2000])
    if ev is None:
        abort(404)
    return jsonify(provider.get_event(event_id, lang=request.args.get("lang", "en")))


@app.route("/api/stats")
def api_stats():
    to = request.args.get("to", DEFAULT_DATE)
    frm = request.args.get(
        "from",
        str(dt.date.fromisoformat(to) - dt.timedelta(days=29)))
    return jsonify(provider.stats(date_from=frm, date_to=to))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5050)
