"""全域設定載入（SPEC §1 Config）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


@dataclass
class Config:
    """對應 SPEC §1 的全域參數。欄位名與 YAML key 一致（小寫）。"""

    retail_tier_max_shares: int = 5000
    big_holder_rule: dict[str, Any] = field(default_factory=dict)
    z_window: int = 60
    dca_trend_window: int = 60
    dca_seasonal_lookback_months: int = 12
    noise_reconcile_tol: float = 0.15
    enable_spread_filter: bool = False
    arb_ratio_z_threshold: float = 2.0
    w_arb_floor: float = 0.2
    arb_k: float = 4.0
    prov_alpha: float = 0.5
    prov_beta: float = 0.5
    matrix_w_inst: float = 0.5
    matrix_w_retail: float = 0.5
    lookback_trading_days: int = 180
    etf_dca_intensity: float = 1.0

    # 衍生路徑
    root: Path = ROOT

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else CONFIG_DIR / "config.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # YAML key 為大寫，轉成 dataclass 的小寫欄位。
        kwargs: dict[str, Any] = {}
        for k, v in raw.items():
            field_name = k.lower()
            if field_name in cls.__annotations__:
                kwargs[field_name] = v
        return cls(**kwargs)


def load_universe(path: str | Path | None = None) -> list[str]:
    """讀取追蹤標的清單。"""
    path = Path(path) if path else CONFIG_DIR / "universe.txt"
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def load_corporate_actions(path: str | Path | None = None) -> dict[str, list[dict]]:
    """讀取公司行動行事曆（SPEC §2.1）。"""
    path = Path(path) if path else CONFIG_DIR / "corporate_actions.yaml"
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
