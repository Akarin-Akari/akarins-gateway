"""
SCID Generator - 稳定的会话标识符生成策略

核心问题：
- Cursor 不发送 conversation_id
- 原方案使用"前 3 条消息"生成 fingerprint
- checkpoint 回退时消息变化 → fingerprint 变化 → SCID 变化
- 导致找不到缓存

解决方案：
- 使用"第一条用户消息"生成 fingerprint（对 checkpoint 友好）
- 第一条消息一般不会因 checkpoint 而变化
- 即使回退，第一条消息仍然存在

Author: 浮浮酱 (Claude Sonnet 4.5)
Date: 2026-01-24
"""

import hashlib
import time
import uuid
from typing import Dict, List, Optional
import logging

log = logging.getLogger("gcli2api.scid_generator")


def _extract_user_text_content(message: Dict) -> str:
    """
    从 user 消息中提取文本内容（兼容 string / Anthropic list）。
    """
    if not isinstance(message, dict):
        return ""

    content = message.get("content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
        return " ".join(text_parts)

    return str(content or "")


def _strip_metadata_prefix_for_scid(text: str) -> str:
    """
    清理 Cursor/IDE 注入的元信息前缀，避免不同会话误用同一 SCID。
    """
    if not text:
        return ""

    normalized = text.strip()
    lower_text = normalized.lower()

    # 场景1: <user_info>...</user_info>
    # 若无闭合标签且以 <user_info> 开头，视为纯元信息。
    open_tag = "<user_info>"
    close_tag = "</user_info>"
    if open_tag in lower_text:
        start = lower_text.find(open_tag)
        end = lower_text.find(close_tag, start)
        if end != -1:
            suffix = normalized[end + len(close_tag):].strip()
            if suffix:
                normalized = suffix
                lower_text = normalized.lower()
            else:
                prefix = normalized[:start].strip()
                normalized = prefix
                lower_text = normalized.lower()
        elif lower_text.startswith(open_tag):
            return ""

    # 场景2: <environment_context>...</environment_context>
    env_open_tag = "<environment_context>"
    env_close_tag = "</environment_context>"
    if env_open_tag in lower_text:
        start = lower_text.find(env_open_tag)
        end = lower_text.find(env_close_tag, start)
        if end != -1:
            suffix = normalized[end + len(env_close_tag):].strip()
            if suffix:
                normalized = suffix
                lower_text = normalized.lower()
            else:
                prefix = normalized[:start].strip()
                normalized = prefix
                lower_text = normalized.lower()
        elif lower_text.startswith(env_open_tag):
            return ""

    # 场景3: 无标签的典型 IDE 环境信息块（保守识别）
    # 避免把「OS Version + Workspace Path」这类固定前缀当作业务首问。
    if lower_text.startswith("os version:") and "workspace path:" in lower_text:
        return ""

    return normalized.strip()


def _get_first_meaningful_user_content(messages: List[Dict]) -> Optional[str]:
    """
    获取第一条“有效业务用户消息”内容：
    - 跳过空消息
    - 跳过 IDE 环境元信息消息（如 <user_info>）
    """
    if not messages or not isinstance(messages, list):
        return None

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue

        raw_text = _extract_user_text_content(msg)
        content_key = _strip_metadata_prefix_for_scid(raw_text)
        if not content_key:
            log.debug(f"[SCID_GEN] Skip non-meaningful user message at index={idx}")
            continue

        return content_key

    return None


def generate_stable_scid_from_first_message(messages: List[Dict]) -> Optional[str]:
    """
    基于第一条有效业务用户消息生成稳定的 SCID
    
    策略：
    - 找到第一条 role=user 的消息
    - 提取消息内容的前 200 个字符
    - 生成 SHA256 hash
    
    优点：
    - ✅ 对 checkpoint 回退友好
    - ✅ 第一条消息一般不会变
    - ✅ 实现简单
    
    缺点：
    - ⚠️ 如果两个对话的第一条消息相同，会误匹配
    - ⚠️ 需要定期清理旧的 SCID
    
    Args:
        messages: 消息列表
    
    Returns:
        SCID 字符串，如果找不到用户消息则返回 None
    """
    if not messages or not isinstance(messages, list):
        return None
    
    content_key = _get_first_meaningful_user_content(messages)
    if not content_key:
        log.debug("[SCID_GEN] No meaningful user message found in messages")
        return None

    # 只使用前 200 个字符（避免过长，提高性能）
    content_key = content_key[:200]
    
    # 生成 SHA256 hash（取前 16 个字符）
    fingerprint = hashlib.sha256(content_key.encode('utf-8')).hexdigest()[:16]
    
    scid = f"scid_first_{fingerprint}"
    
    log.debug(
        f"[SCID_GEN] Generated stable SCID from first message: "
        f"content_preview={content_key[:50]}..., scid={scid}"
    )
    
    return scid


def generate_stable_scid_with_client_ip(
    messages: List[Dict],
    client_ip: str
) -> Optional[str]:
    """
    基于第一条有效业务用户消息 + 客户端 IP 生成 SCID
    
    策略：
    - 结合第一条消息和客户端 IP
    - 降低误匹配风险
    
    Args:
        messages: 消息列表
        client_ip: 客户端 IP
    
    Returns:
        SCID 字符串
    """
    if not messages or not isinstance(messages, list):
        return None
    
    content_key = _get_first_meaningful_user_content(messages)
    if content_key:
        content_key = content_key[:200]

    if not content_key:
        return None
    
    # 结合 IP 和内容
    combined_key = f"{client_ip}:{content_key}"
    fingerprint = hashlib.sha256(combined_key.encode('utf-8')).hexdigest()[:16]
    
    scid = f"scid_ip_{fingerprint}"
    
    log.debug(
        f"[SCID_GEN] Generated IP-based SCID: "
        f"ip={client_ip}, scid={scid}"
    )
    
    return scid


def generate_time_based_scid(
    client_ip: str,
    time_window_minutes: int = 60
) -> str:
    """
    基于 IP + 时间窗口生成 SCID
    
    策略：
    - 同一 IP 在时间窗口内的请求共享 SCID
    - 适合作为兜底方案
    
    优点：
    - ✅ 简单可靠
    - ✅ 自动过期
    
    缺点：
    - ⚠️ 同一 IP 的不同对话会混在一起
    - ⚠️ 不够精确
    
    Args:
        client_ip: 客户端 IP
        time_window_minutes: 时间窗口大小（分钟）
    
    Returns:
        SCID 字符串
    """
    # 计算时间窗口 ID
    current_timestamp = int(time.time())
    window_size = time_window_minutes * 60
    window_id = current_timestamp // window_size
    
    # 生成 SCID
    key = f"{client_ip}:{window_id}"
    fingerprint = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    
    scid = f"scid_time_{fingerprint}"
    
    log.debug(
        f"[SCID_GEN] Generated time-based SCID: "
        f"ip={client_ip}, window_id={window_id}, scid={scid}"
    )
    
    return scid


def extract_or_generate_scid(
    headers: Dict[str, str],
    body: Dict,
    messages: List[Dict],
    client_ip: str = "unknown"
) -> str:
    """
    提取或生成 SCID（混合策略）
    
    优先级：
    1. Header: x-ag-conversation-id（客户端提供，最可靠）
    2. Header: x-conversation-id（客户端提供）
    3. Body: conversation_id（客户端提供）
    4. ✅ 第一条用户消息 + IP（稳定，checkpoint 友好）
    5. 第一条用户消息（稳定，checkpoint 友好）
    6. IP + 时间窗口（兜底方案）
    7. 随机 UUID（最后兜底）
    
    Args:
        headers: HTTP 请求头
        body: 请求体
        messages: 消息列表
        client_ip: 客户端 IP
    
    Returns:
        SCID 字符串
    """
    # 1. Header: x-ag-conversation-id
    scid = headers.get("x-ag-conversation-id", "").strip()
    if scid:
        log.info(f"[SCID_GEN] ✅ Using SCID from x-ag-conversation-id header: {scid[:30]}...")
        return scid
    
    # 2. Header: x-conversation-id
    scid = headers.get("x-conversation-id", "").strip()
    if scid:
        log.info(f"[SCID_GEN] ✅ Using SCID from x-conversation-id header: {scid[:30]}...")
        return scid
    
    # 3. Body: conversation_id
    if isinstance(body, dict):
        scid = body.get("conversation_id")
        if scid and isinstance(scid, str) and scid.strip():
            scid = f"scid_body_{scid.strip()}"
            log.info(f"[SCID_GEN] ✅ Using SCID from body conversation_id: {scid[:30]}...")
            return scid
    
    # 4. ✅ 第一条用户消息 + IP（最稳定）
    if client_ip and client_ip != "unknown":
        scid = generate_stable_scid_with_client_ip(messages, client_ip)
        if scid:
            log.info(
                f"[SCID_GEN] ✅ Using stable SCID from first message + IP "
                f"(checkpoint-friendly): {scid[:30]}..."
            )
            return scid
    
    # 5. 第一条用户消息（不含 IP，可能误匹配）
    scid = generate_stable_scid_from_first_message(messages)
    if scid:
        log.info(
            f"[SCID_GEN] ✅ Using stable SCID from first message "
            f"(checkpoint-friendly, no IP): {scid[:30]}..."
        )
        return scid
    
    # 6. IP + 时间窗口（兜底方案）
    if client_ip and client_ip != "unknown":
        scid = generate_time_based_scid(client_ip, time_window_minutes=60)
        log.info(
            f"[SCID_GEN] ⚠️ Using time-based SCID as fallback "
            f"(IP: {client_ip}): {scid[:30]}..."
        )
        return scid
    
    # 7. 随机 UUID（最后兜底）
    scid = f"scid_random_{uuid.uuid4().hex}"
    log.warning(
        f"[SCID_GEN] ⚠️ Generated random SCID as last resort "
        f"(no stable identifier available): {scid[:30]}..."
    )
    return scid


def extract_client_ip(headers: Dict[str, str]) -> str:
    """
    从 HTTP headers 中提取客户端 IP
    
    优先级：
    1. X-Forwarded-For（代理场景）
    2. X-Real-IP（Nginx 等）
    3. 默认 "unknown"
    
    Args:
        headers: HTTP 请求头
    
    Returns:
        客户端 IP 字符串
    """
    # 转换为小写键
    headers_lower = {k.lower(): v for k, v in headers.items()}
    
    # X-Forwarded-For（可能包含多个 IP，取第一个）
    forwarded_for = headers_lower.get("x-forwarded-for", "")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
        if client_ip:
            return client_ip
    
    # X-Real-IP
    real_ip = headers_lower.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    
    # 默认
    return "unknown"


# 导出函数
__all__ = [
    "generate_stable_scid_from_first_message",
    "generate_stable_scid_with_client_ip",
    "generate_time_based_scid",
    "extract_or_generate_scid",
    "extract_client_ip",
]
