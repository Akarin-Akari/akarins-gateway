"""
Tests for tool_semantic_converter module.

Covers:
  - Forward mapping (Claude Code → generic)
  - Reverse mapping (generic → Claude Code)
  - Round-trip consistency
  - Case insensitivity
  - MCP tool passthrough
  - Unknown tool passthrough
  - Description rewriting
  - System prompt cleaning
  - Request-side batch conversion (apply_tool_semantic_conversion)
  - Response-side non-streaming reverse (reverse_tool_semantic_in_response_body)
  - Response-side SSE streaming reverse (reverse_tool_name_in_sse_chunk)
  - Disabled mode (no-op)
  - Edge cases (empty, None, malformed)

Author: 浮浮酱 (Claude Opus 4.6)
Created: 2026-03-04
"""

import copy
import json
import pytest

from akarins_gateway.converters.tool_semantic_converter import (
    ToolSemanticConverter,
    apply_tool_semantic_conversion,
    reverse_tool_semantic_in_response_body,
    reverse_tool_name_in_sse_chunk,
    _TOOL_MAPPING,
    _FORWARD_MAP,
    _REVERSE_MAP,
)


# ==================== Mapping Table Integrity ====================

class TestMappingTableIntegrity:
    """Verify the mapping tables are consistent and complete."""

    def test_forward_map_has_all_entries(self):
        """Forward map should have one entry per original tool."""
        assert len(_FORWARD_MAP) == len(_TOOL_MAPPING)

    def test_reverse_map_has_all_generic_names(self):
        """Reverse map should cover all unique generic names."""
        unique_generics = set(_TOOL_MAPPING.values())
        assert set(_REVERSE_MAP.keys()) == {g.lower() for g in unique_generics}

    def test_forward_map_keys_are_lowercase(self):
        """Forward map keys should all be lowercase."""
        for key in _FORWARD_MAP:
            assert key == key.lower(), f"Key '{key}' is not lowercase"

    def test_reverse_map_keys_are_lowercase(self):
        """Reverse map keys should all be lowercase."""
        for key in _REVERSE_MAP:
            assert key == key.lower(), f"Key '{key}' is not lowercase"

    def test_no_generic_name_collisions(self):
        """All generic names should be unique (no two originals map to same generic)."""
        generics = list(_TOOL_MAPPING.values())
        assert len(generics) == len(set(generics)), "Duplicate generic names found"


# ==================== Forward Mapping ====================

class TestForwardMapping:
    """Test ToolSemanticConverter.convert_tool_name()."""

    @pytest.mark.parametrize("original,expected", list(_TOOL_MAPPING.items()))
    def test_all_known_tools(self, original, expected):
        """Every known tool should map to its generic name."""
        assert ToolSemanticConverter.convert_tool_name(original) == expected

    def test_case_insensitive_lowercase(self):
        assert ToolSemanticConverter.convert_tool_name("bash") == "execute_command"

    def test_case_insensitive_uppercase(self):
        assert ToolSemanticConverter.convert_tool_name("BASH") == "execute_command"

    def test_case_insensitive_mixed(self):
        assert ToolSemanticConverter.convert_tool_name("bAsH") == "execute_command"

    def test_mcp_tool_passthrough(self):
        assert ToolSemanticConverter.convert_tool_name("mcp__foo__bar") == "mcp__foo__bar"

    def test_mcp_tool_passthrough_complex(self):
        name = "mcp__plugin_oh-my-claudecode_t__lsp_hover"
        assert ToolSemanticConverter.convert_tool_name(name) == name

    def test_unknown_tool_passthrough(self):
        assert ToolSemanticConverter.convert_tool_name("MyCustomTool") == "MyCustomTool"

    def test_empty_string(self):
        assert ToolSemanticConverter.convert_tool_name("") == ""

    def test_none_input(self):
        assert ToolSemanticConverter.convert_tool_name(None) is None


# ==================== Reverse Mapping ====================

class TestReverseMapping:
    """Test ToolSemanticConverter.reverse_tool_name()."""

    @pytest.mark.parametrize("original,generic", list(_TOOL_MAPPING.items()))
    def test_all_known_tools(self, original, generic):
        """Every generic name should reverse to the original."""
        assert ToolSemanticConverter.reverse_tool_name(generic) == original

    def test_case_insensitive(self):
        assert ToolSemanticConverter.reverse_tool_name("EXECUTE_COMMAND") == "Bash"
        assert ToolSemanticConverter.reverse_tool_name("Read_File") == "Read"

    def test_mcp_tool_passthrough(self):
        assert ToolSemanticConverter.reverse_tool_name("mcp__foo__bar") == "mcp__foo__bar"

    def test_unknown_tool_passthrough(self):
        assert ToolSemanticConverter.reverse_tool_name("unknown_tool") == "unknown_tool"

    def test_empty_string(self):
        assert ToolSemanticConverter.reverse_tool_name("") == ""

    def test_none_input(self):
        assert ToolSemanticConverter.reverse_tool_name(None) is None


