"""Strict stdlib reader for the application's flat-list YAML tracker."""
import re


class TrackerCompatibilityError(ValueError):
    """The tracker is outside the supported, unambiguous YAML subset."""


_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_BLOCK_HEADER_RE = re.compile(r"^([|>])([+-]?[1-9]?|[1-9]?[+-]?)$")


def _error(message, line_no):
    raise TrackerCompatibilityError("%s at line %d" % (message, line_no))


def _strip_comment(value):
    """Remove a YAML comment without changing quoted or block header text."""
    value = value.lstrip()
    if value.startswith(("'", '"')):
        quote = value[0]
        index = 1
        while index < len(value):
            char = value[index]
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                    index += 2
                    continue
                suffix = value[index + 1:]
                marker = suffix.find("#")
                if marker >= 0 and suffix[:marker].strip() == "":
                    return value[:index + 1]
                return value
            index += 1
        return value
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _quoted(value, line_no):
    quote = value[0]
    output = []
    index = 1
    escapes = {
        "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n",
        "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ",
        '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
        "L": "\u2028", "P": "\u2029",
    }
    while index < len(value):
        char = value[index]
        if quote == "'":
            if char == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    output.append("'")
                    index += 2
                    continue
                suffix = value[index + 1:]
                if suffix.strip():
                    _error("unexpected text after quoted scalar", line_no)
                return "".join(output)
            output.append(char)
            index += 1
            continue

        if char == '"':
            suffix = value[index + 1:]
            if suffix.strip():
                _error("unexpected text after quoted scalar", line_no)
            return "".join(output)
        if char != "\\":
            output.append(char)
            index += 1
            continue
        index += 1
        if index >= len(value):
            _error("malformed quoted scalar", line_no)
        escape = value[index]
        index += 1
        if escape in escapes:
            output.append(escapes[escape])
            continue
        sizes = {"x": 2, "u": 4, "U": 8}
        size = sizes.get(escape)
        if size is None or index + size > len(value):
            _error("unsupported quoted escape", line_no)
        digits = value[index:index + size]
        if not re.fullmatch(r"[0-9a-fA-F]{%d}" % size, digits):
            _error("malformed quoted escape", line_no)
        codepoint = int(digits, 16)
        if 0xD800 <= codepoint <= 0xDFFF:
            _error("malformed quoted escape", line_no)
        try:
            output.append(chr(codepoint))
        except (ValueError, OverflowError):
            _error("malformed quoted escape", line_no)
        index += size
    _error("unterminated quoted scalar", line_no)


def _quoted_end(value, quote):
    index = 0
    while index < len(value):
        char = value[index]
        if quote == '"' and char == "\\":
            if index + 1 == len(value):
                return None, value[:index], True, ""
            index += 2
            continue
        if char == quote:
            if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            return index, value[:index], False, value[index + 1:]
        index += 1
    return None, value, False, ""


def _valid_quote_suffix(value, line_no):
    if not value.strip():
        return
    leading = len(value) - len(value.lstrip())
    if leading and value[leading:].startswith("#"):
        return
    _error("unexpected text after quoted scalar", line_no)


def _fold_quoted_segments(segments):
    output = []
    previous = None
    pending_blank_lines = 0
    for content, more_indented, escaped_break, closing in segments:
        if not content and not closing:
            pending_blank_lines += 1
            continue
        if previous is not None:
            if previous[2]:
                separator = ""
            elif pending_blank_lines:
                separator = "\n" * pending_blank_lines
            elif previous[1] or more_indented:
                separator = "\n"
            else:
                separator = " "
            output.append(separator)
        output.append(content)
        previous = (content, more_indented, escaped_break)
        pending_blank_lines = 0
    if pending_blank_lines:
        output.append("\n" * pending_blank_lines)
    return "".join(output)


