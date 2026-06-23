"""TWSE 零股：防禦式欄位偵測 + cache upsert（SPEC §1 表 A / §13）。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_sentiment import twse


def test_parser_handles_chinese_keys(monkeypatch):
    payload = [
        {"證券代號": "2330", "證券名稱": "台積電", "成交股數": "12,345",
         "成交金額": "12,345,000", "成交均價": "1000.0", "日期": "113/06/19"},
        {"Code": "0050", "Name": "ETF", "TradeVolume": "999", "成交均價": "150"},
    ]
    monkeypatch.setattr(twse, "_get_json", lambda url: payload)
    out = twse.fetch_oddlot_snapshot("/x", "https://openapi.twse.com.tw")
    assert out["2330"]["volume"] == 12345
    assert out["2330"]["date"] == date(2024, 6, 19)  # 民國 113 → 西元 2024
    assert out["0050"]["volume"] == 999


def test_empty_endpoint_skips():
    assert twse.fetch_oddlot_snapshot("", "https://x") == {}


def test_cache_upsert_roundtrip(tmp_path):
    twse.upsert_cache(tmp_path, "2330", date(2026, 6, 18),
                      intra={"volume": 100, "value": 200, "avg_price": 2},
                      after={"volume": 50, "value": 250, "avg_price": 5})
    twse.upsert_cache(tmp_path, "2330", date(2026, 6, 19),
                      intra=None, after={"volume": 60, "value": 360, "avg_price": 6})
    # 同日再寫應覆蓋、不重複
    twse.upsert_cache(tmp_path, "2330", date(2026, 6, 19),
                      intra=None, after={"volume": 70, "value": 490, "avg_price": 7})
    df = twse.load_cache(tmp_path, "2330")
    assert len(df) == 2
    row = df[df["date"] == date(2026, 6, 18)].iloc[0]
    assert row["vwap_intra"] == 2.0  # 200/100
    assert row["vwap_after"] == 5.0  # 250/50
    last = df[df["date"] == date(2026, 6, 19)].iloc[0]
    assert last["v_after"] == 70
