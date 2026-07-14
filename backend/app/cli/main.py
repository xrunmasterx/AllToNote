from __future__ import annotations

import argparse
import json
from collections.abc import Sequence


RUNTIME_VERSION = "0.1.0"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alltonote")
    subparsers = parser.add_subparsers(dest="command", required=True)
    version_parser = subparsers.add_parser("version")
    version_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.json:
        print(
            json.dumps(
                {
                    "alltonote_cli_protocol_version": 1,
                    "ok": True,
                    "data": {"runtime_version": RUNTIME_VERSION},
                },
                separators=(",", ":"),
            )
        )
    else:
        print(RUNTIME_VERSION)
    return 0


def entrypoint() -> None:
    raise SystemExit(main())
