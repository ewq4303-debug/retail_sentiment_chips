"""共用統計工具。NaN-aware 滾動 z-score / median（SPEC §2.3 缺值不入視窗）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_zscore(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """滾動 z-score。v=0/NaN 不污染視窗（呼叫端先把停牌日設 NaN）。"""
    min_periods = min_periods or max(5, window // 3)
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std(ddof=0)
    z = (s - mean) / std.replace(0, np.nan)
    return z


def rolling_median(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    min_periods = min_periods or max(5, window // 3)
    return s.rolling(window, min_periods=min_periods).median()
