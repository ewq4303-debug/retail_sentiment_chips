#!/usr/bin/env python3
"""實證對帳：TDCC 週快照的對齊截止日 cutoff 該設在快照前第幾個交易日？

背景（SPEC §8）：TDCC 集保快照為週最後營業日（通常週五）收盤後的存摺餘額。
因 T+2 交割，該快照不含快照當日交易。爭議點是 cutoff 該往前推幾個交易日：

    k=0  快照日當天（週五）   — 天真錯誤對齊（含未交割日）
    k=1  快照前 1 交易日（週四）— 現行程式碼 calendar_utils.cutoff = previous_trading_day
    k=2  快照前 2 交易日（週三）— 嚴格 T+2：週三成交 T+2=週五交割，恰好入帳
    k=3  快照前 3 交易日（週二）

驗證原理（關鍵）：cutoff 只決定「日頻驅動如何被分組成週」，**不改變 ΔS_retail 本身**
（後者直接來自集保存量）。因此可滑動 cutoff offset k，看哪個分組讓週散戶淨流
ΔS_retail_w 與「與 cutoff 無關的真實日頻驅動」的關係最強。關係在哪個 k 達到峰值，
就是資料支持的 cutoff。

獨立日頻驅動（皆為真實資料、與 cutoff 無關，故不會循環論證）：
  * inst : z_inst（真實三大法人淨額的 60 日 z 分數）。系統 §9.3 背離論點假設散戶與
           法人對作 → 預期與 ΔS_retail 為「負相關」（法人買、散戶賣）。
  * ret  : 由真實 TWSE 盤後零股均價 vwap_after 算的日報酬，於窗口內累加。

注意：絕不可用 signed_flow / z_retail 當驅動——它們是用現行 k=1 cutoff 算出來的，
會循環論證。本腳本只用 ΔStock（集保真值）與上述兩個獨立驅動。

純標準庫，無第三方依賴。用法：
    python scripts/validate_cutoff.py
"""
from __future__ import annotations

import csv
import json
import math
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TDCC_DIR = ROOT / "cache" / "tdcc"
ODDLOT_DIR = ROOT / "cache" / "oddlot"
DAILY_DIR = ROOT / "docs" / "data" / "daily"

RETAIL_TIER_MAX = 5000   # 散戶：tier_max ≤ 5000（含第 1、2 級，SPEC §3.2）
CANDIDATES = [0, 1, 2, 3]
WEEKDAY_OF_K = {0: "週五(snap)", 1: "週四", 2: "週三", 3: "週二"}  # 以週五快照為基準


# ───────────────────────────── 讀檔 ─────────────────────────────────────────

def _to_date(s: str) -> date:
    s = str(s).strip()[:10].replace("/", "-")
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def load_tdcc(stock_id: str) -> list[dict]:
    """各快照的散戶存量與總存量（散戶 = tier_max ≤ 5000 合計）。"""
    rows: dict[date, dict] = {}
    with open(TDCC_DIR / f"{stock_id}.csv", newline="") as f:
        for r in csv.DictReader(f):
            snap = _to_date(r["snapshot_date"])
            tier_max = float(r["tier_max"])
            shares = float(r["shares"])
            agg = rows.setdefault(snap, {"snapshot": snap, "retail": 0.0, "total": 0.0})
            agg["total"] += shares
            if tier_max <= RETAIL_TIER_MAX:
                agg["retail"] += shares
    return [rows[s] for s in sorted(rows)]


def load_oddlot_returns(stock_id: str) -> dict[date, float]:
    """由真實盤後零股均價 vwap_after（缺則 vwap_intra）算日報酬。"""
    px: dict[date, float] = {}
    with open(ODDLOT_DIR / f"{stock_id}.csv", newline="") as f:
        for r in csv.DictReader(f):
            p = r.get("vwap_after") or r.get("vwap_intra")
            try:
                v = float(p)
            except (TypeError, ValueError):
                continue
            if v > 0:
                px[_to_date(r["date"])] = v
    days = sorted(px)
    ret: dict[date, float] = {}
    for i in range(1, len(days)):
        prev, cur = px[days[i - 1]], px[days[i]]
        ret[days[i]] = cur / prev - 1.0
    return ret


