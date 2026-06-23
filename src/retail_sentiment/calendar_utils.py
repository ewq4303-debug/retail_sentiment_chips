"""交易日曆與 settlement lag 對齊（SPEC §8）。★ 易錯點。

事實：TDCC 快照為週最後營業日（通常週五）收盤後存摺餘額；因 T+2 交割，
該快照含星期四（含）以前交易、不含星期五當日。

    cutoff(snapshot) = previous_trading_day(snapshot)      # 一般 = 週四
    𝒲_w = ( cutoff(snapshot_{w-1}), cutoff(snapshot_w) ]   # Thu→Thu 半開區間

此模組以「實際出現過的交易日集合」推導交易日曆，假日週用 previous_trading_day
自動位移，無需硬編國定假日。
"""
from __future__ import annotations

import bisect
from datetime import date
from typing import Iterable, Sequence

import pandas as pd


class TradingCalendar:
    """以已知交易日（來自整股行情日期）建構的交易日曆。"""

    def __init__(self, trading_days: Iterable[date]):
        days = sorted({_to_date(d) for d in trading_days})
        if not days:
            raise ValueError("trading calendar is empty")
        self._days: list[date] = days

    @property
    def days(self) -> list[date]:
        return self._days

    def is_trading_day(self, d: date) -> bool:
        d = _to_date(d)
        i = bisect.bisect_left(self._days, d)
        return i < len(self._days) and self._days[i] == d

    def previous_trading_day(self, d: date) -> date | None:
        """嚴格小於 d 的最大交易日（SPEC §8 cutoff 定義）。

        snapshot 不一定要是交易日；含假日週仍正確位移。
        """
        d = _to_date(d)
        i = bisect.bisect_left(self._days, d)
        if i == 0:
            return None
        return self._days[i - 1]

    def cutoff(self, snapshot_date: date) -> date | None:
        """SPEC §8：對齊截止日 = 快照日的前一個交易日（一般 = 週四）。"""
        return self.previous_trading_day(snapshot_date)

    def days_in_window(self, lo_exclusive: date, hi_inclusive: date) -> list[date]:
        """半開區間 (lo, hi] 內的交易日，對應 §8 的 𝒲_w。"""
        lo = _to_date(lo_exclusive)
        hi = _to_date(hi_inclusive)
        i = bisect.bisect_right(self._days, lo)
        j = bisect.bisect_right(self._days, hi)
        return self._days[i:j]


def build_windows(
    cal: TradingCalendar, snapshot_dates: Sequence[date]
) -> list[dict]:
    """由連續的集保快照日，建構各週對齊窗口（SPEC §8）。

    回傳每個「已結算週」一筆：
        { 'snapshot': date, 'cutoff': date,
          'prev_cutoff': date|None, 'days': [date, ...] }
    第一個快照無前一 cutoff（prev_cutoff=None），days 取 (None, cutoff] 但
    因無下界會涵蓋過多日，故第一個窗口通常僅供存量基準、不參與流量分配。
    """
    snaps = sorted({_to_date(s) for s in snapshot_dates})
    cutoffs = [(s, cal.cutoff(s)) for s in snaps]
    cutoffs = [(s, c) for s, c in cutoffs if c is not None]

    windows: list[dict] = []
    prev_cutoff: date | None = None
    for snap, cut in cutoffs:
        if prev_cutoff is None:
            days: list[date] = []  # 無下界，流量分配跳過（僅作存量基準）
        else:
            days = cal.days_in_window(prev_cutoff, cut)
        windows.append(
            {
                "snapshot": snap,
                "cutoff": cut,
                "prev_cutoff": prev_cutoff,
                "days": days,
            }
        )
        prev_cutoff = cut
    return windows


def _to_date(d) -> date:
    if isinstance(d, date) and not isinstance(d, pd.Timestamp):
        return d
    return pd.Timestamp(d).date()
