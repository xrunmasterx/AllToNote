from __future__ import annotations

from collections.abc import Iterable, Mapping


def human_diagnostic_lines(
    summary: str,
    checks: Iterable[Mapping[str, object]],
) -> tuple[str, ...]:
    lines = [summary]
    for check in checks:
        status = str(check["status"])
        if status == "pass":
            continue
        line = f"{status.upper()} [{check['code']}]"
        action = check.get("action")
        if action:
            line = f"{line}: {action}"
        lines.append(line)
    return tuple(lines)


__all__ = ["human_diagnostic_lines"]
