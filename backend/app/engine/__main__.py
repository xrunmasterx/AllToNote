from __future__ import annotations

import argparse
from pathlib import Path

from app.engine.host import run_engine_host


def main() -> int:
    parser = argparse.ArgumentParser(prog="alltonote-engine-private")
    parser.add_argument("--engine-root", required=True, type=Path)
    parser.add_argument("--log-root", required=True, type=Path)
    parser.add_argument("--scope-id", required=True)
    args = parser.parse_args()
    return run_engine_host(
        engine_root=args.engine_root,
        log_root=args.log_root,
        scope_id=args.scope_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