def load_z_inst(stock_id: str) -> dict[date, float]:
    """真實三大法人淨額的 z 分數（與 cutoff 無關的獨立驅動）。"""
    obj = json.loads((DAILY_DIR / f"{stock_id}.json").read_text())
    out: dict[date, float] = {}
    for r in obj["daily"]:
        if r.get("z_inst") is not None:
            out[_to_date(r["date"])] = float(r["z_inst"])
    return out


# ─────────────────────────── 交易日曆 / 窗口 ─────────────────────────────────

def cutoff_k(snap: date, trading_days: list[date], k: int) -> date | None:
    """快照前第 k 個交易日（k=0 → 快照當日或之前最近交易日）。"""
    if k == 0:
        le = [d for d in trading_days if d <= snap]
        return le[-1] if le else None
    lt = [d for d in trading_days if d < snap]
    return lt[-k] if len(lt) >= k else None


def window_days(lo_excl: date, hi_incl: date, trading_days: list[date]) -> list[date]:
    """半開區間 (lo, hi] 的交易日（SPEC §8 𝒲_w）。"""
    return [d for d in trading_days if lo_excl < d <= hi_incl]


# ─────────────────────────── 統計（純標準庫）────────────────────────────────

def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def _ranks(vs: list[float]) -> list[float]:
    order = sorted(range(len(vs)), key=lambda i: vs[i])
    ranks = [0.0] * len(vs)
    i = 0
    while i < len(vs):
        j = i
        while j + 1 < len(vs) and vs[order[j + 1]] == vs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 平均秩，1-based
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    return pearson(_ranks(xs), _ranks(ys))


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def pearson_pvalue(r: float, n: int) -> float:
    """Pearson r 的雙尾 p 值（Student-t，df=n-2）。"""
    if n < 3 or math.isnan(r) or abs(r) >= 1.0:
        return float("nan")
    df = n - 2
    t2 = r * r * df / (1.0 - r * r)
    return _betai(0.5 * df, 0.5, df / (df + t2))


def demean_within_stock(pairs: list[tuple[str, float, float]]) -> tuple[list[float], list[float]]:
    """移除個股固定效果：各股各自減去 x、y 的組內均值後再 pool。"""
    by: dict[str, list[tuple[float, float]]] = {}
    for sid, x, y in pairs:
        by.setdefault(sid, []).append((x, y))
    xs, ys = [], []
    for sid, lst in by.items():
        mx = sum(p[0] for p in lst) / len(lst)
        my = sum(p[1] for p in lst) / len(lst)
        for x, y in lst:
            xs.append(x - mx)
            ys.append(y - my)
    return xs, ys


# ─────────────────────────────── 主流程 ─────────────────────────────────────

def build_pairs(stocks: list[str]):
    """回傳 trading_days 與每檔的 (snapshots, returns, z_inst)。"""
    data = {}
    all_days: set[date] = set()
    for sid in stocks:
        snaps = load_tdcc(sid)
        rets = load_oddlot_returns(sid)
        zi = load_z_inst(sid)
        data[sid] = {"snaps": snaps, "ret": rets, "zinst": zi}
        all_days |= set(rets.keys())
    return sorted(all_days), data


def aggregate(driver: str, days: list[date], series: dict[date, float]) -> float | None:
    vals = [series[d] for d in days if d in series]
    if not vals:
        return None
    if driver == "ret":
        return sum(vals)          # 累積報酬
    return sum(vals) / len(vals)  # z_inst：窗口平均法人傾向


