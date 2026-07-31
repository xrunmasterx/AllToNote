from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from app.adapters.documents.document_basic_pack import (
    DOCLING_SLIM_VERSION,
    PACK_ID,
    PACK_VERSION,
    PARSER_MODEL_REVISION,
)
from app.adapters.worker_process import (
    WorkerProcessTimeout,
    WorkerProcessUnavailable,
    run_worker_process,
)
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    MAX_BORN_DIGITAL_PDF_BYTES,
    DocumentPage,
    ParsedDocument,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import CancellationTokenPort


_MAX_DOCTOR_RESULT_BYTES = 64 * 1024
_MAX_PARSER_RESULT_BYTES = 128 * 1024 * 1024
_WORKER_ENVIRONMENT_KEYS = (
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)


def _sha256(
    path: Path,
    check_cancelled: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if check_cancelled is not None:
                check_cancelled()
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _failed(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.WORKSPACE_INCOMPATIBLE, message)


@dataclass(frozen=True, slots=True)
class DoclingWorkerConfig:
    python_executable: Path
    artifacts_path: Path
    backend_root: Path
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise ValueError("Docling worker timeout is invalid")


class DoclingWorkerParser:
    def __init__(
        self,
        config: DoclingWorkerConfig,
        *,
        runner: Callable[..., int] = run_worker_process,
    ) -> None:
        self._config = config
        self._runner = runner

    def doctor(self) -> None:
        python_executable, artifacts_path, backend_root = self._installation()
        with tempfile.TemporaryDirectory(prefix="docling-doctor-") as directory:
            output = Path(directory) / "doctor.json"
            command = [
                str(python_executable),
                "-B",
                "-m",
                "app.adapters.documents.docling_worker_doctor",
                "--artifacts-path",
                str(artifacts_path),
                "--output",
                str(output),
            ]

            def check_running() -> None:
                if output.is_file() and output.stat().st_size > _MAX_DOCTOR_RESULT_BYTES:
                    raise _failed(
                        "document_pack_invalid",
                        "document-basic Pack doctor returned an oversized result",
                    )

            try:
                return_code = self._runner(
                    command,
                    cwd=backend_root,
                    environment=self._environment(backend_root),
                    timeout_seconds=self._config.timeout_seconds,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check_running=check_running,
                )
            except WorkerProcessTimeout as error:
                raise _failed(
                    "document_pack_invalid",
                    "document-basic Pack doctor timed out",
                ) from error
            except WorkerProcessUnavailable as error:
                raise _failed(
                    "document_pack_invalid",
                    "document-basic Pack doctor could not be started",
                ) from error
            problem = _decode_doctor_result(
                _read_bounded_text(output, _MAX_DOCTOR_RESULT_BYTES)
            )
            if return_code != 0 or problem is not None:
                detail = problem or "unknown check"
                raise _failed(
                    "document_pack_invalid",
                    f"document-basic Pack doctor failed: {detail}",
                )

    def parse(
        self,
        source: Path,
        *,
        work_root: Path,
        cancellation_token: CancellationTokenPort,
    ) -> ParsedDocument:
        cancellation_token.raise_if_cancelled()
        source = Path(source).resolve(strict=True)
        work_root = Path(work_root).resolve(strict=True)
        python_executable, artifacts_path, backend_root = self._installation()
        stat = source.stat()
        with source.open("rb") as stream:
            magic = stream.read(5)
        if (
            not source.is_file()
            or source.suffix.lower() != ".pdf"
            or stat.st_size < 5
            or stat.st_size > MAX_BORN_DIGITAL_PDF_BYTES
            or magic != b"%PDF-"
        ):
            raise _failed(
                "document_input_unsupported",
                "The first Document slice requires a bounded born-digital PDF",
            )
        before_hash = _sha256(source, cancellation_token.raise_if_cancelled)
        before_identity = (stat.st_size, stat.st_mtime_ns)
        with tempfile.TemporaryDirectory(prefix="docling-", dir=work_root) as directory:
            staged_source = Path(directory) / "input.pdf"
            cancellation_token.raise_if_cancelled()
            shutil.copyfile(source, staged_source)
            cancellation_token.raise_if_cancelled()
            if (
                _sha256(staged_source, cancellation_token.raise_if_cancelled)
                != before_hash
            ):
                raise DomainError(
                    "document_input_changed",
                    ErrorCategory.CONFLICT,
                    "Document input changed while creating the parser snapshot",
                )
            output = Path(directory) / "parsed.json"
            command = [
                str(python_executable),
                "-B",
                "-m",
                "app.adapters.documents.docling_worker",
                "--input",
                str(staged_source),
                "--output",
                str(output),
                "--artifacts-path",
                str(artifacts_path),
                "--expected-parser-version",
                DOCLING_SLIM_VERSION,
                "--expected-model-revision",
                PARSER_MODEL_REVISION,
            ]

            def check_running() -> None:
                cancellation_token.raise_if_cancelled()
                if output.is_file() and output.stat().st_size > _MAX_PARSER_RESULT_BYTES:
                    raise _failed(
                        "document_parser_result_invalid",
                        "The isolated Document parser result is too large",
                    )

            try:
                return_code = self._runner(
                    command,
                    cwd=backend_root,
                    environment=self._environment(backend_root),
                    timeout_seconds=self._config.timeout_seconds,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check_running=check_running,
                )
            except WorkerProcessTimeout as error:
                raise _failed(
                    "document_parser_timeout",
                    "The isolated Document parser exceeded its time budget",
                ) from error
            except WorkerProcessUnavailable as error:
                raise _failed(
                    "document_parser_failed",
                    "The isolated Document parser could not be started",
                ) from error
            cancellation_token.raise_if_cancelled()
            if return_code != 0 or not output.is_file():
                raise _failed(
                    "document_parser_failed",
                    "The isolated Document parser did not produce a result",
                )
            parsed = replace(
                _decode_result(
                    _read_bounded_text(output, _MAX_PARSER_RESULT_BYTES)
                ),
                source_name=source.name,
            )
        after = source.stat()
        if (
            (after.st_size, after.st_mtime_ns) != before_identity
            or _sha256(source, cancellation_token.raise_if_cancelled) != before_hash
            or parsed.source_sha256 != before_hash
            or parsed.parser_version != DOCLING_SLIM_VERSION
            or parsed.model_revision != PARSER_MODEL_REVISION
        ):
            raise _failed(
                "document_parser_identity_mismatch",
                "Document parser output does not match the frozen input or Pack",
            )
        return parsed

    def _installation(self) -> tuple[Path, Path, Path]:
        try:
            python_executable = self._config.python_executable.resolve(strict=True)
            artifacts_path = self._config.artifacts_path.resolve(strict=True)
            backend_root = self._config.backend_root.resolve(strict=True)
        except OSError as error:
            raise _failed(
                "document_pack_invalid",
                "document-basic Pack installation is unavailable",
            ) from error
        if (
            not python_executable.is_file()
            or not artifacts_path.is_dir()
            or not backend_root.is_dir()
        ):
            raise _failed(
                "document_pack_invalid",
                "document-basic Pack installation is unavailable",
            )
        return python_executable, artifacts_path, backend_root

    @staticmethod
    def _environment(backend_root: Path) -> dict[str, str]:
        environment = {
            key: value
            for key in _WORKER_ENVIRONMENT_KEYS
            if (value := os.environ.get(key)) is not None
        }
        environment["HF_HUB_OFFLINE"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(backend_root)
        return environment


def _read_bounded_text(path: Path, maximum_bytes: int) -> str:
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError:
        return ""
    if len(payload) > maximum_bytes:
        return ""
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        return ""


def _decode_doctor_result(payload: str) -> str | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return "invalid doctor result"
    if type(value) is not dict or value.get("schema_version") != 1:
        return "invalid doctor result"
    if value.get("ok") is True:
        if value.get("pack_id") == PACK_ID and value.get("pack_version") == PACK_VERSION:
            return None
        return "Pack identity mismatch"
    problem = value.get("problem")
    component = value.get("component")
    if type(problem) is str and type(component) is str:
        return f"{component} {problem}"
    return "invalid doctor result"


def _decode_result(payload: str) -> ParsedDocument:
    try:
        value = json.loads(payload)
        if type(value) is not dict or value.get("schema_version") != 1:
            raise ValueError
        pages = tuple(
            DocumentPage(
                page_number=page["page_number"],
                width=page["width"],
                height=page["height"],
                blocks=tuple(
                    DocumentBlock(
                        block_id=block["block_id"],
                        page_number=block["page_number"],
                        reading_order=block["reading_order"],
                        kind=block["kind"],
                        text=block["text"],
                        content_sha256=block["content_sha256"],
                        bbox=DocumentBoundingBox(**block["bbox"]),
                        basis=block["basis"],
                    )
                    for block in page["blocks"]
                ),
            )
            for page in value["pages"]
        )
        return ParsedDocument(
            source_sha256=value["source_sha256"],
            source_name=value["source_name"],
            parser_id=value["parser_id"],
            parser_version=value["parser_version"],
            model_revision=value["model_revision"],
            pages=pages,
            metadata=value["metadata"],
            warnings=tuple(value["warnings"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise _failed(
            "document_parser_result_invalid",
            "The isolated Document parser returned an invalid result",
        ) from error


__all__ = ["DoclingWorkerConfig", "DoclingWorkerParser"]
