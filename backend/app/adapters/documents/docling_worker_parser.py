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
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.errors import DomainError, ErrorCategory


_MAX_FIRST_SLICE_BYTES = 64 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
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
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._config = config
        self._runner = runner

    def doctor(self) -> None:
        python_executable, artifacts_path, backend_root = self._installation()
        with tempfile.TemporaryDirectory(prefix="docling-doctor-") as directory:
            output = Path(directory) / "doctor.json"
            command = [
                str(python_executable),
                "-m",
                "app.adapters.documents.docling_worker_doctor",
                "--artifacts-path",
                str(artifacts_path),
                "--output",
                str(output),
            ]
            try:
                completed = self._runner(
                    command,
                    cwd=backend_root,
                    env=self._environment(backend_root),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._config.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise _failed(
                    "document_pack_invalid",
                    "document-basic Pack doctor timed out",
                ) from error
            problem = _decode_doctor_result(output)
            if completed.returncode != 0 or problem is not None:
                detail = problem or "unknown check"
                raise _failed(
                    "document_pack_invalid",
                    f"document-basic Pack doctor failed: {detail}",
                )

    def parse(self, source: Path, *, work_root: Path) -> ParsedDocument:
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
            or stat.st_size > _MAX_FIRST_SLICE_BYTES
            or magic != b"%PDF-"
        ):
            raise _failed(
                "document_input_unsupported",
                "The first Document slice requires a bounded born-digital PDF",
            )
        before_hash = _sha256(source)
        before_identity = (stat.st_size, stat.st_mtime_ns)
        with tempfile.TemporaryDirectory(prefix="docling-", dir=work_root) as directory:
            staged_source = Path(directory) / "input.pdf"
            shutil.copyfile(source, staged_source)
            if _sha256(staged_source) != before_hash:
                raise DomainError(
                    "document_input_changed",
                    ErrorCategory.CONFLICT,
                    "Document input changed while creating the parser snapshot",
                )
            output = Path(directory) / "parsed.json"
            command = [
                str(python_executable),
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
            try:
                completed = self._runner(
                    command,
                    cwd=backend_root,
                    env=self._environment(backend_root),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._config.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise _failed(
                    "document_parser_timeout",
                    "The isolated Document parser exceeded its time budget",
                ) from error
            if completed.returncode != 0 or not output.is_file():
                raise _failed(
                    "document_parser_failed",
                    "The isolated Document parser did not produce a result",
                )
            parsed = replace(
                _decode_result(output.read_text(encoding="utf-8")),
                source_name=source.name,
            )
        after = source.stat()
        if (
            (after.st_size, after.st_mtime_ns) != before_identity
            or _sha256(source) != before_hash
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
        environment = dict(os.environ)
        environment["HF_HUB_OFFLINE"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(backend_root)
            if not existing_python_path
            else str(backend_root) + os.pathsep + existing_python_path
        )
        return environment


def _decode_doctor_result(output: Path) -> str | None:
    try:
        value = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
