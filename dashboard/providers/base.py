"""DataProvider — the contract between the dashboard UI and any data backend.

The UI talks ONLY to this interface. Today it is implemented by MockProvider;
to go live, implement it against the real sources and switch via
DASHBOARD_PROVIDER env var:

    riskdet engine   -> out/candidates.jsonl, output/cases/*.md,
                        output/reports/*.md, output/data_quality/*
    BigQuery         -> riskdet.bq.CostGuardedBQ (drill-down aggregates)
    AI (Layer 4)     -> investigation/skeptic text per case

Every method returns plain JSON-serialisable dicts/lists shaped as documented
below — the UI renders those shapes verbatim, so a real provider only has to
honour the same keys.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

RISK_TYPES = [
    "Abnormal RTP",
    "Advantage Play",
    "Feature Buy Abuse",
    "Game RTP Shift",
    "Suspicious Betting Pattern",
    "Bot/Automation",
    "Settlement Anomaly",
    "Balance Reconciliation",
    "API Anomaly",
    "Game/Platform Defect",
]

SEVERITIES = ["Critical", "High", "Medium", "Low"]

# lifecycle: detection -> investigation -> skeptic -> disposition
STATUSES = ["new", "queued", "investigating", "skeptic_review",
            "confirmed", "downgraded", "rejected", "monitoring", "closed"]


class DataProvider(ABC):
    """All dates are ISO strings (YYYY-MM-DD), times UTC."""

    @abstractmethod
    def daily_summary(self, date: str) -> dict:
        """{date, totals:{events,new,critical,high,confirmed,downgraded},
            by_severity:{sev:count}, by_type:{type:count},
            players_screened, signals, queries_cost_usd, data_caveats:[str]}"""

    @abstractmethod
    def daily_digest(self, date: str, lang: str = "en") -> dict:
        """{date, generated_at, headline, body_md, look_first, decision_items:[str]}
        lang: 'en' | 'zh' -- the AI-written text is served in the requested
        language (a real provider generates or translates per language)."""

    @abstractmethod
    def list_events(self, *, date_from: str | None = None,
                    date_to: str | None = None,
                    severity: str | None = None,
                    risk_type: str | None = None,
                    status: str | None = None,
                    operator: str | None = None,
                    q: str | None = None,
                    limit: int = 100) -> list[dict]:
        """rows: {id, detected_at, risk_type, severity, status, title,
                  operator, uid, game_id, headline_metric}"""

    @abstractmethod
    def get_event(self, event_id: str) -> dict | None:
        """Full detail:
        {id, detected_at, risk_type, severity, status, title, operator, uid,
         game_id, currency, window:{start,end},
         overview:{...key numbers},
         why_detected:[{signal, family, value, threshold, method, description}],
         evidence_charts:[{kind, title, note, data...}],   # kind: bars|line|split|persec
         evidence_rounds:[{gsid, time, note, log_status}],
         ai_investigation:{summary_md, verified:[str], discrepancies:[str]},
         explanations:[{hypothesis, plausibility, note}],
         recommended_actions:[{action, owner, urgency}],
         status_history:[{at, status, by, note}],
         related:[event ids]}"""

    @abstractmethod
    def stats(self, *, date_from: str, date_to: str) -> dict:
        """{daily:[{date, events, critical_high}],
            by_type:{type:count}, by_severity:{sev:count},
            by_status:{status:count}, by_operator:[{operator,count}],
            mtti_hours, confirm_rate}"""
