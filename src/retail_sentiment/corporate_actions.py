"""公司行動調整（SPEC §2）。

比例型指標（sr/hb 用佔庫存%）對等比例行動免疫；只有流量錨定的股數差需要還原到
調整後共同基準。本模組提供：給定快照日，回傳累積 adj_factor。
"""
from __future__ import annotations

from datetime import date

import pandas as pd


def adj_factor_for(stock_id: str, snapshot_date: date, actions: dict[str, list[dict]]) -> float:
    """回傳該快照日歷史股數應乘上的累積調整因子。

    約定：corporate_actions.yaml 中每筆 ex_date 之後（含當日）的歷史股數乘上 adj_factor。
    對單一快照，累乘所有 ex_date <= 該快照所屬基準日 之後尚未生效的因子。

    簡化模型：把「ex_date 在 snapshot 之後」的所有 adj_factor 連乘，使較舊快照
    還原到最新基準（與最新快照可比）。無行動則回傳 1.0。
    """
    evs = actions.get(stock_id) or []
    factor = 1.0
    snap = pd.Timestamp(snapshot_date).date()
    for ev in evs:
        ex = pd.Timestamp(ev["ex_date"]).date()
        if ex > snap:  # 此快照在除權前 → 需乘上調整因子還原到除權後基準
            factor *= float(ev.get("adj_factor", 1.0))
    return factor
