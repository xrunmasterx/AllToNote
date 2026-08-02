from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TextIO

from app.cli.contracts import ApplicationResult, CLI_PROTOCOL_VERSION
from app.cli.diagnostics import human_diagnostic_lines
from app.cli.errors import (
    ExitCode,
    MappedCliError,
    internal_error,
    map_domain_error,
    map_error_detail,
)
from app.cli.render import (
    configure_standard_streams,
    render_json_lines,
    render_result,
)
from app.core.domain.ids import new_typed_id
from app.core.domain.production import RecipeProduceResult
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    JobSnapshot,
    JobState,
    ScreenshotPolicy,
    VideoDocumentKind,
    VideoProduceRequest,
    VideoProduceResult,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission, RecipeKey

if TYPE_CHECKING:
    from app.adapters.credentials.keyring_broker import CredentialBroker
    from app.core.application.artifact_query_service import ArtifactQueryService
    from app.core.config.model import JobConfigSnapshot
    from app.job_runtime import JobRuntime
    from app.cli.pack_commands import PackService
    from app.runtime_config import EffectiveRuntimeConfig, RuntimeConfigService
    from app.runtime_paths import RuntimePaths


RUNTIME_VERSION = "0.1.0"
_DOCUMENT_NOTE_V1 = RecipeKey("alltonote.document-note", 1)


