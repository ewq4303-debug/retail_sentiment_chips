"""方向分類（SPEC §3）。

核心：用集保存量差錨定（決定正負號與量級），用零股周轉量分配時間形狀。
分「已結算週（§3.2 主算法）」與「當週未結算（§3.3 暫定）」兩條路徑。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .calendar_utils import TradingCalendar, build_windows
from .corporate_actions import adj_factor_for
from .stats import rolling_zscore


def retail_shares_by_snapshot(tdcc: pd.DataFrame, cfg, stock_id: str,
                              actions: dict) -> pd.DataFrame:
    """各快照的散戶/大戶/總存量（調整後股數 + 佔比）。

    散戶：tier_max <= RETAIL_TIER_MAX（含第1、2級，§3.2 級距溢出考量）。
    大戶：依股價門檻（呼叫端決定 min_shares），此處同時算 ≥ 400,001 與 ≥1,000,001。
    回傳 index=snapshot_date 的 DataFrame。
    """
    if tdcc.empty:
        return pd.DataFrame()
    rows = []
    for snap, g in tdcc.groupby("snapshot_date"):
        factor = adj_factor_for(stock_id, snap, actions)
        total = g["shares"].sum()
        retail = g.loc[g["tier_max"] <= cfg.retail_tier_max_shares, "shares"].sum()
        big_high = g.loc[g["tier_min"] >= cfg.big_holder_rule.get("big_tier_min_shares_high", 400000), "shares"].sum()
        big_low = g.loc[g["tier_min"] >= cfg.big_holder_rule.get("big_tier_min_shares_low", 1000000), "shares"].sum()
        rows.append(
            {
                "snapshot_date": snap,
                "total_shares": total,
                "retail_shares_adj": retail * factor,
                "big_shares_high_adj": big_high * factor,
                "big_shares_low_adj": big_low * factor,
                "adj_factor": factor,
                # 佔比（比例型，免疫等比例行動）
                "sr_pct": (retail / total * 100) if total else np.nan,
                "hb_high_pct": (big_high / total * 100) if total else np.nan,
                "hb_low_pct": (big_low / total * 100) if total else np.nan,
            }
        )
    return pd.DataFrame(rows).set_index("snapshot_date").sort_index()


def settled_signed_flow(
    daily: pd.DataFrame,
    T: pd.Series,
    retail_stock: pd.DataFrame,
    cal: TradingCalendar,
    cfg,
) -> pd.DataFrame:
    """§3.2 主算法：把週真值淨流 ΔS_retail_w 依 T_d 分配到每一天。

    回傳 index=date 的 DataFrame，欄位 signed_flow / provisional / week_snapshot /
    delta_retail_shares_adj / T_sum / reconcile_resid。
    """
    out = pd.DataFrame(index=daily.index)
    out["signed_flow"] = np.nan
    out["provisional"] = True
    out["week_snapshot"] = pd.NaT
    out["delta_retail_shares_adj"] = np.nan

    if retail_stock.empty or len(retail_stock) < 2:
        return _provisional_only(daily, T, out, cfg)

    snaps = list(retail_stock.index)
    windows = build_windows(cal, snaps)

    # ΔS_retail_w = S_w - S_{w-1}
    retail_series = retail_stock["retail_shares_adj"]
    for win in windows:
        snap = win["snapshot"]
        prev_cut = win["prev_cutoff"]
        if prev_cut is None:
            continue  # 第一個窗口僅作基準
        days = [d for d in win["days"] if d in out.index]
        if not days:
            continue
        prev_snap = _prev_snapshot(snaps, snap)
        if prev_snap is None:
            continue
        delta = float(retail_series.loc[snap] - retail_series.loc[prev_snap])

        T_win = T.reindex(days)
        T_sum = float(np.nansum(T_win.to_numpy()))
        out.loc[days, "delta_retail_shares_adj"] = delta
        out.loc[days, "week_snapshot"] = pd.Timestamp(snap)
        if T_sum <= 0:
            # 無零股周轉可分配 → 平均分（退化情形），仍保證對帳。
            share = np.full(len(days), 1.0 / len(days))
        else:
            share = (T_win.fillna(0).to_numpy()) / T_sum
        out.loc[days, "signed_flow"] = delta * share
        out.loc[days, "provisional"] = False

    # 最後一個 cutoff 之後的當週未結算 → 暫定路徑。
    last_cut = cal.cutoff(snaps[-1])
    if last_cut is not None:
        pending_days = [d for d in out.index if d > last_cut]
        _fill_provisional(daily, T, out, pending_days, cfg)

    return out


def _provisional_only(daily, T, out, cfg) -> pd.DataFrame:
    _fill_provisional(daily, T, out, list(out.index), cfg)
    return out


def _fill_provisional(daily: pd.DataFrame, T: pd.Series, out: pd.DataFrame,
                      days: list, cfg) -> None:
    """§3.3 暫定路徑：方向 + 強度兩個分離欄位，不偽裝成已對帳股數。

    provisional_direction = sign(α*(vwap_intra-vwap_whole)/vwap_whole + β*ret)
    provisional_intensity = zscore(T, Z_WINDOW)  （純量級，無號）
    signed_flow（暫估）= direction * intensity * T  → 僅供顯示，下次集保出後 restate。
    """
    days = [d for d in days if d in out.index]
    if not days:
        return
    vwap_whole = daily["vwap_whole"].replace(0, np.nan)
    spread = (daily["vwap_intra"] - vwap_whole) / vwap_whole
    direction = np.sign(cfg.prov_alpha * spread.fillna(0) + cfg.prov_beta * daily["ret"].fillna(0))
    intensity = rolling_zscore(T, cfg.z_window)

    for d in days:
        dir_d = float(direction.get(d, 0.0))
        inten = intensity.get(d, np.nan)
        T_d = T.get(d, np.nan)
        # 暫估 signed_flow：方向 × |強度| × 周轉量（量級僅近似，旗標 provisional）。
        if np.isnan(T_d):
            out.loc[d, "signed_flow"] = np.nan
        else:
            mag = abs(inten) if not np.isnan(inten) else 0.0
            out.loc[d, "signed_flow"] = dir_d * mag * T_d
        out.loc[d, "provisional"] = True
    out.loc[days, "prov_direction"] = [float(direction.get(d, 0.0)) for d in days]
    out.loc[days, "prov_intensity"] = [float(intensity.get(d, np.nan)) for d in days]


def _prev_snapshot(snaps: list[date], snap: date):
    i = snaps.index(snap)
    return snaps[i - 1] if i > 0 else None
