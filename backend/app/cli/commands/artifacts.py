from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path

from app.cli.contracts import ApplicationResult
from app.core.application.artifact_query_service import ArtifactQueryService


_DEFAULT_DRAFT_SHOW_BYTES = 256 * 1024


def add_artifact_parsers(subparsers: argparse._SubParsersAction) -> None:
    artifact_parser = subparsers.add_parser("artifact")
    artifact_commands = artifact_parser.add_subparsers(
        dest="artifact_command",
        required=True,
    )
    for command in ("inspect", "show"):
        parser = artifact_commands.add_parser(command)
        parser.add_argument("target_id", help="artifact or bundle ID")
        _add_common_options(parser)

    draft_parser = subparsers.add_parser("draft")
    draft_commands = draft_parser.add_subparsers(
        dest="draft_command",
        required=True,
    )
    draft_inspect = draft_commands.add_parser("inspect")
    draft_inspect.add_argument("draft_id", help="draft artifact ID")
    _add_common_options(draft_inspect)

    draft_show = draft_commands.add_parser("show")
    draft_show.add_argument("draft_id", help="draft artifact ID")
    _add_common_options(draft_show)
    draft_show.add_argument(
        "--presentation",
        choices=("reading", "audit"),
        default="reading",
        help="render clean reading Markdown or the canonical audited draft",
    )


def execute_artifact_command(
    args: argparse.Namespace,
    correlation_id: str,
    *,
    service: ArtifactQueryService,
    workspace_root: Path,
    versions: Mapping[str, object],
) -> ApplicationResult:
    if args.command == "artifact":
        command = f"artifact {args.artifact_command}"
        data = service.inspect_artifact(
            workspace_root,
            args.target_id,
            body_bytes=args.body_bytes,
        )
    else:
        command = f"draft {args.draft_command}"
        show = args.draft_command == "show"
        data = service.inspect_draft(
            workspace_root,
            args.draft_id,
            body_bytes=(
                args.body_bytes
                if args.body_bytes is not None or not show
                else _DEFAULT_DRAFT_SHOW_BYTES
            ),
            presentation=args.presentation if show else "audit",
        )
    artifacts = _result_artifacts(data)
    return ApplicationResult(
        command=command,
        correlation_id=correlation_id,
        ok=True,
        data=data,
        artifacts=artifacts,
        versions=versions,
        human_lines=_human_lines(
            data,
            body_only=args.command == "draft" and args.draft_command == "show",
        ),
    )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--config-profile")
    parser.add_argument(
        "--body-bytes",
        type=int,
        help="include at most N UTF-8 body bytes (maximum 262144)",
    )
    parser.add_argument("--json", action="store_true")


def _result_artifacts(
    data: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    target = data.get("artifact") or data.get("draft")
    if isinstance(target, Mapping):
        return (target,)
    inventory = data.get("artifacts")
    if isinstance(inventory, list):
        return tuple(item for item in inventory if isinstance(item, Mapping))
    return ()


def _human_lines(
    data: Mapping[str, object],
    *,
    body_only: bool = False,
) -> tuple[str, ...]:
    if body_only:
        body = data.get("body")
        if isinstance(body, str):
            return (body.rstrip("\r\n"),)
    bundle = data.get("bundle")
    bundle_id = bundle.get("bundle_id") if isinstance(bundle, Mapping) else None
    target = data.get("artifact") or data.get("draft")
    if isinstance(target, Mapping):
        return (
            f"Artifact: {target.get('artifact_id')}",
            f"Kind: {target.get('kind')}",
            f"Size: {target.get('size_bytes')} bytes",
            f"Bundle: {bundle_id}",
        )
    count = bundle.get("artifact_count") if isinstance(bundle, Mapping) else None
    return (f"Bundle: {bundle_id}", f"Artifacts: {count}")


__all__ = ["add_artifact_parsers", "execute_artifact_command"]
