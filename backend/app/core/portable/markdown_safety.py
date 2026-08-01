from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.core.errors import DomainError, ErrorCategory


_ACTIVE_TAG = re.compile(
    r"<\s*/?\s*(?:script|iframe|object|embed|form|input|button|svg|style|link|meta|base|math|video|audio|source|track|canvas)\b",
    re.IGNORECASE,
)
_ACTIVE_ATTRIBUTE = re.compile(
    r"\b(?:on[a-z0-9_-]+|style|srcdoc|srcset|ping|formaction)\s*=",
    re.IGNORECASE,
)
_DANGEROUS_SCHEME = re.compile(
    r"(?:javascript|vbscript|file|data)\s*:",
    re.IGNORECASE,
)
_HTML_LINK = re.compile(
    r"\b(?:href|src|data|action|poster)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_HTML_IMAGE_OPEN = re.compile(r"<\s*img(?=[\s/>])", re.IGNORECASE)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*)>")
_ENCODED_PATH_CONTROL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[/\\]")
_WINDOWS_DEVICE_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {
        f"{prefix}{suffix}"
        for prefix in ("com", "lpt")
        for suffix in ("¹", "²", "³")
    }
)
_FOOTNOTE = re.compile(r"\[\^(ev_[0-9a-f-]+)\]")
_FOOTNOTE_DEFINITION = re.compile(r"^ {0,3}\[\^(ev_[0-9a-f-]+)\]:")
_SEGMENT_CITATION = re.compile(r"\[\^seg_[^\]\r\n]*\]")
_H2 = re.compile(r"^ {0,3}##(?!#)\s+\S")
_SETEXT_H2_UNDERLINE = re.compile(r"^ {0,3}-+[ \t]*$")
_HTML_LITERAL_OPEN = re.compile(r"<(code|pre)(?=[\s/>])", re.IGNORECASE)
_HTML_LITERAL_CLOSE = {
    name: re.compile(rf"</\s*{name}\s*>", re.IGNORECASE)
    for name in ("code", "pre")
}


def _unsafe() -> DomainError:
    return DomainError(
        "draft_markdown_unsafe",
        ErrorCategory.POLICY_DENIED,
        "Draft Markdown contains unsafe active content or links",
    )


@dataclass(frozen=True)
class _MarkdownScan:
    visible_text: str
    visible_mask: bytes
    has_mermaid_fence: bool


def _backslash_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def is_backslash_escaped(text: str, index: int) -> bool:
    """Return whether the character at index is escaped by backslashes."""

    return _backslash_escaped(text, index)


def _fence_run(line: str) -> tuple[int, str, int] | None:
    indent = 0
    while indent < len(line) and indent < 4 and line[indent] == " ":
        indent += 1
    if indent > 3 or indent >= len(line) or line[indent] not in "`~":
        return None
    marker = line[indent]
    end = indent
    while end < len(line) and line[end] == marker:
        end += 1
    length = end - indent
    return (indent, marker, length) if length >= 3 else None


def _hide(mask: bytearray, start: int, end: int) -> None:
    mask[start:end] = b"\x00" * (end - start)


