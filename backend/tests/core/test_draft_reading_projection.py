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


def test_reading_preserves_literal_and_invalid_edit_log_fences() -> None:
    valid = '```alltonote-edit-log-v1\n{"schema_version":1,"kind":"faithful-edit-log","records":[]}\n```\n'
    literal = "````markdown\n" + valid + "````\n"
    invalid = "```alltonote-edit-log-v1\nnot a log\n```\n"
    assert project_reading_markdown(literal + invalid) == literal + invalid
    assert project_reading_markdown("# 正文\n\n" + valid) == "# 正文\n\n"


def test_illustrated_reading_keeps_chapter_times_images_and_captions() -> None:
    markdown = (
        "# 图文笔记\n\n## 05:35–07:42 趋势管理\n\n"
        f"解释上涨过程。[^{EVIDENCE_ONE}]\n\n"
        "![Video screenshot at 06:20.960](../assets/chart.webp)\n\n"
        "*画面中的文字对应这一段解释。*\n\n"
        f"[^{EVIDENCE_ONE}]: Video 06:03.480-06:05.521\n"
    )
    expected = (
        "# 图文笔记\n\n## 05:35–07:42 趋势管理\n\n"
        "解释上涨过程。\n\n"
        "![Video screenshot at 06:20.960](../assets/chart.webp)\n\n"
        "*画面中的文字对应这一段解释。*\n"
    )

    assert project_reading_markdown(markdown) == expected
    assert project_reading_markdown(expected) == expected
    assert f"[^{EVIDENCE_ONE}]: Video" in markdown


def test_reading_projection_removes_time_only_appendix_without_empty_markers() -> None:
    markdown = (
        f"# 笔记\n\n正文。[^{EVIDENCE_ONE}] [^{EVIDENCE_TWO}]\n\n"
        f"[^{EVIDENCE_ONE}]: Video 00:00.000-00:04.601\n"
        f"[^{EVIDENCE_TWO}]: Video 00:22.520-00:26.620\n"
    )

    assert project_reading_markdown(markdown) == "# 笔记\n\n正文。\n"


FAITHFUL_LOG = (
    '```alltonote-edit-log-v1\n'
    '{"schema_version":1,"kind":"faithful-edit-log","records":[]}\n```\n'
)


def test_faithful_reading_hides_review_notes_but_keeps_body_images_and_summary() -> None:
    markdown = (
        "# 保真稿\n\n"
        "> 正文仅依据转录稿保守整理；疑似识别错误保留待核对，不代表已核验原音频。\n\n"
        "## 正文\n\n### 00:00–02:29 开盘交易\n\n"
        "<!-- time:0-149620 -->\n\n"
        f"目前的浮盈是9000多美金。[^{EVIDENCE_ONE}]\n\n"
        "> 待核对：福音依据上下文改为浮盈。\n\n"
        "现在大概是355,366。\n\n"
        "> 待核对：数字含义不明；数字需要复核。\n\n"
        "![截图](../assets/chart.webp)\n\n*画面 01:05：交易图表*\n\n"
        "## AI 辅助摘要（不属于原文）\n\n"
        "### 00:00–02:29 开盘交易\n\n#### AI 章节摘要\n\n讲者复盘交易。\n\n"
        "#### 待复核项\n\n- [number] 355,366 需要复核。\n\n"
        "### 02:29–04:59 第二段\n\n#### AI 关键点\n\n- 保留原观点。\n\n"
        "#### 待复核项\n\n- 无\n\n"
        + FAITHFUL_LOG
        + f"[^{EVIDENCE_ONE}]: Video 00:00.000-00:04.601\n"
    )
    reading = project_reading_markdown(markdown)
    for hidden in ("待核对", "待复核项", "[number]", "<!-- time:", "alltonote-edit-log", "- 无"):
        assert hidden not in reading
    for retained in (
        "目前的浮盈是9000多美金。", "现在大概是355,366。",
        "![截图](../assets/chart.webp)", "*画面 01:05：交易图表*",
        "### 00:00–02:29 开盘交易", "讲者复盘交易。", "- 保留原观点。",
        "> 正文依据转录稿整理，未核验原音频。",
    ):
        assert retained in reading
    assert "\n\n\n" not in reading
    assert project_reading_markdown(reading) == reading
    assert "> 待核对：" in markdown
    assert "#### 待复核项" in markdown


def test_review_like_user_text_without_system_log_is_unchanged() -> None:
    markdown = "> 待核对：用户自己的备注。\n\n<!-- time:0-1000 -->\n\n#### 待复核项\n\n- 用户清单\n"
    assert project_reading_markdown(markdown) == markdown
    invalid = "```alltonote-edit-log-v1\nnot json\n```\n"
    assert project_reading_markdown(markdown + invalid) == markdown + invalid


