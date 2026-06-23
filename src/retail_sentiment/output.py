"""輸出 JSON Schema（SPEC §11），寫入 docs/data 供 GitHub Pages 讀取。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if (v is None or pd.isna(v)) else round(float(v), 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    if v is pd.NaT:
        return None
    return v


def write_stock_json(
    out_dir: Path,
    stock_id: str,
    as_of: str,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    signals: pd.DataFrame,
    restated_at: str,
) -> dict:
    """寫 daily/weekly/signals 三檔，回傳一筆 index summary。"""
    (out_dir / "daily").mkdir(parents=True, exist_ok=True)
    (out_dir / "weekly").mkdir(parents=True, exist_ok=True)
    (out_dir / "signals").mkdir(parents=True, exist_ok=True)

    daily_rows = []
    for d, row in daily.iterrows():
        daily_rows.append(
            {
                "date": pd.Timestamp(d).date().isoformat(),
                "signed_flow": _clean(row.get("signed_flow")),
                "sentiment_flow": _clean(row.get("sentiment_flow")),
                "retail_net_flow_bps": _clean(row.get("retail_net_flow_bps")),
                "dca_baseline": _clean(row.get("dca_baseline")),
                "w_arb": _clean(row.get("w_arb")),
                "z_inst": _clean(row.get("z_inst")),
                "z_retail": _clean(row.get("z_retail")),
                "divergence_score": _clean(row.get("divergence_score")),
                "provisional": bool(row.get("provisional", True)),
                "restated_at": restated_at if not row.get("provisional", True) else None,
            }
        )
    _dump(out_dir / "daily" / f"{stock_id}.json",
          {"stock_id": stock_id, "as_of": as_of, "daily": daily_rows})

    weekly_rows = []
    for snap, row in weekly.iterrows():
        weekly_rows.append(
            {
                "snapshot_date": pd.Timestamp(snap).date().isoformat(),
                "cutoff": _clean(row.get("cutoff")),
                "window": row.get("window"),
                "sr_pct": _clean(row.get("sr_pct")),
                "hb_pct": _clean(row.get("hb_pct")),
                "scissor_level": _clean(row.get("scissor_level")),
                "scissor_momentum": _clean(row.get("scissor_momentum")),
                "delta_retail_shares_adj": _clean(row.get("retail_shares_adj_delta")),
                "reconcile_ok": bool(row.get("reconcile_ok", True)),
                "tdcc_noise_flag": bool(row.get("tdcc_noise_flag", False)),
            }
        )
    _dump(out_dir / "weekly" / f"{stock_id}.json",
          {"stock_id": stock_id, "weekly": weekly_rows})

    sig_rows = []
    for d, row in signals.iterrows():
        sig_rows.append(
            {
                "date": pd.Timestamp(d).date().isoformat(),
                "signal": row.get("signal"),
                "label": row.get("label"),
                "strength": _clean(row.get("strength")),
                "matrix_cell": row.get("matrix_cell"),
            }
        )
    _dump(out_dir / "signals" / f"{stock_id}.json",
          {"stock_id": stock_id, "signals": sig_rows})

    latest_sig = sig_rows[-1] if sig_rows else {}
    latest_daily = daily_rows[-1] if daily_rows else {}
    return {
        "stock_id": stock_id,
        "as_of": as_of,
        "latest_signal": latest_sig.get("signal"),
        "latest_label": latest_sig.get("label"),
        "latest_strength": latest_sig.get("strength"),
        "latest_retail_net_flow_bps": latest_daily.get("retail_net_flow_bps"),
        "latest_divergence": latest_daily.get("divergence_score"),
    }


def write_index(out_dir: Path, summaries: list[dict], backend: str) -> None:
    _dump(
        out_dir / "index.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "backend": backend,
            "stocks": summaries,
        },
    )


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