class _VideoRuntime(Protocol):
    workspace_instance_id: str | None

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> ProduceSubmission: ...

    def submit_video(
        self,
        request: VideoProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> JobSnapshot: ...

    def get_job(self, job_id: str) -> JobSnapshot: ...

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot: ...


class _CliUsageError(Exception):
    pass


class _DetachedJobNotificationError(DomainError):
    def __init__(self, error: DomainError, snapshot: JobSnapshot) -> None:
        super().__init__(error.code, error.category, error.message, error.details)
        self.snapshot = snapshot


class _CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _CliUsageError


def _build_parser(
    *,
    include_job_commands: bool = True,
    include_artifact_commands: bool = True,
    include_review_commands: bool = True,
) -> argparse.ArgumentParser:
    parser = _CliArgumentParser(prog="alltonote")
    subparsers = parser.add_subparsers(dest="command", required=True)
    version_parser = subparsers.add_parser("version")
    version_parser.add_argument("--json", action="store_true")
    runtime_parser = subparsers.add_parser("runtime")
    runtime_subparsers = runtime_parser.add_subparsers(
        dest="runtime_command", required=True
    )
    paths_parser = runtime_subparsers.add_parser("paths")
    paths_parser.add_argument("--json", action="store_true")
    paths_parser.add_argument("--show-paths", action="store_true")
    runtime_info_parser = runtime_subparsers.add_parser("info")
    runtime_info_parser.add_argument("--json", action="store_true")
    runtime_capabilities_parser = runtime_subparsers.add_parser("capabilities")
    runtime_capabilities_parser.add_argument("--json", action="store_true")
    runtime_doctor_parser = runtime_subparsers.add_parser("doctor")
    runtime_doctor_parser.add_argument("--dynamic", action="store_true")
    runtime_doctor_parser.add_argument("--json", action="store_true")
    sqlite_wal_gate_parser = runtime_subparsers.add_parser("sqlite-wal-gate")
    sqlite_wal_gate_parser.add_argument("--root", required=True, type=Path)
    sqlite_wal_gate_parser.add_argument("--json", action="store_true")
    engine_parser = subparsers.add_parser("engine")
    engine_subparsers = engine_parser.add_subparsers(
        dest="engine_command", required=True
    )
    for engine_command in ("status", "start", "ensure", "stop"):
        engine_command_parser = engine_subparsers.add_parser(engine_command)
        engine_command_parser.add_argument("--json", action="store_true")
    config_parser = subparsers.add_parser("config")
    config_subparsers = config_parser.add_subparsers(
        dest="config_command", required=True
    )
    config_get_parser = config_subparsers.add_parser("get")
    config_get_parser.add_argument("--profile")
    config_get_parser.add_argument("--json", action="store_true")
    config_validate_parser = config_subparsers.add_parser("validate")
    config_validate_parser.add_argument("--profile")
    config_validate_parser.add_argument("--json", action="store_true")
    config_profiles_parser = config_subparsers.add_parser("profiles")
    config_profiles_parser.add_argument("--json", action="store_true")
    config_provider_parser = config_subparsers.add_parser("provider")
    config_provider_subparsers = config_provider_parser.add_subparsers(
        dest="config_provider_command", required=True
    )
    config_provider_set_parser = config_provider_subparsers.add_parser("set")
    config_provider_set_parser.add_argument("profile")
    config_provider_set_parser.add_argument(
        "--type", dest="provider_type", required=True
    )
    config_provider_set_parser.add_argument("--model", required=True)
    config_provider_set_parser.add_argument("--credential-ref")
    config_provider_set_parser.add_argument("--json", action="store_true")
    config_set_parser = config_subparsers.add_parser("set")
    config_set_parser.add_argument(
        "key",
        choices=(
            "default_workspace",
            "default_provider_profile",
            "default_verifier_provider_profile",
            "default_transcriber_profile",
            "ffmpeg_path",
            "recipe_defaults.output_language",
            "recipe_defaults.quality_preset",
            "recipe_defaults.style",
            "recipe_defaults.screenshot_policy",
            "log_level",
            "work_directory",
        ),
    )
    config_set_parser.add_argument("value")
    config_set_parser.add_argument("--json", action="store_true")
    workspace_parser = subparsers.add_parser("workspace")
    workspace_subparsers = workspace_parser.add_subparsers(
        dest="workspace_command", required=True
    )
    workspace_init_parser = workspace_subparsers.add_parser("init")
    workspace_init_parser.add_argument("path", type=Path)
    workspace_init_parser.add_argument("--name", required=True)
    workspace_init_parser.add_argument("--set-default", action="store_true")
    workspace_init_parser.add_argument("--json", action="store_true")
    credential_parser = subparsers.add_parser("credential")
    credential_subparsers = credential_parser.add_subparsers(
        dest="credential_command", required=True
    )
    credential_set_parser = credential_subparsers.add_parser("set")
    credential_set_parser.add_argument("profile")
    credential_set_parser.add_argument("--stdin", action="store_true")
    credential_set_parser.add_argument("--json", action="store_true")
    credential_status_parser = credential_subparsers.add_parser("status")
    credential_status_parser.add_argument("profile")
    credential_status_parser.add_argument("--json", action="store_true")
    credential_delete_parser = credential_subparsers.add_parser("delete")
    credential_delete_parser.add_argument("profile")
    credential_delete_parser.add_argument("--json", action="store_true")
    pack_parser = subparsers.add_parser("pack")
    pack_subparsers = pack_parser.add_subparsers(dest="pack_command", required=True)
    pack_doctor_parser = pack_subparsers.add_parser("doctor")
    pack_doctor_parser.add_argument(
        "pack_id",
        choices=("document-basic", "media-basic", "transcribe-cpu"),
    )
    pack_doctor_parser.add_argument("--dynamic", action="store_true")
    pack_doctor_parser.add_argument("--json", action="store_true")
    pack_install_parser = pack_subparsers.add_parser("install")
    pack_install_parser.add_argument(
        "pack_id",
        choices=("document-basic", "media-basic", "transcribe-cpu"),
    )
    pack_install_parser.add_argument("--source", type=Path, required=True)
    pack_install_parser.add_argument("--repair", action="store_true")
    pack_install_parser.add_argument("--json", action="store_true")
    if include_job_commands:
        from app.cli.commands.jobs import add_job_parsers

        add_job_parsers(subparsers)
    if include_artifact_commands:
        from app.cli.commands.artifacts import add_artifact_parsers

        add_artifact_parsers(subparsers)
    if include_review_commands:
        from app.cli.commands.reviews import add_review_parsers

        add_review_parsers(subparsers)
    recipe_parser = subparsers.add_parser("recipe")
    recipe_subparsers = recipe_parser.add_subparsers(
        dest="recipe_command", required=True
    )
    recipe_list_parser = recipe_subparsers.add_parser("list")
    recipe_list_parser.add_argument("--json", action="store_true")
    recipe_describe_parser = recipe_subparsers.add_parser("describe")
    recipe_describe_parser.add_argument("selector")
    recipe_describe_parser.add_argument("--json", action="store_true")
    produce_parser = subparsers.add_parser("produce")
    produce_subparsers = produce_parser.add_subparsers(
        dest="produce_kind", required=True
    )
    generic_parser = produce_subparsers.add_parser(
        "_generic",
        prog="alltonote produce",
    )
    generic_parser.add_argument("input_value", nargs="?")
    generic_parser.add_argument("--recipe")
    generic_parser.add_argument("--request", type=Path)
    generic_parser.add_argument("--workspace", type=Path)
    generic_mode = generic_parser.add_mutually_exclusive_group()
    generic_mode.add_argument("--wait", dest="wait", action="store_true")
    generic_mode.add_argument("--detach", dest="wait", action="store_false")
    generic_parser.set_defaults(wait=True)
    generic_parser.add_argument("--json", action="store_true")
    video_parser = produce_subparsers.add_parser("video")
    video_parser.add_argument(
        "input_value",
        nargs="?",
        help="deprecated positional alias for --input",
    )
    video_parser.add_argument(
        "--input",
        dest="input_option",
        help="video URL or local video path",
    )
    video_parser.add_argument("--workspace", type=Path)
    video_mode = video_parser.add_mutually_exclusive_group()
    video_mode.add_argument("--wait", dest="wait", action="store_true")
    video_mode.add_argument("--detach", dest="wait", action="store_false")
    video_parser.set_defaults(wait=True)
    video_parser.add_argument("--json", action="store_true")
    video_parser.add_argument("--recipe-version", choices=(1, 2), type=int)
    video_parser.add_argument("--config-profile")
    video_parser.add_argument("--provider-profile")
    video_parser.add_argument("--transcriber-profile")
    video_parser.add_argument("--model")
    video_parser.add_argument("--quality", choices=("balanced",))
    video_parser.add_argument("--style")
    video_parser.add_argument(
        "--screenshot-policy",
        choices=tuple(policy.value for policy in ScreenshotPolicy),
    )
    video_parser.add_argument(
        "--output",
        action="append",
        choices=tuple(kind.value for kind in VideoDocumentKind),
    )
    video_parser.add_argument(
        "--faithful-language",
        choices=tuple(policy.value for policy in FaithfulLanguagePolicy),
        default=FaithfulLanguagePolicy.PRESERVE_SOURCE.value,
    )
    video_parser.add_argument("--output-language")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: _VideoRuntime | None = None,
    config_service: RuntimeConfigService | None = None,
    credential_broker: CredentialBroker | None = None,
    job_runtime: JobRuntime | None = None,
    artifact_query_service: ArtifactQueryService | None = None,
    pack_service: PackService | None = None,
    engine_client: object | None = None,
    input_stream: TextIO | None = None,
) -> int:
    arguments = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    parse_arguments = (
        ("produce", "_generic", *arguments[1:])
        if arguments
        and arguments[0] == "produce"
        and (
            len(arguments) == 1
            or arguments[1] != "video"
            or any(
                argument == option or argument.startswith(f"{option}=")
                for argument in arguments
                for option in ("--recipe", "--request")
            )
        )
        else arguments
    )
    correlation_id = new_typed_id("corr")
    json_requested = "--json" in arguments or "--jsonl" in arguments
    include_job_commands = (
        not arguments
        or arguments[0] == "job"
        or arguments[0] in {"-h", "--help"}
    )
    include_artifact_commands = (
        not arguments
        or arguments[0] in {"artifact", "draft", "-h", "--help"}
    )
    include_review_commands = (
        not arguments or arguments[0] in {"review", "-h", "--help"}
    )
    try:
        args = _build_parser(
            include_job_commands=include_job_commands,
            include_artifact_commands=include_artifact_commands,
            include_review_commands=include_review_commands,
        ).parse_args(parse_arguments)
        if (
            args.command == "produce"
            and args.produce_kind == "video"
            and (
                (args.input_value is None and args.input_option is None)
                or (args.input_value is not None and args.input_option is not None)
            )
        ):
            raise _CliUsageError
        if (
            args.command == "job"
            and args.job_command == "events"
            and args.follow
            and not args.jsonl
        ):
            raise _CliUsageError
        if (
            args.command == "produce"
            and args.produce_kind == "_generic"
            and (
                (args.request is None and (args.input_value is None or args.recipe is None))
                or (
                    args.request is not None
                    and (
                        args.input_value is not None
                        or args.recipe is not None
                        or args.workspace is not None
                    )
                )
            )
        ):
            raise _CliUsageError
    except _CliUsageError:
        mapped = map_domain_error(
            DomainError(
                "cli_usage_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Command arguments are invalid",
            )
        )
        result = _failure_result(
            command=_command_from_arguments(arguments),
            correlation_id=correlation_id,
            mapped=mapped,
        )
        render_result(result, json_mode=json_requested)
        return int(mapped.exit_code)

    if args.command == "version":
        result = ApplicationResult(
            command="version",
            correlation_id=correlation_id,
            ok=True,
            data={"runtime_version": RUNTIME_VERSION},
            versions=_versions(),
            human_lines=(RUNTIME_VERSION,),
        )
        render_result(result, json_mode=args.json)
        return int(ExitCode.SUCCESS)

    if args.command == "engine":
        command = f"engine {args.engine_command}"
        try:
            from app.cli.engine_commands import engine_command_result

            result = engine_command_result(
                args.engine_command,
                correlation_id,
                client=engine_client,
                paths=(config_service.paths if config_service is not None else None),
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "workspace":
        command = f"workspace {args.workspace_command}"
        try:
            from app.cli.workspace_commands import workspace_init_result

            result = workspace_init_result(
                args.path,
                args.name,
                set_default=args.set_default,
                correlation_id=correlation_id,
                config_service=config_service,
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "pack":
        command = f"pack {args.pack_command}"
        try:
            from app.cli.pack_commands import pack_command_result

            result = pack_command_result(
                args,
                correlation_id,
                service=pack_service,
                versions=_versions(),
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except KeyboardInterrupt:
            mapped = map_domain_error(
                DomainError(
                    "interrupted",
                    ErrorCategory.CANCELLED,
                    "Command was interrupted",
                )
            )
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = ExitCode.INTERRUPTED
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "recipe":
        command = f"recipe {args.recipe_command}"
        try:
            from app.cli.recipe_commands import (
                recipe_describe_result,
                recipe_list_result,
            )

            result = (
                recipe_list_result(correlation_id)
                if args.recipe_command == "list"
                else recipe_describe_result(args.selector, correlation_id)
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "runtime":
        try:
            result = _runtime_command_result(args, correlation_id)
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=f"runtime {args.runtime_command}",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=f"runtime {args.runtime_command}",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(
            result,
            json_mode=args.json,
            show_paths=getattr(args, "show_paths", False),
        )
        return int(exit_code)

    if args.command == "config":
        command = (
            f"config provider {args.config_provider_command}"
            if args.config_command == "provider"
            else f"config {args.config_command}"
        )
        try:
            result = _config_command_result(
                args,
                correlation_id,
                config_service=config_service,
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "credential":
        try:
            result = _credential_command_result(
                args,
                correlation_id,
                credential_broker=credential_broker,
                input_stream=input_stream,
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=f"credential {args.credential_command}",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=f"credential {args.credential_command}",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "job":
        command = f"job {args.job_command}"
        try:
            from app.cli.commands.jobs import execute_job_command

            active_job_runtime = job_runtime or _job_runtime_for_args(
                args,
                runtime=runtime,
                config_service=config_service,
            )
            execution = execute_job_command(
                args,
                correlation_id,
                runtime=active_job_runtime,
                versions=_versions(),
            )
            result = execution.result
            exit_code = execution.exit_code
            if getattr(args, "jsonl", False):
                render_json_lines(execution.jsonl_records)
                return int(exit_code)
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
            execution = None
        except KeyboardInterrupt:
            mapped = map_domain_error(
                DomainError(
                    "interrupted",
                    ErrorCategory.CANCELLED,
                    "Command was interrupted",
                )
            )
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = ExitCode.INTERRUPTED
            execution = None
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
            execution = None
        render_result(
            result,
            json_mode=(getattr(args, "json", False) or json_requested),
        )
        return int(exit_code)

    if args.command in {"artifact", "draft"}:
        nested_command = (
            args.artifact_command
            if args.command == "artifact"
            else args.draft_command
        )
        command = f"{args.command} {nested_command}"
        try:
            from app.cli.commands.artifacts import execute_artifact_command
            from app.core.application.artifact_query_service import (
                ArtifactQueryService,
            )

            workspace_root = _artifact_workspace_for_args(
                args,
                config_service=config_service,
            )
            result = execute_artifact_command(
                args,
                correlation_id,
                service=(artifact_query_service or ArtifactQueryService()),
                workspace_root=workspace_root,
                versions=_versions(),
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "review":
        command = f"review {args.review_command}"
        try:
            from app.cli.commands.reviews import execute_review_command
            from app.core.application.review_candidate_service import (
                ReviewCandidateService,
            )

            workspace_root = _artifact_workspace_for_args(
                args,
                config_service=config_service,
            )
            result = execute_review_command(
                args,
                correlation_id,
                service=ReviewCandidateService(),
                workspace_root=workspace_root,
                versions=_versions(),
            )
            exit_code = ExitCode.SUCCESS
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    if args.command == "produce" and args.produce_kind == "_generic":
        try:
            snapshot = _produce_generic(
                args,
                runtime,
                correlation_id,
                config_service=config_service,
                engine_client=engine_client,
            )
            result, exit_code = _video_snapshot_result(
                snapshot,
                correlation_id,
                command="produce",
            )
        except _DetachedJobNotificationError as error:
            mapped = _map_detached_notification_error(error)
            result = _failure_result(
                command="produce",
                correlation_id=correlation_id,
                mapped=mapped,
                data={
                    "job_id": error.snapshot.job_id,
                    "state": error.snapshot.state.value,
                },
                job=_job_projection(error.snapshot),
            )
            exit_code = (
                ExitCode.INTERRUPTED
                if error.code == "interrupted"
                else mapped.exit_code
            )
        except DomainError as error:
            mapped = map_domain_error(error)
            result = _failure_result(
                command="produce",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        except KeyboardInterrupt:
            mapped = map_domain_error(
                DomainError("interrupted", ErrorCategory.CANCELLED, "Command was interrupted")
            )
            result = _failure_result(
                command="produce",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = ExitCode.INTERRUPTED
        except Exception:
            mapped = internal_error()
            result = _failure_result(
                command="produce",
                correlation_id=correlation_id,
                mapped=mapped,
            )
            exit_code = mapped.exit_code
        render_result(result, json_mode=args.json)
        return int(exit_code)

    try:
        snapshot = _produce_video(
            args,
            runtime,
            correlation_id,
            config_service=config_service,
            engine_client=engine_client,
        )
        result, exit_code = _video_snapshot_result(snapshot, correlation_id)
    except _DetachedJobNotificationError as error:
        mapped = _map_detached_notification_error(error)
        result = _failure_result(
            command="produce video",
            correlation_id=correlation_id,
            mapped=mapped,
            data={
                "job_id": error.snapshot.job_id,
                "state": error.snapshot.state.value,
            },
            job=_job_projection(error.snapshot),
        )
        exit_code = (
            ExitCode.INTERRUPTED
            if error.code == "interrupted"
            else mapped.exit_code
        )
    except DomainError as error:
        mapped = map_domain_error(error)
        result = _failure_result(
            command="produce video",
            correlation_id=correlation_id,
            mapped=mapped,
        )
        exit_code = mapped.exit_code
    except KeyboardInterrupt:
        mapped = map_domain_error(
            DomainError(
                "interrupted",
                ErrorCategory.CANCELLED,
                "Command was interrupted",
            )
        )
        result = _failure_result(
            command="produce video",
            correlation_id=correlation_id,
            mapped=mapped,
        )
        exit_code = ExitCode.INTERRUPTED
    except Exception:
        mapped = internal_error()
        result = _failure_result(
            command="produce video",
            correlation_id=correlation_id,
            mapped=mapped,
        )
        exit_code = mapped.exit_code

    if args.input_value is not None:
        result = replace(
            result,
            warnings=(
                *result.warnings,
                "Positional video input is deprecated; use --input",
            ),
        )
    render_result(result, json_mode=args.json)
    return int(exit_code)


def _produce_generic(
    args: argparse.Namespace,
    runtime: _VideoRuntime | None,
    correlation_id: str,
    *,
    config_service: RuntimeConfigService | None,
    engine_client: object | None,
) -> JobSnapshot:
    from app.cli.produce_request import load_produce_request, parse_recipe_selector
    from app.core.recipes.contracts import InputDescriptor

    if args.request is not None:
        request = load_produce_request(args.request)
        workspace_root = Path(request.workspace_ref).resolve()
        if request.recipe_key == _DOCUMENT_NOTE_V1:
            request = replace(request, workspace_ref=str(workspace_root))
            active_runtime = runtime or _default_runtime(
                workspace_root,
                runtime_paths=(
                    config_service.paths if config_service is not None else None
                ),
                recipe_key=request.recipe_key,
                document_provider_profile=request.parameters.get(
                    "provider_profile"
                ),
                document_model_identity=request.parameters.get("model_override"),
                document_verifier_provider_profile=request.parameters.get(
                    "verifier_provider_profile"
                ),
                document_verifier_model_identity=request.parameters.get(
                    "verifier_model_override"
                ),
            )
        else:
            effective_args = argparse.Namespace(
                workspace=workspace_root,
                provider_profile=None,
                transcriber_profile=None,
                output_language=None,
                quality=None,
                style=None,
                screenshot_policy=None,
                config_profile=None,
            )
            effective = _effective_video_config(
                effective_args,
                runtime_injected=runtime is not None,
                config_service=config_service,
            )
            config_snapshot = effective.job_snapshot()
            provider_profile, model_override = _resolved_video_model_selection(
                effective.config,
                requested_provider_profile=request.parameters.get(
                    "provider_profile"
                ),
                requested_model_identity=request.parameters.get("model_override"),
                require_codex_runtime=runtime is None,
            )
            request = replace(
                request,
                workspace_ref=str(workspace_root),
                parameters={
                    **request.parameters,
                    "provider_profile": provider_profile,
                    "model_override": model_override,
                    "config_snapshot": {
                        "snapshot_version": config_snapshot.snapshot_version,
                        "values": dict(config_snapshot.values),
                        "digest": config_snapshot.digest,
                        "semantic_digest": config_snapshot.semantic_digest,
                    },
                },
            )
            active_runtime = runtime or _default_runtime(
                workspace_root,
                runtime_paths=(
                    config_service.paths if config_service is not None else None
                ),
                current_config_snapshot=config_snapshot,
                video_provider_profile=provider_profile,
                video_model_identity=model_override,
            )
    else:
        key = parse_recipe_selector(args.recipe)
        requested_outputs = ("knowledge-note",)
        if key == _DOCUMENT_NOTE_V1:
            effective_args = argparse.Namespace(
                workspace=args.workspace,
                provider_profile=None,
                transcriber_profile=None,
                output_language=None,
                quality=None,
                style=None,
                screenshot_policy=None,
                config_profile=None,
            )
            effective = _effective_video_config(
                effective_args,
                runtime_injected=runtime is not None,
                config_service=config_service,
            )
            config = effective.config
            workspace_value = args.workspace or config.default_workspace
            if workspace_value is None:
                raise DomainError(
                    "workspace_required",
                    ErrorCategory.INVALID_REQUEST,
                    "A Workspace must be provided by flag or Runtime configuration",
                )
            workspace_root = Path(workspace_value).resolve()
            provider_profile = config.default_provider_profile
            provider = config.providers.get(provider_profile)
            config_snapshot = effective.job_snapshot()
            model_override = (
                provider.default_model if provider is not None else None
            )
            verifier_provider_profile = (
                config.default_verifier_provider_profile
            )
            verifier_model_override = None
            if verifier_provider_profile is not None:
                verifier_provider = config.providers.get(
                    verifier_provider_profile
                )
                verifier_model_override = (
                    verifier_provider.default_model
                    if verifier_provider is not None
                    else None
                )
                if (
                    verifier_provider is not None
                    and verifier_provider.provider_type != "codex-app-server"
                ):
                    raise DomainError(
                        "model_provider_unsupported",
                        ErrorCategory.WORKSPACE_INCOMPATIBLE,
                        "The configured Document verifier provider is not supported",
                    )
                if verifier_model_override is None:
                    raise DomainError(
                        "document_verifier_profile_invalid",
                        ErrorCategory.INVALID_REQUEST,
                        "The configured Document verifier profile has no frozen model",
                    )
                if (
                    verifier_model_override == model_override
                ):
                    raise DomainError(
                        "document_verifier_profile_invalid",
                        ErrorCategory.INVALID_REQUEST,
                        "The configured Document verifier must be independent",
                    )
            parameters = {
                "provider_profile": provider_profile,
                "model_override": model_override,
                "output_language": config.recipe_defaults.output_language,
            }
            if verifier_provider_profile is not None:
                parameters.update(
                    {
                        "verifier_provider_profile": verifier_provider_profile,
                        "verifier_model_override": verifier_model_override,
                    }
                )
            request = ProduceRequest(
                1,
                key,
                InputDescriptor("file", args.input_value),
                str(workspace_root),
                requested_outputs,
                parameters,
                client_request_id=correlation_id,
            )
            active_runtime = runtime or _default_runtime(
                workspace_root,
                runtime_paths=(
                    config_service.paths if config_service is not None else None
                ),
                current_config_snapshot=config_snapshot,
                recipe_key=key,
                document_provider_profile=provider_profile,
                document_model_identity=model_override,
                document_verifier_provider_profile=verifier_provider_profile,
                document_verifier_model_identity=verifier_model_override,
            )
        else:
            effective_args = argparse.Namespace(
                workspace=args.workspace,
                provider_profile=None,
                transcriber_profile=None,
                output_language=None,
                quality=None,
                style=None,
                screenshot_policy=None,
                config_profile=None,
            )
            effective = _effective_video_config(
                effective_args,
                runtime_injected=runtime is not None,
                config_service=config_service,
            )
            config = effective.config
            workspace_value = args.workspace or config.default_workspace
            if workspace_value is None:
                raise DomainError(
                    "workspace_required",
                    ErrorCategory.INVALID_REQUEST,
                    "A Workspace must be provided by flag or Runtime configuration",
                )
            workspace_root = Path(workspace_value).resolve()
            provider_profile = config.default_provider_profile
            provider_profile, model_override = _resolved_video_model_selection(
                config,
                requested_provider_profile=provider_profile,
                require_codex_runtime=runtime is None,
            )
            request = ProduceRequest(
                1,
                key,
                InputDescriptor("source", args.input_value),
                str(workspace_root),
                requested_outputs,
                {
                    "provider_profile": provider_profile,
                    "model_override": model_override,
                    "transcriber_profile": config.default_transcriber_profile,
                    "output_language": config.recipe_defaults.output_language,
                    "quality_preset": config.recipe_defaults.quality_preset,
                    "style": config.recipe_defaults.style,
                    "screenshot_policy": config.recipe_defaults.screenshot_policy,
                    "config_snapshot": {
                        "snapshot_version": effective.job_snapshot().snapshot_version,
                        "values": dict(effective.job_snapshot().values),
                        "digest": effective.job_snapshot().digest,
                        "semantic_digest": effective.job_snapshot().semantic_digest,
                    },
                },
                client_request_id=correlation_id,
            )
            active_runtime = runtime or _default_runtime(
                workspace_root,
                runtime_paths=(
                    config_service.paths if config_service is not None else None
                ),
                current_config_snapshot=effective.job_snapshot(),
                video_provider_profile=provider_profile,
                video_model_identity=model_override,
            )

    if not args.wait:
        _require_detachable_runtime(active_runtime)
    submission = (
        active_runtime.submit(request)
        if args.wait
        else active_runtime.submit(
            request,
            execution_owner=JobExecutionOwner.ENGINE,
        )
    )
    snapshot = active_runtime.get_job(submission.job_id)
    return _complete_submitted_job(
        active_runtime,
        snapshot,
        wait=args.wait,
        engine_client=engine_client,
        config_service=config_service,
    )


def _produce_video(
    args: argparse.Namespace,
    runtime: _VideoRuntime | None,
    correlation_id: str,
    *,
    config_service: RuntimeConfigService | None,
    engine_client: object | None,
) -> JobSnapshot:
    effective = _effective_video_config(
        args,
        runtime_injected=runtime is not None,
        config_service=config_service,
    )
    config = effective.config
    workspace_value = args.workspace or config.default_workspace
    if workspace_value is None:
        raise DomainError(
            "workspace_required",
            ErrorCategory.INVALID_REQUEST,
            "A Workspace must be provided by flag or Runtime configuration",
        )
    workspace_root = Path(workspace_value).resolve()
    config_snapshot = effective.job_snapshot()
    provider_profile, model_override = _resolved_video_model_selection(
        config,
        requested_provider_profile=(
            args.provider_profile or config.default_provider_profile
        ),
        requested_model_identity=args.model,
        require_codex_runtime=runtime is None,
    )
    active_runtime = runtime or _default_runtime(
        workspace_root,
        runtime_paths=(
            config_service.paths if config_service is not None else None
        ),
        current_config_snapshot=config_snapshot,
        video_provider_profile=provider_profile,
        video_model_identity=model_override,
    )
    output_requested = bool(args.output)
    faithful_policy = FaithfulLanguagePolicy(args.faithful_language)
    use_v2 = (
        args.recipe_version == 2
        or output_requested
        or faithful_policy is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
    )
    if args.recipe_version == 1 and use_v2:
        raise DomainError(
            "recipe_version_conflict",
            ErrorCategory.INVALID_REQUEST,
            "Video v2 output options cannot be used with recipe version 1",
        )
    requested_outputs = (
        tuple(VideoDocumentKind(value) for value in args.output)
        if output_requested
        else (VideoDocumentKind.KNOWLEDGE_NOTE,)
    )
    request = VideoProduceRequest(
        request_schema_version=2 if use_v2 else 1,
        workspace_root=workspace_root,
        input_value=(
            args.input_option
            if args.input_option is not None
            else args.input_value
        ),
        recipe_id=(
            "alltonote.video-producer" if use_v2 else "alltonote.video-course-note"
        ),
        recipe_version=2 if use_v2 else 1,
        provider_profile=provider_profile,
        model_override=model_override,
        transcriber_profile=(
            args.transcriber_profile or config.default_transcriber_profile
        ),
        quality_preset=(args.quality or config.recipe_defaults.quality_preset),
        requested_outputs=requested_outputs,
        faithful_language_policy=faithful_policy,
        output_language=(
            args.output_language or config.recipe_defaults.output_language
        ),
        style=(args.style or config.recipe_defaults.style),
        screenshot_policy=ScreenshotPolicy(
            args.screenshot_policy or config.recipe_defaults.screenshot_policy
        ),
        client_request_id=correlation_id,
        config_snapshot=config_snapshot,
    )
    if not args.wait:
        _require_detachable_runtime(active_runtime)
    snapshot = (
        active_runtime.submit_video(request)
        if args.wait
        else active_runtime.submit_video(
            request,
            execution_owner=JobExecutionOwner.ENGINE,
        )
    )
    return _complete_submitted_job(
        active_runtime,
        snapshot,
        wait=args.wait,
        engine_client=engine_client,
        config_service=config_service,
    )


def _complete_submitted_job(
    runtime: _VideoRuntime,
    snapshot: JobSnapshot,
    *,
    wait: bool,
    engine_client: object | None,
    config_service: RuntimeConfigService | None,
) -> JobSnapshot:
    if not wait:
        _notify_detached_job(
            runtime,
            snapshot,
            engine_client=engine_client,
            config_service=config_service,
        )
        return snapshot
    return _wait_for_submitted_job(runtime, snapshot, wait=True)


def _notify_detached_job(
    runtime: _VideoRuntime,
    snapshot: JobSnapshot,
    *,
    engine_client: object | None,
    config_service: RuntimeConfigService | None,
) -> None:
    from app.engine.contracts import EngineJobReference, EngineProtocolError

    instance_id = _require_detachable_runtime(runtime)
    try:
        reference = EngineJobReference(instance_id, snapshot.job_id)
    except EngineProtocolError as error:
        raise _DetachedJobNotificationError(
            DomainError(
                "engine_job_reference_invalid",
                ErrorCategory.INTERNAL,
                "Detached Job notification reference is invalid",
            ),
            snapshot,
        ) from error
    client = engine_client
    if client is None:
        from app.engine.client import LocalEngineClient
        from app.runtime_paths import resolve_runtime_paths

        client = LocalEngineClient(
            config_service.paths
            if config_service is not None
            else resolve_runtime_paths()
        )
    notify = getattr(client, "notify_job", None)
    if not callable(notify):
        raise _DetachedJobNotificationError(
            DomainError(
                "engine_client_invalid",
                ErrorCategory.INTERNAL,
                "Engine client does not support Job notification",
            ),
            snapshot,
        )
    try:
        notify(reference)
    except KeyboardInterrupt as error:
        raise _DetachedJobNotificationError(
            DomainError(
                "interrupted",
                ErrorCategory.CANCELLED,
                "Command was interrupted after the detached Job was persisted",
            ),
            snapshot,
        ) from error
    except DomainError as error:
        raise _DetachedJobNotificationError(error, snapshot) from error
    except Exception as error:
        raise _DetachedJobNotificationError(
            DomainError(
                "engine_notification_failed",
                ErrorCategory.INTERNAL,
                "Engine Job notification failed",
            ),
            snapshot,
        ) from error


def _require_detachable_runtime(runtime: _VideoRuntime) -> str:
    instance_id = getattr(runtime, "workspace_instance_id", None)
    if (
        type(instance_id) is not str
        or len(instance_id) != 32
        or any(character not in "0123456789abcdef" for character in instance_id)
    ):
        raise DomainError(
            "engine_workspace_instance_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Detached execution requires a registered Workspace instance",
        )
    return instance_id


def _map_detached_notification_error(
    error: _DetachedJobNotificationError,
) -> MappedCliError:
    mapped = map_domain_error(error)
    job_action = (
        f"Job {error.snapshot.job_id} remains durable; run alltonote engine ensure "
        f"and then alltonote job wait {error.snapshot.job_id}"
    )
    return replace(
        mapped,
        error=replace(
            mapped.error,
            next_actions=(job_action, *mapped.error.next_actions),
        ),
    )


def _wait_for_submitted_job(
    runtime: _VideoRuntime,
    snapshot: JobSnapshot,
    *,
    wait: bool,
) -> JobSnapshot:
    if not wait:
        return snapshot
    try:
        return runtime.wait_job(snapshot.job_id)
    except KeyboardInterrupt:
        cancel = getattr(runtime, "cancel_job", None)
        if callable(cancel):
            try:
                cancel(snapshot.job_id)
            except Exception:
                pass
        raise


def _video_snapshot_result(
    snapshot: JobSnapshot,
    correlation_id: str,
    *,
    command: str = "produce video",
) -> tuple[ApplicationResult, ExitCode]:
    job = _job_projection(snapshot)
    data: dict[str, object] = {
        "job_id": snapshot.job_id,
        "state": snapshot.state.value,
    }
    projection_invalid = (
        (snapshot.state is JobState.SUCCEEDED and snapshot.result is None)
        or (snapshot.state is JobState.FAILED and snapshot.error is None)
        or (
            snapshot.state is not JobState.SUCCEEDED
            and snapshot.result is not None
        )
        or (snapshot.state is not JobState.FAILED and snapshot.error is not None)
    )
    if projection_invalid:
        mapped = map_domain_error(
            DomainError(
                "job_projection_invalid",
                ErrorCategory.INTERNAL,
                "Stored Job state and result projection are inconsistent",
            )
        )
        return (
            _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
                data=data,
                job=job,
            ),
            mapped.exit_code,
        )
    if snapshot.state is JobState.FAILED and snapshot.error is not None:
        mapped = map_error_detail(snapshot.error)
        return (
            _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
                data=data,
                job=job,
            ),
            mapped.exit_code,
        )
    if snapshot.state is JobState.CANCELLED:
        mapped = map_domain_error(
            DomainError(
                "job_cancelled",
                ErrorCategory.CANCELLED,
                "Job was cancelled",
            )
        )
        return (
            _failure_result(
                command=command,
                correlation_id=correlation_id,
                mapped=mapped,
                data=data,
                job=job,
            ),
            mapped.exit_code,
        )

    produced = snapshot.result
    human_lines = [f"Job: {snapshot.job_id}", f"State: {snapshot.state.value}"]
    artifacts: list[dict[str, object]] = []
    if isinstance(produced, RecipeProduceResult):
        primary_draft = produced.artifacts.get("primary_draft")
        data.update(
            {
                "result_kind": produced.result_kind,
                "run_id": produced.run_id,
                "bundle_id": produced.bundle_id,
                "manifest_sha256": produced.manifest_sha256,
                "commit_sha256": produced.commit_sha256,
                "workspace_relative_bundle_path": (
                    produced.workspace_relative_bundle_path
                ),
                "source_id": produced.source_id,
                "source_revision_id": produced.source_revision_id,
                "primary_draft_artifact_id": primary_draft,
                "evidence_set_artifact_id": produced.artifacts.get("evidence_set"),
                "quality": {
                    "overall": produced.quality_overall,
                    "publish_eligible": produced.publish_eligible,
                },
                "usage": dict(produced.usage),
            }
        )
        artifacts = [
            {"artifact_id": artifact_id, "role": role}
            for role, artifact_id in sorted(produced.artifacts.items())
        ]
        human_lines.append(f"Bundle: {produced.bundle_id}")
        if primary_draft is not None:
            human_lines.append(f"Draft: {primary_draft}")
            human_lines.append(f"Read: alltonote draft show {primary_draft}")
            human_lines.append(f"Review: alltonote review show {primary_draft}")
    elif produced is not None:
        data.update(
            {
                "run_id": produced.run_id,
                "bundle_id": produced.bundle_id,
                "manifest_sha256": produced.manifest_sha256,
                "commit_sha256": produced.commit_sha256,
                "workspace_relative_bundle_path": (
                    produced.workspace_relative_bundle_path
                ),
                "primary_draft_artifact_id": produced.primary_draft_artifact_id,
                "quality": {
                    "overall": produced.quality_overall.value,
                    "publish_eligible": produced.publish_eligible,
                },
                "documents": [
                    {
                        "document_kind": document.document_kind.value,
                        "draft_artifact_id": document.draft_artifact_id,
                        "quality_report_artifact_id": (
                            document.quality_report_artifact_id
                        ),
                        "quality_overall": document.quality_overall.value,
                        "publish_eligible": document.publish_eligible,
                    }
                    for document in produced.documents
                ],
            }
        )
        artifacts = _video_artifact_projection(produced)
        human_lines.extend(
            (
                f"Bundle: {produced.bundle_id}",
                f"Draft: {produced.primary_draft_artifact_id}",
                (
                    "Read: alltonote draft show "
                    f"{produced.primary_draft_artifact_id}"
                ),
                (
                    "Review: alltonote review show "
                    f"{produced.primary_draft_artifact_id}"
                ),
            )
        )
    return (
        ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data=data,
            warnings=produced.warnings if produced is not None else (),
            job=job,
            artifacts=tuple(artifacts),
            versions=_versions(),
            human_lines=tuple(human_lines),
        ),
        ExitCode.SUCCESS,
    )


def _failure_result(
    *,
    command: str,
    correlation_id: str,
    mapped: MappedCliError,
    data: dict[str, object] | None = None,
    job: dict[str, object] | None = None,
) -> ApplicationResult:
    return ApplicationResult(
        command=command,
        correlation_id=correlation_id,
        ok=False,
        data=data or {},
        error=mapped.error,
        job=job,
        versions=_versions(),
    )


def _runtime_command_result(
    args: argparse.Namespace,
    correlation_id: str,
) -> ApplicationResult:
    command = f"runtime {args.runtime_command}"
    if args.runtime_command == "paths":
        from app.runtime_paths import resolve_runtime_paths

        paths = resolve_runtime_paths()
        records = paths.role_records()
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={"paths": records},
            versions=_versions(),
            human_lines=tuple(
                f"{record['role']}: {record['path']}" for record in records
            ),
        )

    if args.runtime_command == "sqlite-wal-gate":
        from app.runtime_sqlite_gate import run_sqlite_wal_gate

        report = run_sqlite_wal_gate(args.root)
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data=report,
            versions=_versions(),
            human_lines=(
                "SQLite WAL gate: scenarios passed; "
                "parallel Job execution remains disabled",
            ),
        )

    from app.runtime_info import build_runtime_info, runtime_doctor

    if args.runtime_command == "doctor":
        checks = runtime_doctor(dynamic=args.dynamic)
        healthy = all(check.status != "fail" for check in checks)
        check_records = tuple(check.to_mapping() for check in checks)
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={
                "healthy": healthy,
                "dynamic": args.dynamic,
                "checks": check_records,
            },
            versions=_versions(),
            human_lines=human_diagnostic_lines(
                f"Runtime healthy: {'yes' if healthy else 'no'}",
                check_records,
            ),
        )

    info = build_runtime_info()
    capabilities = tuple(
        capability.to_mapping() for capability in info.capabilities
    )
    data = info.to_mapping()
    if args.runtime_command == "capabilities":
        data = {"count": len(capabilities)}
    return ApplicationResult(
        command=command,
        correlation_id=correlation_id,
        ok=True,
        data=data,
        capabilities=capabilities,
        versions=_versions(),
        human_lines=(
            (
                f"Runtime {info.runtime_version}"
                if args.runtime_command == "info"
                else f"Capabilities: {len(capabilities)}"
            ),
        ),
    )


def _config_command_result(
    args: argparse.Namespace,
    correlation_id: str,
    *,
    config_service: RuntimeConfigService | None,
) -> ApplicationResult:
    service = config_service or _default_config_service()
    command = (
        f"config provider {args.config_provider_command}"
        if args.config_command == "provider"
        else f"config {args.config_command}"
    )
    if args.config_command == "provider":
        effective, changed = service.set_provider_profile(
            args.profile,
            provider_type=args.provider_type,
            default_model=args.model,
            credential_ref=args.credential_ref,
        )
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={
                "profile": args.profile,
                "provider_type": args.provider_type,
                "default_model": args.model,
                "changed": changed,
                "digest": effective.digest,
                "semantic_digest": effective.semantic_digest,
            },
            versions=_versions(),
            human_lines=(
                (
                    f"Provider profile configured: {args.profile}"
                    if changed
                    else f"Provider profile already configured: {args.profile}"
                ),
            ),
        )
    if args.config_command == "profiles":
        profiles = service.list_profiles()
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={"profiles": profiles},
            versions=_versions(),
            human_lines=profiles or ("No configuration profiles",),
        )
    if args.config_command == "set":
        effective = service.set_value(args.key, args.value)
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={
                "updated": args.key,
                "digest": effective.digest,
                "semantic_digest": effective.semantic_digest,
            },
            versions=_versions(),
            human_lines=(f"Updated: {args.key}",),
        )

    effective = (
        service.validate(profile=args.profile)
        if args.config_command == "validate"
        else service.effective(profile=args.profile)
    )
    data: dict[str, object] = {
        "valid": True,
        "profile": effective.profile,
        "digest": effective.digest,
        "semantic_digest": effective.semantic_digest,
    }
    if args.config_command == "get":
        data["config"] = effective.values
    return ApplicationResult(
        command=command,
        correlation_id=correlation_id,
        ok=True,
        data=data,
        warnings=effective.warnings,
        versions=_versions(),
        human_lines=(
            f"Configuration valid: {effective.digest}",
        ),
    )


