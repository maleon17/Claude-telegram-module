"""Markdown -> Telegram MarkdownV2 formatter.

Ported from hermes-agent (gateway/platforms/telegram/adapter.py and
gateway/platforms/helpers.py), MIT License, Copyright (c) 2025 Nous Research.
Adapted from instance methods to standalone functions for reuse outside the
hermes-agent gateway.
"""

import re
import secrets

# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


def strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escape backslashes to produce clean plain text."""
    cleaned = re.sub(r'\\([_*\[\]()~`>#\+\-=|{}.!\\])', r'\1', text)
    cleaned = re.sub(r'\*\*([^*]+)\*\*', r'\1', cleaned)
    cleaned = re.sub(r'\*([^*]+)\*', r'\1', cleaned)
    cleaned = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', cleaned)
    cleaned = re.sub(r'~([^~]+)~', r'\1', cleaned)
    cleaned = re.sub(r'\|\|([^|]+)\|\|', r'\1', cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# GFM table -> bullet-group conversion
# ---------------------------------------------------------------------------

TABLE_SEPARATOR_RE = re.compile(
    r'^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$'
)


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and '|' in stripped


def _split_markdown_table_row(line: str) -> list:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_block(table_block: list) -> str:
    if len(table_block) < 3:
        return "\n".join(table_block)

    headers = _split_markdown_table_row(table_block[0])
    if len(headers) < 2:
        return "\n".join(table_block)

    first_data_row = (
        _split_markdown_table_row(table_block[2]) if len(table_block) > 2 else []
    )
    has_row_label_col = len(first_data_row) == len(headers) + 1

    rendered_groups = []
    for index, row in enumerate(table_block[2:], start=1):
        cells = _split_markdown_table_row(row)
        if has_row_label_col:
            heading = cells[0] if cells and cells[0] else f"Row {index}"
            data_cells = cells[1:]
        else:
            heading = next((cell for cell in cells if cell), f"Row {index}")
            data_cells = cells

        if len(data_cells) < len(headers):
            data_cells.extend([""] * (len(headers) - len(data_cells)))
        elif len(data_cells) > len(headers):
            data_cells = data_cells[: len(headers)]

        bullets = []
        for header, value in zip(headers, data_cells):
            if not has_row_label_col and value == heading:
                continue
            bullets.append(f"• {header}: {value}")

        group_lines = [f"**{heading}**", *bullets]
        rendered_groups.append("\n".join(group_lines))

    return "\n\n".join(rendered_groups)


def convert_table_to_bullets(text: str) -> str:
    """Rewrite GFM pipe tables into bold-heading + bullet groups.

    Tables inside fenced code blocks are left alone.
    """
    if '|' not in text or '-' not in text:
        return text

    lines = text.split('\n')
    out = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if stripped.startswith('```'):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        if (
            '|' in line
            and i + 1 < len(lines)
            and TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            table_block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j]):
                table_block.append(lines[j])
                j += 1
            out.append(_render_table_block(table_block))
            i = j
            continue

        out.append(line)
        i += 1

    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Full markdown -> MarkdownV2 conversion
# ---------------------------------------------------------------------------

def format_message(content: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 format.

    Protected regions (code blocks, inline code) are extracted first so
    their contents are never modified. Standard markdown constructs
    (headers, bold, italic, links) are translated to MarkdownV2 syntax,
    and all remaining special characters are escaped.
    """
    if not content:
        return content

    # Random per-call nonce, verified absent from the real input before use
    # -- a bare counter (\x00PH0\x00 etc) can collide with the SAME literal
    # byte sequence if it's already present in attacker/tool-controlled
    # content (e.g. Claude echoing file/tool output), silently corrupting
    # step 11's restore (a real span of user text gets replaced by an
    # unrelated protected fragment). Since `nonce` is verified not to occur
    # anywhere in `content`, no string containing nonce as a substring can
    # occur in `content` either, so the full key can never collide.
    # Hex-only (no MarkdownV2 special chars) so step 10's escape pass, which
    # runs on the whole text before placeholders are restored, never
    # mangles the key itself.
    nonce = secrets.token_hex(8)
    while nonce in content:
        nonce = secrets.token_hex(8)

    placeholders = {}
    counter = [0]

    def _ph(value):
        key = f"\x00PH{nonce}{counter[0]:04d}\x00"
        counter[0] += 1
        placeholders[key] = value
        return key

    text = content

    # 0) Rewrite GFM-style pipe tables into Telegram-friendly row groups.
    text = convert_table_to_bullets(text)

    # 1) Protect fenced code blocks (``` ... ```)
    def _protect_fenced(m):
        raw = m.group(0)
        open_end = raw.index('\n') + 1 if '\n' in raw[3:] else 3
        opening = raw[:open_end]
        body_and_close = raw[open_end:]
        body = body_and_close[:-3]
        body = body.replace('\\', '\\\\').replace('`', '\\`')
        return _ph(opening + body + '```')

    text = re.sub(r'(```(?:[^\n]*\n)?[\s\S]*?```)', _protect_fenced, text)

    # 2) Protect inline code (`...`)
    text = re.sub(
        r'(`[^`]+`)',
        lambda m: _ph(m.group(0).replace('\\', '\\\\')),
        text,
    )

    # 3) Convert markdown links.
    def _convert_link(m):
        display = escape_mdv2(m.group(1))
        url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
        return _ph(f'[{display}]({url})')

    text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _convert_link, text)

    # 4) Convert markdown headers (## Title) -> bold *Title*
    def _convert_header(m):
        inner = m.group(1).strip()
        inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
        return _ph(f'*{escape_mdv2(inner)}*')

    text = re.sub(r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE)

    # 5) Convert bold: **text** -> *text*
    text = re.sub(
        r'\*\*(.+?)\*\*',
        lambda m: _ph(f'*{escape_mdv2(m.group(1))}*'),
        text,
    )

    # 6) Convert italic: *text* -> _text_
    text = re.sub(
        r'\*([^*\n]+)\*',
        lambda m: _ph(f'_{escape_mdv2(m.group(1))}_'),
        text,
    )

    # 7) Convert strikethrough: ~~text~~ -> ~text~
    text = re.sub(
        r'~~(.+?)~~',
        lambda m: _ph(f'~{escape_mdv2(m.group(1))}~'),
        text,
    )

    # 8) Convert spoiler: ||text|| -> ||text||
    text = re.sub(
        r'\|\|(.+?)\|\|',
        lambda m: _ph(f'||{escape_mdv2(m.group(1))}||'),
        text,
    )

    # 9) Convert blockquotes.
    def _convert_blockquote(m):
        prefix = m.group(1)
        blk_content = m.group(2)
        if prefix.startswith('**') and blk_content.endswith('||'):
            return _ph(f'{prefix} {escape_mdv2(blk_content[:-2])}||')
        return _ph(f'{prefix} {escape_mdv2(blk_content)}')

    text = re.sub(
        r'^((?:\*\*)?>{1,3}) (.+)$', _convert_blockquote, text, flags=re.MULTILINE
    )

    # 10) Escape remaining special characters in plain text.
    text = escape_mdv2(text)

    # 11) Restore placeholders in reverse insertion order.
    for key in reversed(list(placeholders.keys())):
        text = text.replace(key, placeholders[key])

    # 12) Safety net: escape unescaped ( ) { } that slipped through, skipping
    #     content inside code spans/blocks.
    _code_split = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
    _safe_parts = []
    for _idx, _seg in enumerate(_code_split):
        if _idx % 2 == 1:
            _safe_parts.append(_seg)
        else:
            def _esc_bare(m, _seg=_seg):
                s = m.start()
                ch = m.group(0)
                if s > 0 and _seg[s - 1] == '\\':
                    return ch
                if ch == '(' and s > 0 and _seg[s - 1] == ']':
                    return ch
                if ch == ')':
                    before = _seg[:s]
                    if '](http' in before or '](' in before:
                        depth = 0
                        for j in range(s - 1, max(s - 2000, -1), -1):
                            if _seg[j] == '(':
                                depth -= 1
                                if depth < 0:
                                    if j > 0 and _seg[j - 1] == ']':
                                        return ch
                                    break
                            elif _seg[j] == ')':
                                depth += 1
                return '\\' + ch
            _safe_parts.append(re.sub(r'[(){}]', _esc_bare, _seg))
    text = ''.join(_safe_parts)

    return text