# ==================== Round-Trip Consistency ====================

class TestRoundTrip:
    """Verify forward→reverse and reverse→forward produce consistent results."""

    @pytest.mark.parametrize("original,generic", list(_TOOL_MAPPING.items()))
    def test_forward_then_reverse(self, original, generic):
        """forward(original) → reverse(result) == original"""
        converted = ToolSemanticConverter.convert_tool_name(original)
        assert converted == generic
        back = ToolSemanticConverter.reverse_tool_name(converted)
        assert back == original

    @pytest.mark.parametrize("original,generic", list(_TOOL_MAPPING.items()))
    def test_reverse_then_forward(self, original, generic):
        """reverse(generic) → forward(result) == generic"""
        reversed_name = ToolSemanticConverter.reverse_tool_name(generic)
        assert reversed_name == original
        back = ToolSemanticConverter.convert_tool_name(reversed_name)
        assert back == generic

    def test_unknown_round_trip(self):
        """Unknown tools should survive round-trip unchanged."""
        name = "SomeRandomTool"
        assert ToolSemanticConverter.reverse_tool_name(
            ToolSemanticConverter.convert_tool_name(name)
        ) == name

    def test_mcp_round_trip(self):
        """MCP tools should survive round-trip unchanged."""
        name = "mcp__server__tool"
        assert ToolSemanticConverter.reverse_tool_name(
            ToolSemanticConverter.convert_tool_name(name)
        ) == name


# ==================== Description Rewriting ====================

class TestDescriptionRewriting:
    """Test ToolSemanticConverter.rewrite_description()."""

    def test_claude_code_removal(self):
        desc = "Claude Code provides this tool for file operations."
        result = ToolSemanticConverter.rewrite_description(desc)
        assert "Claude Code" not in result
        assert "the coding assistant" in result

    def test_anthropic_cli_removal(self):
        desc = "Anthropic's official CLI for Claude does this."
        result = ToolSemanticConverter.rewrite_description(desc)
        assert "Anthropic" not in result

    def test_tool_cross_reference_replacement(self):
        desc = "Use the Bash tool to run commands, then use the Read tool to read output."
        result = ToolSemanticConverter.rewrite_description(desc)
        assert "the Bash tool" not in result
        assert "the Read tool" not in result
        assert "command execution tool" in result
        assert "file reading tool" in result

    def test_backtick_tool_name_replacement(self):
        desc = "Call `Bash` with a command, or use `Grep` for search."
        result = ToolSemanticConverter.rewrite_description(desc)
        assert "`execute_command`" in result
        assert "`search_content`" in result

    def test_empty_description(self):
        assert ToolSemanticConverter.rewrite_description("") == ""

    def test_none_description(self):
        assert ToolSemanticConverter.rewrite_description(None) is None

    def test_no_matches(self):
        desc = "This is a generic description with no tool references."
        assert ToolSemanticConverter.rewrite_description(desc) == desc


# ==================== System Prompt Cleaning ====================

class TestSystemPromptCleaning:
    """Test ToolSemanticConverter.clean_system_prompt()."""

    def test_identity_statement_removal(self):
        prompt = "You are Claude Code, Anthropic's official CLI for Claude. You help with coding."
        result = ToolSemanticConverter.clean_system_prompt(prompt)
        assert "Claude Code" not in result
        assert "Anthropic" not in result
        assert "AI coding assistant" in result

    def test_preserves_functional_content(self):
        prompt = "Always validate user input. Handle errors gracefully."
        result = ToolSemanticConverter.clean_system_prompt(prompt)
        assert result == prompt  # no changes needed

    def test_empty_prompt(self):
        assert ToolSemanticConverter.clean_system_prompt("") == ""

    def test_none_prompt(self):
        assert ToolSemanticConverter.clean_system_prompt(None) is None

    def test_mixed_content(self):
        prompt = (
            "You are Claude Code. Use the Bash tool for commands. "
            "Always be helpful and accurate."
        )
        result = ToolSemanticConverter.clean_system_prompt(prompt)
        assert "Claude Code" not in result
        assert "the Bash tool" not in result
        assert "Always be helpful and accurate." in result


# ==================== Request-Side Batch Conversion ====================