def _scan_markdown(markdown: str) -> _MarkdownScan:
    mask = bytearray(b"\x01" * len(markdown))
    fence_marker: str | None = None
    fence_length = 0
    has_mermaid_fence = False
    line_start = 0
    while line_start < len(markdown):
        newline = markdown.find("\n", line_start)
        line_end = len(markdown) if newline < 0 else newline
        next_line = len(markdown) if newline < 0 else newline + 1
        line = markdown[line_start:line_end].rstrip("\r")
        fence = _fence_run(line)
        if fence_marker is not None:
            _hide(mask, line_start, next_line)
            if (
                fence is not None
                and fence[1] == fence_marker
                and fence[2] >= fence_length
                and not line[fence[0] + fence[2] :].strip(" \t")
            ):
                fence_marker = None
                fence_length = 0
            line_start = next_line
            continue
        if fence is not None:
            suffix = line[fence[0] + fence[2] :]
            if fence[1] != "`" or "`" not in suffix:
                fence_marker = fence[1]
                fence_length = fence[2]
                info = suffix.strip().split(None, 1)
                has_mermaid_fence = has_mermaid_fence or (
                    bool(info) and info[0].casefold() == "mermaid"
                )
                _hide(mask, line_start, next_line)
                line_start = next_line
                continue
        if line.startswith(("    ", "\t")):
            _hide(mask, line_start, next_line)
        line_start = next_line

    block_mask = bytes(mask)
    inline_start: int | None = None
    inline_length = 0
    cursor = 0
    while cursor < len(markdown):
        if block_mask[cursor] == 0:
            if inline_start is not None:
                mask[inline_start:cursor] = block_mask[inline_start:cursor]
                inline_start = None
                inline_length = 0
            cursor += 1
            continue
        if markdown[cursor] != "`" or (
            inline_start is None and _backslash_escaped(markdown, cursor)
        ):
            if inline_start is not None:
                mask[cursor] = 0
            cursor += 1
            continue
        run_end = cursor
        while (
            run_end < len(markdown)
            and block_mask[run_end] == 1
            and markdown[run_end] == "`"
        ):
            run_end += 1
        run_length = run_end - cursor
        if inline_start is None:
            inline_start = cursor
            inline_length = run_length
            _hide(mask, cursor, run_end)
        elif run_length == inline_length:
            _hide(mask, cursor, run_end)
            inline_start = None
            inline_length = 0
        else:
            _hide(mask, cursor, run_end)
        cursor = run_end
    if inline_start is not None:
        mask[inline_start:] = block_mask[inline_start:]

    visible_text = "".join(
        character if mask[index] else ("\n" if character == "\n" else " ")
        for index, character in enumerate(markdown)
    )
    return _MarkdownScan(
        visible_text=visible_text,
        visible_mask=bytes(mask),
        has_mermaid_fence=has_mermaid_fence,
    )


def downgrade_mermaid_fences(markdown: str) -> str:
    """Preserve Mermaid source as inert text without relaxing Markdown safety."""

    if not isinstance(markdown, str):
        raise _unsafe()
    output: list[str] = []
    fence_marker: str | None = None
    fence_length = 0
    for source_line in markdown.splitlines(keepends=True):
        line = source_line.rstrip("\r\n")
        ending = source_line[len(line) :]
        fence = _fence_run(line)
        if fence_marker is not None:
            output.append(source_line)
            if (
                fence is not None
                and fence[1] == fence_marker
                and fence[2] >= fence_length
                and not line[fence[0] + fence[2] :].strip(" \t")
            ):
                fence_marker = None
                fence_length = 0
            continue
        if fence is None:
            output.append(source_line)
            continue
        suffix_start = fence[0] + fence[2]
        suffix = line[suffix_start:]
        if fence[1] == "`" and "`" in suffix:
            output.append(source_line)
            continue
        fence_marker = fence[1]
        fence_length = fence[2]
        token_start = suffix_start
        while token_start < len(line) and line[token_start] in " \t":
            token_start += 1
        token_end = token_start
        while token_end < len(line) and line[token_end] not in " \t":
            token_end += 1
        if line[token_start:token_end].casefold() == "mermaid":
            line = line[:token_start] + "text" + line[token_end:]
        output.append(line + ending)
    return "".join(output)


def markdown_visible_mask(markdown: str) -> bytes:
    """Return a mask for rendered text, excluding code and destinations."""

    mask = bytearray(_scan_markdown(markdown).visible_mask)
    _hide_nonrendered_markdown_regions(markdown, mask)
    for image in _rendered_images(markdown):
        _hide(mask, image.start, image.end)
    return bytes(mask)


def markdown_visible_lines(markdown: str) -> tuple[str, ...]:
    """Return rendered Markdown lines with literal/non-rendered regions blanked."""

    mask = markdown_visible_mask(markdown)
    visible = "".join(
        character if mask[index] else ("\n" if character == "\n" else " ")
        for index, character in enumerate(markdown)
    )
    return tuple(visible.splitlines())


def _reference_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _bracket_matches(markdown: str) -> dict[int, int]:
    stack: list[int] = []
    matches: dict[int, int] = {}
    for index, character in enumerate(markdown):
        if character not in "[]":
            continue
        if _backslash_escaped(markdown, index):
            continue
        if character == "[":
            stack.append(index)
        elif character == "]" and stack:
            matches[stack.pop()] = index
    return matches


