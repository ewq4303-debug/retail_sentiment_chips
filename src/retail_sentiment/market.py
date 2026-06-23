"""整體市場散戶指標（市場級 aggregate，SPEC §9.2 的跨股推廣）。

集保 opendata / seed 檔含「全市場」每檔每級距，故可算市場級籌碼指標：
對每個快照，取所有上市普通股（4 碼、非 00 開頭、total>0）的 per-stock 散戶比/大戶比，
做橫斷面等權平均 → 市場散戶比 sr%、市場大戶比 hb%、剪刀差與動能。

cache：cache/market_weekly.csv（每快照一列，極小、隨 repo commit、逐週累積）。
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

RETAIL_TIER_MAX = 5000        # 散戶：tier_max ≤ 5000（含第1、2級）
BIG_TIER_MIN = 1000001        # 大戶：頂層級距 ≥ 1,000,001（價格無關、跨股一致）
_COMMON = re.compile(r"^[1-9]\d{3}$")   # 上市普通股 4 碼、非 00 開頭（排除 ETF/權證）


def market_rows(df_all: pd.DataFrame) -> pd.DataFrame:
    """由 normalized 全市場 long-format（含多快照）算每快照一列市場指標。

    欄位：snapshot_date, sr_pct, hb_pct, n_stocks, retail_shares, total_shares。
    """
    if df_all is None or df_all.empty:
        return pd.DataFrame()
    df = df_all[df_all["stock_id"].astype(str).str.match(_COMMON)].copy()
    if df.empty:
        return pd.DataFrame()

    rows = []
    for snap, g in df.groupby("snapshot_date"):
        per = g.groupby("stock_id").apply(_per_stock_ratio, include_groups=False)
        per = per.dropna(subset=["sr"])
        if per.empty:
            continue
        rows.append(
            {
                "snapshot_date": snap,
                "sr_pct": float(per["sr"].mean()),
                "hb_pct": float(per["hb"].mean()),
                "n_stocks": int(len(per)),
                "retail_shares": float(per["retail"].sum()),
                "total_shares": float(per["total"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("snapshot_date") if rows else pd.DataFrame()


def _per_stock_ratio(g: pd.DataFrame) -> pd.Series:
    total = g["shares"].sum()
    if total <= 0:
        return pd.Series({"sr": np.nan, "hb": np.nan, "retail": 0.0, "total": 0.0})
    retail = g.loc[g["tier_max"] <= RETAIL_TIER_MAX, "shares"].sum()
    big = g.loc[g["tier_min"] >= BIG_TIER_MIN, "shares"].sum()
    return pd.Series({"sr": retail / total * 100, "hb": big / total * 100,
                      "retail": retail, "total": total})


# ──────────────────────────── cache ─────────────────────────────────────────

def _cache_path(root: Path) -> Path:
    d = Path(root) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "market_weekly.csv"


def update_cache(root: Path, df_all: pd.DataFrame) -> None:
    """由全市場 normalized df 算市場指標、upsert 進 market_weekly.csv（dedupe by snapshot）。"""
    new = market_rows(df_all)
    if new.empty:
        return
    p = _cache_path(root)
    if p.exists():
        old = pd.read_csv(p)
        old["snapshot_date"] = pd.to_datetime(old["snapshot_date"]).dt.date
        new["snapshot_date"] = pd.to_datetime(new["snapshot_date"]).dt.date
        old = old[~old["snapshot_date"].isin(new["snapshot_date"])]
        new = pd.concat([old, new], ignore_index=True)
    new = new.sort_values("snapshot_date")
    new.to_csv(p, index=False)


def build_json(root: Path, out_dir: Path) -> bool:
    """讀 cache 算剪刀差/動能，寫 docs/data/market.json。回傳是否有資料。"""
    p = _cache_path(root)
    if not p.exists():
        return False
    df = pd.read_csv(p).sort_values("snapshot_date")
    if df.empty:
        return False
    df["scissor_level"] = df["hb_pct"] - df["sr_pct"]
    df["scissor_momentum"] = df["hb_pct"].diff() - df["sr_pct"].diff()

    weekly = [
        {
            "snapshot_date": str(pd.to_datetime(r["snapshot_date"]).date()),
            "sr_pct": round(float(r["sr_pct"]), 4),
            "hb_pct": round(float(r["hb_pct"]), 4),
            "scissor_level": round(float(r["scissor_level"]), 4),
            "scissor_momentum": None if pd.isna(r["scissor_momentum"]) else round(float(r["scissor_momentum"]), 4),
            "n_stocks": int(r["n_stocks"]),
        }
        for _, r in df.iterrows()
    ]
    import json

    last = weekly[-1]
    obj = {
        "weekly": weekly,
        "latest": {
            "snapshot_date": last["snapshot_date"],
            "sr_pct": last["sr_pct"],
            "hb_pct": last["hb_pct"],
            "scissor_momentum": last["scissor_momentum"],
            # 動能>0：市場籌碼向大戶集中（偏多）；<0：散戶化（偏空）
            "tilt": "大戶集中" if (last["scissor_momentum"] or 0) > 0 else "散戶化",
        },
    }
    (Path(out_dir) / "market.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True
