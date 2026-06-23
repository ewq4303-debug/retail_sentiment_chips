"""驗證與對帳（SPEC §12.1 流量↔存量對帳）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def reconcile_weeks(flow: pd.DataFrame, retail_stock: pd.DataFrame, cfg) -> dict:
    """每結算週驗 |Σ signed_flow_d − ΔS_retail_w| / |ΔS_retail_w| ≤ tol。

    回傳 {snapshot_date(str): {reconcile_ok, resid_rel, sum_flow, delta}}。
    由 §3.2 分配方式，理論殘差應為 0；此處檢出公司行動/抓取缺日造成的偏離。
    """
    result: dict[str, dict] = {}
    if flow.empty or "week_snapshot" not in flow.columns or retail_stock.empty:
        return result

    for snap, g in flow.groupby("week_snapshot"):
        if pd.isna(snap):
            continue
        snap_d = pd.Timestamp(snap).date()
        settled = g[~g["provisional"]]
        if settled.empty:
            continue
        sum_flow = float(np.nansum(settled["signed_flow"].to_numpy()))
        delta = float(settled["delta_retail_shares_adj"].dropna().iloc[0]) if settled["delta_retail_shares_adj"].notna().any() else np.nan
        if np.isnan(delta) or delta == 0:
            rel = 0.0 if abs(sum_flow) < 1 else np.inf
        else:
            rel = abs(sum_flow - delta) / abs(delta)
        result[snap_d.isoformat()] = {
            "reconcile_ok": bool(rel <= cfg.noise_reconcile_tol),
            "resid_rel": None if np.isinf(rel) else round(rel, 6),
            "sum_flow": sum_flow,
            "delta": delta,
        }
    return result