def _read_quoted_scalar(lines, start, parent_indent, value, line_no):
    quote = value[0]
    end, content, escaped_break, suffix = _quoted_end(value[1:], quote)
    if end is not None:
        _valid_quote_suffix(suffix, line_no)
        return _quoted(value, line_no), start

    segments = [(content, False, escaped_break, False)]
    continuation_indent = None
    index = start
    while index < len(lines):
        raw = lines[index]
        if raw.strip() == "":
            segments.append(("", False, False, False))
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= parent_indent or "\t" in raw[:indent]:
            _error("unterminated quoted scalar", line_no)
        if continuation_indent is None:
            continuation_indent = indent
        if indent < continuation_indent:
            _error("malformed quoted scalar indentation", index + 1)
        extra_indent = indent - continuation_indent
        line = raw[continuation_indent:]
        end, content, escaped_break, suffix = _quoted_end(line, quote)
        more_indented = extra_indent > 0
        if end is None:
            segments.append((" " * extra_indent + content, more_indented,
                             escaped_break, False))
            index += 1
            continue
        _valid_quote_suffix(suffix, index + 1)
        segments.append((" " * extra_indent + content, more_indented,
                         escaped_break, True))
        folded = _fold_quoted_segments(segments)
        return _quoted(quote + folded + quote, line_no), index + 1
    _error("unterminated quoted scalar", line_no)


def _scalar(value, line_no):
    value = _strip_comment(value).strip()
    if value == "":
        return None
    if value.startswith(("'", '"')):
        return _quoted(value, line_no)
    if value[0] in "[]{}&*!|>@`" or ": " in value or "\t" in value:
        _error("unsupported tracker scalar", line_no)
    if value.lower() in ("null", "~"):
        return None
    # Tracker fields are text even when they resemble YAML booleans, numbers,
    # or dates. Nullable fields above are the only implicit type conversion.
    return value


def _block_header(value, line_no):
    value = _strip_comment(value).strip()
    if not value.startswith(("|", ">")):
        return None
    match = _BLOCK_HEADER_RE.fullmatch(value)
    if not match:
        _error("unsupported block scalar header", line_no)
    suffix = value[1:]
    indent_match = re.search(r"[1-9]", suffix)
    chomp_match = re.search(r"[+-]", suffix)
    indent = int(indent_match.group()) if indent_match else None
    chomp = chomp_match.group() if chomp_match else ""
    return value[0], indent, chomp


def _fold_block(lines):
    output = []
    for index, line in enumerate(lines):
        if index:
            previous = lines[index - 1]
            if previous and line and not previous.startswith(" ") and not line.startswith(" "):
                output.append(" ")
            else:
                output.append("\n")
        output.append(line)
    return "".join(output)


def _read_block(lines, start, parent_indent, header, line_no):
    style, explicit_indent, chomp = header
    content_indent = parent_indent + explicit_indent if explicit_indent else None
    block_lines = []
    index = start
    while index < len(lines):
        raw = lines[index]
        indent = len(raw) - len(raw.lstrip(" "))
        if raw.strip() == "":
            block_lines.append("")
            index += 1
            continue
        if "\t" in raw[:indent]:
            _error("tabs are not supported for YAML indentation", index + 1)
        if indent <= parent_indent or (content_indent is not None and indent < content_indent):
            break
        if content_indent is None:
            content_indent = indent
        block_lines.append(raw[content_indent:])
        index += 1

    trailing_blank_lines = 0
    for line in reversed(block_lines):
        if line:
            break
        trailing_blank_lines += 1
    body_lines = block_lines[:-trailing_blank_lines] if trailing_blank_lines else block_lines
    body = "\n".join(body_lines) if style == "|" else _fold_block(body_lines)
    if chomp == "-":
        return body, index
    if not body_lines:
        return ("\n" * trailing_blank_lines if chomp == "+" else ""), index
    suffix_count = 1 + trailing_blank_lines if chomp == "+" else 1
    return body.rstrip("\n") + "\n" * suffix_count, index


