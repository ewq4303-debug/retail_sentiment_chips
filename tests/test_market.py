"""整體市場散戶指標 aggregate（market.py）。"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_sentiment import market


def _row(snap, sid, tmin, tmax, shares):
    return {"snapshot_date": snap, "stock_id": sid, "tier_min": tmin,
            "tier_max": tmax, "holders": 1, "shares": shares, "pct": 0.0}


def test_market_rows_equal_weight_mean_and_filters():
    snap = date(2026, 6, 18)
    rows = [
        # 2330: 散戶(≤5000) 200, 大戶(≥1,000,001) 800, total 1000 → sr=20%, hb=80%
        _row(snap, "2330", 1, 999, 200), _row(snap, "2330", 1000001, 10**12, 800),
        # 1101: 散戶 400, 大戶 600, total 1000 → sr=40%, hb=60%
        _row(snap, "1101", 1, 5000, 400), _row(snap, "1101", 1000001, 10**12, 600),
        # 0050 (ETF, 00 開頭) 應被排除
        _row(snap, "0050", 1, 999, 999), _row(snap, "0050", 1000001, 10**12, 1),
    ]
    df = market.market_rows(pd.DataFrame(rows))
    assert len(df) == 1
    r = df.iloc[0]
    assert r["n_stocks"] == 2                 # ETF 被排除
    assert abs(r["sr_pct"] - 30.0) < 1e-9     # (20+40)/2
    assert abs(r["hb_pct"] - 70.0) < 1e-9     # (80+60)/2


def test_build_json_computes_momentum(tmp_path):
    snaps = [date(2026, 6, 5), date(2026, 6, 12), date(2026, 6, 18)]
    all_rows = []
    for i, snap in enumerate(snaps):
        big = 600 + i * 50   # 大戶逐週增 → 籌碼集中
        all_rows += [_row(snap, "1101", 1, 5000, 400), _row(snap, "1101", 1000001, 10**12, big)]
    market.update_cache(tmp_path, pd.DataFrame(all_rows))
    out = tmp_path / "out"
    out.mkdir()
    assert market.build_json(tmp_path, out)
    import json
    m = json.loads((out / "market.json").read_text())
    assert len(m["weekly"]) == 3
    assert m["weekly"][0]["scissor_momentum"] is None       # 首週無前值
    assert m["latest"]["tilt"] == "大戶集中"                  # 大戶比上升