def _effective_video_config(
    args: argparse.Namespace,
    *,
    runtime_injected: bool,
    config_service: RuntimeConfigService | None,
) -> EffectiveRuntimeConfig:
    from app.core.config.model import RecipeDefaults, RuntimeConfig
    from app.runtime_config import effective_runtime_config

    overrides: dict[str, object] = {}
    if args.workspace is not None:
        overrides["default_workspace"] = str(args.workspace)
    if args.provider_profile is not None:
        overrides["default_provider_profile"] = args.provider_profile
    if args.transcriber_profile is not None:
        overrides["default_transcriber_profile"] = args.transcriber_profile
    recipe_overrides = {
        key: value
        for key, value in {
            "output_language": args.output_language,
            "quality_preset": args.quality,
            "style": args.style,
            "screenshot_policy": args.screenshot_policy,
        }.items()
        if value is not None
    }
    if recipe_overrides:
        overrides["recipe_defaults"] = recipe_overrides

    if config_service is not None:
        return config_service.effective(
            profile=args.config_profile,
            cli_overrides=overrides,
        )
    if not runtime_injected or args.config_profile is not None:
        return _default_config_service().effective(
            profile=args.config_profile,
            cli_overrides=overrides,
        )

    recipe_defaults = RecipeDefaults(**recipe_overrides)
    config = RuntimeConfig(
        default_workspace=args.workspace,
        default_provider_profile=(args.provider_profile or "default"),
        default_transcriber_profile=(args.transcriber_profile or "default"),
        recipe_defaults=recipe_defaults,
    )
    return effective_runtime_config(config)