class TestApplyToolSemanticConversion:
    """Test apply_tool_semantic_conversion()."""

    def _make_body(self):
        """Create a realistic request body with Claude Code tools."""
        return {
            "model": "gemini-2.5-pro",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "Use the Bash tool to execute commands.",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "description": "Use the Read tool to read files.",
                        "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "mcp__server__custom",
                        "description": "A custom MCP tool.",
                        "parameters": {}
                    }
                },
            ],
            "messages": [
                {"role": "system", "content": "You are Claude Code, Anthropic's official CLI for Claude."},
                {"role": "user", "content": "List files in current directory"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "tc_1", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}
                    ]
                },
                {"role": "tool", "tool_call_id": "tc_1", "name": "Bash", "content": "file1.txt\nfile2.txt"},
                {"role": "assistant", "content": "Here are the files."},
            ]
        }

    def test_tool_definitions_converted(self):
        body = self._make_body()
        apply_tool_semantic_conversion(body, enabled=True)
        assert body["tools"][0]["function"]["name"] == "execute_command"
        assert body["tools"][1]["function"]["name"] == "read_file"

    def test_mcp_tool_not_converted(self):
        body = self._make_body()
        apply_tool_semantic_conversion(body, enabled=True)
        assert body["tools"][2]["function"]["name"] == "mcp__server__custom"

    def test_tool_descriptions_rewritten(self):
        body = self._make_body()
        apply_tool_semantic_conversion(body, enabled=True)
        assert "the Bash tool" not in body["tools"][0]["function"]["description"]

    def test_assistant_tool_calls_converted(self):
        body = self._make_body()
        apply_tool_semantic_conversion(body, enabled=True)
        tc = body["messages"][2]["tool_calls"][0]
        assert tc["function"]["name"] == "execute_command"

    def test_tool_result_name_converted(self):
        body = self._make_body()
        apply_tool_semantic_conversion(body, enabled=True)
        assert body["messages"][3]["name"] == "execute_command"

    def test_system_prompt_cleaned(self):
        body = self._make_body()
        apply_tool_semantic_conversion(body, enabled=True)
        system_msg = body["messages"][0]["content"]
        assert "Claude Code" not in system_msg
        assert "Anthropic" not in system_msg

    def test_disabled_no_changes(self):
        body = self._make_body()
        original = copy.deepcopy(body)
        apply_tool_semantic_conversion(body, enabled=False)
        assert body == original

    def test_empty_body(self):
        body = {}
        apply_tool_semantic_conversion(body, enabled=True)
        assert body == {}

    def test_none_body(self):
        # Should not raise
        apply_tool_semantic_conversion(None, enabled=True)

    def test_no_tools_key(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        apply_tool_semantic_conversion(body, enabled=True)
        assert body["messages"][0]["content"] == "hello"

    def test_malformed_tool_entry(self):
        body = {"tools": [None, "not a dict", {"no_function": True}]}
        apply_tool_semantic_conversion(body, enabled=True)  # should not raise

    def test_malformed_message_entry(self):
        body = {"messages": [None, "not a dict", 42]}
        apply_tool_semantic_conversion(body, enabled=True)  # should not raise

    def test_multiple_tool_calls_in_one_message(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "Bash"}},
                        {"function": {"name": "Read"}},
                        {"function": {"name": "Grep"}},
                    ]
                }
            ]
        }
        apply_tool_semantic_conversion(body, enabled=True)
        names = [tc["function"]["name"] for tc in body["messages"][0]["tool_calls"]]
        assert names == ["execute_command", "read_file", "search_content"]


# ==================== Response-Side Non-Streaming Reverse ====================

class TestReverseToolSemanticInResponseBody:
    """Test reverse_tool_semantic_in_response_body()."""

    def test_single_tool_call(self):
        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "tc_1",
                        "function": {"name": "execute_command", "arguments": '{"command":"ls"}'}
                    }]
                }
            }]
        }
        reverse_tool_semantic_in_response_body(response, enabled=True)
        assert response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Bash"

    def test_multiple_tool_calls(self):
        response = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {"function": {"name": "execute_command"}},
                        {"function": {"name": "read_file"}},
                        {"function": {"name": "search_content"}},
                    ]
                }
            }]
        }
        reverse_tool_semantic_in_response_body(response, enabled=True)
        names = [tc["function"]["name"] for tc in response["choices"][0]["message"]["tool_calls"]]
        assert names == ["Bash", "Read", "Grep"]

    def test_no_tool_calls(self):
        response = {"choices": [{"message": {"role": "assistant", "content": "Hello"}}]}
        original = copy.deepcopy(response)
        reverse_tool_semantic_in_response_body(response, enabled=True)
        assert response == original

    def test_disabled(self):
        response = {
            "choices": [{"message": {"tool_calls": [{"function": {"name": "execute_command"}}]}}]
        }
        reverse_tool_semantic_in_response_body(response, enabled=False)
        assert response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "execute_command"

    def test_empty_response(self):
        reverse_tool_semantic_in_response_body({}, enabled=True)

    def test_none_response(self):
        reverse_tool_semantic_in_response_body(None, enabled=True)


