"""Pipeline 編排（串起 SPEC §2–§12）。

每檔流程：
  抓資料 → 建交易日曆 → W_arb(§5)/T(§3.1) → 散戶存量(§3.2) → signed_flow 分配 →
  baseline 剝離(§4) → 正規化(§7) → 指標(§9) → 矩陣 gating(§10) → 對帳(§12) → 輸出(§11)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import arbitrage, baseline, direction, indicators, matrix, output, reconcile, sources
from .calendar_utils import TradingCalendar, build_windows
from .config import Config, load_corporate_actions, load_universe


def run(out_dir: Path, cfg: Config | None = None, universe: list[str] | None = None,
        backend: str | None = None) -> dict:
    cfg = cfg or Config.load()
    universe = universe or load_universe()
    actions = load_corporate_actions()
    out_dir = Path(out_dir)

    end = date.today()
    start = end - timedelta(days=int(cfg.lookback_trading_days * 1.7))  # 含週末/假日緩衝

    summaries: list[dict] = []
    used_backend = backend or sources._select_backend()
    restated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # 真實資料批次回補（每來源只抓一次、服務全 universe）。
    if used_backend == "finmind":
        try:
            from . import twse

            weekdays = [d for d in pd.bdate_range(start, end).date]
            twse.backfill_universe(cfg.root, cfg, universe, weekdays)
        except Exception as e:  # noqa: BLE001 — 回補失敗不阻斷（per-stock 以 proxy 補）
            print(f"[twse] backfill skipped: {type(e).__name__}: {e}")
        try:
            from . import tdcc

            tdcc.backfill_universe(cfg.root, universe)  # 集保 opendata（FinMind 無權限時的來源）
        except Exception as e:  # noqa: BLE001
            print(f"[tdcc] backfill skipped: {type(e).__name__}: {e}")

    for stock_id in universe:
        try:
            summary = _run_one(stock_id, start, end, cfg, actions, out_dir,
                               backend, restated_at)
            summaries.append(summary)
            print(f"[ok] {stock_id}: {summary.get('latest_signal')} / {summary.get('latest_label')}")
        except Exception as e:  # 單檔失敗不阻斷整批
            print(f"[err] {stock_id}: {type(e).__name__}: {e}")
            summaries.append({"stock_id": stock_id, "error": str(e)})

    output.write_index(out_dir, summaries, used_backend)

    # 整體市場散戶指標（由集保全市場 aggregate；無 cache 則略過）
    try:
        from . import market

        if market.build_json(cfg.root, out_dir):
            print("[market] wrote market.json")
    except Exception as e:  # noqa: BLE001
        print(f"[market] build skipped: {type(e).__name__}: {e}")

    return {"backend": used_backend, "count": len(summaries)}


def _run_one(stock_id, start, end, cfg, actions, out_dir, backend, restated_at) -> dict:
    sd = sources.fetch_stock(stock_id, start, end, cfg, backend)
    daily = sd.daily.copy()
    daily = daily.sort_index()

    cal = TradingCalendar(daily.index.tolist())

    # §5 套利權重 → §3.1 零股周轉量 T
    arb = arbitrage.compute_w_arb(daily, cfg)
    daily["w_arb"] = arb["w_arb"]
    T = arbitrage.turnover_T(daily, arb["w_arb"])

    # §3.2 散戶/大戶存量（調整後股數 + 佔比）
    retail_stock = direction.retail_shares_by_snapshot(sd.weekly_tdcc, cfg, stock_id, actions)

    # §3 帶號淨流分配（含 §3.3 暫定）
    flow = direction.settled_signed_flow(daily, T, retail_stock, cal, cfg)
    daily["signed_flow"] = flow["signed_flow"]
    daily["provisional"] = flow["provisional"]

    # §4 baseline 剝離
    dca_intensity = cfg.etf_dca_intensity if sd.is_etf else 1.0
    bl = baseline.strip_dca_baseline(daily["signed_flow"], cfg, dca_intensity)
    daily["dca_baseline"] = bl["dca_baseline"]
    daily["sentiment_flow"] = bl["sentiment_flow"]

    # §7 正規化（float_shares 取集保總計，固定）
    float_shares = _float_shares_series(daily.index, retail_stock)
    daily["retail_net_flow_bps"] = indicators.normalize_flow(daily["sentiment_flow"], float_shares)

    # §9.3 背離
    div = indicators.divergence_score(daily, daily["retail_net_flow_bps"], cfg)
    daily["z_inst"] = div["z_inst"]
    daily["z_retail"] = div["z_retail"]
    daily["divergence_score"] = div["divergence_score"]

    # §9.2 大戶剪刀差（週頻）
    scissor_df = indicators.scissor(retail_stock, daily["vwap_whole"], cfg)

    # §10 矩陣 gating
    signals = matrix.compute_signals(div, scissor_df, flow, retail_stock, cal, cfg)

    # §12.1 對帳
    rec = reconcile.reconcile_weeks(flow, retail_stock, cfg)

    # 組週頻輸出表
    weekly = _build_weekly_table(scissor_df, retail_stock, cal, rec)

    as_of = end.isoformat()
    return output.write_stock_json(out_dir, stock_id, as_of, daily, weekly, signals, restated_at)


def _float_shares_series(index, retail_stock) -> pd.Series:
    """以集保總股數為 float_shares（固定）。無集保資料時退化為 NaN。"""
    if retail_stock.empty:
        return pd.Series(np.nan, index=index)
    total = retail_stock["total_shares"]
    # forward-fill 到每個交易日（用最近一次快照的總股數）。
    s = pd.Series(index=index, dtype=float)
    snap_index = pd.to_datetime(pd.Index(total.index)).date
    pairs = sorted(zip(snap_index, total.to_numpy()))
    for d in index:
        val = np.nan
        for sd_, tv in pairs:
            if sd_ <= d:
                val = tv
            else:
                break
        s.loc[d] = val
    return s


def _build_weekly_table(scissor_df, retail_stock, cal, rec) -> pd.DataFrame:
    if scissor_df is None or scissor_df.empty:
        return pd.DataFrame()
    df = scissor_df.copy()
    df["retail_shares_adj_delta"] = df["retail_shares_adj"].diff()
    cutoffs, windows = [], []
    snaps = list(df.index)
    win_map = {w["snapshot"]: w for w in build_windows(cal, snaps)}
    for snap in df.index:
        w = win_map.get(snap)
        cut = w["cutoff"] if w else cal.cutoff(snap)
        cutoffs.append(cut.isoformat() if cut else None)
        days = w["days"] if w else []
        windows.append([days[0].isoformat(), days[-1].isoformat()] if days else None)
    df["cutoff"] = cutoffs
    df["window"] = windows
    df["reconcile_ok"] = [rec.get(pd.Timestamp(s).date().isoformat(), {}).get("reconcile_ok", True) for s in df.index]
    df["tdcc_noise_flag"] = False  # §2.2：可由 holders 週變動異常觸發，預留
    return df
