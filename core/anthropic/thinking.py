"""Streaming parser for provider-emitted thinking tags."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum


class ContentType(Enum):
    """Type of content chunk."""

    TEXT = "text"
    THINKING = "thinking"


@dataclass
class ContentChunk:
    """A chunk of parsed content."""

    type: ContentType
    content: str


class ThinkTagParser:
    """
    Streaming parser for ``<think>...</think>`` and ``<thought>...</thought>`` tags.

    Handles partial tags at chunk boundaries by buffering.
    """

    OPEN_TAGS = ("<think>", "<thought>")
    CLOSE_TAGS = ("</think>", "</thought>")

    def __init__(self):
        self._buffer: str = ""
        self._in_think_tag: bool = False

    @property
    def in_think_mode(self) -> bool:
        """Whether currently inside a think tag."""
        return self._in_think_tag

    def _any_tag_start(self, tag: str, tags: tuple[str, ...]) -> int | None:
        """Return position of the first occurrence of any tag in ``tags``, or None."""
        best: int | None = None
        for t in tags:
            pos = tag.find(t)
            if pos != -1 and (best is None or pos < best):
                best = pos
        return best

    def _is_partial_tag(self, text: str) -> bool:
        """Check if ``text`` could be a partial open or close tag."""
        tags = self.OPEN_TAGS + self.CLOSE_TAGS
        return any(t.startswith(text) for t in tags)

    def feed(self, content: str) -> Iterator[ContentChunk]:
        """Feed content and yield parsed chunks."""
        # Normalize <thought> to <think> inside buffer for consistent single-tag tracking.
        # Partial <thou / <though that complete across chunks will be normalized too.
        content = content.replace("<thought>", "<think>").replace(
            "</thought>", "</think>"
        )
        self._buffer += content

        # Normalise any <thought> / </thought> that formed in the buffer across chunks.
        self._buffer = self._buffer.replace("<thought>", "<think>").replace(
            "</thought>", "</think>"
        )

        while self._buffer:
            prev_len = len(self._buffer)
            if not self._in_think_tag:
                chunk = self._parse_outside_think()
            else:
                chunk = self._parse_inside_think()

            if chunk:
                yield chunk
            elif len(self._buffer) == prev_len:
                break

    def _parse_outside_think(self) -> ContentChunk | None:
        """Parse content outside think tags."""
        think_start = self._buffer.find("<think>")
        orphan_close = self._buffer.find("</think>")

        if orphan_close != -1 and (think_start == -1 or orphan_close < think_start):
            pre_orphan = self._buffer[:orphan_close]
            self._buffer = self._buffer[orphan_close + len("</think>") :]
            if pre_orphan:
                return ContentChunk(ContentType.TEXT, pre_orphan)
            return None

        if think_start == -1:
            last_bracket = self._buffer.rfind("<")
            if last_bracket != -1:
                potential_tag = self._buffer[last_bracket:]
                if self._is_partial_tag(potential_tag):
                    emit = self._buffer[:last_bracket]
                    self._buffer = self._buffer[last_bracket:]
                    if emit:
                        return ContentChunk(ContentType.TEXT, emit)
                    return None

            emit = self._buffer
            self._buffer = ""
            if emit:
                return ContentChunk(ContentType.TEXT, emit)
            return None

        pre_think = self._buffer[:think_start]
        self._buffer = self._buffer[think_start + len("<think>") :]
        self._in_think_tag = True
        if pre_think:
            return ContentChunk(ContentType.TEXT, pre_think)
        return None

    def _parse_inside_think(self) -> ContentChunk | None:
        """Parse content inside think tags."""
        think_end = self._buffer.find("</think>")

        if think_end == -1:
            last_bracket = self._buffer.rfind("<")
            if last_bracket != -1 and len(self._buffer) - last_bracket < len(
                "</think>"
            ):
                potential_tag = self._buffer[last_bracket:]
                if any(
                    close_tag.startswith(potential_tag) for close_tag in self.CLOSE_TAGS
                ):
                    emit = self._buffer[:last_bracket]
                    self._buffer = self._buffer[last_bracket:]
                    if emit:
                        return ContentChunk(ContentType.THINKING, emit)
                    return None

            emit = self._buffer
            self._buffer = ""
            if emit:
                return ContentChunk(ContentType.THINKING, emit)
            return None

        thinking_content = self._buffer[:think_end]
        self._buffer = self._buffer[think_end + len("</think>") :]
        self._in_think_tag = False
        if thinking_content:
            return ContentChunk(ContentType.THINKING, thinking_content)
        return None

    def flush(self) -> ContentChunk | None:
        """Flush any remaining buffered content."""
        if self._buffer:
            chunk_type = (
                ContentType.THINKING if self._in_think_tag else ContentType.TEXT
            )
            content = self._buffer
            self._buffer = ""
            return ContentChunk(chunk_type, content)
        return None
