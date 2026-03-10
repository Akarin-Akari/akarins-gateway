"""
Tool Semantic Converter — Stateless bidirectional tool name mapping.

Converts Claude Code tool names (Bash, Read, Write, etc.) to generic names
(execute_command, read_file, write_file, etc.) on the request side, and
reverse-maps them back on the response side.

Purpose: Hide Claude Code tool fingerprints from upstream providers (e.g. Google)
that might detect and fingerprint requests based on characteristic tool names.

Design:
  - Stateless: all methods are static, no instance state, thread-safe
  - O(1) lookup via pre-built dicts
  - MCP tools (mcp__* prefix) pass through unchanged
  - Unknown tools pass through unchanged
  - Case-insensitive forward matching

Author: 浮浮酱 (Claude Opus 4.6)
Created: 2026-03-04
"""

import re
import json
import logging
from typing import Dict, Optional

_logger = logging.getLogger(__name__)

# ====================== Mapping Table ======================
# Key: original Claude Code tool name (mixed case)
# Value: generic replacement name
_TOOL_MAPPING: Dict[str, str] = {
    "Bash": "execute_command",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "multi_edit_file",
    "Grep": "search_content",
    "Glob": "find_files",
    "WebFetch": "fetch_url",
    "WebSearch": "web_search",
    "Task": "run_subtask",
    "TodoRead": "list_tasks",
    "TodoWrite": "update_tasks",
    "NotebookEdit": "edit_notebook",
    "AskUserQuestion": "prompt_user",
    "LSP": "language_server",
    "EnterWorktree": "switch_worktree",
    "ToolSearch": "discover_tools",
    "TaskCreate": "create_task",
    "TaskUpdate": "update_task",
    "TaskGet": "get_task",
    "TaskList": "list_all_tasks",
    "EnterPlanMode": "start_planning",
    "ExitPlanMode": "finish_planning",
    "Skill": "invoke_skill",
    "TaskOutput": "get_task_output",
    "TaskStop": "stop_task",
}

# Forward map: lowercase original → generic name
_FORWARD_MAP: Dict[str, str] = {k.lower(): v for k, v in _TOOL_MAPPING.items()}

# Reverse map: generic name (lowercase) → original Claude Code name
# Uses the FIRST original name for each generic name (deterministic since dict is ordered)
_REVERSE_MAP: Dict[str, str] = {}
for _orig, _generic in _TOOL_MAPPING.items():
    _key = _generic.lower()
    if _key not in _REVERSE_MAP:
        _REVERSE_MAP[_key] = _orig

# ====================== Description Rewrite Patterns ======================
# (compiled_regex, replacement_string)
_DESCRIPTION_PATTERNS = [
    # Remove Claude Code identity references
    (re.compile(r"Claude\s*Code", re.IGNORECASE), "the coding assistant"),
    (re.compile(r"Anthropic'?s?\s+(official\s+)?CLI\s+(for\s+Claude)?", re.IGNORECASE), "the CLI tool"),
    # Replace tool cross-references with generic names
    (re.compile(r"\bthe\s+Bash\s+tool\b", re.IGNORECASE), "the command execution tool"),
    (re.compile(r"\bthe\s+Read\s+tool\b", re.IGNORECASE), "the file reading tool"),
    (re.compile(r"\bthe\s+Write\s+tool\b", re.IGNORECASE), "the file writing tool"),
    (re.compile(r"\bthe\s+Edit\s+tool\b", re.IGNORECASE), "the file editing tool"),
    (re.compile(r"\bthe\s+Grep\s+tool\b", re.IGNORECASE), "the content search tool"),
    (re.compile(r"\bthe\s+Glob\s+tool\b", re.IGNORECASE), "the file search tool"),
    (re.compile(r"\bthe\s+Task\s+tool\b", re.IGNORECASE), "the subtask tool"),
    (re.compile(r"\bthe\s+MultiEdit\s+tool\b", re.IGNORECASE), "the batch editing tool"),
    (re.compile(r"\bthe\s+WebFetch\s+tool\b", re.IGNORECASE), "the URL fetch tool"),
    (re.compile(r"\bthe\s+WebSearch\s+tool\b", re.IGNORECASE), "the web search tool"),
    (re.compile(r"\bthe\s+LSP\s+tool\b", re.IGNORECASE), "the language server tool"),
    (re.compile(r"\bthe\s+NotebookEdit\s+tool\b", re.IGNORECASE), "the notebook editing tool"),
    # Replace bare tool names in backticks
    (re.compile(r"`Bash`"), "`execute_command`"),
    (re.compile(r"`Read`"), "`read_file`"),
    (re.compile(r"`Write`"), "`write_file`"),
    (re.compile(r"`Edit`"), "`edit_file`"),
    (re.compile(r"`MultiEdit`"), "`multi_edit_file`"),
    (re.compile(r"`Grep`"), "`search_content`"),
    (re.compile(r"`Glob`"), "`find_files`"),
    (re.compile(r"`WebFetch`"), "`fetch_url`"),
    (re.compile(r"`WebSearch`"), "`web_search`"),
    (re.compile(r"`Task`"), "`run_subtask`"),
    (re.compile(r"`LSP`"), "`language_server`"),
    (re.compile(r"`NotebookEdit`"), "`edit_notebook`"),
    (re.compile(r"`AskUserQuestion`"), "`prompt_user`"),
    (re.compile(r"`ToolSearch`"), "`discover_tools`"),
    (re.compile(r"`Skill`"), "`invoke_skill`"),
]

