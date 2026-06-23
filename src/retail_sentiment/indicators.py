"""核心指標（SPEC §7, §9）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .stats import rolling_zscore


def normalize_flow(sentiment_flow: pd.Series, float_shares: pd.Series) -> pd.Series:
    """SPEC §7：RetailNetFlow_d = sentiment_flow / float_shares * 1e4（流通股數 bps）。"""
    fs = float_shares.replace(0, np.nan)
    return sentiment_flow / fs * 1e4


def divergence_score(daily: pd.DataFrame, retail_net_flow: pd.Series, cfg) -> pd.DataFrame:
    """SPEC §9.3：z 差值（不對累積序列做相關係數，避免 spurious corr）。

    DivergenceScore = zscore(inst_net) - zscore(RetailNetFlow)。
    """
    z_inst = rolling_zscore(daily["inst_net"], cfg.z_window)
    z_retail = rolling_zscore(retail_net_flow, cfg.z_window)
    out = pd.DataFrame(index=daily.index)
    out["z_inst"] = z_inst
    out["z_retail"] = z_retail
    out["divergence_score"] = z_inst - z_retail
    return out


def scissor(retail_stock: pd.DataFrame, price_for_tier: pd.Series, cfg) -> pd.DataFrame:
    """SPEC §9.2 大戶剪刀差（週頻，存量）。

    依股價選大戶級距：股價>50 用 ≥400,001（hb_high）；≤50 用 ≥1,000,001（hb_low）。
    ScissorLevel = hb - sr；ScissorMomentum = Δhb - Δsr。
    """
    if retail_stock.empty:
        return pd.DataFrame()
    df = retail_stock.copy()

    # 各 snapshot 對應的代表股價（取該快照日或之前最近的收盤）。
    hb_pct = []
    for snap in df.index:
        px = _price_asof(price_for_tier, snap)
        if px is not None and px > cfg.big_holder_rule.get("high_price_threshold", 50):
            hb_pct.append(df.loc[snap, "hb_high_pct"])
        else:
            hb_pct.append(df.loc[snap, "hb_low_pct"])
    df["hb_pct"] = hb_pct
    df["scissor_level"] = df["hb_pct"] - df["sr_pct"]
    df["scissor_momentum"] = df["hb_pct"].diff() - df["sr_pct"].diff()
    return df


def _price_asof(price: pd.Series, snap) -> float | None:
    if price is None or price.empty:
        return None
    snap_ts = pd.Timestamp(snap)
    idx = pd.to_datetime(pd.Index(price.index))
    s = pd.Series(price.to_numpy(), index=idx).sort_index()
    s = s.loc[s.index <= snap_ts]
    return float(s.iloc[-1]) if len(s) else None