def test_faithful_reading_preserves_review_markers_in_code_and_ordinary_quotes() -> None:
    literal = (
        "````markdown\n> 待核对：代码示例\n<!-- time:0-1000 -->\n"
        "#### 待复核项\n```\n代码\n```\n````\n\n"
        "行内代码 `> 待核对：例子`\n\n"
        "    > 待核对：缩进代码\n\n"
        "> 讲者原话不能删除。\n"
    )
    assert project_reading_markdown(literal + FAITHFUL_LOG) == literal


def test_faithful_summaries_lead_matching_chapters_with_missing_middle_summary() -> None:
    markdown = (
        "## 正文\n\n### 00:00–02:00 第一段\n\n正文一。\n\n"
        "![截图](../assets/chart.webp)\n\n"
        "### 02:00–04:00 第二段\n\n正文二。\n\n"
        "### 04:00–06:00 第三段\n\n正文三。\n\n"
        "## AI 辅助摘要（不属于原文）\n\n"
        "> 部分章节的 AI 摘要未通过复核，已省略；不影响上方已独立复核的正文。\n\n"
        "### 00:00–02:00 第一段\n\n#### AI 章节摘要\n\n摘要一。\n\n"
        f"#### AI 关键点\n\n- 关键点一。[^{EVIDENCE_ONE}]\n\n"
        "### 04:00–06:00 第三段\n\n#### AI 章节摘要\n\n摘要三。\n\n"
        + FAITHFUL_LOG
        + f"[^{EVIDENCE_ONE}]: Video 00:00.000-00:04.601\n"
    )
    reading = project_reading_markdown(markdown)
    assert reading.count("<details open>") == reading.count("</details>") == 2
    assert reading.count("<summary>摘要</summary>") == 2
    assert reading.index("### 00:00") < reading.index("摘要一") < reading.index("关键点一")
    assert reading.index("关键点一") < reading.index("正文一") < reading.index("![截图]")
    assert reading.index("![截图]") < reading.index("### 02:00") < reading.index("正文二")
    assert reading.index("正文二") < reading.index("### 04:00") < reading.index("摘要三") < reading.index("正文三")
    assert "## AI 辅助摘要" not in reading
    assert "未通过复核" not in reading
    assert EVIDENCE_ONE not in reading
    assert "**AI 章节摘要**" not in reading
    assert "**AI 关键点**" not in reading
    assert "#### AI" not in reading
    assert project_reading_markdown(reading) == reading


def test_summary_labels_are_removed_without_changing_body_headings_or_code_literals() -> None:
    literal = "```markdown\n#### AI 章节摘要\n#### AI 关键点\n```"
    body = "## 正文\n\n### 00:00–02:00 第一段\n\n#### AI 关键点\n\n正文。\n"
    for label in ("#### AI 章节摘要", "**AI 章节摘要**"):
        markdown = (
            body + "\n## AI 辅助摘要（不属于原文）\n\n"
            "### 00:00–02:00 第一段\n\n" + label + "\n\n摘要。\n\n"
            "#### AI 关键点\n\n- 关键点。\n\n" + literal + "\n" + FAITHFUL_LOG
        )
        reading = project_reading_markdown(markdown)
        assert literal in reading
        assert "#### AI 关键点\n\n正文。" in reading
        assert "摘要。" in reading
        assert "- 关键点。" in reading
        assert "**AI 章节摘要**" not in reading
        assert "**AI 关键点**" not in reading
        assert "<details open>" in reading
        assert project_reading_markdown(reading) == reading


def test_faithful_reading_without_approved_summaries_has_no_empty_disclosures() -> None:
    body = "## 正文\n\n### 00:00–02:00 第一段\n\n正文。\n"
    markdown = body + "\n## AI 辅助摘要（不属于原文）\n\n> 部分章节未通过复核。\n" + FAITHFUL_LOG
    assert project_reading_markdown(markdown) == body


def test_faithful_reading_does_not_guess_mismatched_or_duplicate_chapter_bindings() -> None:
    for summary_titles in (("未知章节",), ("同名章节", "同名章节")):
        markdown = "## 精编正文\n\n### 同名章节\n\n正文。\n\n## AI 辅助摘要（不属于原文）\n\n"
        markdown += "\n\n".join(f"### {title}\n\n摘要。" for title in summary_titles) + "\n"
        assert project_reading_markdown(markdown + FAITHFUL_LOG) == markdown
