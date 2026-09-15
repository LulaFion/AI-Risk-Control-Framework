"""Behavioural tests for the case-store state gate (cadence-agnostic)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from riskdet.casestore import CaseStore, ALERT_ACTIONS   # noqa: E402


def _c(cid, esc="monitor", sev="Low", fams=("OUTCOME_MAGNITUDE",), **kw):
    return {"case_id": cid, "escalation": esc, "severity": sev,
            "families": list(fams), **kw}


def test_opened_once_then_suppressed(tmp_path):
    """The core promise: same candidate every scan -> exactly ONE alert."""
    s = CaseStore(tmp_path / "cs.json")
    actions = []
    for day in range(1, 11):                       # 10 identical daily scans
        a = s.ingest(f"d{day}", [_c("X")])
        actions.append([x["action"] for x in a])
    assert actions[0] == ["opened"]
    assert all(a == [] for a in actions[1:]), actions   # 9 days suppressed


def test_escalation_realerts(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    assert [x["action"] for x in s.ingest("d1", [_c("X", "monitor", "Low")])] == ["opened"]
    assert s.ingest("d2", [_c("X", "monitor", "Low")]) == []          # ongoing
    a = s.ingest("d3", [_c("X", "human_review", "High")])             # tier + sev up
    assert [x["action"] for x in a] == ["escalated"]
    assert "escalation tier increased" in a[0]["note"]
    assert s.ingest("d4", [_c("X", "human_review", "High")]) == []    # ongoing again


def test_new_family_is_material(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    s.ingest("d1", [_c("X", fams=("OUTCOME_MAGNITUDE",))])
    a = s.ingest("d2", [_c("X", fams=("OUTCOME_MAGNITUDE", "INTEGRITY"))])
    assert [x["action"] for x in a] == ["escalated"]
    assert "INTEGRITY" in a[0]["note"]


def test_disposition_suppresses_then_reopens_on_material(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    s.ingest("d1", [_c("X")])
    # human dismisses it as benign; a plain repeat must NOT re-alert
    assert s.ingest("d2", [_c("X")], dispositions={"X": "dismiss_benign"}) == []
    assert s.ingest("d3", [_c("X")], dispositions={"X": "dismiss_benign"}) == []
    # but genuinely new evidence overrides the disposition -> reopened
    a = s.ingest("d4", [_c("X", "human_review", "Critical")],
                 dispositions={"X": "dismiss_benign"})
    assert [x["action"] for x in a] == ["reopened"]
    assert "after 'dismiss_benign'" in a[0]["note"]


def test_close_and_reopen_on_recurrence(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    s.ingest("d1", [_c("X")])
    s.ingest("d2", [])                              # absent -> closes (close_after_absent=1)
    assert s.cases["X"].status == "closed"
    a = s.ingest("d3", [_c("X")])                   # recurs
    assert [x["action"] for x in a] == ["reopened"]


def test_min_persistence_debounces_blips(tmp_path):
    """High-frequency: a one-scan blip must not page until it persists."""
    s = CaseStore(tmp_path / "cs.json", min_persistence=3)
    assert s.ingest("t1", [_c("X")]) == []         # pending
    assert s.ingest("t2", [_c("X")]) == []         # pending
    a = s.ingest("t3", [_c("X")])                  # 3rd consecutive -> opens
    assert [x["action"] for x in a] == ["opened"]


def test_blip_resets_persistence(tmp_path):
    s = CaseStore(tmp_path / "cs.json", min_persistence=3)
    s.ingest("t1", [_c("X")])
    s.ingest("t2", [])                              # gap -> streak resets
    s.ingest("t3", [_c("X")])
    s.ingest("t4", [_c("X")])
    assert s.cases["X"].status == "pending"        # only 2 consecutive since reset
    a = s.ingest("t5", [_c("X")])
    assert [x["action"] for x in a] == ["opened"]


def test_close_hysteresis(tmp_path):
    """close_after_absent>1: a single missed scan must not close (anti-flap)."""
    s = CaseStore(tmp_path / "cs.json", close_after_absent=3)
    s.ingest("t1", [_c("X")])
    s.ingest("t2", [])
    s.ingest("t3", [])
    assert s.cases["X"].status != "closed"         # only 2 absent
    assert s.ingest("t4", [_c("X")]) == []         # returns -> still ongoing, no re-alert
    assert s.cases["X"].status == "monitoring"


def test_persistence_survives_reload(tmp_path):
    p = tmp_path / "cs.json"
    CaseStore(p).ingest_and_save = None
    s = CaseStore(p); s.ingest("d1", [_c("X")]); s.save()
    s2 = CaseStore(p)                              # fresh instance, same file
    assert s2.ingest("d2", [_c("X")]) == []        # remembers X was opened
    assert s2.cases["X"].status == "monitoring"


def test_alert_actions_constant():
    assert set(ALERT_ACTIONS) == {"opened", "escalated", "reopened"}


# --- Layer-4 auto-routing ------------------------------------------------- #
def test_human_review_auto_enters_layer4(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    a = s.ingest("d1", [_c("X", "human_review", "High",
                           fams=("INTEGRITY", "OUTCOME_MAGNITUDE"))])
    assert a[0]["l4_route"] is True
    assert [q.case_id for q in s.layer4_queue()] == ["X"]
    assert s.cases["X"].l4_status == "queued"


def test_monitor_tier_does_not_enter_layer4(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    a = s.ingest("d1", [_c("X", "monitor", "Low")])
    assert a[0]["l4_route"] is False
    assert s.layer4_queue() == []


def test_escalation_into_human_review_routes_once(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    s.ingest("d1", [_c("X", "monitor", "Low")])                 # opened, not L4
    assert s.layer4_queue() == []
    a = s.ingest("d3", [_c("X", "human_review", "High",
                           fams=("INTEGRITY", "OUTCOME_MAGNITUDE"))])
    assert a[0]["l4_route"] is True                             # escalation routes it
    assert [q.case_id for q in s.layer4_queue()] == ["X"]
    # ongoing human_review next scan -> NOT re-queued (dedup)
    s.ingest("d4", [_c("X", "human_review", "High",
                       fams=("INTEGRITY", "OUTCOME_MAGNITUDE"))])
    assert len(s.layer4_queue()) == 1


def test_mark_investigated_clears_queue_and_no_requeue(tmp_path):
    s = CaseStore(tmp_path / "cs.json")
    s.ingest("d1", [_c("X", "human_review", "High",
                       fams=("INTEGRITY", "OUTCOME_MAGNITUDE"))])
    s.mark_investigated("X", "d1")
    assert s.layer4_queue() == []
    # a plain ongoing repeat must not re-queue an investigated case
    s.ingest("d2", [_c("X", "human_review", "High",
                       fams=("INTEGRITY", "OUTCOME_MAGNITUDE"))])
    assert s.layer4_queue() == []
    # but a reopen (new evidence after disposition) DOES re-queue for re-investigation
    a = s.ingest("d3", [_c("X", "human_review", "Critical",
                           fams=("INTEGRITY", "OUTCOME_MAGNITUDE", "TIMING"))],
                 dispositions={"X": "dismiss_benign"})
    assert a[0]["action"] == "reopened" and a[0]["l4_route"] is True
    assert [q.case_id for q in s.layer4_queue()] == ["X"]