def _resolved_video_model_selection(
    config: object,
    *,
    requested_provider_profile: object = None,
    requested_model_identity: object = None,
    require_codex_runtime: bool,
) -> tuple[str, str | None]:
    from app.core.portable.identity import is_executor_identity

    provider_profile = (
        requested_provider_profile
        if type(requested_provider_profile) is str
        else config.default_provider_profile
    )
    if not is_executor_identity(provider_profile):
        raise DomainError(
            "model_provider_profile_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The configured Video provider profile is invalid",
        )
    provider = config.providers.get(provider_profile)
    if require_codex_runtime and provider is None:
        if provider_profile != "default":
            raise DomainError(
                "model_provider_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The configured Video provider is not supported",
            )
    elif require_codex_runtime and (
        provider.provider_type != "codex-app-server"
        or provider.base_url is not None
        or provider.credential_ref not in {None, "codex/local-login"}
    ):
        raise DomainError(
            "model_provider_unsupported",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "The configured Video provider is not supported",
        )

    model_identity = (
        requested_model_identity
        if type(requested_model_identity) is str
        else (provider.default_model if provider is not None else None)
    )
    if model_identity is not None and not is_executor_identity(model_identity):
        raise DomainError(
            "model_identity_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The configured Video model identity is invalid",
        )
    if provider is not None and model_identity is None:
        raise DomainError(
            "model_identity_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The configured Video model identity is invalid",
        )
    return provider_profile, model_identity


