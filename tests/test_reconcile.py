"""SPEC §12.1 / §3.2：流量↔存量對帳（分配後 Σ signed_flow == ΔStock）。"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_sentiment import arbitrage, direction, reconcile
from retail_sentiment.calendar_utils import TradingCalendar
from retail_sentiment.config import Config


def _make_daily(days):
    rng = np.random.default_rng(1)
    n = len(days)
    df = pd.DataFrame(
        {
            "v_whole": rng.uniform(1e6, 5e6, n),
            "vwap_whole": 100 + rng.normal(0, 1, n).cumsum(),
            "ret": rng.normal(0, 0.01, n),
            "inst_net": rng.normal(0, 1e4, n),
            "v_intra": rng.uniform(1e4, 5e4, n),
            "v_after": rng.uniform(5e3, 2e4, n),
        },
        index=days,
    )
    df["vwap_intra"] = df["vwap_whole"]
    df["vwap_after"] = df["vwap_whole"]
    return df


def test_distribution_reconciles_exactly():
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(28) if (date(2026, 6, 1) + timedelta(days=i)).weekday() < 5]
    cal = TradingCalendar(days)
    daily = _make_daily(days)
    cfg = Config()

    arb = arbitrage.compute_w_arb(daily, cfg)
    T = arbitrage.turnover_T(daily, arb["w_arb"])

    # 兩個快照（週五），散戶股數變化已知
    snaps = [d for d in days if d.weekday() == 4][:3]
    retail_stock = pd.DataFrame(
        {
            "total_shares": [1e9, 1e9, 1e9],
            "retail_shares_adj": [3.0e8, 2.95e8, 3.02e8],
            "big_shares_high_adj": [5e8, 5e8, 5e8],
            "big_shares_low_adj": [4e8, 4e8, 4e8],
            "adj_factor": [1.0, 1.0, 1.0],
            "sr_pct": [30, 29.5, 30.2],
            "hb_high_pct": [50, 50, 50],
            "hb_low_pct": [40, 40, 40],
        },
        index=pd.Index(snaps, name="snapshot_date"),
    )

    flow = direction.settled_signed_flow(daily, T, retail_stock, cal, cfg)
    rec = reconcile.reconcile_weeks(flow, retail_stock, cfg)

    assert rec, "should have at least one settled week"
    for snap, r in rec.items():
        assert r["reconcile_ok"], f"{snap} failed reconcile: {r}"
        assert r["resid_rel"] is None or r["resid_rel"] < 1e-6


def test_signed_flow_sign_comes_from_stock():
    # ΔStock 為負 → 該週 signed_flow 總和為負（方向來自存量）
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(21) if (date(2026, 6, 1) + timedelta(days=i)).weekday() < 5]
    cal = TradingCalendar(days)
    daily = _make_daily(days)
    cfg = Config()
    arb = arbitrage.compute_w_arb(daily, cfg)
    T = arbitrage.turnover_T(daily, arb["w_arb"])
    snaps = [d for d in days if d.weekday() == 4][:2]
    retail_stock = pd.DataFrame(
        {
            "total_shares": [1e9, 1e9],
            "retail_shares_adj": [3.0e8, 2.9e8],  # 散戶減 → 負
            "big_shares_high_adj": [5e8, 5e8],
            "big_shares_low_adj": [4e8, 4e8],
            "adj_factor": [1.0, 1.0],
            "sr_pct": [30, 29], "hb_high_pct": [50, 50], "hb_low_pct": [40, 40],
        },
        index=pd.Index(snaps, name="snapshot_date"),
    )
    flow = direction.settled_signed_flow(daily, T, retail_stock, cal, cfg)
    settled = flow[~flow["provisional"]]
    assert settled["signed_flow"].sum() < 0
