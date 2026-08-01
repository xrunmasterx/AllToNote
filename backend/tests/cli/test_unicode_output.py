from __future__ import annotations

import io
import json
import sys

import pytest

from app.cli.contracts import ApplicationResult, CliError
from app.cli.main import main
from app.cli.render import render_json_lines, render_result


def _cp936_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp936", newline="\n")


def _utf8_text(raw: io.BytesIO, stream: io.TextIOWrapper) -> str:
    stream.flush()
    return raw.getvalue().decode("utf-8")


def test_human_result_preserves_unicode_on_a_cp936_stream() -> None:
    raw, output = _cp936_stream()

    render_result(
        ApplicationResult(
            command="review show",
            correlation_id="corr_unicode_human",
            ok=True,
            human_lines=("J3BakedVolumetricGI 🚀 中文",),
        ),
        json_mode=False,
        stdout=output,
    )

    assert _utf8_text(raw, output) == "J3BakedVolumetricGI 🚀 中文\n"


def test_json_result_preserves_unicode_and_protocol_shape_on_a_cp936_stream() -> None:
    raw, output = _cp936_stream()

    render_result(
        ApplicationResult(
            command="draft show",
            correlation_id="corr_unicode_json",
            ok=True,
            data={"body": "# 火箭 🚀"},
        ),
        json_mode=True,
        stdout=output,
    )

    text = _utf8_text(raw, output)
    assert text.count("\n") == 1
    assert json.loads(text)["data"]["body"] == "# 火箭 🚀"


def test_jsonl_preserves_unicode_and_flushes_each_record_on_a_cp936_stream() -> None:
    raw, output = _cp936_stream()

    render_json_lines(
        ({"message": value} for value in ("阶段一 🚀", "阶段二 ✓")),
        stdout=output,
    )

    lines = _utf8_text(raw, output).splitlines()
    assert [json.loads(line)["message"] for line in lines] == [
        "阶段一 🚀",
        "阶段二 ✓",
    ]


def test_non_reconfigurable_cp936_jsonl_is_utf8_from_the_first_record() -> None:
    raw, delegate = _cp936_stream()

    class NonReconfigurableStream:
        encoding = "cp936"
        errors = "strict"
        buffer = raw

        @staticmethod
        def write(value: str) -> int:
            return delegate.write(value)

        @staticmethod
        def flush() -> None:
            delegate.flush()

    render_json_lines(
        (
            {"message": "第一条可由 GBK 编码"},
            {"message": "第二条包含 🚀"},
        ),
        stdout=NonReconfigurableStream(),  # type: ignore[arg-type]
    )

    delegate.flush()
    lines = raw.getvalue().decode("utf-8").splitlines()
    assert [json.loads(line)["message"] for line in lines] == [
        "第一条可由 GBK 编码",
        "第二条包含 🚀",
    ]


def test_dependency_injectable_main_does_not_reconfigure_global_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_raw, stdout = _cp936_stream()
    _stderr_raw, stderr = _cp936_stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    code = main(["version", "--json"])

    stdout.flush()
    assert code == 0
    assert stdout.encoding == "cp936"
    assert stderr.encoding == "cp936"
    assert json.loads(stdout_raw.getvalue().decode("utf-8"))["ok"] is True


def test_error_result_preserves_unicode_on_cp936_stderr() -> None:
    raw, diagnostics = _cp936_stream()

    render_result(
        ApplicationResult(
            command="review show",
            correlation_id="corr_unicode_error",
            ok=False,
            error=CliError(
                code="review_candidate_invalid",
                category="workspace_incompatible",
                message="无法读取 🚀",
                retryable=False,
                next_actions=("重新生成候选文档",),
            ),
        ),
        json_mode=False,
        stderr=diagnostics,
    )

    assert _utf8_text(raw, diagnostics) == (
        "Error [review_candidate_invalid]: 无法读取 🚀\n"
        "Action: 重新生成候选文档\n"
    )
