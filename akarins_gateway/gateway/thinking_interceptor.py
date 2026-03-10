"""
Anthropic SSE Thinking 标签拦截器

用于拦截 Anthropic 格式 SSE 流中的内联 <thinking> 标签，
并将其转换为标准 Anthropic Messages API 的 thinking 内容块。

背景：
- antigravity-tools 后端返回标准 Anthropic SSE 格式
- 但 thinking 内容作为 <thinking>...</thinking> 标签嵌入在 text_delta 中
- Claude Code 需要结构化的 thinking 内容块（type: "thinking"）才能识别
- gcli2api 自带的 antigravity 后端通过 antigravity_sse_to_anthropic_sse() 已正确处理，
  本拦截器只用于 antigravity-tools 等直接返回 Anthropic 格式但内联 thinking 标签的后端

作者: 浮浮酱 (Claude Opus 4.6)
创建日期: 2026-02-12
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from akarins_gateway.core.log import log

__all__ = ["intercept_thinking_in_anthropic_sse"]

# ==================== SSE 事件构造辅助函数 ====================


def _sse_event(event_type: str, data: dict) -> str:
    """Construct a complete SSE event string (with trailing double newline)."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _thinking_block_start(index: int) -> str:
    return _sse_event("content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "thinking", "thinking": ""},
    })


def _text_block_start(index: int) -> str:
    return _sse_event("content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    })


def _thinking_delta(index: int, thinking: str) -> str:
    return _sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "thinking_delta", "thinking": thinking},
    })


def _text_delta(index: int, text: str) -> str:
    return _sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    })


def _block_stop(index: int) -> str:
    return _sse_event("content_block_stop", {
        "type": "content_block_stop",
        "index": index,
    })


# ==================== 主拦截器 ====================


