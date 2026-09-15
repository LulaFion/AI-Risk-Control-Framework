"""Case store + state-transition gate -- turns per-scan candidates into stateful cases.

WHY THIS EXISTS
    A scan (Layer 1-3 fuse) is *stateless*: it re-emits every candidate that
    currently crosses threshold. Run daily, one real finding re-appears every
    day; run hourly, 24x/day; run per-minute, ~1,440x/day. Without state, one
    anomaly becomes thousands of duplicate alerts and the human queue is unusable.

WHAT THIS DOES
    Sits UNDER any scan cadence and decides, per candidate, whether this is:
        opened     -- first time this case crosses threshold        -> ALERT
        escalated  -- materially worse than when last surfaced       -> ALERT
        reopened   -- recurred after being closed, or new evidence
                      after a human disposition                      -> ALERT
        ongoing    -- still true, nothing materially new             -> suppressed
        closed     -- absent for `close_after_absent` scans          -> (state only)
    Only opened / escalated / reopened reach the human queue. This DECOUPLES the
    scan cadence (how often we look -> fast detection) from the alert cadence
    (state changes -> what a human sees).

HIGH-FREQUENCY KNOBS (the same gate serves daily and per-minute)
    min_persistence      -- a candidate must appear in >= N consecutive scans
                            before it OPENS. Debounces one-scan blips at minute
                            cadence. Default 1 (daily: open immediately).
    close_after_absent   -- scans a case may be absent before it CLOSES. Higher
                            values give hysteresis so a case hovering at the
                            threshold does not flap open/closed. Default 1.

MATERIAL CHANGE (what counts as "worse" -> re-alert)
    - escalation tier increased (monitor < watch/enhanced < human_review), or
    - severity increased (Low < Medium < High < Critical), or
    - a signal FAMILY fired that this case had never shown before.
    Pure value drift (a z-score wobbling) is NOT material -> stays suppressed.

DECISION BOUNDARY
    Suppression is about *alert de-duplication*, never about hiding evidence.
    Every case stays in the persistent store with its full transition history;
    a suppressed 'ongoing' case is still visible in the audit trail. The gate
    never enforces, closes an investigation, or acts on a disposition -- it only
    decides what is *new enough* to surface. Enforcement remains 100% human.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Iterable

# --- ordinal scales for "is this worse than before?" ----------------------- #
ESCALATION_TIER = {"monitor": 1, "watch": 2, "enhanced": 2, "human_review": 3}
SEVERITY_RANK = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}

# Recorded human decisions that ACKNOWLEDGE a case -> repeats are suppressed
# until something materially new happens. (The gate respects the disposition;
# it does not act on it.) A dismissed case should not nag every scan.
ACK_DISPOSITIONS = {
    "dismiss_benign", "keep_monitoring", "enhanced_monitoring",
    "needs_more_evidence", "escalate_to_board", "confirmed_for_action",
}
ALERT_ACTIONS = ("opened", "escalated", "reopened")   # what reaches the queue

# The escalation tier at (or above) which a case is AUTOMATICALLY routed into the
# Layer-4 investigation queue. human_review is the tier fusion assigns to a case
# with >=2 independent families (or the exceptional single-family route); those
# are the only cases the agent team (player-investigator -> skeptic) should spend
# Cloud Logging queries and LLM tokens on. Routing is deterministic here; whether
# a human_review case auto-enters is NOT an agent decision.
L4_AUTO_TIER = ESCALATION_TIER["human_review"]


@dataclasses.dataclass
class CaseState:
    case_id: str
    status: str = "pending"          # pending|open|monitoring|closed
    first_seen: str = ""             # scan_id of first appearance
    opened_scan: str = ""            # scan_id it crossed min_persistence
    last_seen: str = ""             # scan_id of most recent appearance
    last_alert_scan: str = ""        # scan_id we last surfaced it to a human
    seen_scans: int = 0              # consecutive scans present (for min_persistence)
    absent_scans: int = 0            # consecutive scans absent (for close hysteresis)
    max_tier: int = 0                # highest escalation tier ever seen
    max_sev: int = 0                 # highest severity rank ever seen
    families_ever: list = dataclasses.field(default_factory=list)
    l4_status: str = ""              # ""|queued|investigated -- Layer-4 routing
    l4_queued_scan: str = ""         # scan_id the case auto-entered Layer 4
    disposition: str = ""            # last recorded human decision, if any
    context: dict = dataclasses.field(default_factory=dict)   # uid/parent/game_id/currency
    history: list = dataclasses.field(default_factory=list)   # [{scan,action,note}]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CaseState":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})


def fingerprint(cand: dict) -> dict:
    """The comparable shape of a candidate: tier, severity, families."""
    return {
        "tier": ESCALATION_TIER.get(cand.get("escalation", ""), 1),
        "sev": SEVERITY_RANK.get(cand.get("severity", ""), 1),
        "families": sorted(set(cand.get("families", []))),
    }


class CaseStore:
    """Persistent, cadence-agnostic case state. One instance per detection scope."""

    def __init__(self, path: str | pathlib.Path,
                 *, min_persistence: int = 1, close_after_absent: int = 1) -> None:
        self.path = pathlib.Path(path)
        self.min_persistence = max(1, int(min_persistence))
        self.close_after_absent = max(1, int(close_after_absent))
        self.cases: dict[str, CaseState] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.cases = {k: CaseState.from_dict(v) for k, v in raw.get("cases", {}).items()}

    # ------------------------------------------------------------------ #
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"min_persistence": self.min_persistence,
             "close_after_absent": self.close_after_absent,
             "cases": {k: v.to_dict() for k, v in self.cases.items()}},
            indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------ #
    def ingest(self, scan_id: str, candidates: Iterable[dict],
               *, dispositions: dict | None = None) -> list[dict]:
        """Feed one scan's candidates through the gate.

        Returns the list of ACTIONABLE transitions (opened/escalated/reopened)
        -- i.e. the de-duplicated alert queue for this scan. State is updated in
        place; call save() to persist. `dispositions` maps case_id -> decision
        string (the human review the dashboard recorded).
        """
        dispositions = dispositions or {}
        seen_ids = set()
        alerts: list[dict] = []

        for cand in candidates:
            cid = cand["case_id"]
            seen_ids.add(cid)
            fp = fingerprint(cand)
            disp = dispositions.get(cid, "")
            st = self.cases.get(cid)

            if st is None:                                   # brand new case
                st = CaseState(case_id=cid, first_seen=scan_id)
                self.cases[cid] = st
            st.disposition = disp or st.disposition
            st.absent_scans = 0
            st.last_seen = scan_id
            st.context = {k: cand.get(k) for k in
                          ("uid", "parent", "game_id", "currency", "risk_type")
                          if cand.get(k) is not None} or st.context

            new_families = [f for f in fp["families"] if f not in st.families_ever]
            worse_tier = fp["tier"] > st.max_tier
            worse_sev = fp["sev"] > st.max_sev
            material = bool(new_families) or worse_tier or worse_sev

            action = None
            note = ""
            if st.status == "pending":
                st.seen_scans += 1
                if st.seen_scans >= self.min_persistence:
                    action = "opened"
                    st.status = "open" if fp["tier"] >= ESCALATION_TIER["human_review"] \
                        else "monitoring"
                    st.opened_scan = scan_id
                    note = f"opened at {cand.get('escalation','?')}/{cand.get('severity','?')}"
                # else: still debouncing -> not surfaced yet
            elif st.status == "closed":
                # recurred after being closed -> a fresh alert
                action = "reopened"
                st.status = "open" if fp["tier"] >= ESCALATION_TIER["human_review"] \
                    else "monitoring"
                note = "recurred after close"
            elif disp in ACK_DISPOSITIONS and not material:
                # human already acknowledged it and nothing new -> suppress
                pass
            elif disp in ACK_DISPOSITIONS and material:
                action = "reopened"
                note = f"new evidence after '{disp}': " + _why(new_families, worse_tier, worse_sev)
            elif material:
                action = "escalated"
                note = _why(new_families, worse_tier, worse_sev)
            # else: ongoing, no disposition, nothing material -> suppress

            # roll the running maxima AFTER deciding materiality
            st.max_tier = max(st.max_tier, fp["tier"])
            st.max_sev = max(st.max_sev, fp["sev"])
            for f in new_families:
                st.families_ever.append(f)

            if action:
                st.last_alert_scan = scan_id
                rec = {"scan": scan_id, "action": action, "note": note}
                st.history.append(rec)
                # AUTO-ROUTE to Layer 4: a human_review-tier alert enters the
                # investigation queue automatically. Deduplicated by state -- an
                # ongoing human_review case (no alert this scan) is NOT re-queued;
                # a reopened one IS (new evidence warrants re-investigation).
                l4_route = False
                if fp["tier"] >= L4_AUTO_TIER and (
                        st.l4_status != "investigated" or action == "reopened"):
                    st.l4_status = "queued"
                    st.l4_queued_scan = scan_id
                    st.history.append({"scan": scan_id, "action": "layer4_queued",
                                       "note": f"auto-routed to Layer 4 ({action})"})
                    l4_route = True
                alerts.append({
                    "case_id": cid, "action": action, "scan": scan_id,
                    "escalation": cand.get("escalation"), "severity": cand.get("severity"),
                    "families": fp["families"], "note": note,
                    "l4_route": l4_route, **st.context})

        # cases NOT seen this scan: age them, close on hysteresis expiry
        drop: list[str] = []
        for cid, st in self.cases.items():
            if cid in seen_ids or st.status == "closed":
                continue
            st.absent_scans += 1
            if st.status == "pending":
                # never crossed min_persistence -> a debounce blip, not a real
                # case. Forget it once it stops persisting; a later reappearance
                # starts the persistence count fresh (does NOT count as reopen).
                st.seen_scans = 0
                if st.absent_scans >= self.close_after_absent:
                    drop.append(cid)
                continue
            if st.absent_scans >= self.close_after_absent:
                st.status = "closed"
                st.history.append({"scan": scan_id, "action": "closed",
                                   "note": f"absent {st.absent_scans} scan(s)"})
        for cid in drop:
            del self.cases[cid]
        return alerts

    # ------------------------------------------------------------------ #
    def open_cases(self) -> list[CaseState]:
        """Cases currently open/monitoring (the live queue), newest alert first."""
        live = [s for s in self.cases.values() if s.status in ("open", "monitoring")]
        return sorted(live, key=lambda s: s.last_alert_scan or s.opened_scan, reverse=True)

    def layer4_queue(self) -> list[CaseState]:
        """Cases auto-routed to Layer 4 and not yet investigated (the work list
        the player-investigator/skeptic agents consume), newest first."""
        q = [s for s in self.cases.values() if s.l4_status == "queued"]
        return sorted(q, key=lambda s: s.l4_queued_scan, reverse=True)

    def mark_investigated(self, case_id: str, scan_id: str = "") -> None:
        """Record that Layer 4 has produced a case file for a QUEUED case, so it
        leaves the pending queue. Only a case that was actually auto-routed
        (l4_status == 'queued') is cleared -- a case file for a never-routed
        (e.g. monitor-tier) case does not count as an investigation. Idempotent:
        safe to call every scan (an already-investigated case is skipped)."""
        st = self.cases.get(case_id)
        if st is None or st.l4_status != "queued":
            return
        st.l4_status = "investigated"
        st.history.append({"scan": scan_id, "action": "layer4_investigated",
                           "note": "Layer-4 case file on record"})


def _why(new_families: list, worse_tier: bool, worse_sev: bool) -> str:
    bits = []
    if worse_tier:
        bits.append("escalation tier increased")
    if worse_sev:
        bits.append("severity increased")
    if new_families:
        bits.append("new signal family: " + ", ".join(new_families))
    return "; ".join(bits) or "material change"
