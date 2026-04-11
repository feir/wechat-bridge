"""Smart text chunking for WeChat message size limits.

WeChat has a practical limit of ~4000 chars per message. We use 3800
to stay within limits while maximizing information density per message.
"""

from __future__ import annotations

import re

MAX_CHUNK = 3800

# Lines starting with these patterns are "continuation" lines that should
# stay attached to the previous line (list sub-items, indented blocks, etc.)
_CONTINUATION_RE = re.compile(r"^(?:\s+[-*+•]|\s{2,}\S|\s+\d+[.)]\s)")


def chunk_text(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    """Split text into chunks respecting paragraph, code block, and indentation boundaries.

    Priority: code block > paragraph > safe newline (not continuation) > sentence > hard cut.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Find best split point within max_len
        candidate = remaining[:max_len]
        split_at = _find_split(candidate, remaining)

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return [c for c in chunks if c.strip()]


def _find_split(text: str, full_text: str) -> int:
    """Find the best split point in text, searching backwards from end.

    Args:
        text: The candidate chunk (up to max_len).
        full_text: The full remaining text (used to check if next line is a continuation).
    """
    n = len(text)

    # Try: code block boundary (```)
    idx = text.rfind("\n```\n")
    if idx > n // 3:
        return idx + 4  # after the closing ```\n

    idx = text.rfind("\n```")
    if idx > n // 3 and idx == n - 4:
        return idx + 4

    # Try: double newline (paragraph break)
    idx = text.rfind("\n\n")
    if idx > n // 3:
        return idx + 1

    # Try: single newline — but not before a continuation line
    # Search backwards for a newline where the NEXT line is not indented/continuation
    idx = _find_safe_newline(text, full_text)
    if idx > n // 3:
        return idx + 1

    # Fallback: any single newline
    idx = text.rfind("\n")
    if idx > n // 3:
        return idx + 1

    # Try: sentence end
    for sep in ("。", ". ", "！", "？", "! ", "? "):
        idx = text.rfind(sep)
        if idx > n // 3:
            return idx + len(sep)

    # Hard cut
    return n


def _find_safe_newline(text: str, full_text: str) -> int:
    """Find the last newline in text where the following line is NOT a continuation.

    This prevents splitting between a list item and its sub-items, or between
    a paragraph and its indented continuation.
    """
    # Search backwards for newline positions
    pos = len(text) - 1
    while pos > len(text) // 3:
        idx = text.rfind("\n", 0, pos)
        if idx < 0:
            break

        # Check if the line after this newline is a continuation
        # Use full_text to look beyond the candidate boundary
        next_line_start = idx + 1
        if next_line_start < len(full_text):
            # Extract next line
            next_nl = full_text.find("\n", next_line_start)
            next_line = full_text[next_line_start:next_nl] if next_nl >= 0 else full_text[next_line_start:]
            if not _CONTINUATION_RE.match(next_line):
                return idx  # Safe to split here

        pos = idx  # Continue searching backwards

    return -1  # No safe newline found
