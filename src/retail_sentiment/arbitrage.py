"""套利湊張過濾 → 盤中零股權重 W_arb（SPEC §5）。

制度事實：盤中零股不可當沖（T 買進須 T+1），故零股爆量不可能是當沖，一定留倉。
真正雜訊是「分批買零股 → 湊滿 1,000 股 → 整股市場賣」的跨日套利，會在集保過水不留。
盤後零股恆為權重 1.0。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .stats import rolling_zscore


def compute_w_arb(daily: pd.DataFrame, cfg) -> pd.DataFrame:
    """回傳含 arb_ratio / arb_z / w_arb 欄位的 DataFrame（index 對齊 daily）。"""
    v_intra = daily["v_intra"].where(daily["v_intra"] > 0)
    v_whole = daily["v_whole"].clip(lower=1)
    arb_ratio = v_intra / v_whole
    arb_z = rolling_zscore(arb_ratio, cfg.z_window)

    thr = cfg.arb_ratio_z_threshold
    k = cfg.arb_k
    floor = cfg.w_arb_floor

    # W_arb = 1 if arb_z < thr else clip(1 - (arb_z - thr)/k, floor, 1)
    decayed = 1.0 - (arb_z - thr) / k
    w_arb = np.where(arb_z < thr, 1.0, np.clip(decayed, floor, 1.0))
    w_arb = pd.Series(w_arb, index=daily.index)
    # arb_z 為 NaN（視窗不足）時，保守給 1.0（不打折）。
    w_arb = w_arb.where(arb_z.notna(), 1.0)

    out = pd.DataFrame(index=daily.index)
    out["arb_ratio"] = arb_ratio
    out["arb_z"] = arb_z
    out["w_arb"] = w_arb
    return out


def turnover_T(daily: pd.DataFrame, w_arb: pd.Series) -> pd.Series:
    """零股周轉量（套利調整後）T_d = v_after*1.0 + v_intra*W_arb（SPEC §3.1）。

    停牌/無成交日（v=0）以 NaN 處理，不入分配（§2.3）。
    """
    v_after = daily["v_after"].where(daily["v_after"] > 0, 0.0)
    v_intra = daily["v_intra"].where(daily["v_intra"] > 0, 0.0)
    T = v_after * 1.0 + v_intra * w_arb
    # 全無成交日設 NaN。
    no_trade = (daily["v_after"].fillna(0) <= 0) & (daily["v_intra"].fillna(0) <= 0)
    return T.where(~no_trade, np.nan)
