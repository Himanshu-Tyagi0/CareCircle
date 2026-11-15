"""Thin background worker entrypoint for scheduled reminder and outbox runs."""

from __future__ import annotations

import json
import sys

from core.config import load_config
from core.errors import ConfigurationError
from services.container import build_container


def main() -> int:
    """Run one bounded reminder cycle and emit a safe structured summary."""
    cfg = load_config()
    try:
        container = build_container(cfg)
    except ConfigurationError as exc:
        print(json.dumps({"status": "error", "code": exc.code}), file=sys.stderr)
        return 2

    summary = container.worker.run_cycle()
    print(
        json.dumps(
            {"status": "ok", "mode": "demo" if cfg.demo_mode else "production", "summary": summary}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
