from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.recipes.document.adapter as adapter_module
from app.core.domain.document import MAX_BORN_DIGITAL_PDF_BYTES
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner
from app.core.recipes.contracts import InputDescriptor, ProduceRequest, RecipeKey
from app.core.recipes.document.adapter import DocumentRecipeAdapter


class _Service:
    def __init__(self) -> None:
        self.submissions = 0

    def submit_document(
        self,
        request: object,
        *,
        execution_owner: JobExecutionOwner,
    ) -> object:
        del request, execution_owner
        self.submissions += 1
        raise AssertionError("unsupported input must not create a Job")


class _ObservedBinaryStream:
    def __init__(self, stream: object, on_read: object) -> None:
        self._stream = stream
        self._on_read = on_read

    def __enter__(self) -> _ObservedBinaryStream:
        return self

    def __exit__(self, *args: object) -> object:
        return self._stream.__exit__(*args)  # type: ignore[attr-defined,no-any-return]

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[attr-defined,no-any-return]

    def read(self, size: int = -1) -> bytes:
        value = self._stream.read(size)  # type: ignore[attr-defined]
        self._on_read(value)  # type: ignore[operator]
        return value


def _request(source: Path, workspace: Path) -> ProduceRequest:
    return ProduceRequest(
        1,
        RecipeKey("alltonote.document-note", 1),
        InputDescriptor("file", str(source)),
        str(workspace),
        ("knowledge-note",),
    )


@pytest.mark.parametrize(
    ("file_name", "payload"),
    (
        ("paper.txt", b"%PDF-1.7\nfixture\n"),
        ("paper.pdf", b"not-a-pdf"),
    ),
)
def test_unsupported_document_is_rejected_before_hash_or_job_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    payload: bytes,
) -> None:
    source = tmp_path / file_name
    source.write_bytes(payload)
    service = _Service()
    inspection_calls = 0

    original_inspection = adapter_module._inspect_pdf_source

    def observed_inspection(path: Path) -> tuple[str, object]:
        nonlocal inspection_calls
        inspection_calls += 1
        return original_inspection(path)

    monkeypatch.setattr(adapter_module, "_inspect_pdf_source", observed_inspection)

    with pytest.raises(DomainError) as caught:
        DocumentRecipeAdapter(service).submit(  # type: ignore[arg-type]
            _request(source, tmp_path / "workspace")
        )

    assert caught.value.code == "document_input_unsupported"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert inspection_calls == 1
    assert service.submissions == 0


def test_oversized_document_is_rejected_before_hash_or_job_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized.pdf"
    with source.open("wb") as stream:
        stream.write(b"%PDF-")
        stream.seek(MAX_BORN_DIGITAL_PDF_BYTES)
        stream.write(b"x")
    service = _Service()
    bytes_read = 0

    original_open = Path.open

    def observed_open(path: Path, *args: object, **kwargs: object) -> object:
        stream = original_open(path, *args, **kwargs)

        def record_read(value: bytes) -> None:
            nonlocal bytes_read
            bytes_read += len(value)

        return _ObservedBinaryStream(stream, record_read)

    monkeypatch.setattr(Path, "open", observed_open)

    with pytest.raises(DomainError) as caught:
        DocumentRecipeAdapter(service).submit(  # type: ignore[arg-type]
            _request(source, tmp_path / "workspace")
        )

    assert caught.value.code == "document_input_unsupported"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert bytes_read == 0
    assert service.submissions == 0


def test_growing_pdf_is_bounded_and_rejected_before_job_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "growing.pdf"
    source.write_bytes(b"%PDF-fixture")
    service = _Service()
    bytes_read = 0
    grew = False
    original_open = Path.open
    monkeypatch.setattr(adapter_module, "MAX_BORN_DIGITAL_PDF_BYTES", 16)

    def observed_open(path: Path, *args: object, **kwargs: object) -> object:
        stream = original_open(path, *args, **kwargs)

        def grow_after_magic(value: bytes) -> None:
            nonlocal bytes_read, grew
            bytes_read += len(value)
            if not grew:
                grew = True
                with original_open(path, "r+b") as writer:
                    writer.seek(16)
                    writer.write(b"x")

        return _ObservedBinaryStream(stream, grow_after_magic)

    monkeypatch.setattr(Path, "open", observed_open)

    with pytest.raises(DomainError) as caught:
        DocumentRecipeAdapter(service).submit(  # type: ignore[arg-type]
            _request(source, tmp_path / "workspace")
        )

    assert caught.value.code == "document_input_unsupported"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert bytes_read == 17
    assert service.submissions == 0


def test_pdf_directory_is_rejected_before_job_submission(tmp_path: Path) -> None:
    source = tmp_path / "directory.pdf"
    source.mkdir()
    service = _Service()

    with pytest.raises(DomainError) as caught:
        DocumentRecipeAdapter(service).submit(  # type: ignore[arg-type]
            _request(source, tmp_path / "workspace")
        )

    assert caught.value.code == "document_input_unsupported"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert service.submissions == 0


@pytest.mark.parametrize(
    "parameters",
    (
        {
            "provider_profile": "composer",
            "model_override": "fixture/model-v1",
            "output_language": "en",
            "verifier_provider_profile": "reviewer",
        },
        {
            "provider_profile": "composer",
            "model_override": "fixture/model-v1",
            "output_language": "en",
            "verifier_provider_profile": "different-profile",
            "verifier_model_override": "fixture/model-v1",
        },
    ),
)
def test_invalid_verifier_selection_fails_before_pdf_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parameters: dict[str, object],
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    service = _Service()
    inspected = False

    def inspect(_path: Path) -> tuple[str, object]:
        nonlocal inspected
        inspected = True
        raise AssertionError("invalid model selection must fail before source hashing")

    monkeypatch.setattr(adapter_module, "_inspect_pdf_source", inspect)
    request = ProduceRequest(
        1,
        RecipeKey("alltonote.document-note", 1),
        InputDescriptor("file", str(source)),
        str(tmp_path / "workspace"),
        ("knowledge-note",),
        parameters,
    )

    with pytest.raises(DomainError) as caught:
        DocumentRecipeAdapter(service).submit(request)  # type: ignore[arg-type]

    assert caught.value.code == "document_recipe_parameters_invalid"
    assert inspected is False
    assert service.submissions == 0


def test_independent_verifier_selection_creates_frozen_v3_request(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")

    class Service:
        def __init__(self) -> None:
            self.request = None

        def knowledge_model_identity(self) -> str:
            return "fixture/composer-v1"

        def submit_document(
            self,
            request: object,
            *,
            execution_owner: JobExecutionOwner,
        ) -> object:
            del execution_owner
            self.request = request
            return SimpleNamespace(job_id="job_fixture", state=JobState.QUEUED)

    service = Service()
    DocumentRecipeAdapter(service).submit(  # type: ignore[arg-type]
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(tmp_path / "workspace"),
            ("knowledge-note",),
            {
                "provider_profile": "composer",
                "model_override": None,
                "output_language": "en",
                "verifier_provider_profile": "reviewer",
                "verifier_model_override": "fixture/reviewer-v1",
            },
        )
    )

    assert service.request.request_schema_version == 3
    assert service.request.model_override == "fixture/composer-v1"
    assert service.request.verifier_provider_profile == "reviewer"
    assert service.request.verifier_model_override == "fixture/reviewer-v1"
