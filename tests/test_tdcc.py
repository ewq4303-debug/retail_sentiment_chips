"""TDCC opendata 集保股權分散表正規化（SPEC §1 表 C）。"""
import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_sentiment import tdcc


def test_normalize_maps_levels_and_skips_total():
    csv = (
        "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
        "20260619,2330,1,100000,50000000,2.5\n"      # 級1 1-999
        "20260619,2330,2,80000,300000000,15.0\n"     # 級2 1,000-5,000
        "20260619,2330,15,50,1000000000,50.0\n"      # 級15 1,000,001↑
        "20260619,2330,16,0,500,0.0\n"               # 差異調整 → 跳過
        "20260619,2330,17,180050,1350000500,100.0\n" # 合計 → 跳過
    )
    raw = pd.read_csv(io.StringIO(csv))
    df = tdcc._normalize(raw)
    assert set(df["tier_min"]) == {1, 1000, 1000001}
    assert df[df["tier_min"] == 1].iloc[0]["tier_max"] == 999
    assert df["snapshot_date"].iloc[0] == date(2026, 6, 19)
    assert 16 not in df["tier_min"].values and len(df) == 3


def test_cache_roundtrip(tmp_path):
    csv = (
        "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
        "20260619,2330,2,80000,300000000,15.0\n"
    )
    df = tdcc._normalize(pd.read_csv(io.StringIO(csv)))
    tdcc._upsert_cache(tmp_path, "2330", df[df["stock_id"] == "2330"])
    loaded = tdcc.load_cache(tmp_path, "2330")
    assert loaded["snapshot_date"].iloc[0] == date(2026, 6, 19)
    assert loaded["shares"].iloc[0] == 300000000
