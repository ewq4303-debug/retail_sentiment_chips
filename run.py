#!/usr/bin/env python3
"""Pipeline 進入點。GitHub Actions cron 與本地皆呼叫此檔。

用法：
    python run.py                      # 用 config/universe.txt，輸出到 docs/data
    python run.py --backend synthetic  # 強制合成資料（CI smoke test / demo）
    python run.py --stocks 2330 0050   # 覆寫追蹤標的
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from retail_sentiment.config import Config  # noqa: E402
from retail_sentiment.pipeline import run  # noqa: E402

ROOT = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Retail sentiment chips pipeline")
    ap.add_argument("--out", default=str(ROOT / "docs" / "data"), help="輸出目錄")
    ap.add_argument("--backend", default=None, choices=[None, "finmind", "synthetic"])
    ap.add_argument("--stocks", nargs="*", default=None, help="覆寫 universe")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = Config.load(args.config)
    result = run(Path(args.out), cfg=cfg, universe=args.stocks, backend=args.backend)
    print(f"[done] backend={result['backend']} stocks={result['count']} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
