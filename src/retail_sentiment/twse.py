"""TWSE OpenAPI 真實零股資料（SPEC §1 表 A，解 §13 open item）。

關鍵限制：TWSE OpenAPI 只回「最新一個交易日」快照、**無歷史**。
因此本模組每次執行抓當日快照、**累積寫入 cache CSV**（隨 repo commit），
pipeline 由 cache 組出歷史時序；cache 未涵蓋的舊日以整股量比例合成代理並標旗標。

欄位採「防禦式偵測」：TWSE 各報表 JSON key 命名不一（中文/英文、⚠️待確認），
故以關鍵字模糊比對 code / 成交股數 / 成交金額 / 均價，不硬編單一 key。
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


# ───────────────────────────── HTTP 抓取 ────────────────────────────────────

def _get_json(url: str) -> list[dict]:
    for attempt in range(4):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    return []


def fetch_oddlot_snapshot(endpoint: str, base_url: str) -> dict[str, dict]:
    """抓一個 OpenAPI 零股端點，回傳 {stock_id: {date, volume, value, avg_price}}。

    端點為空字串時回傳 {}（呼叫端可略過盤中）。
    """
    if not endpoint:
        return {}
    rows = _get_json(base_url.rstrip("/") + endpoint)
    out: dict[str, dict] = {}
    snap_date = _infer_snapshot_date(rows)
    for row in rows:
        code = _pick(row, ("Code", "證券代號", "股票代號", "代號"))
        if not code:
            continue
        code = str(code).strip()
        if not code.isdigit():
            continue
        vol = _num(_pick(row, ("成交股數", "TradeVolume", "Volume", "成交數量")))
        val = _num(_pick(row, ("成交金額", "TradeValue", "Value")))
        avg = _num(_pick(row, ("成交均價", "均價", "ClosingPrice", "收盤價", "Price")))
        out[code] = {"date": snap_date, "volume": vol, "value": val, "avg_price": avg}
    return out


def _infer_snapshot_date(rows: list[dict]) -> date | None:
    """部分 TWSE 端點不帶日期欄；找得到就用，否則回傳 None（呼叫端填今天）。"""
    for row in rows[:1]:
        raw = _pick(row, ("Date", "日期", "資料日期"))
        if raw:
            return _roc_or_iso_to_date(str(raw))
    return None


# ──────────────────────────── cache 持久化 ──────────────────────────────────

def _cache_path(root: Path, stock_id: str) -> Path:
    d = Path(root) / "cache" / "oddlot"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stock_id}.csv"


def load_cache(root: Path, stock_id: str) -> pd.DataFrame:
    p = _cache_path(root, stock_id)
    if not p.exists():
        return pd.DataFrame(columns=["date", "v_intra", "vwap_intra", "v_after", "vwap_after"])
    df = pd.read_csv(p)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def upsert_cache(root: Path, stock_id: str, day: date, intra: dict | None,
                 after: dict | None) -> None:
    """把當日盤中/盤後零股 upsert 進 cache CSV。"""
    df = load_cache(root, stock_id)
    rec = {"date": day, "v_intra": np.nan, "vwap_intra": np.nan,
           "v_after": np.nan, "vwap_after": np.nan}
    if intra:
        rec["v_intra"] = intra.get("volume", np.nan)
        rec["vwap_intra"] = _vwap(intra)
    if after:
        rec["v_after"] = after.get("volume", np.nan)
        rec["vwap_after"] = _vwap(after)

    df = df[df["date"] != day]
    df = pd.concat([df, pd.DataFrame([rec])], ignore_index=True).sort_values("date")
    df.to_csv(_cache_path(root, stock_id), index=False)


def update_and_load(root: Path, cfg, stock_id: str, today: date | None = None) -> pd.DataFrame:
    """抓今日 TWSE 零股快照 → upsert cache → 回傳該檔 cache（date-indexed）。

    抓取失敗不致命：回傳既有 cache（pipeline 仍可用 proxy 補）。
    """
    today = today or date.today()
    twse = cfg.twse or {}
    base = twse.get("base_url", "https://openapi.twse.com.tw")
    try:
        intra_all = fetch_oddlot_snapshot(twse.get("oddlot_intra_endpoint", ""), base)
        after_all = fetch_oddlot_snapshot(twse.get("oddlot_after_endpoint", ""), base)
        intra = intra_all.get(stock_id)
        after = after_all.get(stock_id)
        day = (after or intra or {}).get("date") or today
        if intra or after:
            upsert_cache(root, stock_id, day, intra, after)
    except Exception as e:  # noqa: BLE001
        print(f"[twse] {stock_id} oddlot fetch failed: {type(e).__name__}: {e}")
    cache = load_cache(root, stock_id)
    return cache.set_index("date").sort_index() if not cache.empty else cache


# ──────────────────────────── 小工具 ────────────────────────────────────────

def _pick(row: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in row and row[k] not in ("", None):
            return row[k]
    # 模糊比對：key 含關鍵字
    for rk, rv in row.items():
        for k in keys:
            if k in str(rk) and rv not in ("", None):
                return rv
    return None


def _num(v):
    if v is None:
        return np.nan
    try:
        return float(str(v).replace(",", "").replace("--", "").strip() or "nan")
    except ValueError:
        return np.nan


def _vwap(d: dict) -> float:
    vol, val, avg = d.get("volume"), d.get("value"), d.get("avg_price")
    if vol and not np.isnan(vol) and val and not np.isnan(val) and vol > 0:
        return val / vol
    return avg if avg is not None else np.nan


def _roc_or_iso_to_date(s: str) -> date | None:
    s = s.strip()
    try:
        if "/" in s:  # 民國 113/06/19
            y, m, d = s.split("/")
            y = int(y) + (1911 if int(y) < 1911 else 0)
            return date(y, int(m), int(d))
        return pd.Timestamp(s).date()
    except Exception:  # noqa: BLE001
        return None
