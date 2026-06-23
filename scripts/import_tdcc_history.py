#!/usr/bin/env python3
"""把外部 repo 累積的 TDCC 週頻集保歷史(原始 opendata 格式)匯入成本專案 cache。

來源格式（與 TDCC opendata 一致）：
    資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%

輸出：cache/tdcc/{stock_id}.csv（snapshot_date,tier_min,tier_max,holders,shares,pct），
之後 pipeline 直接吃、cron 繼續往後累積。

用法：
    # 從 GitHub 公開 repo 目錄匯入（每週一檔 .csv）
    python scripts/import_tdcc_history.py --from-github ewq4303-debug/stocknotice:tdcc_history
    # 或從本機目錄匯入
    python scripts/import_tdcc_history.py --local /path/to/tdcc_history
    # 預設只匯入 config/universe.txt 的標的；--all 匯入全部
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retail_sentiment import tdcc  # noqa: E402
from retail_sentiment.config import load_universe  # noqa: E402

API = "https://api.github.com/repos/{owner}/{repo}/contents/{path}"
_HDR = {"User-Agent": "tdcc-importer", "Accept": "application/vnd.github+json"}


def iter_github_csvs(spec: str):
    owner_repo, _, path = spec.partition(":")
    owner, repo = owner_repo.split("/")
    r = requests.get(API.format(owner=owner, repo=repo, path=path), headers=_HDR, timeout=30)
    r.raise_for_status()
    for item in r.json():
        if item["name"].lower().endswith(".csv"):
            txt = requests.get(item["download_url"], headers=_HDR, timeout=60).text
            yield item["name"], txt


def iter_local_csvs(d: str):
    for p in sorted(Path(d).glob("*.csv")):
        yield p.name, p.read_text(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-github", help="owner/repo:path")
    g.add_argument("--local", help="本機目錄")
    ap.add_argument("--all", action="store_true", help="匯入全部標的（預設只 universe）")
    args = ap.parse_args()

    universe = None if args.all else set(load_universe())
    source = iter_github_csvs(args.from_github) if args.from_github else iter_local_csvs(args.local)

    # 累積 {stock_id: [週 DataFrame, ...]}
    by_stock: dict[str, list[pd.DataFrame]] = {}
    n_files = 0
    for name, text in source:
        raw = pd.read_csv(io.StringIO(text), dtype=str)
        norm = tdcc._normalize(raw)
        if norm.empty:
            print(f"[skip] {name}: 無法解析")
            continue
        if universe is not None:
            norm = norm[norm["stock_id"].isin(universe)]
        for sid, g in norm.groupby("stock_id"):
            by_stock.setdefault(sid, []).append(g)
        n_files += 1
        print(f"[ok] {name}: {norm['snapshot_date'].iloc[0]} · {norm['stock_id'].nunique()} stocks")

    written = 0
    for sid, parts in by_stock.items():
        merged = pd.concat(parts, ignore_index=True)
        tdcc._upsert_cache(ROOT, sid, merged)
        written += 1
    weeks = sorted({p["snapshot_date"].iloc[0] for parts in by_stock.values() for p in parts})
    print(f"\n[done] {n_files} files → cache/tdcc/, {written} stocks, "
          f"{len(weeks)} weeks ({weeks[0]} … {weeks[-1]})" if weeks else "[done] nothing imported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
