"""雙頻驗證矩陣 + 量級 + 存量 gating（SPEC §10）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_signals(
    div: pd.DataFrame,
    scissor_df: pd.DataFrame,
    flow: pd.DataFrame,
    retail_stock: pd.DataFrame,
    cal,
    cfg,
) -> pd.DataFrame:
    """每個交易日輸出 signal / label / strength / matrix_cell。

    連續分數 + gating：
        C_flow = w1*z_inst + w2*(-z_retail)
        G_w    = sign(ScissorMomentum_w)
        dStock = ΔS_retail_w / Total_w
        if |dStock| < tol*typical_weekly_move: NOISE
        elif sign(mean_week(C_flow)) == G_w:  CONFIRMED
        else:                                 PENDING
    """
    out = pd.DataFrame(index=div.index)
    C_flow = cfg.matrix_w_inst * div["z_inst"].fillna(0) + cfg.matrix_w_retail * (-div["z_retail"].fillna(0))
    out["c_flow"] = C_flow

    if scissor_df is None or scissor_df.empty or retail_stock.empty:
        out["signal"] = "PENDING"
        out["label"] = "待確認"
        out["strength"] = np.nan
        out["matrix_cell"] = "—"
        return out

    # typical_weekly_move：散戶存量週變動（佔總股數）的歷史絕對中位數。
    dstock_series = (retail_stock["retail_shares_adj"].diff() / retail_stock["total_shares"]).abs()
    typical = float(dstock_series.median()) if dstock_series.notna().any() else 0.0
    typical = typical if typical > 0 else 1e-9

    # 把每個交易日對應到其所屬週的 snapshot（用 flow 已標的 week_snapshot）。
    week_snap = flow.get("week_snapshot")
    signals, labels, strengths, cells = [], [], [], []
    for d in out.index:
        snap = week_snap.get(d) if week_snap is not None else None
        c = float(C_flow.get(d, 0.0))
        if snap is None or (isinstance(snap, float) and np.isnan(snap)) or pd.isna(snap):
            signals.append("PROVISIONAL"); labels.append("暫定（集保未出）")
            strengths.append(float(abs(c))); cells.append("provisional")
            continue
        snap_d = pd.Timestamp(snap).date()
        if snap_d not in scissor_df.index:
            signals.append("PENDING"); labels.append("待確認")
            strengths.append(np.nan); cells.append("—")
            continue

        delta = scissor_df.loc[snap_d, "retail_shares_adj"] - _prev_val(scissor_df["retail_shares_adj"], snap_d)
        total = scissor_df.loc[snap_d, "total_shares"]
        dstock = (delta / total) if total else 0.0
        G = np.sign(scissor_df.loc[snap_d, "scissor_momentum"]) if not pd.isna(scissor_df.loc[snap_d, "scissor_momentum"]) else 0.0

        # 該週 C_flow 平均（用同 snapshot 的日）
        mask = (week_snap == snap) if week_snap is not None else pd.Series(False, index=out.index)
        c_week = float(C_flow[mask].mean()) if mask.any() else c

        if abs(dstock) < cfg.noise_reconcile_tol * typical:
            sig, lab = "NOISE", "雜訊／未留倉（湊張過水）"
            strength = 0.0
        elif np.sign(c_week) == G and G != 0:
            sig = "CONFIRMED"
            strength = float(abs(c_week) * _weight(abs(dstock), typical))
            lab = _label(c_week, G)
        else:
            sig, lab, strength = "PENDING", "待確認（流量與存量衝突）", float(abs(c_week))

        signals.append(sig); labels.append(lab)
        strengths.append(strength); cells.append(_cell(div.get("z_inst").get(d), div.get("z_retail").get(d), G))

    out["signal"] = signals
    out["label"] = labels
    out["strength"] = strengths
    out["matrix_cell"] = cells
    return out


def _label(c_week: float, G: float) -> str:
    if c_week > 0 and G > 0:
        return "強多"
    if c_week < 0 and G < 0:
        return "強空"
    return "中性"


def _cell(z_inst, z_retail, G) -> str:
    inst = "買" if (z_inst or 0) > 0 else "賣"
    ret = "買" if (z_retail or 0) > 0 else "賣"
    chip = "大戶增" if G > 0 else ("散戶增" if G < 0 else "持平")
    return f"{inst}/{ret}/{chip}"


def _weight(abs_dstock: float, typical: float) -> float:
    return float(np.clip(abs_dstock / (typical + 1e-12), 0.2, 3.0))


def _prev_val(s: pd.Series, key):
    s = s.sort_index()
    i = list(s.index).index(key)
    return s.iloc[i - 1] if i > 0 else s.iloc[i]
