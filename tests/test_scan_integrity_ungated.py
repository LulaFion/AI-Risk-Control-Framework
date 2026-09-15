"""Production scan must run ABSOLUTE integrity rules on an UNGATED player_cell
frame, while statistical rules keep the >=min_rounds floor. Regression guard for
the fix that a low-volume max-multiple breach was silently dropped in production
(only the w1 sim passed integrity_pc; the real scan did not)."""
import datetime as _dt
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import riskdet  # noqa: E402
from riskdet.pipeline import run as R  # noqa: E402


def test_scan_passes_ungated_integrity_frame(monkeypatch):
    cfg = riskdet.settings()
    floor = int(cfg.thresholds["global"]["min_rounds_per_cell"])

    # one sub-floor cell (a low-volume breach) + one high-volume cell
    pc_full = pd.DataFrame([
        {"parent": "op", "uid": "low", "game_id": "1049s", "play_type": 0,
         "currency": "MMK", "sm_tag": "x", "rounds": 5,
         "first_round_utc": pd.Timestamp("2026-08-01")},
        {"parent": "op", "uid": "big", "game_id": "1049s", "play_type": 0,
         "currency": "MMK", "sm_tag": "x", "rounds": floor + 300,
         "first_round_utc": pd.Timestamp("2026-08-01")},
    ])
    captured = {}

    def fake_l1(cfg, catalog, frozen, metrics, pc, pt, cc, *,
                integrity_pc=None, **kw):
        captured["stat_rounds"] = sorted(pc.rounds.tolist())
        captured["integrity_rounds"] = (
            sorted(integrity_pc.rounds.tolist()) if integrity_pc is not None else None)
        return []

    class _Val:
        verdict = "OK"

    class FakeBQ:
        pass
    FakeBQ.cfg = cfg
    FakeBQ.query_df = lambda self, *a, **k: pd.DataFrame()   # empty game-day series

    monkeypatch.setattr(R, "load_catalog", lambda p: type("Cat", (), {"verdict": "PASS"})())
    monkeypatch.setattr(R.C, "load_frozen", lambda c: object())
    monkeypatch.setattr(R.C, "build_metrics", lambda *a, **k: [])
    monkeypatch.setattr(R, "extract", lambda *a, **k: "ARTS")
    monkeypatch.setattr(R, "_load", lambda arts: (pc_full.copy(), pd.DataFrame(), pd.DataFrame()))
    monkeypatch.setattr(R, "_cell_operator", lambda bq, as_of: pd.DataFrame())
    monkeypatch.setattr(R, "run_layer1", fake_l1)
    monkeypatch.setattr(R, "run_layer2", lambda *a, **k: [])
    monkeypatch.setattr(R, "detect_cohorts", lambda *a, **k: [])
    monkeypatch.setattr(R, "membership", lambda p: {})
    monkeypatch.setattr(R, "run_ml_discovery", lambda *a, **k: ([], _Val()))
    monkeypatch.setattr(R, "fuse", lambda *a, **k: [])
    monkeypatch.setattr(R, "emit", lambda *a, **k: {})

    R.scan(FakeBQ(), _dt.datetime(2026, 8, 8))

    # integrity sees BOTH cells (ungated); statistics see only the >=floor cell
    assert captured["integrity_rounds"] == [5, floor + 300]
    assert captured["stat_rounds"] == [floor + 300]
