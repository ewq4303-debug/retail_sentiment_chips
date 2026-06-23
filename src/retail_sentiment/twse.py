"""TWSE 歷史零股行情單（SPEC §1 表 A，解 §13 open item）。

證交所「盤中零股交易行情單(TWTC7U)」與「盤後零股交易行情單(TWT53U)」皆提供
**帶 date 參數的歷史單日查詢**（回傳全市場），故可一次回補整個 lookback 視窗，
再以 cache + markers 避免重複抓取（TWSE 限流，需禮貌請求）。

端點：{base_url}/{REPORT}?response=json&date=YYYYMMDD
欄位採關鍵字模糊比對（中文 fields），不硬編單一 index。
cache：cache/oddlot/{id}.csv（隨 repo commit，逐日累積）。
markers：cache/oddlot/_fetched_{REPORT}.json（已抓過的交易日，含假日空資料，避免重抓）。
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


# ───────────────────────────── HTTP 抓取 ────────────────────────────────────

def _templates(cfg) -> list[str]:
    twse = cfg.twse or {}
    tpls = twse.get("url_templates")
    if tpls:
        return list(tpls)
    base = twse.get("base_url", "https://www.twse.com.tw/exchangeReport")
    return [f"{base.rstrip('/')}/{{report}}?response=json&date={{ymd}}"]


def _format_url(template: str, report: str, day: date) -> str:
    return template.format(report=report, ymd=day.strftime("%Y%m%d"))


def _fetch_json(url: str):
    """回傳解析後的 JSON（dict），非 JSON / HTML / 空 body 回 None。"""
    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def probe_template(cfg, report: str, probe_day: date) -> str | None:
    """探測哪個 URL 模板能回合法 TWSE JSON（含 stat/fields/tables 任一）。回 None 表示全失敗。"""
    for tpl in _templates(cfg):
        try:
            payload = _fetch_json(_format_url(tpl, report, probe_day))
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict) and any(k in payload for k in ("stat", "fields", "tables", "data")):
            return tpl
    return None


def fetch_report_for_date(cfg, report: str, day: date, template: str) -> dict[str, dict]:
    """以選定模板抓單一報表單日（全市場），回傳 {stock_id: {volume, value, avg_price}}。"""
    if not report or not template:
        return {}
    payload = _fetch_json(_format_url(template, report, day))
    return _parse_report(payload) if payload else {}


def _parse_report(payload: dict) -> dict[str, dict]:
    """解析 TWSE JSON：支援 {stat,fields,data} 與 {tables:[{fields,data}]} 兩種。"""
    out: dict[str, dict] = {}
    if not isinstance(payload, dict):
        return out
    tables = payload.get("tables") or [payload]
    for t in tables:
        fields = t.get("fields") or []
        data = t.get("data") or []
        if not fields or not data:
            continue
        i_code = _find_idx(fields, ("證券代號", "股票代號", "代號", "Code"))
        i_vol = _find_idx(fields, ("成交股數", "股數", "Volume"))
        i_val = _find_idx(fields, ("成交金額", "金額", "Value"))
        i_avg = _find_idx(fields, ("成交均價", "均價", "收盤價", "Price"))
        if i_code is None or i_vol is None:
            continue
        for row in data:
            if i_code >= len(row):
                continue
            code = str(row[i_code]).strip()
            if not code.isdigit():
                continue
            out[code] = {
                "volume": _num(row[i_vol]) if i_vol is not None and i_vol < len(row) else np.nan,
                "value": _num(row[i_val]) if i_val is not None and i_val < len(row) else np.nan,
                "avg_price": _num(row[i_avg]) if i_avg is not None and i_avg < len(row) else np.nan,
            }
    return out


# ──────────────────────────── 批次回補（整 universe） ────────────────────────

def backfill_universe(root: Path, cfg, universe: list[str], trading_days: list[date]) -> None:
    """為整個 universe 一次回補 TWSE 零股歷史（每個交易日只抓一次、服務全標的）。

    跳過 markers 已記錄的日；每次執行受 oddlot_backfill_max_days_per_run 限額（限流）。
    cache 較新的日先補（recent first），舊日由後續 cron 慢慢補齊。
    """
    twse = cfg.twse or {}
    intra_report = twse.get("oddlot_intra_report", "")
    after_report = twse.get("oddlot_after_report", "")
    cap = int(twse.get("oddlot_backfill_max_days_per_run", 120))
    delay = float(twse.get("oddlot_request_delay_sec", 0.25))

    days_desc = sorted(set(trading_days), reverse=True)
    # 每檔股票累積待寫入記錄：{stock_id: {date: {intra:.., after:..}}}
    pending: dict[str, dict[date, dict]] = {s: {} for s in universe}

    for report, part in ((intra_report, "intra"), (after_report, "after")):
        if not report:
            continue
        markers = _load_markers(root, report)
        todo = [d for d in days_desc if d.isoformat() not in markers][:cap]
        if not todo:
            continue
        # 先探測可用的 URL 模板（只試一個日期），失敗就整個跳過、不洗版。
        template = probe_template(cfg, report, todo[0])
        if template is None:
            print(f"[twse] {report}: no working endpoint (tried {len(_templates(cfg))} templates) "
                  f"→ skip, odd-lot will use proxy. 請於 config 校正 TWSE.url_templates")
            continue
        ok = 0
        for d in todo:
            try:
                m = fetch_report_for_date(cfg, report, d, template)
            except Exception:  # noqa: BLE001 — 單日網路錯誤：不標記，留待下次重試
                continue
            for s in universe:
                rec = m.get(s)
                if rec:
                    pending[s].setdefault(d, {})[part] = rec
            markers.add(d.isoformat())  # 有回應即標記（假日空資料也算抓過）
            ok += 1
            time.sleep(delay)
        print(f"[twse] {report}: fetched {ok}/{len(todo)} days via {template.split('//')[1].split('/')[0]}")
        _save_markers(root, report, markers)

    for s in universe:
        if pending[s]:
            _merge_into_cache(root, s, pending[s])


def _merge_into_cache(root: Path, stock_id: str, by_date: dict[date, dict]) -> None:
    df = load_cache(root, stock_id)
    rows = []
    for d, parts in by_date.items():
        intra, after = parts.get("intra"), parts.get("after")
        rows.append(
            {
                "date": d,
                "v_intra": (intra or {}).get("volume", np.nan),
                "vwap_intra": _vwap(intra) if intra else np.nan,
                "v_after": (after or {}).get("volume", np.nan),
                "vwap_after": _vwap(after) if after else np.nan,
            }
        )
    new = pd.DataFrame(rows)
    df = df[~df["date"].isin(new["date"])] if not df.empty else df
    df = pd.concat([df, new], ignore_index=True).sort_values("date")
    df.to_csv(_cache_path(root, stock_id), index=False)


# ──────────────────────────── cache / markers ───────────────────────────────

def _cache_dir(root: Path) -> Path:
    d = Path(root) / "cache" / "oddlot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(root: Path, stock_id: str) -> Path:
    return _cache_dir(root) / f"{stock_id}.csv"


def load_cache(root: Path, stock_id: str) -> pd.DataFrame:
    p = _cache_path(root, stock_id)
    cols = ["date", "v_intra", "vwap_intra", "v_after", "vwap_after"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(p)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def upsert_cache(root: Path, stock_id: str, day: date, intra: dict | None,
                 after: dict | None) -> None:
    """單日 upsert（供測試與單檔即時更新）。"""
    rec = {
        "intra": intra,
        "after": after,
    }
    _merge_into_cache(root, stock_id, {day: rec})


def _load_markers(root: Path, report: str) -> set[str]:
    p = _cache_dir(root) / f"_fetched_{report}.json"
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception:  # noqa: BLE001
        return set()


def _save_markers(root: Path, report: str, markers: set[str]) -> None:
    p = _cache_dir(root) / f"_fetched_{report}.json"
    p.write_text(json.dumps(sorted(markers)))


def load_for_index(root: Path, stock_id: str, index) -> pd.DataFrame:
    """回傳對齊到給定交易日 index 的零股欄位（cache 未涵蓋日為 NaN）。"""
    cache = load_cache(root, stock_id)
    if cache.empty:
        return pd.DataFrame(index=index)
    c = cache.set_index("date").sort_index()
    return c.reindex(index)


# ──────────────────────────── 小工具 ────────────────────────────────────────

def _find_idx(fields: list, keys: tuple[str, ...]):
    for i, f in enumerate(fields):
        for k in keys:
            if k in str(f):
                return i
    return None


def _num(v):
    if v is None:
        return np.nan
    try:
        return float(str(v).replace(",", "").replace("--", "").strip() or "nan")
    except ValueError:
        return np.nan


def _vwap(d: dict | None) -> float:
    if not d:
        return np.nan
    vol, val, avg = d.get("volume"), d.get("value"), d.get("avg_price")
    if vol and not np.isnan(vol) and val and not np.isnan(val) and vol > 0:
        return val / vol
    return avg if avg is not None else np.nan
