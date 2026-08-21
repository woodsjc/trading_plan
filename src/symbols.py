from pathlib import Path
from typing import Any

import pandas as pd


def load_symbols(cfg: dict[str, Any]) -> list[str]:
    symbols = cfg.get("symbols") or []

    symbols_file = cfg.get("symbols_file")
    if not symbols and symbols_file and Path(symbols_file).exists():
        df = pd.read_csv(symbols_file)
        col = "symbol" if "symbol" in df.columns else df.columns[0]
        symbols = df[col].dropna().astype(str).tolist()

    return sorted({s.strip().upper() for s in symbols if s and s.strip()})
