from typing import Any, Dict, List, Optional
from pathlib import Path

import pandas as pd


def load_symbols(cfg: Dict[str, Any]) -> List[str]:
    symbols = cfg.get("symbols") or []

    universe_file = cfg.get("universe_file")
    if not symbols and universe_file and Path(universe_file).exists():
        df = pd.read_csv(universe_file)
        col = "symbol" if "symbol" in df.columns else df.columns[0]
        symbols = df[col].dropna().astype(str).tolist()

    return sorted({s.strip().upper() for s in symbols if s and s.strip()})
