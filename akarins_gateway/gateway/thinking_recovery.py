"""
Thinking Recovery Module - Signature 400 Error Detection & Recovery

Ported from upstream Antigravity-Manager (Rust):
  - src-tauri/src/proxy/handlers/claude.rs (signature error patterns)
  - src-tauri/src/proxy/handlers/openai.rs (repair prompt injection)
  - src-tauri/src/proxy/mappers/openai/thinking_recovery.rs (strip/close_tool_loop)

Author: 浮浮酱 (Claude Opus 4.6)
Date: 2026-02-12
"""

import copy
from typing import Any, Dict, List

try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


__all__ = [
    "is_signature_400_error",
    "is_in_tool_loop",
    "strip_all_thinking_blocks",
    "close_tool_loop_for_thinking",
    "prepare_signature_retry_body",
    "REPAIR_PROMPT",
    "SIGNATURE_RETRY_DELAY_MS",
]


# ==================== Signature Error Patterns ====================
#
# From upstream handlers/claude.rs lines 348-454:
# 13 signature error patterns that indicate corrupted/invalid thinking signatures
#
# These errors occur when:
# 1. Model routing fluctuates (Claude → Gemini → Claude)
# 2. Gemini returns thinking blocks without valid signatures
# 3. Thinking block order gets corrupted
# 4. Client sends stale/invalid signatures from previous sessions

SIGNATURE_ERROR_PATTERNS: List[str] = [
    # Core signature validation errors (Claude API)
    "Invalid `signature`",
    "thinking.signature",
    "thinking.thinking",
    "Corrupted thought signature",
    # Thinking block order/structure errors
    "thinking block must be the first content block",
    "thinking block order",
    "thinking content block",
    "redacted_thinking",
    # Extended thinking validation errors
    "extended thinking",
    "thinking is not enabled",
    "thinking configuration",
    # Signature format errors
    "invalid signature format",
    "signature verification failed",
]

# Thinking block type identifiers (for stripping)
THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}

# Fixed retry delay for signature errors (200ms, matching upstream)
SIGNATURE_RETRY_DELAY_MS = 200

# Repair prompt injected on signature 400 retry
# From upstream handlers/openai.rs lines 601-637
REPAIR_PROMPT = (
    "\n\n[System Recovery] Your previous output contained an invalid signature "
    "that caused a processing error. Please continue normally without referencing "
    "the error. Generate a fresh response to the user's request."
)


def is_signature_400_error(status_code: int, error_text: str) -> bool:
    """
    Detect if an HTTP 400 error is a signature/thinking-related error.

    Ported from upstream determine_retry_strategy() in handlers/common.rs.

    Args:
        status_code: HTTP status code
        error_text: Error response body text

    Returns:
        True if this is a retryable signature 400 error
    """
    if status_code != 400:
        return False

    if not error_text:
        return False

    error_lower = error_text.lower()

    for pattern in SIGNATURE_ERROR_PATTERNS:
        if pattern.lower() in error_lower:
            log.info(
                f"[THINKING_RECOVERY] Detected signature 400 error: "
                f"matched pattern '{pattern}'"
            )
            return True

    return False


def is_in_tool_loop(body: Dict[str, Any]) -> bool:
    """
    Detect if the request body indicates an active tool-use loop.

    Ported from upstream thinking_recovery.rs close_tool_loop_for_thinking().

    A tool loop is detected when recent messages contain:
    - Assistant messages with tool_use/tool_calls/function_call blocks
    - User/tool messages with tool_result blocks
    - Messages with role="tool" (OpenAI format)

    This is used to decide whether to inject synthetic tool loop closure
    messages when recovering from signature 400 errors.

    Args:
        body: Request body dict containing "messages" list

    Returns:
        True if messages indicate an active tool-use loop
    """
    messages = body.get("messages", [])
    if not messages:
        return False

    # Tool-use indicator types (Anthropic format)
    TOOL_USE_BLOCK_TYPES = {"tool_use", "tool_result"}

    # Check last 4 messages for tool-use indicators
    recent = messages[-4:] if len(messages) >= 4 else messages

    for msg in recent:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")

        # OpenAI format: role="tool" indicates tool result message
        if role == "tool":
            return True

        # OpenAI format: assistant message with tool_calls
        if role == "assistant" and msg.get("tool_calls"):
            return True

        # OpenAI format: assistant message with function_call (legacy)
        if role == "assistant" and msg.get("function_call"):
            return True

        # Anthropic format: content blocks with tool_use/tool_result type
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type in TOOL_USE_BLOCK_TYPES:
                        return True

    return False