async def intercept_thinking_in_anthropic_sse(
    byte_stream: AsyncIterator[bytes],
) -> AsyncIterator[str]:
    """
    Intercept inline ``<thinking>`` tags in Anthropic SSE streams and convert
    them to structured thinking content blocks.

    The function is a transparent pass-through for streams that already use
    proper thinking blocks (``type: "thinking"``).  It only activates when
    it detects ``<thinking>`` inside a ``text_delta`` event.

    Processing logic:
    1. Buffer ``content_block_start type:text`` events (don't emit immediately)
    2. On the first ``text_delta`` for that block:
       - If it contains ``<thinking>``: emit a *thinking* block start instead
       - Otherwise: flush the buffered text block start and proceed normally
    3. While in thinking mode: convert ``text_delta`` → ``thinking_delta``
    4. On ``</thinking>``: close thinking block → open text block → resume normal
    5. All non-text-delta events are forwarded unchanged (with index fixup)
    6. If upstream already emits native thinking blocks, pass through everything

    Yields:
        Complete SSE event strings (each ending with ``\\n\\n``).
    """

    # ---- state ----
    in_thinking: bool = False
    index_offset: int = 0
    pending_block_start: dict | None = None
    pending_event_type: str | None = None
    detected_native_thinking: bool = False
    event_count: int = 0  # diagnostic counter

    # ---- byte buffer for SSE framing ----
    buf = b""
    chunk_count = 0  # diagnostic counter

    async for chunk in byte_stream:
        if not chunk:
            continue

        chunk_count += 1
        if chunk_count <= 3:
            # Log first few raw chunks to see the actual format
            preview = chunk[:200].decode("utf-8", errors="ignore").replace("\n", "\\n")
            log.info(
                f"[THINKING INTERCEPTOR] Raw chunk #{chunk_count} ({len(chunk)} bytes): "
                f"{preview!r}"
            )

        buf += chunk

        # Process complete SSE event blocks (delimited by \n\n)
        while b"\n\n" in buf:
            raw_block, buf = buf.split(b"\n\n", 1)
            event_str = raw_block.decode("utf-8", errors="ignore")

            # ---- parse SSE fields ----
            event_type: str | None = None
            data_str: str | None = None

            for line in event_str.split("\n"):
                line = line.rstrip("\r")
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_str = line[6:]

            # No data line → forward as-is (e.g. comments, keep-alive)
            if data_str is None:
                yield event_str + "\n\n"
                continue

            # Try JSON parse
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                yield event_str + "\n\n"
                continue

            msg_type = data.get("type", "")
            event_count += 1

            # Log first 5 parsed events to understand the stream format
            if event_count <= 5:
                log.info(
                    f"[THINKING INTERCEPTOR] SSE event #{event_count}: "
                    f"event_type={event_type!r}, msg_type={msg_type!r}, "
                    f"data_keys={list(data.keys())}"
                )

            # ==================================================================
            # If upstream already uses native thinking blocks, skip all
            # interception and just pass through everything from now on.
            # ==================================================================
            if detected_native_thinking:
                yield event_str + "\n\n"
                continue

            # ------------------------------------------------------------------
            # content_block_start
            # ------------------------------------------------------------------
            if msg_type == "content_block_start":
                block = data.get("content_block", {})
                block_type = block.get("type", "")

                if block_type == "thinking":
                    # Upstream already emits structured thinking → pass through all
                    detected_native_thinking = True
                    in_thinking = True
                    # Flush any pending text block start
                    if pending_block_start is not None:
                        yield _text_block_start(pending_block_start["index"])
                        pending_block_start = None
                    yield event_str + "\n\n"
                    continue

                if block_type == "text":
                    # Buffer this – we might need to convert it to thinking
                    orig_index = data.get("index", 0)
                    data["index"] = orig_index + index_offset
                    pending_block_start = data
                    pending_event_type = event_type or "content_block_start"
                    continue

                # Other block types (tool_use, etc.) → apply offset and pass through
                data["index"] = data.get("index", 0) + index_offset
                yield _sse_event(event_type or "content_block_start", data)
                continue

            # ------------------------------------------------------------------
            # content_block_delta
            # ------------------------------------------------------------------
            if msg_type == "content_block_delta":
                delta = data.get("delta", {})
                delta_type = delta.get("type", "")
                orig_index = data.get("index", 0)
                adjusted_index = orig_index + index_offset

                # Native thinking delta → mark as native and pass through all
                if delta_type == "thinking_delta":
                    detected_native_thinking = True
                    in_thinking = True
                    if pending_block_start is not None:
                        yield _text_block_start(pending_block_start["index"])
                        pending_block_start = None
                    yield event_str + "\n\n"
                    continue

                if delta_type != "text_delta":
                    # Non-text delta (e.g. input_json_delta) → offset and pass through
                    data["index"] = adjusted_index
                    yield _sse_event(event_type or "content_block_delta", data)
                    continue

                # ---- text_delta processing ----
                text = delta.get("text", "")

                # Case A: We have a pending (buffered) text block start
                if pending_block_start is not None:
                    pbs_index = pending_block_start["index"]
                    pending_block_start = None

                    if "<thinking>" in text:
                        # Convert buffered text block → thinking block
                        in_thinking = True
                        log.info(
                            "[THINKING INTERCEPTOR] Detected inline <thinking> tag, "
                            "converting to structured thinking block",
                            tag="GATEWAY",
                        )

                        # Text before <thinking> tag (rare but handle it)
                        before_tag, after_open = text.split("<thinking>", 1)
                        if before_tag:
                            # Emit text block for content before thinking
                            yield _text_block_start(pbs_index)
                            yield _text_delta(pbs_index, before_tag)
                            yield _block_stop(pbs_index)
                            index_offset += 1
                            pbs_index += 1

                        # Emit thinking block start
                        yield _thinking_block_start(pbs_index)

                        # Check if </thinking> is also in this chunk
                        if "</thinking>" in after_open:
                            thinking_part, remainder = after_open.split("</thinking>", 1)
                            if thinking_part:
                                yield _thinking_delta(pbs_index, thinking_part)
                            yield _block_stop(pbs_index)
                            in_thinking = False

                            # New text block for remaining content
                            index_offset += 1
                            new_index = pbs_index + 1
                            yield _text_block_start(new_index)
                            if remainder:
                                yield _text_delta(new_index, remainder)
                        else:
                            # Thinking continues in subsequent events
                            if after_open:
                                yield _thinking_delta(pbs_index, after_open)
                        continue
                    else:
                        # Normal text – flush the buffered text block start
                        yield _text_block_start(pbs_index)
                        yield _text_delta(pbs_index, text)
                        continue

                # Case B: Currently in thinking mode
                if in_thinking:
                    if "</thinking>" in text:
                        before, after = text.split("</thinking>", 1)
                        if before:
                            yield _thinking_delta(adjusted_index, before)
                        yield _block_stop(adjusted_index)
                        in_thinking = False

                        # Start new text block with incremented index
                        index_offset += 1
                        new_index = adjusted_index + 1
                        yield _text_block_start(new_index)
                        if after:
                            yield _text_delta(new_index, after)
                    else:
                        # Pure thinking content
                        yield _thinking_delta(adjusted_index, text)
                    continue

                # Case C: Normal mode, check for late <thinking> tag
                if "<thinking>" in text:
                    before, after_open = text.split("<thinking>", 1)
                    if before:
                        yield _text_delta(adjusted_index, before)
                    # Close current text block
                    yield _block_stop(adjusted_index)

                    # Open thinking block
                    index_offset += 1
                    think_index = adjusted_index + 1
                    in_thinking = True
                    yield _thinking_block_start(think_index)

                    if "</thinking>" in after_open:
                        thinking_part, remainder = after_open.split("</thinking>", 1)
                        if thinking_part:
                            yield _thinking_delta(think_index, thinking_part)
                        yield _block_stop(think_index)
                        in_thinking = False

                        index_offset += 1
                        text_index = think_index + 1
                        yield _text_block_start(text_index)
                        if remainder:
                            yield _text_delta(text_index, remainder)
                    else:
                        if after_open:
                            yield _thinking_delta(think_index, after_open)
                    continue

                # Case D: Normal text, no tags → pass through with offset
                data["index"] = adjusted_index
                yield _sse_event(event_type or "content_block_delta", data)
                continue

            # ------------------------------------------------------------------
            # content_block_stop
            # ------------------------------------------------------------------
            if msg_type == "content_block_stop":
                # Flush any pending block start
                if pending_block_start is not None:
                    yield _text_block_start(pending_block_start["index"])
                    pending_block_start = None

                data["index"] = data.get("index", 0) + index_offset
                yield _sse_event(event_type or "content_block_stop", data)
                if in_thinking:
                    in_thinking = False
                continue

            # ------------------------------------------------------------------
            # message_start / message_delta / message_stop / ping / etc.
            # ------------------------------------------------------------------
            yield event_str + "\n\n"

    # Flush remaining bytes in buffer
    if buf:
        remaining = buf.decode("utf-8", errors="ignore").strip()
        if remaining:
            yield remaining + "\n\n"

    # Flush pending block start (edge case: stream ended right after block_start)
    if pending_block_start is not None:
        yield _text_block_start(pending_block_start["index"])