def _job_runtime_for_args(
    args: argparse.Namespace,
    *,
    runtime: _VideoRuntime | None,
    config_service: RuntimeConfigService | None,
) -> JobRuntime:
    from app.core.config.model import RuntimeConfig
    from app.job_runtime import (
        create_job_runtime_for_execution_runtime,
        create_job_runtime_for_workspace,
    )
    from app.runtime_config import effective_runtime_config

    overrides = (
        {"default_workspace": str(args.workspace)}
        if args.workspace is not None
        else {}
    )
    service = config_service
    if service is not None:
        effective = service.effective(
            profile=args.config_profile,
            cli_overrides=overrides,
        )
    elif runtime is None or args.config_profile is not None:
        service = _default_config_service()
        effective = service.effective(
            profile=args.config_profile,
            cli_overrides=overrides,
        )
    else:
        effective = effective_runtime_config(
            RuntimeConfig(default_workspace=args.workspace)
        )
    workspace = args.workspace or effective.config.default_workspace
    if workspace is None:
        raise DomainError(
            "workspace_required",
            ErrorCategory.INVALID_REQUEST,
            "A Workspace must be provided by flag or Runtime configuration",
        )
    workspace_root = Path(workspace).resolve()
    snapshot = effective.job_snapshot()
    if runtime is not None:
        return create_job_runtime_for_execution_runtime(
            runtime,
            current_config_snapshot=snapshot,
        )
    return create_job_runtime_for_workspace(
        workspace_root,
        runtime_paths=(service.paths if service is not None else None),
        current_config_snapshot=snapshot,
    )