def _parenthesized_end(markdown: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    backslashes = 0
    cursor = start
    while cursor < len(markdown):
        character = markdown[cursor]
        if character == "\\":
            backslashes += 1
            cursor += 1
            continue
        escaped = backslashes % 2 == 1
        backslashes = 0
        if character in "\r\n" and quote is None and not escaped:
            return None
        if quote is not None:
            if character == quote and not escaped:
                quote = None
        elif character in "\"'" and not escaped:
            quote = character
        elif character == "(" and not escaped:
            depth += 1
        elif character == ")" and not escaped:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _html_tag_end(markdown: str, start: int) -> int | None:
    if markdown.startswith("<!--", start):
        closing = markdown.find("-->", start + 4)
        return len(markdown) if closing < 0 else closing + 3
    cursor = start + 1
    if cursor >= len(markdown):
        return None
    if markdown[cursor] in "!?":
        cursor += 1
        while cursor < len(markdown):
            if markdown[cursor] == ">":
                return cursor + 1
            if markdown[cursor] == "<":
                return None
            cursor += 1
        return None
    if markdown[cursor] == "/":
        cursor += 1
    name_start = cursor
    while cursor < len(markdown) and (
        markdown[cursor].isalnum() or markdown[cursor] in "-:"
    ):
        cursor += 1
    if cursor == name_start or (
        cursor < len(markdown) and markdown[cursor] not in " \t\r\n/>"
    ):
        return None
    quote: str | None = None
    while cursor < len(markdown):
        character = markdown[cursor]
        if quote is not None:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "<":
            return None
        elif character == ">":
            return cursor + 1
        cursor += 1
    return None


def _html_literal_end(markdown: str, tag_name: str, start: int) -> int:
    closing = _HTML_LITERAL_CLOSE[tag_name.casefold()].search(markdown, start)
    return len(markdown) if closing is None else closing.end()


def _raw_html_image_source(markdown: str, start: int, end: int) -> str | None:
    sources: list[str] = []
    cursor = start + 1
    while cursor < end and markdown[cursor] in " \t\r\n":
        cursor += 1
    while cursor < end and (
        markdown[cursor].isalnum() or markdown[cursor] in "-:"
    ):
        cursor += 1

    while cursor < end:
        while cursor < end and markdown[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= end or markdown[cursor] in "/>":
            break
        name_start = cursor
        while (
            cursor < end
            and markdown[cursor] not in " \t\r\n/>=\"'"
        ):
            cursor += 1
        if cursor == name_start:
            raise _unsafe()
        name = markdown[name_start:cursor]
        while cursor < end and markdown[cursor] in " \t\r\n":
            cursor += 1

        value: str | None = None
        if cursor < end and markdown[cursor] == "=":
            cursor += 1
            while cursor < end and markdown[cursor] in " \t\r\n":
                cursor += 1
            if cursor < end and markdown[cursor] in "\"'":
                quote = markdown[cursor]
                cursor += 1
                value_start = cursor
                while cursor < end and markdown[cursor] != quote:
                    cursor += 1
                if cursor >= end:
                    raise _unsafe()
                value = markdown[value_start:cursor]
                cursor += 1
            else:
                value_start = cursor
                while (
                    cursor < end
                    and markdown[cursor] not in " \t\r\n>"
                ):
                    cursor += 1
                value = markdown[value_start:cursor]

        if (
            len(name) == 3
            and name[0] in "sS"
            and name[1] in "rR"
            and name[2] in "cC"
        ):
            sources.append(value or "")

    return sources[0] if len(sources) == 1 else None


def _hide_nonrendered_markdown_regions(markdown: str, mask: bytearray) -> None:
    bracket_matches = _bracket_matches(markdown)
    cursor = 0
    line_start = 0
    while cursor < len(markdown):
        if markdown[cursor] == "\n":
            line_start = cursor + 1
            cursor += 1
            continue
        if not mask[cursor]:
            cursor += 1
            continue
        if markdown[cursor] == "<" and not _backslash_escaped(markdown, cursor):
            literal_open = _HTML_LITERAL_OPEN.match(markdown, cursor)
            if literal_open is not None:
                opening_end = _html_tag_end(markdown, cursor)
                if opening_end is not None:
                    end = _html_literal_end(
                        markdown,
                        literal_open.group(1),
                        opening_end,
                    )
                    _hide(mask, cursor, end)
                    cursor = end
                    continue
            end = _html_tag_end(markdown, cursor)
            if end is not None:
                _hide(mask, cursor, end)
                cursor = end
                continue
        if markdown[cursor] != "[" or _backslash_escaped(markdown, cursor):
            cursor += 1
            continue
        closing = bracket_matches.get(cursor)
        if closing is None:
            cursor += 1
            continue
        label = markdown[cursor + 1 : closing]
        after = closing + 1
        is_definition = (
            not label.startswith("^")
            and after < len(markdown)
            and markdown[after] == ":"
            and cursor - line_start <= 3
            and not markdown[line_start:cursor].strip(" ")
        )
        if is_definition:
            line_end = markdown.find("\n", after + 1)
            end = len(markdown) if line_end < 0 else line_end
            _hide(mask, line_start, end)
            cursor = end
            continue
        if after < len(markdown) and markdown[after] == "(":
            end = _parenthesized_end(markdown, after)
            if end is not None:
                _hide(mask, after, end)
                cursor = end
                continue
        if label.startswith("^"):
            cursor = closing + 1
            continue
        reference_start = after
        while (
            reference_start < len(markdown)
            and markdown[reference_start] in " \t"
        ):
            reference_start += 1
        if (
            reference_start < len(markdown)
            and markdown[reference_start] == "["
        ):
            reference_closing = bracket_matches.get(reference_start)
            if reference_closing is not None:
                _hide(mask, reference_start, reference_closing + 1)
                cursor = reference_closing + 1
                continue
        cursor = closing + 1


def _destination_after(markdown: str, start: int) -> tuple[str, int] | None:
    cursor = start
    while cursor < len(markdown) and markdown[cursor] in " \t":
        cursor += 1
    if cursor >= len(markdown):
        return None
    if markdown[cursor] == "<":
        end = cursor + 1
        while end < len(markdown):
            if markdown[end] == ">" and not _backslash_escaped(markdown, end):
                return markdown[cursor : end + 1], end + 1
            if markdown[end] in "\r\n":
                return None
            end += 1
        return None
    end = cursor
    parentheses = 0
    while end < len(markdown):
        character = markdown[end]
        if character in " \t\r\n" and parentheses == 0:
            break
        if character == "(" and not _backslash_escaped(markdown, end):
            parentheses += 1
        elif character == ")" and not _backslash_escaped(markdown, end):
            if parentheses == 0:
                break
            parentheses -= 1
        end += 1
    return (markdown[cursor:end], end) if end > cursor else None


def _iter_markdown_destinations(markdown: str):
    bracket_matches = _bracket_matches(markdown)
    definitions: dict[str, str] = {}
    references: list[tuple[str, bool]] = []
    destinations: list[tuple[str, bool]] = []
    cursor = 0
    line_start = 0
    while cursor < len(markdown):
        if markdown[cursor] == "\n":
            line_start = cursor + 1
            cursor += 1
            continue
        if markdown[cursor] != "[" or _backslash_escaped(markdown, cursor):
            cursor += 1
            continue
        is_image = (
            cursor > 0
            and markdown[cursor - 1] == "!"
            and not _backslash_escaped(markdown, cursor - 1)
        )
        if cursor + 1 < len(markdown) and markdown[cursor + 1] == "[":
            closing = bracket_matches.get(cursor + 1)
            if closing is None or bracket_matches.get(cursor) != closing + 1:
                cursor += 1
                continue
            destinations.append(
                (markdown[cursor + 2 : closing].split("|", 1)[0], is_image)
            )
            cursor = closing + 2
            continue
        closing = bracket_matches.get(cursor)
        if closing is None:
            cursor += 1
            continue
        label = markdown[cursor + 1 : closing]
        after = closing + 1
        is_definition = (
            not is_image
            and not label.startswith("^")
            and after < len(markdown)
            and markdown[after] == ":"
            and cursor - line_start <= 3
            and not markdown[line_start:cursor].strip(" ")
        )
        if is_definition:
            parsed = _destination_after(markdown, after + 1)
            if parsed is not None:
                destination, end = parsed
                definitions.setdefault(_reference_label(label), destination)
                destinations.append((destination, False))
                cursor = end
                continue
        destination_start = closing + 1
        if destination_start < len(markdown) and markdown[destination_start] == "(":
            parsed = _destination_after(markdown, destination_start + 1)
            if parsed is not None:
                destination, end = parsed
                destinations.append((destination, is_image))
                cursor = end + (
                    1 if end < len(markdown) and markdown[end] == ")" else 0
                )
                continue
        if label.startswith("^"):
            cursor = closing + 1
            continue
        reference_label = label
        if after < len(markdown) and markdown[after] == "[":
            reference_closing = bracket_matches.get(after)
            if reference_closing is not None:
                explicit_label = markdown[after + 1 : reference_closing]
                reference_label = explicit_label or label
                cursor = reference_closing + 1
            else:
                cursor = closing + 1
        else:
            cursor = closing + 1
        references.append((_reference_label(reference_label), is_image))

    for destination in destinations:
        yield destination
    for label, is_image in references:
        destination = definitions.get(label)
        if destination is not None:
            yield destination, is_image


@dataclass(frozen=True)
class _RenderedImage:
    destination: str | None
    start: int
    end: int


def _rendered_images(markdown: str) -> tuple[_RenderedImage, ...]:
    visible = _scan_markdown(markdown).visible_text
    characters = list(visible)
    bracket_matches = _bracket_matches(visible)
    image_label_intervals = [
        (opening + 1, closing)
        for opening in range(len(visible))
        if visible[opening] == "["
        and opening > 0
        and visible[opening - 1] == "!"
        and not _backslash_escaped(visible, opening - 1)
        and (closing := bracket_matches.get(opening)) is not None
    ]

    def hide_context(start: int, end: int) -> None:
        for index in range(start, end):
            if characters[index] != "\n":
                characters[index] = " "

    raw_images: dict[int, _RenderedImage] = {}
    interval_cursor = 0
    image_label_end = 0
    cursor = 0
    while cursor < len(visible):
        character = visible[cursor]
        while (
            interval_cursor < len(image_label_intervals)
            and image_label_intervals[interval_cursor][0] <= cursor
        ):
            image_label_end = max(
                image_label_end,
                image_label_intervals[interval_cursor][1],
            )
            interval_cursor += 1
        if (
            character != "<"
            or cursor < image_label_end
            or _backslash_escaped(visible, cursor)
        ):
            cursor += 1
            continue
        end = _html_tag_end(visible, cursor)
        if end is None:
            cursor += 1
            continue
        if visible.startswith("<!--", cursor):
            hide_context(cursor, end)
            cursor = end
            continue
        literal_open = _HTML_LITERAL_OPEN.match(visible, cursor)
        if literal_open is not None:
            literal_end = _html_literal_end(
                visible,
                literal_open.group(1),
                end,
            )
            hide_context(cursor, literal_end)
            cursor = literal_end
            continue
        if _HTML_IMAGE_OPEN.match(visible, cursor, end) is not None:
            raw_images[cursor] = _RenderedImage(
                _raw_html_image_source(visible, cursor, end),
                cursor,
                end,
            )
        hide_context(cursor, end)
        cursor = end

    contextual = "".join(characters)
    bracket_matches = _bracket_matches(contextual)
    definitions: dict[str, str] = {}
    found: list[tuple[_RenderedImage | None, str | None, int, int]] = []
    cursor = 0
    line_start = 0
    while cursor < len(contextual):
        raw_image = raw_images.get(cursor)
        if raw_image is not None:
            found.append((raw_image, None, raw_image.start, raw_image.end))
            cursor = raw_image.end
            continue
        if contextual[cursor] == "\n":
            line_start = cursor + 1
            cursor += 1
            continue
        if contextual[cursor] == "[" and not _backslash_escaped(contextual, cursor):
            closing = bracket_matches.get(cursor)
            if closing is not None:
                after = closing + 1
                label = contextual[cursor + 1 : closing]
                if (
                    not label.startswith("^")
                    and after < len(contextual)
                    and contextual[after] == ":"
                    and cursor - line_start <= 3
                    and not contextual[line_start:cursor].strip(" ")
                ):
                    parsed = _destination_after(contextual, after + 1)
                    if parsed is not None:
                        definitions.setdefault(
                            _reference_label(label), parsed[0]
                        )
                    line_end = contextual.find("\n", after + 1)
                    cursor = len(contextual) if line_end < 0 else line_end
                    continue
        if (
            contextual[cursor] != "!"
            or _backslash_escaped(contextual, cursor)
            or cursor + 1 >= len(contextual)
            or contextual[cursor + 1] != "["
        ):
            cursor += 1
            continue

        opening = cursor + 1
        if contextual.startswith("![[", cursor):
            inner_opening = cursor + 2
            inner_closing = bracket_matches.get(inner_opening)
            outer_closing = bracket_matches.get(opening)
            if (
                inner_closing is not None
                and outer_closing == inner_closing + 1
            ):
                destination = contextual[inner_opening + 1 : inner_closing].split(
                    "|", 1
                )[0]
                end = outer_closing + 1
                found.append(
                    (_RenderedImage(destination, cursor, end), None, cursor, end)
                )
                cursor = end
                continue

        closing = bracket_matches.get(opening)
        if closing is None:
            cursor += 1
            continue
        label = contextual[opening + 1 : closing]
        after = closing + 1
        if after < len(contextual) and contextual[after] == "(":
            end = _parenthesized_end(contextual, after)
            parsed = _destination_after(contextual, after + 1)
            if end is not None and parsed is not None and parsed[1] < end:
                found.append(
                    (_RenderedImage(parsed[0], cursor, end), None, cursor, end)
                )
                cursor = end
                continue
        reference_label = label
        end = closing + 1
        if after < len(contextual) and contextual[after] == "[":
            reference_closing = bracket_matches.get(after)
            if reference_closing is None:
                cursor += 1
                continue
            explicit = contextual[after + 1 : reference_closing]
            reference_label = explicit or label
            end = reference_closing + 1
        normalized_label = _reference_label(reference_label)
        if not normalized_label.startswith("^"):
            found.append((None, normalized_label, cursor, end))
        cursor = end

    images: list[_RenderedImage] = []
    for image, reference_label, start, end in found:
        if image is not None:
            images.append(image)
            continue
        destination = definitions.get(reference_label or "")
        if destination is not None:
            images.append(_RenderedImage(destination, start, end))
    return tuple(images)


def _validate_destination(
    destination: str,
    bundle_relative_path: str,
    *,
    allow_external: bool = True,
) -> str | None:
    value = html.unescape(destination.strip())
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if not value:
        raise _unsafe()
    decoded = value
    for _ in range(8):
        if _ENCODED_PATH_CONTROL.search(decoded) is not None:
            raise _unsafe()
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        raise _unsafe()
    if _DANGEROUS_SCHEME.search(decoded) is not None:
        raise _unsafe()
    if "\\" in decoded or _WINDOWS_DRIVE.match(decoded) is not None:
        raise _unsafe()

    try:
        parsed = urlsplit(decoded)
    except ValueError:
        raise _unsafe() from None
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not allow_external:
            raise _unsafe()
        return None
    if decoded.startswith(("/", "//")):
        raise _unsafe()
    if decoded.startswith("#"):
        return None

    relative_path = decoded.split("#", 1)[0].split("?", 1)[0]
    if not relative_path:
        return None
    base_directory = posixpath.dirname(bundle_relative_path)
    normalized = posixpath.normpath(posixpath.join(base_directory, relative_path))
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise _unsafe()
    return normalized


def _is_portable_bundle_path(value: str) -> bool:
    decoded = value
    for _ in range(8):
        if (
            any(ord(character) < 32 or ord(character) == 127 for character in decoded)
            or _ENCODED_PATH_CONTROL.search(decoded) is not None
        ):
            return False
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        return False
    if (
        not decoded
        or ":" in decoded
        or "\\" in decoded
        or decoded.startswith("/")
        or _WINDOWS_DRIVE.match(decoded) is not None
        or posixpath.normpath(decoded) != decoded
        or posixpath.normpath(decoded).startswith("../")
    ):
        return False
    for segment in decoded.split("/"):
        if (
            segment.endswith((".", " "))
            or segment.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
        ):
            return False
    return True


def validate_markdown_safety(
    markdown: str,
    *,
    bundle_relative_path: str,
) -> None:
    if not isinstance(markdown, str) or not isinstance(bundle_relative_path, str):
        raise _unsafe()
    if not _is_portable_bundle_path(bundle_relative_path):
        raise _unsafe()
    if any(ord(character) < 32 and character not in "\t\n\r" for character in markdown):
        raise _unsafe()

    scan = _scan_markdown(markdown)
    if scan.has_mermaid_fence:
        raise _unsafe()
    visible = scan.visible_text
    decoded_visible = html.unescape(visible)
    if (
        _ACTIVE_TAG.search(decoded_visible) is not None
        or _ACTIVE_ATTRIBUTE.search(decoded_visible) is not None
        or _DANGEROUS_SCHEME.search(decoded_visible) is not None
    ):
        raise _unsafe()

    destinations: list[tuple[str, bool]] = []
    destinations.extend(_iter_markdown_destinations(visible))
    destinations.extend((match.group(1), False) for match in _AUTOLINK.finditer(visible))
    for match in _HTML_LINK.finditer(visible):
        destinations.append(
            (next(value for value in match.groups() if value is not None), False)
        )
    for image in _rendered_images(markdown):
        if image.destination is None:
            raise _unsafe()
        destinations.append((image.destination, True))
    for destination, is_image in destinations:
        _validate_destination(
            destination,
            bundle_relative_path,
            allow_external=not is_image,
        )


def rendered_image_bundle_paths(
    markdown: str,
    *,
    bundle_relative_path: str,
) -> tuple[str, ...]:
    if not isinstance(markdown, str) or not isinstance(bundle_relative_path, str):
        raise _unsafe()
    if not _is_portable_bundle_path(bundle_relative_path):
        raise _unsafe()

    paths: list[str] = []
    for image in _rendered_images(markdown):
        if image.destination is None:
            raise _unsafe()
        path = _validate_destination(
            image.destination,
            bundle_relative_path,
            allow_external=False,
        )
        if path is None:
            raise _unsafe()
        paths.append(path)
    return tuple(paths)


@dataclass(frozen=True)
class _MarkdownAnalysis:
    citation_ids: tuple[str, ...]
    definition_ids: tuple[str, ...]
    duplicate_definition_ids: frozenset[str]
    substantive_h2_citations: tuple[tuple[str, ...], ...]
    has_segment_citation: bool


def _analyze_markdown(markdown: str) -> _MarkdownAnalysis:
    citations: list[str] = []
    definitions: list[str] = []
    duplicate_definitions: set[str] = set()
    sections: list[list[str]] = []
    current_section: list[str] | None = None
    current_substantive = False
    current_citations: list[str] = []
    has_segment_citation = False

    def finish_section() -> None:
        nonlocal current_section, current_substantive, current_citations
        if current_section is not None and current_substantive:
            sections.append(list(current_citations))
        current_section = None
        current_substantive = False
        current_citations = []

    lines = markdown_visible_lines(markdown)
    setext_h2_titles = {
        index
        for index in range(len(lines) - 1)
        if lines[index].strip() and _SETEXT_H2_UNDERLINE.match(lines[index + 1])
    }
    setext_h2_underlines = {index + 1 for index in setext_h2_titles}

    for index, line in enumerate(lines):
        if index in setext_h2_titles:
            finish_section()
            current_section = []
            continue
        if index in setext_h2_underlines:
            continue
        if _H2.match(line) is not None:
            finish_section()
            current_section = []
            continue
        indent = len(line) - len(line.lstrip(" "))
        definition_line = line
        if indent <= 3:
            slash_end = indent
            while slash_end < len(line) and line[slash_end] == "\\":
                slash_end += 1
            if (slash_end - indent) % 2:
                definition_line = ""
            elif slash_end > indent:
                definition_line = line[:indent] + line[slash_end:]
        definition = _FOOTNOTE_DEFINITION.match(definition_line)
        if definition is not None:
            evidence_id = definition.group(1)
            if evidence_id in definitions:
                duplicate_definitions.add(evidence_id)
            definitions.append(evidence_id)
            continue
        line_citations = [
            match.group(1)
            for match in _FOOTNOTE.finditer(line)
            if not _backslash_escaped(line, match.start())
        ]
        citations.extend(line_citations)
        has_segment_citation = has_segment_citation or any(
            not _backslash_escaped(line, match.start())
            for match in _SEGMENT_CITATION.finditer(line)
        )
        if current_section is not None and line.strip():
            current_substantive = True
            current_citations.extend(line_citations)
    finish_section()
    return _MarkdownAnalysis(
        citation_ids=tuple(citations),
        definition_ids=tuple(definitions),
        duplicate_definition_ids=frozenset(duplicate_definitions),
        substantive_h2_citations=tuple(tuple(values) for values in sections),
        has_segment_citation=has_segment_citation,
    )
