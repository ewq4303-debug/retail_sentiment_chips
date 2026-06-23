"""Baseline 剝離：定期定額結構性流入（SPEC §4）。

定期定額是真散戶、但價格不敏感、集中在月初到月中扣款、且只買不賣，會讓淨流長期
偏多、污染情緒判讀。signed_flow = DCA_baseline + sentiment。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .stats import rolling_median


def strip_dca_baseline(signed_flow: pd.Series, cfg, dca_intensity: float = 1.0) -> pd.DataFrame:
    """回傳含 dca_baseline / sentiment_flow 的 DataFrame。

    1) 趨勢項：clip>=0 的滾動中位數（抗尖峰、僅取買方偏壓）。
    2) 月內季節項：按 day-of-month 的歷史中位數（定期定額按日曆日 cluster）。
    3) dca_intensity 縮放（無表 D 資料時設 1，ETF 給較高值）。
    4) 情緒殘差 = signed_flow - DCA_baseline（可正可負）。
    """
    sf = signed_flow.astype(float)

    trend = rolling_median(sf, cfg.dca_trend_window).clip(lower=0)

    seasonal = _seasonal_dom(sf, cfg.dca_seasonal_lookback_months).clip(lower=0)

    dca_baseline = dca_intensity * (trend.fillna(0) + seasonal.fillna(0))
    sentiment = sf - dca_baseline

    out = pd.DataFrame(index=sf.index)
    out["dca_baseline"] = dca_baseline
    out["sentiment_flow"] = sentiment
    return out


def _seasonal_dom(sf: pd.Series, lookback_months: int) -> pd.Series:
    """trailing median by day-of-month，回看 lookback_months 個月。"""
    idx = pd.to_datetime(pd.Index(sf.index))
    dom = idx.day
    df = pd.DataFrame({"val": sf.to_numpy(), "dom": dom}, index=idx).sort_index()

    out = pd.Series(index=sf.index, dtype=float)
    window = pd.DateOffset(months=lookback_months)
    for i, (ts, row) in enumerate(df.iterrows()):
        lo = ts - window
        hist = df.loc[(df.index >= lo) & (df.index < ts) & (df["dom"] == row["dom"]), "val"]
        out.iloc[i] = hist.median() if len(hist) else np.nan
    return out
