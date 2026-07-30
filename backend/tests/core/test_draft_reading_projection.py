from __future__ import annotations

from app.core.application.artifact_query_service import project_reading_markdown


EVIDENCE_ONE = "ev_018f0000-0000-7000-8000-000000000109"
EVIDENCE_TWO = "ev_018f0000-0000-7000-8000-00000000010a"


def test_reading_projection_hides_only_rendered_system_evidence_footnotes() -> None:
    markdown = (
        "# 笔记\n\n"
        f"第一条结论。[^{EVIDENCE_ONE}] [^{EVIDENCE_TWO}]\n\n"
        f"第二条结论 [^{EVIDENCE_ONE}] 仍需复核。\n\n"
        "用户脚注必须保留。[^note]\n\n"
        f"行内代码 `{f'[^{EVIDENCE_ONE}]'}` 必须保留。\n\n"
        "```markdown\n"
        f"[^{EVIDENCE_ONE}]\n"
        f"[^{EVIDENCE_ONE}]: 代码示例\n"
        "```\n\n"
        f"转义字面量 \\[^{EVIDENCE_ONE}] 必须保留。\n\n"
        "[^note]: 用户自己的补充说明。\n"
        f"[^{EVIDENCE_ONE}]: Video 00:00.000–00:01.000\n"
        f"[^{EVIDENCE_TWO}]: Video 00:01.000–00:02.000\n"
    )

    assert project_reading_markdown(markdown) == (
        "# 笔记\n\n"
        "第一条结论。\n\n"
        "第二条结论 仍需复核。\n\n"
        "用户脚注必须保留。[^note]\n\n"
        f"行内代码 `{f'[^{EVIDENCE_ONE}]'}` 必须保留。\n\n"
        "```markdown\n"
        f"[^{EVIDENCE_ONE}]\n"
        f"[^{EVIDENCE_ONE}]: 代码示例\n"
        "```\n\n"
        f"转义字面量 \\[^{EVIDENCE_ONE}] 必须保留。\n\n"
        "[^note]: 用户自己的补充说明。\n"
    )


def test_reading_projection_leaves_markdown_without_evidence_unchanged() -> None:
    markdown = "# 笔记\n\n没有 Evidence 脚注。\n"

    assert project_reading_markdown(markdown) == markdown