def scan(stocks: list[str]):
    trading_days, data = build_pairs(stocks)

    print(f"標的：{', '.join(stocks)}")
    print(f"交易日曆：{trading_days[0]} → {trading_days[-1]}（{len(trading_days)} 個交易日）\n")

    # 快照星期幾分布（說明 k→星期 的映射在假日週會位移）。
    wk = ["一", "二", "三", "四", "五", "六", "日"]
    snaps0 = data[stocks[0]]["snaps"]
    print("集保快照（以 " + stocks[0] + " 為例）與星期：")
    for s in snaps0:
        print(f"  {s['snapshot']} (週{wk[s['snapshot'].weekday()]})")
    print()

    results = {}
    for driver in ("inst", "ret"):
        series_key = "zinst" if driver == "inst" else "ret"
        rows = []
        for k in CANDIDATES:
            pairs: list[tuple[str, float, float]] = []   # (stock, driver, ΔS_retail_bps)
            for sid in stocks:
                snaps = data[sid]["snaps"]
                series = data[sid][series_key]
                for w in range(1, len(snaps)):
                    prev, cur = snaps[w - 1], snaps[w]
                    c_prev = cutoff_k(prev["snapshot"], trading_days, k)
                    c_cur = cutoff_k(cur["snapshot"], trading_days, k)
                    if c_prev is None or c_cur is None or c_prev >= c_cur:
                        continue
                    wd = window_days(c_prev, c_cur, trading_days)
                    drv = aggregate(driver, wd, series)
                    if drv is None or cur["total"] <= 0:
                        continue
                    d_retail_bps = (cur["retail"] - prev["retail"]) / cur["total"] * 1e4
                    pairs.append((sid, drv, d_retail_bps))
            if len(pairs) < 3:
                continue
            xs = [p[1] for p in pairs]
            ys = [p[2] for p in pairs]
            r = pearson(xs, ys)
            rho = spearman(xs, ys)
            p = pearson_pvalue(r, len(xs))
            dx, dy = demean_within_stock(pairs)
            r_w = pearson(dx, dy)
            p_w = pearson_pvalue(r_w, len(dx))
            rows.append({"k": k, "n": len(xs), "r": r, "p": p,
                         "rho": rho, "r_within": r_w, "p_within": p_w})
        results[driver] = rows

    return results


def _fmt(v: float) -> str:
    return "  nan" if (v is None or math.isnan(v)) else f"{v:+.3f}"


def report(results: dict):
    expectation = {"inst": "預期負相關（法人買→散戶賣，SPEC §9.3）",
                   "ret": "符號待定，看峰值落點"}
    for driver, rows in results.items():
        print("=" * 74)
        print(f"驅動：{driver}    {expectation[driver]}")
        print("-" * 74)
        print(f"{'k':>2} {'cutoff(週五快照)':<14} {'n':>3} {'pearson_r':>10} "
              f"{'p':>8} {'spearman':>9} {'within_r':>9} {'within_p':>9}")
        for row in rows:
            print(f"{row['k']:>2} {WEEKDAY_OF_K[row['k']]:<14} {row['n']:>3} "
                  f"{_fmt(row['r']):>10} {_fmt(row['p']):>8} {_fmt(row['rho']):>9} "
                  f"{_fmt(row['r_within']):>9} {_fmt(row['p_within']):>9}")

        # 以「組內去均值 Pearson」做判讀（已移除個股規模固定效果）。
        cand = [r for r in rows if not math.isnan(r["r_within"])]
        if cand:
            if driver == "inst":
                best = min(cand, key=lambda r: r["r_within"])   # 最負
                strength = best["r_within"]
            else:
                best = max(cand, key=lambda r: abs(r["r_within"]))
                strength = best["r_within"]
            print(f"\n  → 關係最強落在 k={best['k']}（{WEEKDAY_OF_K[best['k']]}），"
                  f"within_r={strength:+.3f}, p={best['p_within']:.3f}")
        print()

    print("=" * 74)
    print("判讀指引：")
    print("  k=1（週四）= 現行程式碼 calendar_utils.cutoff；k=2（週三）= 嚴格 T+2 假設。")
    print("  關係峰值落在 k=1 → 支持現行；落在 k=2 → 支持改為週三。")
    print("  注意：僅 8 週 × 5 檔的小樣本，p 值僅供參考；本腳本隨集保每週累積會更可靠。")


def main() -> int:
    stocks = sorted(p.stem for p in TDCC_DIR.glob("*.csv"))
    if not stocks:
        print("找不到 cache/tdcc/*.csv")
        return 1
    results = scan(stocks)
    report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
