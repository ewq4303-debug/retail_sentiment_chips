"""TWSE 零股：防禦式欄位偵測 + cache upsert（SPEC §1 表 A / §13）。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_sentiment import twse


def test_parser_handles_twse_fields_data():
    # TWSE JSON：{stat, fields, data}，數字帶千分位逗號
    payload = {
        "stat": "OK",
        "date": "20240619",
        "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "成交均價"],
        "data": [
            ["2330", "台積電", "12,345", "100", "12,345,000", "1000.0"],
            ["0050", "元大台灣50", "999", "10", "149,850", "150.0"],
            ["合計", "", "", "", "", ""],  # 非數字代號應被忽略
        ],
    }
    out = twse._parse_report(payload)
    assert out["2330"]["volume"] == 12345
    assert out["2330"]["value"] == 12345000
    assert out["0050"]["volume"] == 999
    assert "合計" not in out


def test_parser_handles_tables_wrapper():
    payload = {"tables": [{
        "fields": ["證券代號", "成交股數", "成交金額"],
        "data": [["2317", "5,000", "500,000"]],
    }]}
    out = twse._parse_report(payload)
    assert out["2317"]["volume"] == 5000


def test_vwap_prefers_value_over_avg():
    assert twse._vwap({"volume": 100, "value": 250, "avg_price": 9}) == 2.5
    assert twse._vwap({"volume": float("nan"), "value": float("nan"), "avg_price": 9}) == 9


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
