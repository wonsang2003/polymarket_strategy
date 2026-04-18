from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_strat.infrastructure.real_data import load_real_data_status


def main() -> None:
    print(json.dumps(load_real_data_status(), indent=2))


if __name__ == "__main__":
    main()