def _read_plain_continuations(lines, start, parent_indent, initial):
    """Fold valid indented continuation lines of a plain YAML scalar."""
    parts = [initial]
    index = start
    continuation_indent = None
    previous_more_indented = False
    pending_blank_lines = 0
    blank_start = None
    had_continuation = False
    while index < len(lines):
        raw = lines[index]
        if raw.strip() == "":
            if blank_start is None:
                blank_start = index
            pending_blank_lines += 1
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= parent_indent:
            if blank_start is not None:
                index = blank_start
            break
        content = raw[indent:]
        if content.startswith("#"):
            index += 1
            continue
        if continuation_indent is None:
            continuation_indent = indent
        if indent < continuation_indent:
            if blank_start is not None:
                index = blank_start
            break
        extra_indent = indent - continuation_indent
        content = (" " * extra_indent
                   + _strip_comment(raw[indent:]).rstrip())
        if not content:
            index += 1
            continue
        more_indented = indent > continuation_indent
        if pending_blank_lines:
            separator = "\n" * pending_blank_lines
        elif had_continuation and (more_indented or previous_more_indented):
            separator = "\n"
        else:
            separator = " "
        parts.append(separator + content)
        had_continuation = True
        previous_more_indented = more_indented
        pending_blank_lines = 0
        blank_start = None
        index += 1
    return "".join(parts), index


def _entry(text, line_no, indent):
    body = text[indent:]
    if ":" not in body:
        _error("malformed tracker mapping", line_no)
    key, value = body.split(":", 1)
    key = key.strip()
    if not _KEY_RE.fullmatch(key):
        _error("malformed tracker key", line_no)
    return key, value.lstrip()


def parse_application_tracker(text, *, allow_comment_only=False):
    """Parse a top-level list of flat mappings and ``[]``.

    Strings, including date-looking text and status values, remain unchanged.
    Unquoted null scalars become ``None``. Literal and folded block scalars
    preserve multiline notes; unsupported YAML structures fail closed.
    """
    if not isinstance(text, str):
        raise TrackerCompatibilityError("tracker text must be a string")
    if allow_comment_only and not any(
            line.strip() and not line.lstrip().startswith("#")
            for line in text.splitlines()):
        return []
    lines = text.splitlines()
    rows = []
    current = None
    empty_seen = False
    document_started = False
    document_ended = False
    index = 0
    while index < len(lines):
        raw = lines[index]
        line_no = index + 1
        if "\t" in raw[:len(raw) - len(raw.lstrip(" ")) + 1]:
            _error("tabs are not supported for YAML indentation", line_no)
        stripped = _strip_comment(raw).strip()
        if not stripped:
            index += 1
            continue
        if stripped == "---":
            if document_started or rows or empty_seen or document_ended:
                _error("multiple YAML documents are not supported", line_no)
            document_started = True
            index += 1
            continue
        if stripped == "...":
            if document_ended:
                _error("multiple YAML document endings are not supported", line_no)
            document_ended = True
            index += 1
            continue
        if document_ended:
            _error("content follows the YAML document ending", line_no)
        if stripped == "[]":
            if rows or empty_seen:
                _error("malformed tracker empty-list marker", line_no)
            empty_seen = True
            index += 1
            continue
        if empty_seen:
            _error("content follows the tracker empty-list marker", line_no)

        if raw.startswith("- "):
            current = {}
            rows.append(current)
            key, value = _entry(raw[2:], line_no, 0)
            indent = 2
        elif current is not None and raw.startswith("  ") and not raw.startswith("   "):
            key, value = _entry(raw, line_no, 2)
            indent = 2
        else:
            _error("unsupported tracker structure", line_no)
        if key in current:
            _error("duplicate tracker key", line_no)

        header = _block_header(value, line_no)
        if header is not None:
            parsed, index = _read_block(lines, index + 1, indent, header, line_no)
            current[key] = parsed
            continue
        scalar_text = _strip_comment(value).strip()
        if scalar_text.startswith(("'", '"')):
            parsed, index = _read_quoted_scalar(
                lines, index + 1, indent, scalar_text, line_no)
            current[key] = parsed
            continue
        parsed = _scalar(scalar_text, line_no)
        if isinstance(parsed, str):
            parsed, index = _read_plain_continuations(
                lines, index + 1, indent, parsed)
            current[key] = parsed
            continue
        current[key] = parsed
        index += 1

    if empty_seen:
        return []
    if not rows:
        raise TrackerCompatibilityError(
            "tracker needs entries or an explicit empty-list marker")
    return rows
