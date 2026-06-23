"""資料來源（SPEC §1 表 A–D）。

兩個 backend：
  * ``finmind``   — 真實抓取（FinMind v4 + TWSE OpenAPI 盡力補零股）。需 FINMIND_TOKEN。
  * ``synthetic`` — 自洽的確定性假資料，保證 pipeline / dashboard 在 CI 無金鑰也可跑通。

選擇：環境變數 ``DATA_BACKEND``（finmind|synthetic）；未設且無 token → synthetic。

許多欄位於 SPEC 標 ⚠️確認（零股、定期定額、借券），實作以乾淨抽象層隔離，
真實欄位名以抓取端為準，缺漏者降級為 NaN 或合成代理並在輸出標旗標。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


@dataclass
class StockData:
    """單檔標的的所有原始時序（已對齊為 DataFrame）。

    daily: index=date，欄位見 §1 表 A/A'/B。
    weekly_tdcc: long format，欄位 [snapshot_date, tier_min, tier_max, holders, shares, pct]。
    """

    stock_id: str
    daily: pd.DataFrame
    weekly_tdcc: pd.DataFrame
    is_etf: bool = False
    backend: str = "synthetic"
    notes: list[str] = None  # type: ignore

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


# 集保 15 級距上界（股），最後一級為頂層（用大數表示 ∞）。SPEC §1 表 C。
TDCC_TIER_UPPER = [
    999, 5000, 10000, 15000, 20000, 30000, 40000, 50000, 100000,
    200000, 400000, 600000, 800000, 1000000, 10**12,
]


def fetch_stock(stock_id: str, start: date, end: date, cfg, backend: str | None = None) -> StockData:
    backend = backend or _select_backend()
    if backend == "finmind":
        return _fetch_finmind(stock_id, start, end, cfg)
    return _fetch_synthetic(stock_id, start, end, cfg)


def _select_backend() -> str:
    import os

    b = os.environ.get("DATA_BACKEND")
    if b:
        return b.lower()
    return "finmind" if os.environ.get("FINMIND_TOKEN") else "synthetic"


# ───────────────────────────── FinMind backend ──────────────────────────────

def _finmind_get(dataset: str, data_id: str, start: date, end: date, token: str | None) -> pd.DataFrame:
    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    if token:
        params["token"] = token
    for attempt in range(4):
        try:
            r = requests.get(FINMIND_URL, params=params, timeout=30)
            if r.status_code == 402:  # 額度用罄
                raise RuntimeError("FinMind quota exhausted (402)")
            r.raise_for_status()
            payload = r.json()
            return pd.DataFrame(payload.get("data", []))
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    return pd.DataFrame()


def _fetch_finmind(stock_id: str, start: date, end: date, cfg) -> StockData:
    import os

    token = os.environ.get("FINMIND_TOKEN")
    notes: list[str] = []

    price = _finmind_get("TaiwanStockPrice", stock_id, start, end, token)
    inst = _finmind_get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end, token)
    margin = _finmind_get("TaiwanStockMarginPurchaseShortSale", stock_id, start, end, token)
    daytrade = _finmind_get("TaiwanStockDayTrading", stock_id, start, end, token)
    tdcc = _finmind_get("TaiwanStockHoldingSharesPer", stock_id, start, end, token)

    if price.empty:
        notes.append("FinMind price empty → fallback synthetic")
        return _fetch_synthetic(stock_id, start, end, cfg)

    daily = _assemble_finmind_daily(price, inst, margin, daytrade, notes)
    weekly = _assemble_finmind_tdcc(tdcc, notes)

    # 零股：抓 TWSE OpenAPI 當日快照、累積 cache，由 cache 組歷史時序（SPEC §1 表 A / §13）。
    _attach_oddlot_twse(daily, stock_id, cfg, notes)

    return StockData(stock_id, daily, weekly, _is_etf(stock_id), "finmind", notes)


def _attach_oddlot_twse(daily: pd.DataFrame, stock_id: str, cfg, notes) -> None:
    """以 TWSE 零股 cache 填欄位；cache 未涵蓋日以整股量比例代理並標旗標。

    cache 由 pipeline 的 backfill_universe 批次回補（每交易日只抓一次、服務全標的）。
    """
    from . import twse

    cache = twse.load_for_index(cfg.root, stock_id, daily.index)
    for col in ("v_intra", "vwap_intra", "v_after", "vwap_after"):
        daily[col] = cache[col] if col in cache else np.nan
    daily["oddlot_is_proxy"] = daily["v_after"].isna() & daily["v_intra"].isna()

    proxy_on = (cfg.twse or {}).get("oddlot_proxy_fallback", True)
    n_real = int((~daily["oddlot_is_proxy"]).sum())
    if proxy_on and daily["oddlot_is_proxy"].any():
        _fill_oddlot_proxy_where_missing(daily)
        notes.append(f"odd-lot: {n_real} days from TWSE cache, rest synthetic proxy (SPEC §13)")
    else:
        notes.append(f"odd-lot: {n_real} days from TWSE cache (no proxy fill)")


def _fill_oddlot_proxy_where_missing(daily: pd.DataFrame) -> None:
    """僅對缺值日以整股量比例補零股代理，保留 TWSE 真值。"""
    rng = np.random.default_rng(7)
    v = daily["v_whole"].to_numpy()
    n = len(v)
    intra = np.clip(v * 0.02 * (1 + rng.normal(0, 0.3, n)), 0, None)
    after = np.clip(v * 0.015 * (1 + rng.normal(0, 0.2, n)), 0, None)
    miss = daily["oddlot_is_proxy"].to_numpy()
    daily.loc[miss, "v_intra"] = intra[miss]
    daily.loc[miss, "v_after"] = after[miss]
    daily.loc[miss, "vwap_intra"] = daily["vwap_whole"][miss]
    daily.loc[miss, "vwap_after"] = daily["vwap_whole"][miss]


def _assemble_finmind_daily(price, inst, margin, daytrade, notes) -> pd.DataFrame:
    p = price.rename(columns={"Trading_Volume": "v_whole", "close": "close"})
    p["date"] = pd.to_datetime(p["date"]).dt.date
    p = p.set_index("date").sort_index()
    out = pd.DataFrame(index=p.index)
    out["v_whole"] = pd.to_numeric(p.get("v_whole"), errors="coerce")
    out["vwap_whole"] = pd.to_numeric(p.get("close"), errors="coerce")
    out["ret"] = out["vwap_whole"].pct_change()

    if not inst.empty:
        inst["date"] = pd.to_datetime(inst["date"]).dt.date
        # buy/sell 為股數；合計三大法人淨買超。
        g = inst.groupby("date").apply(
            lambda d: pd.to_numeric(d["buy"], errors="coerce").sum()
            - pd.to_numeric(d["sell"], errors="coerce").sum()
        )
        out["inst_net"] = g.reindex(out.index)
    else:
        out["inst_net"] = np.nan
        notes.append("institutional data empty")

    if not margin.empty:
        margin["date"] = pd.to_datetime(margin["date"]).dt.date
        m = margin.set_index("date")
        out["margin_bal"] = pd.to_numeric(m.get("MarginPurchaseTodayBalance"), errors="coerce").reindex(out.index)
        out["short_bal"] = pd.to_numeric(m.get("ShortSaleTodayBalance"), errors="coerce").reindex(out.index)
    else:
        out["margin_bal"] = np.nan
        out["short_bal"] = np.nan

    if not daytrade.empty and "Volume" in daytrade.columns:
        daytrade["date"] = pd.to_datetime(daytrade["date"]).dt.date
        dt = daytrade.set_index("date")
        out["v_daytrade"] = pd.to_numeric(dt.get("Volume"), errors="coerce").reindex(out.index).fillna(0)
    else:
        out["v_daytrade"] = 0.0
        notes.append("day-trade data empty → v_daytrade=0")

    out["sbl_short_bal"] = np.nan  # ⚠️確認：借券賣出餘額需另接 dataset（SPEC §13）
    return out


def _assemble_finmind_tdcc(tdcc, notes) -> pd.DataFrame:
    if tdcc.empty:
        notes.append("TDCC holding shares empty")
        return pd.DataFrame(columns=["snapshot_date", "tier_min", "tier_max", "holders", "shares", "pct"])
    df = tdcc.copy()
    df["snapshot_date"] = pd.to_datetime(df["date"]).dt.date
    # FinMind TaiwanStockHoldingSharesPer 欄位：HoldingSharesLevel, people, percent, unit(股數)
    rows = []
    for _, r in df.iterrows():
        lo, hi = _parse_tier(str(r.get("HoldingSharesLevel", "")))
        rows.append(
            {
                "snapshot_date": r["snapshot_date"],
                "tier_min": lo,
                "tier_max": hi,
                "holders": pd.to_numeric(r.get("people"), errors="coerce"),
                "shares": pd.to_numeric(r.get("unit"), errors="coerce"),
                "pct": pd.to_numeric(r.get("percent"), errors="coerce"),
            }
        )
    return pd.DataFrame(rows)


def _parse_tier(label: str) -> tuple[int, int]:
    """把集保級距字串解析成 (下界, 上界)。"""
    digits = [int(s.replace(",", "")) for s in _find_numbers(label)]
    if not digits:
        return (0, TDCC_TIER_UPPER[0])
    if len(digits) == 1:
        return (digits[0], 10**12)
    return (digits[0], digits[1])


def _find_numbers(s: str) -> list[str]:
    import re

    return re.findall(r"[\d,]+", s)


# ─────────────────────────── synthetic backend ──────────────────────────────

def _fetch_synthetic(stock_id: str, start: date, end: date, cfg) -> StockData:
    """確定性假資料：自洽，含一段可被偵測的籌碼集中事件。"""
    rng = np.random.default_rng(_seed(stock_id))
    days = _business_days(start, end)
    n = len(days)

    base_price = 50 + (_seed(stock_id) % 600)
    ret = rng.normal(0.0003, 0.018, n)
    price = base_price * np.cumprod(1 + ret)
    v_whole = rng.lognormal(mean=16.5, sigma=0.4, size=n)  # ~10M 量級
    v_daytrade = v_whole * rng.uniform(0.1, 0.35, n)
    inst_net = rng.normal(0, 1, n) * v_whole * 0.05

    daily = pd.DataFrame(
        {
            "v_whole": v_whole,
            "vwap_whole": price,
            "ret": ret,
            "inst_net": inst_net,
            "margin_bal": np.cumsum(rng.normal(0, 1e4, n)) + 5e6,
            "short_bal": np.cumsum(rng.normal(0, 5e3, n)) + 1e6,
            "v_daytrade": v_daytrade,
            "sbl_short_bal": np.nan,
        },
        index=days,
    )
    _attach_oddlot_proxy(daily, rng)

    weekly = _synthetic_tdcc(days, daily, rng, cfg)
    return StockData(stock_id, daily, weekly, _is_etf(stock_id), "synthetic",
                     ["synthetic deterministic data"])


def _synthetic_tdcc(days, daily, rng, cfg) -> pd.DataFrame:
    """合成週頻集保分級：散戶股數隨機漫步，總股數固定。"""
    total = 2e9
    # 取每週最後一個營業日為 snapshot（模擬週五）。
    df = pd.DataFrame({"d": days})
    df["wk"] = pd.to_datetime(df["d"]).dt.to_period("W")
    snaps = df.groupby("wk")["d"].max().tolist()

    retail_pct = 0.30
    big_pct = 0.55
    rows = []
    for snap in snaps:
        # 散戶比與大戶比反向漫步（剪刀差），製造可偵測訊號。
        drift = rng.normal(0, 0.004)
        retail_pct = float(np.clip(retail_pct - abs(drift) * np.sign(drift if drift else 1) , 0.15, 0.45))
        big_pct = float(np.clip(big_pct + drift, 0.40, 0.70))
        mid_pct = max(0.0, 1 - retail_pct - big_pct)
        retail_sh = total * retail_pct
        big_sh = total * big_pct
        mid_sh = total * mid_pct
        # 散戶分兩級（第1、2級，§3.2 級距溢出考量）。
        rows += [
            {"snapshot_date": snap, "tier_min": 1, "tier_max": 999,
             "holders": 200000, "shares": retail_sh * 0.4, "pct": retail_pct * 40},
            {"snapshot_date": snap, "tier_min": 1000, "tier_max": 5000,
             "holders": 80000, "shares": retail_sh * 0.6, "pct": retail_pct * 60},
            {"snapshot_date": snap, "tier_min": 5001, "tier_max": 1000000,
             "holders": 5000, "shares": mid_sh, "pct": mid_pct * 100},
            {"snapshot_date": snap, "tier_min": 1000001, "tier_max": 10**12,
             "holders": 50, "shares": big_sh, "pct": big_pct * 100},
        ]
    return pd.DataFrame(rows)


def _attach_oddlot_proxy(daily: pd.DataFrame, rng=None) -> None:
    """零股代理（SPEC §13 open item）：以整股量比例 + 雜訊合成盤中/盤後零股量。

    盤後零股套利雜訊最低（§1 表 A，權重 1.0）；盤中零股偶發飆高以觸發 §5 套利偵測。
    """
    if rng is None:
        rng = np.random.default_rng(7)
    v = daily["v_whole"].to_numpy()
    n = len(v)
    intra_ratio = np.full(n, 0.02) * (1 + rng.normal(0, 0.3, n))
    # 注入幾天套利飆高。
    spikes = rng.choice(n, size=max(1, n // 40), replace=False)
    intra_ratio[spikes] *= rng.uniform(4, 9, len(spikes))
    daily["v_intra"] = np.clip(v * intra_ratio, 0, None)
    daily["v_after"] = np.clip(v * 0.015 * (1 + rng.normal(0, 0.2, n)), 0, None)
    daily["vwap_intra"] = daily["vwap_whole"] * (1 + rng.normal(0, 0.002, n))
    daily["vwap_after"] = daily["vwap_whole"] * (1 + rng.normal(0, 0.0015, n))


def _business_days(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _is_etf(stock_id: str) -> bool:
    return stock_id.startswith("00")


def _seed(stock_id: str) -> int:
    return int(hashlib.md5(stock_id.encode()).hexdigest(), 16) % (2**32)
