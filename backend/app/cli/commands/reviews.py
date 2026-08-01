from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from app.cli.contracts import ApplicationResult

if TYPE_CHECKING:
    from app.core.application.review_candidate_service import ReviewCandidateService


def add_review_parsers(subparsers: argparse._SubParsersAction) -> None:
    review_parser = subparsers.add_parser("review")
    review_commands = review_parser.add_subparsers(
        dest="review_command",
        required=True,
    )
    show = review_commands.add_parser("show")
    show.add_argument("draft_id", help="draft artifact ID")
    focus = show.add_mutually_exclusive_group()
    focus.add_argument("--evidence-id")
    focus.add_argument("--note-item-id")
    show.add_argument("--workspace", type=Path)
    show.add_argument("--config-profile")
    show.add_argument("--json", action="store_true")


def execute_review_command(
    args: argparse.Namespace,
    correlation_id: str,
    *,
    service: ReviewCandidateService,
    workspace_root: Path,
    versions: Mapping[str, object],
) -> ApplicationResult:
    data = service.show(
        workspace_root,
        args.draft_id,
        evidence_id=args.evidence_id,
        note_item_id=args.note_item_id,
    )
    candidate = data.get("candidate")
    artifacts = (candidate,) if isinstance(candidate, Mapping) else ()
    return ApplicationResult(
        command="review show",
        correlation_id=correlation_id,
        ok=True,
        data=data,
        artifacts=artifacts,
        versions=versions,
        human_lines=_human_lines(data),
    )


def _human_lines(data: Mapping[str, object]) -> tuple[str, ...]:
    source = data.get("source")
    quality = data.get("quality")
    focus = data.get("focus")
    lines: list[str] = []
    if isinstance(source, Mapping):
        label = source.get("title") or source.get("name") or "Review candidate"
        lines.append(str(label))
    if isinstance(quality, Mapping):
        lines.append(f"Quality: {quality.get('overall')}")
        reports = quality.get("reports")
        if isinstance(reports, list):
            for report in reports:
                if not isinstance(report, Mapping):
                    continue
                checks = report.get("checks")
                if isinstance(checks, list):
                    for check in checks:
                        if not isinstance(check, Mapping):
                            continue
                        if check.get("status") in {"warn", "fail", "skipped"}:
                            reason = check.get("reason")
                            suffix = f" — {reason}" if reason else ""
                            lines.append(
                                f"{check.get('status')}: {check.get('id')}{suffix}"
                            )
                messages = report.get("messages")
                if isinstance(messages, list):
                    lines.extend(f"Message: {message}" for message in messages)
    if isinstance(focus, Mapping) and focus.get("kind") == "evidence":
        locator = focus.get("locator")
        if isinstance(locator, Mapping):
            lines.append(
                f"Evidence: {_format_time(locator.get('start_ms'))}–"
                f"{_format_time(locator.get('end_ms'))}"
            )
        excerpt = focus.get("excerpt")
        if isinstance(excerpt, str):
            lines.append(excerpt)
    elif isinstance(focus, Mapping) and focus.get("kind") == "note_item":
        verification = focus.get("verification")
        if isinstance(verification, Mapping):
            lines.append(f"Verification: {verification.get('status')}")
        blocks = focus.get("source_blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                lines.append(f"Page {block.get('page')}: {block.get('text')}")
    return tuple(lines)


def _format_time(value: object) -> str:
    milliseconds = value if type(value) is int and value >= 0 else 0
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


__all__ = ["add_review_parsers", "execute_review_command"]
