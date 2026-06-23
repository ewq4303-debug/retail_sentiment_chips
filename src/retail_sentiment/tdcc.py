"""集保戶股權分散表 — TDCC opendata（SPEC §1 表 C）。

FinMind 的 TaiwanStockHoldingSharesPer 為贊助會員專屬（免費 token 常回 HTTP 400），
故改用 TDCC 官方 opendata（免金鑰、免費）：
    https://opendata.tdcc.com.tw/getOD.ashx?id=1-5   （集保戶股權分散表，全市場最新一週）

限制：opendata 只含「最新一週」快照、無歷史 → 每次執行抓一次、累積寫入
cache/tdcc/{id}.csv（隨 repo commit）。需累積 ≥2 週才能算 ΔStock（§3.2）；
在那之前該檔走 §3.3 暫定路徑。FinMind 若有權限（含歷史）則優先用 FinMind。
"""
from __future__ import annotations

import io
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

OPENDATA_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}

# TDCC 持股分級「級次」→ (下界, 上界) 股數。15=1,000,001↑；16 差異調整 / 17 合計 略過。
TIER_BOUNDS = {
    1: (1, 999), 2: (1000, 5000), 3: (5001, 10000), 4: (10001, 15000),
    5: (15001, 20000), 6: (20001, 30000), 7: (30001, 40000), 8: (40001, 50000),
    9: (50001, 100000), 10: (100001, 200000), 11: (200001, 400000),
    12: (400001, 600000), 13: (600001, 800000), 14: (800001, 1000000),
    15: (1000001, 10**12),
}


def backfill_universe(root: Path, universe: list[str]) -> None:
    """抓一次 opendata（全市場最新一週），把 universe 各檔 upsert 進 weekly cache。"""
    try:
        df = _fetch_opendata()
    except Exception as e:  # noqa: BLE001 — 抓取失敗不致命
        print(f"[tdcc] opendata fetch failed: {type(e).__name__}: {e}")
        return
    if df.empty:
        print("[tdcc] opendata returned no rows")
        return
    uni = set(universe)
    for stock_id, g in df[df["stock_id"].isin(uni)].groupby("stock_id"):
        _upsert_cache(root, stock_id, g)
    print(f"[tdcc] opendata snapshot {df['snapshot_date'].iloc[0]}: cached {df[df['stock_id'].isin(uni)]['stock_id'].nunique()} stocks")


def _fetch_opendata() -> pd.DataFrame:
    for attempt in range(4):
        try:
            r = requests.get(OPENDATA_URL, headers=_HEADERS, timeout=60)
            r.raise_for_status()
            # 全部讀為字串：保留證券代號的前導零(0050)與英數碼(00679B)。
            raw = pd.read_csv(io.StringIO(r.text), dtype=str)
            return _normalize(raw)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    return pd.DataFrame()


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """把 opendata 欄位正規化為 [snapshot_date, stock_id, tier_min, tier_max, holders, shares, pct]。"""
    cols = {c: str(c) for c in raw.columns}
    raw = raw.rename(columns=cols)

    def col(*keys):
        for k in keys:
            for c in raw.columns:
                if k in c:
                    return c
        return None

    c_date = col("資料日期", "date")
    c_id = col("證券代號", "stock_id", "代號")
    c_lvl = col("持股分級", "級")
    c_ppl = col("人數", "people")
    c_shr = col("股數", "shares")
    c_pct = col("比例", "percent", "占")
    if not all([c_date, c_id, c_lvl, c_shr]):
        return pd.DataFrame()

    rows = []
    for _, r in raw.iterrows():
        try:
            lvl = int(r[c_lvl])
        except (ValueError, TypeError):
            continue
        if lvl not in TIER_BOUNDS:  # 跳過 16 差異調整 / 17 合計
            continue
        lo, hi = TIER_BOUNDS[lvl]
        sid = str(r[c_id]).strip()
        if sid.endswith(".0"):  # 若代號被當數值讀入（如 2330.0）→ 還原
            sid = sid[:-2]
        rows.append(
            {
                "snapshot_date": _to_date(r[c_date]),
                "stock_id": sid,
                "tier_min": lo,
                "tier_max": hi,
                "holders": _num(r[c_ppl]) if c_ppl else np.nan,
                "shares": _num(r[c_shr]),
                "pct": _num(r[c_pct]) if c_pct else np.nan,
            }
        )
    return pd.DataFrame(rows)


# ──────────────────────────── cache ─────────────────────────────────────────

def _cache_path(root: Path, stock_id: str) -> Path:
    d = Path(root) / "cache" / "tdcc"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stock_id}.csv"


def _upsert_cache(root: Path, stock_id: str, snapshot_rows: pd.DataFrame) -> None:
    keep = ["snapshot_date", "tier_min", "tier_max", "holders", "shares", "pct"]
    new = snapshot_rows[keep].copy()
    p = _cache_path(root, stock_id)
    if p.exists():
        old = pd.read_csv(p)
        old["snapshot_date"] = pd.to_datetime(old["snapshot_date"]).dt.date
        old = old[~old["snapshot_date"].isin(new["snapshot_date"])]
        new = pd.concat([old, new], ignore_index=True)
    new = new.sort_values(["snapshot_date", "tier_min"])
    new.to_csv(p, index=False)


def load_cache(root: Path, stock_id: str) -> pd.DataFrame:
    """回傳 long-format 週頻集保（與 direction.retail_shares_by_snapshot 相容）。"""
    p = _cache_path(root, stock_id)
    if not p.exists():
        return pd.DataFrame(columns=["snapshot_date", "tier_min", "tier_max", "holders", "shares", "pct"])
    df = pd.read_csv(p)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
    return df


# ──────────────────────────── 小工具 ────────────────────────────────────────

def _num(v):
    if v is None:
        return np.nan
    try:
        return float(str(v).replace(",", "").strip() or "nan")
    except ValueError:
        return np.nan


def _to_date(v) -> date:
    s = str(v).strip().replace("/", "").replace("-", "")
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