# ==================== Response-Side SSE Streaming Reverse ====================

class TestReverseToolNameInSseChunk:
    """Test reverse_tool_name_in_sse_chunk()."""

    def test_tool_call_name_reversed(self):
        obj = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"name": "execute_command"}
                    }]
                }
            }]
        }
        chunk = f"data: {json.dumps(obj)}\n\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        parsed = json.loads(result.strip().removeprefix("data: "))
        assert parsed["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "Bash"

    def test_fast_path_no_tool_calls(self):
        chunk = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        assert result == chunk  # unchanged, fast path

    def test_done_marker_passthrough(self):
        chunk = "data: [DONE]\n\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        assert result == chunk

    def test_disabled(self):
        obj = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "execute_command"}}]}}]}
        chunk = f"data: {json.dumps(obj)}\n\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=False)
        assert result == chunk

    def test_empty_chunk(self):
        assert reverse_tool_name_in_sse_chunk("", enabled=True) == ""

    def test_none_chunk(self):
        assert reverse_tool_name_in_sse_chunk(None, enabled=True) is None

    def test_multiple_lines(self):
        line1 = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
        line2 = 'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"read_file"}}]}}]}'
        chunk = f"{line1}\n{line2}\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        assert '"Read"' in result
        # First line should be unchanged (no tool_calls... wait, fast path is per-chunk not per-line)
        # The chunk contains "tool_calls" so it will be processed line by line
        assert '"content":"hi"' in result  # first line preserved

    def test_mcp_tool_not_reversed(self):
        obj = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "mcp__foo__bar"}}]}}]}
        chunk = f"data: {json.dumps(obj)}\n\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        assert "mcp__foo__bar" in result

    def test_unknown_tool_not_reversed(self):
        obj = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "some_random_tool"}}]}}]}
        chunk = f"data: {json.dumps(obj)}\n\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        assert "some_random_tool" in result

    def test_invalid_json_passthrough(self):
        chunk = "data: not valid json\n\n"
        # Contains no "tool_calls" so fast path returns unchanged
        assert reverse_tool_name_in_sse_chunk(chunk, enabled=True) == chunk

    def test_sse_comment_passthrough(self):
        chunk = ": this is a comment\ndata: {\"tool_calls\": true}\n"
        # Comment line should pass through, data line gets processed
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        assert ": this is a comment" in result

    def test_arguments_only_no_name(self):
        """Tool call delta with only arguments (no name) should pass through."""
        obj = {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"cmd":"ls"}'}}]}}]}
        chunk = f"data: {json.dumps(obj)}\n\n"
        result = reverse_tool_name_in_sse_chunk(chunk, enabled=True)
        # No name field to reverse, should not crash
        assert "arguments" in result


# ==================== Config Integration ====================

class TestConfigIntegration:
    """Test config function exists and returns correct type."""

    def test_config_function_returns_bool(self):
        from akarins_gateway.core.config import get_tool_semantic_conversion_enabled
        result = get_tool_semantic_conversion_enabled()
        assert isinstance(result, bool)

    def test_config_default_is_true(self):
        import os
        # Clear env var to test default
        old = os.environ.pop("TOOL_SEMANTIC_CONVERSION_ENABLED", None)
        try:
            from akarins_gateway.core.config import get_tool_semantic_conversion_enabled
            assert get_tool_semantic_conversion_enabled() is True
        finally:
            if old is not None:
                os.environ["TOOL_SEMANTIC_CONVERSION_ENABLED"] = old

    def test_config_can_be_disabled(self):
        import os
        old = os.environ.get("TOOL_SEMANTIC_CONVERSION_ENABLED")
        os.environ["TOOL_SEMANTIC_CONVERSION_ENABLED"] = "false"
        try:
            from akarins_gateway.core.config import get_tool_semantic_conversion_enabled
            assert get_tool_semantic_conversion_enabled() is False
        finally:
            if old is not None:
                os.environ["TOOL_SEMANTIC_CONVERSION_ENABLED"] = old
            else:
                os.environ.pop("TOOL_SEMANTIC_CONVERSION_ENABLED", None)
