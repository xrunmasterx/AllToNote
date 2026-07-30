from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.adapters.documents.document_basic_pack import (
    PACK_VERSION,
    PARSER_MODEL_REVISION,
)
from app.adapters.documents.docling_worker_parser import (
    DoclingWorkerConfig,
    DoclingWorkerParser,
)
from app.core.errors import DomainError


def _parser(
    tmp_path: Path,
    runner,
) -> DoclingWorkerParser:
    python = tmp_path / "python.exe"
    python.write_bytes(b"python")
    artifacts = tmp_path / "models"
    artifacts.mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    return DoclingWorkerParser(
        DoclingWorkerConfig(python, artifacts, backend),
        runner=runner,
    )


def _result(source: Path) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    text = "Document title"
    return {
        "schema_version": 1,
        "source_sha256": digest,
        "source_name": source.name,
        "parser_id": "docling",
        "parser_version": "2.117.0",
        "model_revision": PARSER_MODEL_REVISION,
        "pages": [
            {
                "page_number": 1,
                "width": 612.0,
                "height": 792.0,
                "blocks": [
                    {
                        "block_id": "blk_title",
                        "page_number": 1,
                        "reading_order": 0,
                        "kind": "title",
                        "text": text,
                        "content_sha256": "sha256:"
                        + hashlib.sha256(text.encode()).hexdigest(),
                        "bbox": {
                            "left": 1.0,
                            "top": 2.0,
                            "right": 100.0,
                            "bottom": 20.0,
                            "origin": "top-left",
                        },
                        "basis": "native",
                    }
                ],
            }
        ],
        "metadata": {"status": "success"},
        "warnings": [],
    }


def test_adapter_uses_argument_list_offline_worker_and_validates_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-born-digital")
    work = tmp_path / "work"
    work.mkdir()
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps(_result(source)), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    parsed = _parser(tmp_path, runner).parse(source, work_root=work)

    assert parsed.pages[0].blocks[0].text == "Document title"
    assert isinstance(captured["command"], list)
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in captured
    assert captured["command"][1:3] == [
        "-m",
        "app.adapters.documents.docling_worker",
    ]


def test_doctor_checks_locked_pack_in_offline_worker(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": True,
                    "pack_id": "document-basic",
                    "pack_version": PACK_VERSION,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    _parser(tmp_path, runner).doctor()

    assert captured["command"][1:3] == [
        "-m",
        "app.adapters.documents.docling_worker_doctor",
    ]
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


def test_doctor_rejects_dependency_mismatch(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": False,
                    "problem": "dependency_version",
                    "component": "scipy",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(
        DomainError,
        match="document_pack_invalid.*scipy",
    ):
        _parser(tmp_path, runner).doctor()


def test_adapter_rejects_worker_identity_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-born-digital")
    work = tmp_path / "work"
    work.mkdir()

    def runner(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        payload = _result(source)
        payload["model_revision"] = "wrong"
        output.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(DomainError, match="document_parser_identity_mismatch"):
        _parser(tmp_path, runner).parse(source, work_root=work)


def test_adapter_stages_non_ascii_source_for_worker_and_keeps_original_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "技术分析精简版.pdf"
    source.write_bytes(b"%PDF-born-digital")
    work = tmp_path / "work"
    work.mkdir()

    def runner(command, **_kwargs):
        staged = Path(command[command.index("--input") + 1])
        assert staged.name == "input.pdf"
        assert staged.read_bytes() == source.read_bytes()
        assert staged != source
        payload = _result(source)
        payload["source_name"] = staged.name
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    parsed = _parser(tmp_path, runner).parse(source, work_root=work)

    assert parsed.source_name == source.name


def test_adapter_rejects_non_pdf_before_worker(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"not a pdf")
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(DomainError, match="document_input_unsupported"):
        _parser(tmp_path, lambda *_args, **_kwargs: None).parse(
            source,
            work_root=work,
        )