def _artifact_workspace_for_args(
    args: argparse.Namespace,
    *,
    config_service: RuntimeConfigService | None,
) -> Path:
    if args.workspace is not None and args.config_profile is None:
        return Path(args.workspace).resolve()
    service = config_service or _default_config_service()
    overrides = (
        {"default_workspace": str(args.workspace)}
        if args.workspace is not None
        else {}
    )
    effective = service.effective(
        profile=args.config_profile,
        cli_overrides=overrides,
    )
    workspace = args.workspace or effective.config.default_workspace
    if workspace is None:
        raise DomainError(
            "workspace_required",
            ErrorCategory.INVALID_REQUEST,
            "A Workspace must be provided by flag or Runtime configuration",
        )
    return Path(workspace).resolve()


def _credential_command_result(
    args: argparse.Namespace,
    correlation_id: str,
    *,
    credential_broker: CredentialBroker | None,
    input_stream: TextIO | None,
) -> ApplicationResult:
    from app.adapters.credentials.keyring_broker import CredentialBroker

    broker = credential_broker or CredentialBroker()
    command = f"credential {args.credential_command}"
    if args.credential_command == "status":
        status = broker.status(args.profile)
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={
                "present": status.present,
                "validated": status.validated,
                "last_checked_at": status.last_checked_at,
            },
            versions=_versions(),
            human_lines=(
                f"Credential present: {'yes' if status.present else 'no'}",
            ),
        )
    if args.credential_command == "delete":
        broker.delete(args.profile)
        return ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={"deleted": True},
            versions=_versions(),
            human_lines=("Credential deleted",),
        )

    broker.set(
        args.profile,
        _read_credential_secret(args, input_stream=input_stream),
    )
    return ApplicationResult(
        command=command,
        correlation_id=correlation_id,
        ok=True,
        data={"stored": True},
        versions=_versions(),
        human_lines=("Credential stored in the secure backend",),
    )


