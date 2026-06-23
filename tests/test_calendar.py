"""SPEC §8 / §12.2：時間軸對齊單元測試（含假日週）。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_sentiment.calendar_utils import TradingCalendar, build_windows


def _weekdays(start: date, end: date):
    from datetime import timedelta
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_cutoff_is_thursday_for_friday_snapshot():
    cal = TradingCalendar(_weekdays(date(2026, 6, 1), date(2026, 6, 30)))
    # 2026-06-19 是週五 → cutoff 應為 2026-06-18（週四）
    assert cal.cutoff(date(2026, 6, 19)) == date(2026, 6, 18)


def test_cutoff_shifts_on_holiday_week():
    # 移除週四 6/18（假日）→ cutoff 應自動位移到週三 6/17
    days = [d for d in _weekdays(date(2026, 6, 1), date(2026, 6, 30)) if d != date(2026, 6, 18)]
    cal = TradingCalendar(days)
    assert cal.cutoff(date(2026, 6, 19)) == date(2026, 6, 17)


def test_window_is_half_open_thu_to_thu_no_overlap():
    cal = TradingCalendar(_weekdays(date(2026, 5, 1), date(2026, 6, 30)))
    snaps = [date(2026, 6, 5), date(2026, 6, 12), date(2026, 6, 19)]  # 三個週五
    wins = build_windows(cal, snaps)
    # 第二、三週的窗口應為 (前cutoff, cutoff]，半開、不重疊
    w2 = wins[1]
    w3 = wins[2]
    assert w2["prev_cutoff"] == date(2026, 6, 4)   # 第一週五 6/5 → cutoff 6/4
    assert w2["cutoff"] == date(2026, 6, 11)
    assert w3["cutoff"] == date(2026, 6, 18)
    # 不重疊：w2 最後一天 < w3 第一天
    assert max(w2["days"]) < min(w3["days"])
    # 不漏：w3 第一天是 w2 cutoff 之後第一個交易日
    assert min(w3["days"]) == date(2026, 6, 12)


def test_previous_trading_day_none_before_start():
    cal = TradingCalendar(_weekdays(date(2026, 6, 1), date(2026, 6, 5)))
    assert cal.previous_trading_day(date(2026, 6, 1)) is None