# ====================== System Prompt Cleaning Patterns ======================
_SYSTEM_PROMPT_PATTERNS = [
    # Remove explicit Claude Code identity statements
    (re.compile(
        r"You\s+are\s+Claude\s+Code,?\s*Anthropic'?s?\s+official\s+CLI\s+for\s+Claude\.?",
        re.IGNORECASE,
    ), "You are an AI coding assistant."),
    # Remove "Claude Code" references in general text
    (re.compile(r"\bClaude\s+Code\b", re.IGNORECASE), "the coding assistant"),
    # Remove Anthropic references
    (re.compile(r"\bAnthropic'?s?\s+(official\s+)?(CLI|tool|assistant)\b", re.IGNORECASE), "the assistant"),
]


class ToolSemanticConverter:
    """Stateless bidirectional tool semantic converter."""

    @staticmethod
    def convert_tool_name(name: str) -> str:
        """
        Forward-convert a Claude Code tool name to a generic name.

        - Case-insensitive matching
        - MCP tools (mcp__* prefix) pass through unchanged
        - Unknown tools pass through unchanged

        Args:
            name: Original tool name (e.g. "Bash", "Read")

        Returns:
            Converted name (e.g. "execute_command", "read_file") or original if unknown
        """
        if not name:
            return name
        # MCP tools pass through
        if name.startswith("mcp__"):
            return name
        return _FORWARD_MAP.get(name.lower(), name)

    @staticmethod
    def reverse_tool_name(name: str) -> str:
        """
        Reverse-convert a generic tool name back to the Claude Code original.

        - Case-insensitive matching
        - MCP tools pass through unchanged
        - Unknown tools pass through unchanged

        Args:
            name: Generic tool name (e.g. "execute_command", "read_file")

        Returns:
            Original Claude Code name (e.g. "Bash", "Read") or input if unknown
        """
        if not name:
            return name
        if name.startswith("mcp__"):
            return name
        return _REVERSE_MAP.get(name.lower(), name)

    @staticmethod
    def rewrite_description(desc: str) -> str:
        """
        Rewrite a tool description to remove Claude Code identity references
        and replace tool cross-references with generic names.

        Args:
            desc: Original tool description text

        Returns:
            Cleaned description text
        """
        if not desc:
            return desc
        result = desc
        for pattern, replacement in _DESCRIPTION_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    @staticmethod
    def clean_system_prompt(content: str) -> str:
        """
        Clean system prompt content to remove Claude Code identity markers.

        Args:
            content: Original system prompt text

        Returns:
            Cleaned system prompt text
        """
        if not content:
            return content
        result = content
        for pattern, replacement in _SYSTEM_PROMPT_PATTERNS:
            result = pattern.sub(replacement, result)
        # Also apply description patterns (tool name references in system prompts)
        for pattern, replacement in _DESCRIPTION_PATTERNS:
            result = pattern.sub(replacement, result)
        return result


# ====================== Request-Side Batch Conversion ======================