def _read_credential_secret(
    args: argparse.Namespace,
    *,
    input_stream: TextIO | None,
) -> str:
    import getpass

    stream = input_stream or sys.stdin
    if args.stdin:
        value = stream.readline(65_538)
        if len(value) > 65_536:
            raise DomainError(
                "credential_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Credential value is invalid",
            )
        return value.rstrip("\r\n")
    if stream.isatty():
        return getpass.getpass("Credential: ")
    raise DomainError(
        "credential_input_required",
        ErrorCategory.POLICY_DENIED,
        "Credential input requires --stdin or an interactive terminal",
    )


def _default_config_service() -> RuntimeConfigService:
    import os

    from app.runtime_config import RuntimeConfigService

    return RuntimeConfigService(environ=os.environ)


def _job_projection(snapshot: JobSnapshot) -> dict[str, object]:
    return {
        "job_id": snapshot.job_id,
        "state": snapshot.state.value,
        "cancellation_requested": snapshot.cancellation_requested,
        "active_attempt_id": snapshot.active_attempt_id,
        "challenge_id": snapshot.challenge_id,
        "retry_of_job_id": snapshot.retry_of_job_id,
    }


def _video_artifact_projection(
    result: VideoProduceResult,
) -> list[dict[str, object]]:
    artifacts = [
        {
            "artifact_id": result.primary_draft_artifact_id,
            "role": "primary_draft",
        },
        {"artifact_id": result.transcript_artifact_id, "role": "transcript"},
        {"artifact_id": result.evidence_set_artifact_id, "role": "evidence_set"},
        {
            "artifact_id": result.quality_report_artifact_id,
            "role": "quality_report",
        },
    ]
    known_ids = {item["artifact_id"] for item in artifacts}
    for document in result.documents:
        if document.draft_artifact_id not in known_ids:
            artifacts.append(
                {
                    "artifact_id": document.draft_artifact_id,
                    "role": document.document_kind.value,
                }
            )
            known_ids.add(document.draft_artifact_id)
        if document.quality_report_artifact_id not in known_ids:
            artifacts.append(
                {
                    "artifact_id": document.quality_report_artifact_id,
                    "role": f"{document.document_kind.value}_quality",
                }
            )
            known_ids.add(document.quality_report_artifact_id)
    for artifact_id in result.display_asset_ids:
        artifacts.append({"artifact_id": artifact_id, "role": "display_asset"})
    return artifacts


