"""Smart text chunking for WeChat message size limits.

WeChat has a practical limit of ~4000 chars per message, but we use 2000
as a conservative boundary to ensure readability.
"""

from __future__ import annotations

MAX_CHUNK = 2000


def chunk_text(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    """Split text into chunks respecting paragraph and code block boundaries.

    Priority: code block > paragraph > sentence > hard cut.
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
        split_at = _find_split(candidate)

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return [c for c in chunks if c.strip()]


def _find_split(text: str) -> int:
    """Find the best split point in text, searching backwards from end."""
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

    # Try: single newline
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