def apply_tool_semantic_conversion(body: dict, enabled: bool = True) -> None:
    """
    Apply tool semantic conversion to a request body (in-place mutation).

    Converts:
      - tools[].function.name — tool definition names
      - tools[].function.description — tool descriptions
      - messages[].tool_calls[].function.name — historical tool call names
      - messages[role="tool"].name — historical tool result names
      - messages[role="system"].content — system prompt content

    Args:
        body: Request body dict (mutated in-place)
        enabled: Whether conversion is enabled (False = no-op)
    """
    if not enabled or not isinstance(body, dict):
        return

    converter = ToolSemanticConverter
    converted_count = 0

    # 1. Convert tool definitions: tools[].function.name + description
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            func = tool.get("function")
            if not isinstance(func, dict):
                continue
            orig_name = func.get("name", "")
            new_name = converter.convert_tool_name(orig_name)
            if new_name != orig_name:
                func["name"] = new_name
                converted_count += 1
            # Rewrite description
            orig_desc = func.get("description", "")
            if orig_desc:
                new_desc = converter.rewrite_description(orig_desc)
                if new_desc != orig_desc:
                    func["description"] = new_desc

    # 2. Convert messages: tool_calls, tool results, system prompts
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")

            # 2a. assistant messages: tool_calls[].function.name
            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        func = tc.get("function")
                        if not isinstance(func, dict):
                            continue
                        orig = func.get("name", "")
                        new = converter.convert_tool_name(orig)
                        if new != orig:
                            func["name"] = new
                            converted_count += 1

            # 2b. tool messages: name field
            elif role == "tool":
                orig = msg.get("name", "")
                if orig:
                    new = converter.convert_tool_name(orig)
                    if new != orig:
                        msg["name"] = new
                        converted_count += 1

            # 2c. system messages: clean content
            elif role == "system":
                content = msg.get("content")
                if isinstance(content, str) and content:
                    new_content = converter.clean_system_prompt(content)
                    if new_content != content:
                        msg["content"] = new_content

    if converted_count > 0:
        _logger.info(
            f"[STEALTH] Tool semantic conversion applied: {converted_count} tool name(s) converted"
        )


# ====================== Response-Side Reverse Mapping ======================

def reverse_tool_semantic_in_response_body(response: dict, enabled: bool = True) -> None:
    """
    Reverse-map tool names in a non-streaming response body (in-place mutation).

    Handles OpenAI format: choices[].message.tool_calls[].function.name

    Args:
        response: Response body dict (mutated in-place)
        enabled: Whether conversion is enabled (False = no-op)
    """
    if not enabled or not isinstance(response, dict):
        return

    converter = ToolSemanticConverter
    choices = response.get("choices")
    if not isinstance(choices, list):
        return

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function")
            if not isinstance(func, dict):
                continue
            orig = func.get("name", "")
            new = converter.reverse_tool_name(orig)
            if new != orig:
                func["name"] = new


def reverse_tool_name_in_sse_chunk(chunk_text: str, enabled: bool = True) -> str:
    """
    Reverse-map tool names in an SSE chunk string.

    Only parses JSON when "tool_calls" keyword is detected in the chunk,
    otherwise returns the chunk unchanged for zero overhead on normal text chunks.

    Handles: data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"..."}}]}}]}

    Args:
        chunk_text: Raw SSE chunk text (may contain multiple lines)
        enabled: Whether conversion is enabled (False = no-op)

    Returns:
        Chunk text with tool names reverse-mapped
    """
    if not enabled or not chunk_text:
        return chunk_text

    # Fast path: skip chunks that don't contain tool_calls
    if "tool_calls" not in chunk_text:
        return chunk_text

    converter = ToolSemanticConverter
    lines = chunk_text.split("\n")
    modified = False
    result_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("data: "):
            result_lines.append(line)
            continue

        json_str = stripped[6:].strip()
        if json_str == "[DONE]":
            result_lines.append(line)
            continue

        try:
            obj = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            result_lines.append(line)
            continue

        # Navigate: choices[0].delta.tool_calls[].function.name
        choices = obj.get("choices")
        if not isinstance(choices, list):
            result_lines.append(line)
            continue

        line_modified = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            tool_calls = delta.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function")
                if not isinstance(func, dict):
                    continue
                orig = func.get("name")
                if orig is not None:
                    new = converter.reverse_tool_name(orig)
                    if new != orig:
                        func["name"] = new
                        line_modified = True

        if line_modified:
            new_json = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            result_lines.append(f"data: {new_json}")
            modified = True
        else:
            result_lines.append(line)

    return "\n".join(result_lines) if modified else chunk_text