def strip_all_thinking_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove all thinking/redacted_thinking blocks from message history.

    Ported from upstream thinking_recovery.rs strip_all_thinking_blocks().

    Supports both formats:
    - Anthropic Messages API: content blocks with type "thinking"/"redacted_thinking"
    - OpenAI Chat API: no native thinking blocks, but some proxies inject them

    Args:
        messages: List of message dicts (will NOT be mutated)

    Returns:
        New message list with thinking blocks removed
    """
    result = []
    stripped_count = 0

    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue

        role = msg.get("role", "")
        content = msg.get("content")

        # Only process assistant messages (thinking blocks come from assistant)
        if role != "assistant":
            result.append(msg)
            continue

        # Handle content as list of blocks (Anthropic format)
        if isinstance(content, list):
            filtered_content = []
            for block in content:
                if not isinstance(block, dict):
                    filtered_content.append(block)
                    continue

                block_type = block.get("type", "")
                if block_type in THINKING_BLOCK_TYPES:
                    stripped_count += 1
                    continue

                # Also check for "thought" field (Gemini thinking format)
                if block.get("thought") is True:
                    stripped_count += 1
                    continue

                filtered_content.append(block)

            # Skip message entirely if all content blocks were thinking
            if not filtered_content:
                stripped_count += 1
                continue

            new_msg = {**msg, "content": filtered_content}
            result.append(new_msg)

        # Handle content as string (OpenAI format) - no thinking blocks to strip
        elif isinstance(content, str):
            result.append(msg)

        # Handle content as None (tool_calls only message)
        elif content is None:
            result.append(msg)

        else:
            result.append(msg)

    if stripped_count > 0:
        log.info(
            f"[THINKING_RECOVERY] Stripped {stripped_count} thinking blocks "
            f"from {len(messages)} messages"
        )

    return result


def close_tool_loop_for_thinking(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Strip thinking blocks and inject synthetic tool loop closure messages.

    Ported from upstream thinking_recovery.rs close_tool_loop_for_thinking().

    When signature errors occur during tool execution, we need to:
    1. Strip all thinking blocks (they have invalid signatures)
    2. Add synthetic messages to properly close the tool loop
    3. Allow the model to continue without signature validation

    Args:
        messages: List of message dicts (will NOT be mutated)

    Returns:
        New message list with thinking stripped and tool loop closed
    """
    stripped = strip_all_thinking_blocks(messages)

    # Add synthetic closure messages
    stripped.append({
        "role": "assistant",
        "content": "[Tool execution completed.]"
    })
    stripped.append({
        "role": "user",
        "content": "[Continue]"
    })

    log.info("[THINKING_RECOVERY] Injected tool loop closure messages")
    return stripped


def _inject_repair_prompt(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Inject repair prompt into the last user message.

    Ported from upstream handlers/openai.rs.

    Args:
        messages: List of message dicts (will NOT be mutated)

    Returns:
        New message list with repair prompt appended to last user message
    """
    if not messages:
        return messages

    result = list(messages)

    # Find last user message and append repair prompt
    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        if isinstance(content, str):
            result[i] = {**msg, "content": content + REPAIR_PROMPT}
            log.info("[THINKING_RECOVERY] Injected repair prompt into last user message")
            return result
        elif isinstance(content, list):
            # For list content (multimodal), append as new text block
            new_content = list(content) + [{"type": "text", "text": REPAIR_PROMPT}]
            result[i] = {**msg, "content": new_content}
            log.info("[THINKING_RECOVERY] Injected repair prompt into last user message (list content)")
            return result

    # No user message found, append as new user message
    result.append({"role": "user", "content": REPAIR_PROMPT.strip()})
    log.info("[THINKING_RECOVERY] Appended repair prompt as new user message")
    return result


def prepare_signature_retry_body(
    body: Dict[str, Any],
    *,
    disable_thinking: bool = True,
    inject_repair_prompt: bool = True,
    close_tool_loop: bool = False,
) -> Dict[str, Any]:
    """
    Prepare request body for retry after signature 400 error.

    This function performs the full recovery sequence:
    1. Strip all thinking blocks from message history
    2. Optionally close tool loop (add synthetic messages)
    3. Optionally disable thinking in the request
    4. Optionally inject repair prompt
    5. Clean model name (remove -thinking suffix for retry)

    Args:
        body: Original request body (will NOT be mutated)
        disable_thinking: Whether to disable thinking for retry
        inject_repair_prompt: Whether to inject repair prompt
        close_tool_loop: Whether to inject tool loop closure messages

    Returns:
        New request body ready for retry
    """
    new_body = copy.deepcopy(body)

    messages = new_body.get("messages", [])

    # Step 1: Strip thinking blocks
    if close_tool_loop:
        messages = close_tool_loop_for_thinking(messages)
    else:
        messages = strip_all_thinking_blocks(messages)

    # Step 2: Inject repair prompt
    if inject_repair_prompt:
        messages = _inject_repair_prompt(messages)

    new_body["messages"] = messages

    # Step 3: Disable thinking if requested
    if disable_thinking:
        # Remove thinking-related parameters
        new_body.pop("thinking", None)
        new_body.pop("thinking_budget", None)

        # For Anthropic format: set thinking to disabled
        # This will be handled by the converter layer

    # Step 4: Clean model name (remove -thinking suffix)
    model = new_body.get("model", "")
    if isinstance(model, str) and model.endswith("-thinking"):
        clean_model = model.rsplit("-thinking", 1)[0]
        new_body["model"] = clean_model
        log.info(
            f"[THINKING_RECOVERY] Cleaned model name: "
            f"{model} → {clean_model}"
        )

    log.info(
        f"[THINKING_RECOVERY] Prepared retry body: "
        f"disable_thinking={disable_thinking}, "
        f"inject_repair_prompt={inject_repair_prompt}, "
        f"close_tool_loop={close_tool_loop}, "
        f"messages_count={len(new_body.get('messages', []))}"
    )

    return new_body