def _versions() -> dict[str, object]:
    return {
        "runtime_version": RUNTIME_VERSION,
        "cli_protocol_version": CLI_PROTOCOL_VERSION,
    }


def _command_from_arguments(arguments: Sequence[str]) -> str:
    if arguments and arguments[0] == "version":
        return "version"
    if len(arguments) >= 2 and arguments[0] == "runtime":
        return f"runtime {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "engine":
        return f"engine {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "config":
        if len(arguments) >= 3 and arguments[1] == "provider":
            return f"config provider {arguments[2]}"
        return f"config {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "credential":
        return f"credential {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "workspace":
        return f"workspace {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "pack":
        return f"pack {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "job":
        return f"job {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] in {"artifact", "draft"}:
        return f"{arguments[0]} {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "review":
        return f"review {arguments[1]}"
    if len(arguments) >= 2 and arguments[0] == "recipe":
        return f"recipe {arguments[1]}"
    if arguments and arguments[0] == "produce":
        return "produce video" if len(arguments) >= 2 and arguments[1] == "video" else "produce"
    return "alltonote"


def _default_runtime(
    workspace_root: Path,
    *,
    runtime_paths: RuntimePaths | None = None,
    current_config_snapshot: JobConfigSnapshot | None = None,
    recipe_key: RecipeKey | None = None,
    video_provider_profile: str | None = None,
    video_model_identity: str | None = None,
    document_provider_profile: object = None,
    document_model_identity: object = None,
    document_verifier_provider_profile: object = None,
    document_verifier_model_identity: object = None,
) -> _VideoRuntime:
    if recipe_key == _DOCUMENT_NOTE_V1:
        from app.runtime import create_document_runtime_for_workspace

        return create_document_runtime_for_workspace(
            workspace_root,
            runtime_paths=runtime_paths,
            current_config_snapshot=current_config_snapshot,
            requested_model_identity=(
                document_model_identity
                if type(document_model_identity) is str
                else None
            ),
            requested_provider_profile=(
                document_provider_profile
                if type(document_provider_profile) is str
                else None
            ),
            requested_verifier_model_identity=(
                document_verifier_model_identity
                if type(document_verifier_model_identity) is str
                else None
            ),
            requested_verifier_provider_profile=(
                document_verifier_provider_profile
                if type(document_verifier_provider_profile) is str
                else None
            ),
        )
    from app.runtime import create_codex_app_server_runtime_for_workspace

    return create_codex_app_server_runtime_for_workspace(
        workspace_root,
        runtime_paths=runtime_paths,
        current_config_snapshot=current_config_snapshot,
        requested_model_identity=video_model_identity,
        requested_provider_profile=video_provider_profile,
    )


def entrypoint() -> None:
    configure_standard_streams()
    raise SystemExit(main())
