"""Markdown → WeChat plain-text format adaptation.

WeChat personal chat does not render full Markdown. This module converts
common Markdown patterns to visually readable plain text:
- # Heading      → 【Heading】
- ## Heading     → **Heading**
- | tables |     → key: value list
- Extra blank lines → collapsed
"""

from __future__ import annotations

import re


def md_to_wechat(text: str) -> str:
    """Convert Markdown text to WeChat-friendly plain text."""
    text = _convert_headings(text)
    text = _convert_tables(text)
    text = _collapse_blank_lines(text)
    return text


def _convert_headings(text: str) -> str:
    """Convert Markdown headings to WeChat format.

    # H1 → 【H1】
    ## H2+ → **H2+**
    """
    lines = text.split("\n")
    result = []
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level == 1:
                result.append(f"【{title}】")
            else:
                result.append(f"**{title}**")
        else:
            result.append(line)
    return "\n".join(result)


_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*[-:]+[-|:\s]*$")


def _convert_tables(text: str) -> str:
    """Convert Markdown tables to key-value list format.

    | Name | Value |     →  - Name: Value
    |------|-------|        - Name2: Value2
    | A    | B     |
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        # Detect table: header row + separator row
        if (i + 1 < len(lines)
                and "|" in lines[i]
                and _TABLE_SEP_RE.match(lines[i + 1])):
            headers = _parse_table_row(lines[i])
            i += 2  # skip header + separator
            while i < len(lines) and "|" in lines[i] and not _TABLE_SEP_RE.match(lines[i]):
                cells = _parse_table_row(lines[i])
                if len(headers) >= 2 and len(cells) >= 2:
                    # Multi-column: format as key-value pairs
                    pairs = []
                    for h, c in zip(headers, cells):
                        if c.strip():
                            pairs.append(f"{h}: {c}")
                    result.append("- " + " | ".join(pairs))
                elif cells:
                    result.append("- " + " | ".join(cells))
                i += 1
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


def _parse_table_row(line: str) -> list[str]:
    """Parse a Markdown table row into cells."""
    # Strip leading/trailing |
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _collapse_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive blank lines to 2 (double newline)."""
    return re.sub(r"\n{3,}", "\n\n", text)
