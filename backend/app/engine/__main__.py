from __future__ import annotations

import argparse
from pathlib import Path

from app.engine.host import run_engine_host
from app.engine.instance import EngineInstancePaths
from app.runtime_paths import RuntimePaths


def main() -> int:
    parser = argparse.ArgumentParser(prog="alltonote-engine-private")
    parser.add_argument("--engine-root", required=True, type=Path)
    parser.add_argument("--log-root", required=True, type=Path)
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--runtime-log-dir", required=True, type=Path)
    args = parser.parse_args()
    paths = RuntimePaths(
        config_dir=args.config_dir,
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        state_dir=args.state_dir,
        log_dir=args.runtime_log_dir,
    )
    expected = EngineInstancePaths.from_runtime_paths(paths)
    if (
        args.engine_root.resolve(strict=False) != expected.root
        or args.log_root.resolve(strict=False) != expected.log_root
        or args.scope_id != expected.scope_id
    ):
        return 10
    return run_engine_host(paths=paths)


if __name__ == "__main__":
    raise SystemExit(main())
