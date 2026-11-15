"""Safe configuration diagnostic tool reporting status without printing secret values."""

from __future__ import annotations

import json

from core.config import load_config


def main() -> int:
    """Print non-secret runtime capability diagnostics as JSON."""
    cfg = load_config()
    print(json.dumps(cfg.safe_diagnostics(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
