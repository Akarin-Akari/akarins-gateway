"""
Gateway 工具循环模块

包含服务端工具循环处理逻辑。

从 unified_gateway_router.py 抽取的工具循环逻辑。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any, List, AsyncGenerator, Optional, Tuple
from pathlib import Path
import hashlib
import json
import os
import time

# 延迟导入 log，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# 延迟导入代理函数
try:
    from .proxy import route_request_with_fallback
except ImportError:
    route_request_with_fallback = None

__all__ = [
    "stream_openai_with_tool_loop",
    "run_local_tool",
]


# ==================== Local Tool Execution (Gateway) ====================
#
# Augment's VSCode extension can advertise tools to the model, but in this gateway setup we're
# forwarding to OpenAI-compatible upstreams that return `tool_calls` in the streaming response.
# The VSCode client does not execute OpenAI `tool_calls` directly in the `/chat-stream` NDJSON
# protocol, so we implement a minimal server-side tool loop here to keep conversations alive.
#
# Security note: This is intended for local development. Tools that write to disk are intentionally
# not implemented here. Add an allowlist + root path restrictions if you extend this.


def _safe_read_text_file(path_str: str, *, max_chars: int = 200_000) -> str:
    """安全读取文本文件"""
    p = Path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path_str}")
    if p.is_dir():
        raise IsADirectoryError(f"Path is a directory: {path_str}")
    text = p.read_text(encoding="utf-8", errors="ignore")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n...[truncated to {max_chars} chars]..."
    return text


def _tool_view(args: Dict[str, Any]) -> str:
    """查看文件内容工具"""
    path_str = args.get("path") or args.get("file_path") or args.get("filePath")
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("Missing required argument: path")
    content = _safe_read_text_file(path_str.strip())
    return content


def _tool_view_range_untruncated(args: Dict[str, Any]) -> str:
    """查看文件指定行范围工具"""
    path_str = args.get("path") or args.get("file_path") or args.get("filePath")
    start = args.get("start_line") or args.get("startLine") or args.get("start")
    end = args.get("end_line") or args.get("endLine") or args.get("end")
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("Missing required argument: path")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        raise ValueError("Invalid line range: start_line/end_line must be ints and 1 <= start <= end")

    text = _safe_read_text_file(path_str.strip(), max_chars=2_000_000)
    lines = text.splitlines()
    # Convert 1-based inclusive to Python slice
    selected = lines[start - 1:end]
    return "\n".join(selected)


def _tool_search_untruncated(args: Dict[str, Any]) -> str:
    """在文件中搜索工具"""
    # Very small subset: search within a single file.
    path_str = args.get("path") or args.get("file_path") or args.get("filePath")
    query = args.get("query") or args.get("pattern") or args.get("text")
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("Missing required argument: path")
    if not isinstance(query, str) or not query:
        raise ValueError("Missing required argument: query")

    text = _safe_read_text_file(path_str.strip(), max_chars=2_000_000)
    lines = text.splitlines()
    matches: List[Dict[str, Any]] = []
    for i, line in enumerate(lines, start=1):
        if query in line:
            matches.append({"line": i, "text": line})
            if len(matches) >= 200:
                break
    return json.dumps({"path": path_str, "query": query, "matches": matches}, ensure_ascii=False)


# ==================== Tool Cache / Idempotent Tools ====================

_IDEMPOTENT_TOOLS = {"view", "view-range-untruncated", "search-untruncated"}
_TOOL_CACHE_TTL_SECONDS = float(os.environ.get("TOOL_CACHE_TTL", 60.0))
_TOOL_RESULT_CACHE: Dict[str, Tuple[float, Optional[float], str]] = {}

# ==================== Prompt Cache (Tool-followup) ====================

_PROMPT_CACHE_TTL_SECONDS = 10.0
_PROMPT_RESPONSE_CACHE: Dict[str, Tuple[float, str]] = {}
_MIN_THOUGHT_SIGNATURE_LENGTH = 50
_SKIP_SIGNATURE_SENTINEL = "skip_thought_signature_validator"


def _make_tool_cache_key(tool_name: str, args: Dict[str, Any]) -> str:
    try:
        args_json = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        args_json = json.dumps({"raw": str(args)}, ensure_ascii=False, separators=(",", ":"))
    return f"{tool_name}:{args_json}"


def _get_tool_cache_mtime(args: Dict[str, Any]) -> Optional[float]:
    path_str = args.get("path") or args.get("file_path") or args.get("filePath")
    if not isinstance(path_str, str) or not path_str.strip():
        return None
    try:
        return os.path.getmtime(path_str)
    except Exception:
        return None


def _get_cached_tool_output(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    if tool_name not in _IDEMPOTENT_TOOLS:
        return None
    cache_key = _make_tool_cache_key(tool_name, args)
    cached = _TOOL_RESULT_CACHE.get(cache_key)
    if not cached:
        log.debug(f"[TOOL_CACHE] 未命中: tool={tool_name}")
        return None
    expires_at, cached_mtime, cached_output = cached
    if time.time() > expires_at:
        _TOOL_RESULT_CACHE.pop(cache_key, None)
        log.debug(f"[TOOL_CACHE] 过期: tool={tool_name}")
        return None
    current_mtime = _get_tool_cache_mtime(args)
    if cached_mtime is not None and current_mtime is not None and current_mtime != cached_mtime:
        _TOOL_RESULT_CACHE.pop(cache_key, None)
        log.debug(f"[TOOL_CACHE] 文件变更失效: tool={tool_name}")
        return None
    log.info(f"[TOOL_CACHE] 命中: tool={tool_name}, len={len(cached_output)}")
    return cached_output


def _store_cached_tool_output(tool_name: str, args: Dict[str, Any], output: str) -> None:
    if tool_name not in _IDEMPOTENT_TOOLS:
        return
    if not isinstance(output, str) or len(output) > 200_000:
        return
    cache_key = _make_tool_cache_key(tool_name, args)
    expires_at = time.time() + _TOOL_CACHE_TTL_SECONDS
    cached_mtime = _get_tool_cache_mtime(args)
    _TOOL_RESULT_CACHE[cache_key] = (expires_at, cached_mtime, output)
    log.info(f"[TOOL_CACHE] 写入: tool={tool_name}, ttl={_TOOL_CACHE_TTL_SECONDS}s, len={len(output)}")


def _stable_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))


def _get_header_value(headers: Dict[str, str], key: str) -> str:
    value = headers.get(key)
    if value:
        return value
    return headers.get(key.lower()) or headers.get(key.upper()) or ""


def _extract_project_id(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    project_id = body.get("project")
    if project_id:
        return str(project_id)
    request = body.get("request")
    if isinstance(request, dict):
        project_id = request.get("project")
        if project_id:
            return str(project_id)
    return ""


def _extract_scid(headers: Dict[str, str], body: Any) -> str:
    scid = _get_header_value(headers, "x-ag-conversation-id") or _get_header_value(headers, "x-conversation-id")
    if scid:
        return scid
    if isinstance(body, dict):
        scid = body.get("_scid") or body.get("session_id")
        if scid:
            return str(scid)
        request = body.get("request")
        if isinstance(request, dict):
            scid = request.get("_scid") or request.get("session_id")
            if scid:
                return str(scid)
    return ""


def _hash_prompt_cache_parts(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        if part:
            hasher.update(part.encode("utf-8"))
        hasher.update(b"|")
    return hasher.hexdigest()


def _build_prompt_cache_routing_scope(model: Optional[str]) -> str:
    try:
        from .adapter import get_backend_for_model, get_sorted_backends
    except Exception:
        return ""

    try:
        chain = [k for k, _ in get_sorted_backends()]
        specified = get_backend_for_model(model) if model else None
        if specified:
            chain = [specified] + [k for k in chain if k != specified]
        return ",".join(chain)
    except Exception:
        return ""


def _build_prompt_cache_key(
    *,
    headers: Dict[str, str],
    body: Dict[str, Any],
    model: str,
    messages: List[Dict[str, Any]],
    tools: Any,
    tool_choice: Any,
) -> str:
    auth = _get_header_value(headers, "authorization")
    api_key = _get_header_value(headers, "x-api-key")
    client_tag = _get_header_value(headers, "x-augment-client") or _get_header_value(headers, "user-agent")
    project_id = _extract_project_id(body)
    backend_hint = _get_header_value(headers, "x-gateway-backend") or _get_header_value(headers, "x-backend")
    routing_scope = _build_prompt_cache_routing_scope(model)
    scid = _extract_scid(headers, body)
    scope = _hash_prompt_cache_parts(auth, api_key, client_tag, project_id, backend_hint, routing_scope, scid)
    return _hash_prompt_cache_parts(
        str(model or ""),
        scope,
        _stable_json_dumps(tools),
        _stable_json_dumps(tool_choice),
        _stable_json_dumps(messages),
    )


def _get_cached_prompt_response(cache_key: str) -> Optional[str]:
    cached = _PROMPT_RESPONSE_CACHE.get(cache_key)
    if not cached:
        return None
    expires_at, payload = cached
    if time.time() > expires_at:
        _PROMPT_RESPONSE_CACHE.pop(cache_key, None)
        return None
    return payload


def _store_prompt_response(cache_key: str, payload: str) -> None:
    if not isinstance(payload, str) or len(payload) > 200_000:
        return
    _PROMPT_RESPONSE_CACHE[cache_key] = (time.time() + _PROMPT_CACHE_TTL_SECONDS, payload)


def _extract_leading_thought_signature(message: Dict[str, Any]) -> Optional[str]:
    if not isinstance(message, dict):
        return None
    sig = message.get("signature") or message.get("thoughtSignature")
    if isinstance(sig, str) and sig and sig != _SKIP_SIGNATURE_SENTINEL:
        return sig
    content = message.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            sig = first.get("signature") or first.get("thoughtSignature")
            if isinstance(sig, str) and sig and sig != _SKIP_SIGNATURE_SENTINEL:
                return sig
    if isinstance(content, dict):
        sig = content.get("signature") or content.get("thoughtSignature")
        if isinstance(sig, str) and sig and sig != _SKIP_SIGNATURE_SENTINEL:
            return sig
    return None


def _has_authoritative_leading_thought_signature(messages: List[Dict[str, Any]]) -> bool:
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("assistant", "model"):
            continue
        sig = _extract_leading_thought_signature(msg)
        if isinstance(sig, str) and len(sig) >= _MIN_THOUGHT_SIGNATURE_LENGTH:
            return True
        return False
    return False


def run_local_tool(tool_name: str, args: Dict[str, Any]) -> str:
    """
    执行本地工具

    Args:
        tool_name: 工具名称
        args: 工具参数

    Returns:
        工具执行结果

    Raises:
        NotImplementedError: 工具未实现
    """
    if tool_name == "view":
        return _tool_view(args)
    if tool_name == "view-range-untruncated":
        return _tool_view_range_untruncated(args)
    if tool_name == "search-untruncated":
        return _tool_search_untruncated(args)
    raise NotImplementedError(f"Tool not implemented in gateway: {tool_name}")


async def stream_openai_with_tool_loop(
    *,
    headers: Dict[str, str],
    body: Dict[str, Any],
    model: str,
    max_tool_rounds: int = 6,
) -> AsyncGenerator[str, None]:
    """
    Call upstream /chat/completions with stream=True, proxy text to Augment NDJSON, and if upstream
    returns tool_calls, execute them locally and continue the loop until a final answer is produced.

    Args:
        headers: 请求头
        body: 请求体
        model: 模型名称
        max_tool_rounds: 最大工具循环轮数

    Yields:
        NDJSON 格式的响应字符串
    """
    if route_request_with_fallback is None:
        yield json.dumps({"text": "[Gateway Error] route_request_with_fallback not available"}, ensure_ascii=False) + "\n"
        return

    debug_tool_loop = str(headers.get("x-debug-tool-loop", "")).strip().lower() in ("1", "true", "yes", "on")

    # We mutate messages across rounds
    messages = list(body.get("messages") or [])
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    for round_idx in range(max_tool_rounds):
        prompt_cache_key = None
        if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "tool":
            if _has_authoritative_leading_thought_signature(messages):
                prompt_cache_key = _build_prompt_cache_key(
                    headers=headers,
                    body=body,
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
                log.info(f"[PROMPT_CACHE] 尝试命中: key={prompt_cache_key[:16]}")
                cached_text = _get_cached_prompt_response(prompt_cache_key)
                if cached_text is not None:
                    log.info(f"[PROMPT_CACHE] 命中: key={prompt_cache_key[:16]}, len={len(cached_text)}")
                    yield json.dumps({"text": cached_text}, separators=(",", ":"), ensure_ascii=False) + "\n"
                    return
                log.info(f"[PROMPT_CACHE] 未命中: key={prompt_cache_key[:16]}")
            else:
                log.info("[PROMPT_CACHE] 跳过命中：权威历史缺少 leading thoughtSignature")

        request_body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            request_body["tools"] = tools
        if tool_choice is not None:
            request_body["tool_choice"] = tool_choice
        # Preserve a few common OpenAI params if present
        for k in ("temperature", "top_p", "max_tokens", "stop", "seed"):
            if k in body:
                request_body[k] = body[k]

        sse_stream = await route_request_with_fallback(
            endpoint="/chat/completions",
            method="POST",
            headers=headers,
            body=request_body,
            model=model,
            stream=True,
        )

        buffer = ""
        tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
        saw_tool_calls = False
        saw_done = False
        assistant_text_parts: List[str] = []

        async for chunk in sse_stream:
            if not chunk:
                continue
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="ignore")
            buffer += chunk

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue

                json_str = line[6:].strip()
                if json_str == "[DONE]":
                    saw_done = True
                    break

                try:
                    evt = json.loads(json_str)
                except Exception:
                    continue

                choices = evt.get("choices") or []
                if not choices:
                    continue
                choice0 = choices[0] if isinstance(choices[0], dict) else None
                if not choice0:
                    continue

                # Text streaming
                delta = choice0.get("delta") or {}
                if isinstance(delta, dict) and "content" in delta and delta["content"] is not None:
                    yield json.dumps({"text": delta["content"]}, separators=(",", ":"), ensure_ascii=False) + "\n"
                    if prompt_cache_key is not None and isinstance(delta["content"], str):
                        assistant_text_parts.append(delta["content"])

                # Tool calls streaming
                tool_calls = delta.get("tool_calls") if isinstance(delta, dict) else None
                if isinstance(tool_calls, list) and tool_calls:
                    saw_tool_calls = True
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        if not isinstance(idx, int):
                            idx = 0
                        cur = tool_calls_by_index.setdefault(idx, {"id": None, "type": "function", "function": {"name": None, "arguments": ""}})
                        if "id" in tc and isinstance(tc["id"], str):
                            cur["id"] = tc["id"]
                        if "type" in tc and isinstance(tc["type"], str):
                            cur["type"] = tc["type"]
                        func = tc.get("function")
                        if isinstance(func, dict):
                            if "name" in func and isinstance(func["name"], str):
                                cur["function"]["name"] = func["name"]
                            if "arguments" in func and isinstance(func["arguments"], str):
                                cur["function"]["arguments"] += func["arguments"]

                finish_reason = choice0.get("finish_reason")
                if finish_reason in ("tool_calls", "function_call"):
                    log.warning(
                        f"[TOOL LOOP] finish_reason={finish_reason} round={round_idx} tool_calls_indexes={list(tool_calls_by_index.keys())}",
                        tag="GATEWAY",
                    )

            if saw_done:
                break

        if not saw_tool_calls or not tool_calls_by_index:
            if prompt_cache_key is not None and not saw_tool_calls:
                full_text = "".join(assistant_text_parts)
                if full_text:
                    _store_prompt_response(prompt_cache_key, full_text)
                    log.info(f"[PROMPT_CACHE] 写入: key={prompt_cache_key[:16]}, len={len(full_text)}")
            return

        # Build ordered tool calls
        ordered: List[Dict[str, Any]] = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index.keys())]
        # Append assistant tool_calls message (OpenAI format)
        assistant_tool_calls = []
        for tc in ordered:
            tool_id = tc.get("id") or f"call_round{round_idx}_{len(assistant_tool_calls)}"
            fn = tc.get("function") or {}
            assistant_tool_calls.append(
                {
                    "id": tool_id,
                    "type": tc.get("type") or "function",
                    "function": {"name": fn.get("name"), "arguments": fn.get("arguments", "")},
                }
            )

        if debug_tool_loop:
            yield json.dumps(
                {
                    "text": f"\n[Gateway debug] round={round_idx} tool_calls={json.dumps(assistant_tool_calls, ensure_ascii=False)[:2000]}"
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ) + "\n"

        # Some backends are picky about `content` being null; use empty string for tool-call turns.
        messages.append({"role": "assistant", "content": "", "tool_calls": assistant_tool_calls})

        # Execute each tool and append tool result messages
        round_tool_cache: Dict[str, str] = {}
        for tc in assistant_tool_calls:
            tool_id = tc["id"]
            fn = tc.get("function") or {}
            tool_name = fn.get("name") or "unknown"
            arg_str = fn.get("arguments") or ""

            def _infer_windows_path_from_messages() -> Optional[str]:
                try:
                    import re as _re

                    for m in reversed(messages):
                        if not isinstance(m, dict):
                            continue
                        if m.get("role") != "user":
                            continue
                        content = m.get("content")
                        if not isinstance(content, str) or not content:
                            continue
                        match = _re.search(r"([A-Za-z]:\\\\[^\\s\"']+)", content)
                        if not match:
                            continue
                        p = match.group(1).strip()
                        p = p.rstrip("，。,;:)]}>'\"")
                        return p
                except Exception:
                    return None
                return None

            def _repair_tool_args(tool: str, raw: str) -> Optional[Dict[str, Any]]:
                # Best-effort fallback for upstreams that stream non-JSON argument fragments.
                if tool in ("view", "view-range-untruncated") and (not raw or not raw.strip()):
                    p = _infer_windows_path_from_messages()
                    return {"path": p} if p else None
                if tool == "view" and isinstance(raw, str):
                    # If JSON parsing fails, try to infer path from the user message.
                    p = _infer_windows_path_from_messages()
                    return {"path": p} if p else None
                return None

            try:
                args = json.loads(arg_str) if isinstance(arg_str, str) and arg_str.strip() else {}
            except Exception as e:
                repaired = _repair_tool_args(tool_name, arg_str)
                if repaired is None:
                    tool_out = f"Failed to parse tool arguments as JSON: {e}\nRaw arguments: {arg_str[:5000]}"
                    messages.append({"role": "tool", "tool_call_id": tool_id, "content": tool_out})
                    continue
                args = repaired

            # If args are empty/missing required fields, try a small inference for common tools.
            if tool_name == "view" and isinstance(args, dict) and not args.get("path"):
                inferred = _infer_windows_path_from_messages()
                if inferred:
                    args["path"] = inferred

            args_dict = args if isinstance(args, dict) else {"value": args}
            try:
                tool_out = None
                if tool_name in _IDEMPOTENT_TOOLS:
                    cache_key = _make_tool_cache_key(tool_name, args_dict)
                    if cache_key in round_tool_cache:
                        log.info(f"[TOOL_CACHE] 命中(轮内): tool={tool_name}, key={cache_key[:16]}")
                        tool_out = round_tool_cache[cache_key]
                    else:
                        tool_out = _get_cached_tool_output(tool_name, args_dict)
                        if tool_out is not None:
                            log.info(f"[TOOL_CACHE] 命中(TTL): tool={tool_name}, key={cache_key[:16]}")
                if tool_out is None:
                    if tool_name in _IDEMPOTENT_TOOLS:
                        log.info(f"[TOOL_CACHE] 未命中: tool={tool_name}, key={_make_tool_cache_key(tool_name, args_dict)[:16]}")
                    tool_out = run_local_tool(tool_name, args_dict)
                    if (
                        tool_name in _IDEMPOTENT_TOOLS
                        and isinstance(tool_out, str)
                        and not tool_out.startswith("Tool execution error")
                    ):
                        cache_key = _make_tool_cache_key(tool_name, args_dict)
                        round_tool_cache[cache_key] = tool_out
                        _store_cached_tool_output(tool_name, args_dict, tool_out)
                        log.info(f"[TOOL_CACHE] 写入: tool={tool_name}, key={cache_key[:16]}, len={len(tool_out)}")
            except Exception as e:
                tool_out = f"Tool execution error: {e}"

            if debug_tool_loop:
                preview = tool_out if isinstance(tool_out, str) else str(tool_out)
                yield json.dumps(
                    {
                        "text": f"\n[Gateway debug] tool={tool_name} args={json.dumps(args, ensure_ascii=False)[:500]} out_preview={preview[:500]}"
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                ) + "\n"

            # Keep tool output bounded to avoid runaway context growth
            if isinstance(tool_out, str) and len(tool_out) > 200_000:
                tool_out = tool_out[:200_000] + "\n\n...[truncated]..."

            messages.append({"role": "tool", "tool_call_id": tool_id, "content": tool_out})

        # Continue loop for next model call (with updated messages)

    yield json.dumps(
        {"text": "\n[Gateway] 工具循环次数超限，已终止（max_tool_rounds reached）。"},
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n"
