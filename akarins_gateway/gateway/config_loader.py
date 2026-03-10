"""
Gateway 配置加载器

从 YAML 配置文件加载后端配置，支持环境变量替换。

作者: 浮浮酱 (Claude Sonnet 4.5)
创建日期: 2026-01-18
"""

import os
import re
import fnmatch
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, List, Union, Set, Tuple, Optional

from akarins_gateway.gateway.backends.interface import BackendConfig
from akarins_gateway.core.constants import log  # ✅ [FIX 2026-01-25] 添加 log 导入，修复 NameError

__all__ = [
    "load_gateway_config",
    "load_model_routing_config",
    "expand_env_vars",
    "ModelRoutingRule",
    "BackendEntry",
    "normalize_model_for_comparison",
    # Phase B: Data-Driven Routing exports
    "match_model_pattern",
    "DefaultRoutingRule",
    "DefaultRoutingEntry",
    "CrossModelFallbackRule",
    "FinalFallbackConfig",
    "BackendCapability",
    "is_backend_capable",
    "get_default_routing_rule",
    "get_catch_all_routing",
    "get_cross_model_fallback",
    "get_copilot_model_mapping_yaml",
    "get_final_fallback",
    "reload_model_routing_config",
]


# ==================== ZeroGravity Port Auto-Discovery ====================

_zerogravity_port_discovered = False


def _ensure_zerogravity_port_env():
    """
    Auto-discover ZeroGravity proxy port from .zerogravity-port file.

    The start-zerogravity.ps1 script writes the actual listening port
    to .zerogravity-port at project root. This function reads it and sets
    ZEROGRAVITY_BASE_URL env var so that ${ZEROGRAVITY_BASE_URL:...} in
    gateway.yaml picks up the correct port automatically.

    Only runs once per process lifetime. No-op if ZEROGRAVITY_BASE_URL
    is already set explicitly by the user/environment.
    """
    global _zerogravity_port_discovered
    if _zerogravity_port_discovered:
        return
    _zerogravity_port_discovered = True

    # Don't override explicit env var
    if os.environ.get("ZEROGRAVITY_BASE_URL"):
        return

    # Look for port file relative to project root (gcli2api/)
    project_root = Path(__file__).parent.parent.parent
    port_file = project_root / ".zerogravity-port"

    if not port_file.exists():
        return

    try:
        port_str = port_file.read_text(encoding="utf-8").strip()
        if port_str.isdigit():
            port = int(port_str)
            if 1024 <= port <= 65535:
                os.environ["ZEROGRAVITY_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
                log.info(f"[CONFIG_LOADER] ZeroGravity port auto-discovered: {port} (from {port_file})")
            else:
                log.warning(f"[CONFIG_LOADER] ZeroGravity port file contains out-of-range port: {port}")
        else:
            log.warning(f"[CONFIG_LOADER] ZeroGravity port file contains non-numeric value: {port_str!r}")
    except Exception as e:
        log.warning(f"[CONFIG_LOADER] Failed to read ZeroGravity port file: {e}")


def normalize_model_for_comparison(model: str) -> str:
    """
    将模型名归一化为可比形式，与 get_model_routing_rule 的查找逻辑一致。

    用于判断两个模型名是否指向同一型号（如 claude-sonnet-4.5 与 claude-sonnet-4-5-thinking）。
    归一化步骤：
    1. model_mapping (Cursor 格式 -> Antigravity 格式)
    2. 移除 -thinking/-extended/-preview/-latest 后缀
    3. 移除日期后缀 (-YYYYMMDD)
    4. 4.5/4-5 归一化为 4.5

    Args:
        model: 原始模型名称

    Returns:
        归一化后的可比字符串

    作者: 浮浮酱 (Claude Opus 4.5)
    创建日期: 2026-02-02 - 抽取自 get_model_routing_rule，供 _is_cross_model_entry 复用
    """
    if model is None or not model:
        return ""
    # 1. model_mapping (Cursor 格式 -> Antigravity 格式)
    try:
        from akarins_gateway.converters.model_config import model_mapping
        model = model_mapping(model)
    except ImportError:
        pass
    model_lower = model.lower()
    # 2. 移除 -thinking/-extended/-preview/-latest 和日期后缀（与 get_model_routing_rule 一致）
    normalized = re.sub(r'-(thinking|extended|preview|latest)$', '', model_lower)
    normalized = re.sub(r'-\d{8}$', '', normalized)
    # 3. 4.5/4-5 and 4.6/4-6 归一化（与 gateway.yaml 配置键一致）
    normalized = normalized.replace("4-5", "4.5").replace("4-6", "4.6")
    return normalized


@dataclass
class BackendEntry:
    """
    后端链条目

    表示降级链中的一个后端配置，包含后端名称和目标模型

    Attributes:
        backend: 后端名称（如 kiro-gateway, antigravity, copilot）
        model: 目标模型名称（如 claude-sonnet-4.5, gemini-3-pro）
    """
    backend: str
    model: str

    def __repr__(self) -> str:
        return f"BackendEntry({self.backend}, {self.model})"


@dataclass
class ModelRoutingRule:
    """
    模型特定路由规则

    用于配置特定模型的后端优先级链和降级条件

    Attributes:
        model: 模型名称（如 claude-sonnet-4.5）
        backend_chain: 按优先级排序的后端链（包含后端和目标模型）
        fallback_on: 触发降级的条件（HTTP 状态码或特殊条件）
        enabled: 是否启用此规则
    """
    model: str
    backend_chain: List[BackendEntry] = field(default_factory=list)
    fallback_on: Set[Union[int, str]] = field(default_factory=set)
    enabled: bool = True

    @property
    def backends(self) -> List[str]:
        """
        兼容属性：返回后端名称列表（不包含目标模型）

        用于向后兼容旧代码
        """
        return [entry.backend for entry in self.backend_chain]

    def should_fallback(self, status_code: int = None, error_type: str = None) -> bool:
        """
        判断是否应该降级到下一个后端

        Args:
            status_code: HTTP 状态码
            error_type: 错误类型（timeout, connection_error, unavailable）

        Returns:
            是否应该降级
        """
        if status_code and status_code in self.fallback_on:
            return True
        if error_type and error_type in self.fallback_on:
            return True
        return False

    def get_first_backend(self) -> Optional[BackendEntry]:
        """
        获取第一个后端

        Returns:
            第一个后端条目，如果没有则返回 None
        """
        return self.backend_chain[0] if self.backend_chain else None

    def get_next_backend(self, current_backend: str) -> Optional[str]:
        """
        获取下一个后端名称（向后兼容）

        Args:
            current_backend: 当前后端名称

        Returns:
            下一个后端名称，如果没有则返回 None
        """
        entry = self.get_next_backend_entry(current_backend)
        return entry.backend if entry else None

    def get_next_backend_entry(self, current_backend: str) -> Optional[BackendEntry]:
        """
        获取下一个后端条目（包含后端和目标模型）

        Args:
            current_backend: 当前后端名称

        Returns:
            下一个后端条目，如果没有则返回 None
        """
        # 查找当前后端在链中的位置
        for i, entry in enumerate(self.backend_chain):
            if entry.backend == current_backend:
                if i + 1 < len(self.backend_chain):
                    return self.backend_chain[i + 1]
                return None

        # ✅ [FIX 2026-01-22] 当前后端不在链中，不应该返回第一个（可能导致降级链断裂）
        # 返回 None，让调用者处理
        from akarins_gateway.core.log import log
        log.warning(
            f"[FALLBACK] 后端 {current_backend} 不在模型 {self.model} 的降级链中",
            tag="GATEWAY"
        )
        return None

    def get_backend_entry_by_name(self, backend_name: str) -> Optional[BackendEntry]:
        """
        根据后端名称获取条目

        Args:
            backend_name: 后端名称

        Returns:
            后端条目，如果不存在则返回 None
        """
        for entry in self.backend_chain:
            if entry.backend == backend_name:
                return entry
        return None


# ==================== Phase B: Data-Driven Routing Dataclasses ====================
# [REFACTOR 2026-02-21] 新增 dataclass 支持 YAML 驱动路由

@dataclass(frozen=True)
class DefaultRoutingEntry:
    """默认路由链中的单个后端条目"""
    backend: str


@dataclass(frozen=True)
class DefaultRoutingRule:
    """
    默认路由规则（模式匹配）

    用于 default_routing 配置节，通过 fnmatch 模式匹配模型名称

    Attributes:
        pattern: fnmatch 模式（如 "claude-*opus*4.6*"）
        chain: 按优先级排序的后端链
        fallback_on: 触发降级的条件
    """
    pattern: str
    chain: Tuple[DefaultRoutingEntry, ...] = field(default_factory=tuple)
    fallback_on: frozenset = field(default_factory=frozenset)

    @property
    def backends(self) -> List[str]:
        """返回后端名称列表"""
        return [entry.backend for entry in self.chain]

    def should_fallback(self, status_code: int = None, error_type: str = None) -> bool:
        """判断是否应该降级到下一个后端"""
        if status_code and status_code in self.fallback_on:
            return True
        if error_type and error_type in self.fallback_on:
            return True
        return False


@dataclass(frozen=True)
class CrossModelFallbackRule:
    """
    跨模型降级规则

    当某模型在所有后端都失败后，切换到不同模型

    Attributes:
        pattern: fnmatch 模式
        fallback_model: 降级目标模型名
        backend: 降级目标后端
    """
    pattern: str
    fallback_model: str
    backend: str


@dataclass(frozen=True)
class FinalFallbackConfig:
    """
    最终兜底配置

    Attributes:
        enabled: 是否启用
        backend: 兜底后端名称
        respect_circuit_breaker: 是否遵守熔断器
    """
    enabled: bool
    backend: str
    respect_circuit_breaker: bool


@dataclass(frozen=True)
class BackendCapability:
    """
    后端能力声明（支持的模型模式）

    通过 include/exclude 模式声明后端支持哪些模型

    Attributes:
        include_patterns: 包含模式列表（匹配任一即认为支持）
        exclude_patterns: 排除模式列表（优先于 include，匹配任一即排除）
    """
    include_patterns: Tuple[str, ...] = field(default_factory=tuple)
    exclude_patterns: Tuple[str, ...] = field(default_factory=tuple)


def expand_env_vars(value: Any) -> Any:
    """
    递归展开环境变量

    支持语法：${VAR_NAME:default_value}

    Args:
        value: 配置值（可以是字符串、列表、字典等）

    Returns:
        展开后的值

    Examples:
        >>> os.environ["TEST_VAR"] = "hello"
        >>> expand_env_vars("${TEST_VAR:world}")
        'hello'
        >>> expand_env_vars("${MISSING_VAR:world}")
        'world'
        >>> expand_env_vars("${BOOL_VAR:true}")
        True
        >>> expand_env_vars("${INT_VAR:42}")
        42
    """
    if isinstance(value, str):
        # 匹配 ${VAR:default} 或 ${VAR}
        pattern = r'\$\{([A-Za-z_][A-Za-z0-9_]*?)(?::([^}]*))?\}'

        def replacer(match):
            var_name = match.group(1)
            default_value = match.group(2) if match.group(2) is not None else ""
            env_value = os.getenv(var_name, default_value)
            return env_value

        result = re.sub(pattern, replacer, value)

        # 类型转换
        # 布尔值
        if result.lower() in ("true", "yes", "1"):
            return True
        elif result.lower() in ("false", "no", "0"):
            return False

        # 数字
        try:
            if "." in result:
                return float(result)
            else:
                return int(result)
        except ValueError:
            pass

        # 列表（JSON 格式）
        if result.startswith("[") and result.endswith("]"):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, ValueError):
                pass

        return result

    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]

    elif isinstance(value, dict):
        return {key: expand_env_vars(val) for key, val in value.items()}

    else:
        return value


def load_gateway_config(config_path: str = None) -> Dict[str, BackendConfig]:
    """
    从 YAML 文件加载 Gateway 配置

    Args:
        config_path: 配置文件路径（默认为 config/gateway.yaml）

    Returns:
        后端配置字典 {backend_name: BackendConfig}

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置格式错误

    Examples:
        >>> configs = load_gateway_config()
        >>> antigravity_config = configs["gcli2api-antigravity"]
        >>> print(antigravity_config.base_url)
        http://127.0.0.1:7861/antigravity/v1
    """
    # 默认配置文件路径
    if config_path is None:
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "gateway.yaml"
    else:
        config_path = Path(config_path)

    # 检查文件是否存在
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    # Auto-discover ZeroGravity port before env var expansion
    _ensure_zerogravity_port_env()

    # 读取 YAML 文件
    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    if not isinstance(raw_config, dict) or "backends" not in raw_config:
        raise ValueError("配置文件格式错误：缺少 'backends' 字段")

    backends_raw = raw_config["backends"]
    if not isinstance(backends_raw, dict):
        raise ValueError("配置文件格式错误：'backends' 必须是字典")

    # 转换为 BackendConfig 对象
    configs: Dict[str, BackendConfig] = {}

    for backend_name, backend_data in backends_raw.items():
        if not isinstance(backend_data, dict):
            raise ValueError(f"后端 '{backend_name}' 配置格式错误：必须是字典")

        # 展开环境变量
        expanded_data = expand_env_vars(backend_data)

        # 提取字段
        name = backend_name  # 使用 key 作为名称
        base_url = expanded_data.get("base_url")
        priority = expanded_data.get("priority")
        models = expanded_data.get("models", [])
        enabled = expanded_data.get("enabled", True)
        timeout = expanded_data.get("timeout", 30.0)
        stream_timeout = expanded_data.get("stream_timeout", timeout * 2)  # 默认为普通超时的 2 倍
        max_retries = expanded_data.get("max_retries", 3)
        api_key = expanded_data.get("api_key")  # [FIX 2026-01-24] 读取 API Key

        # 验证必填字段
        if not base_url:
            raise ValueError(f"后端 '{backend_name}' 缺少 'base_url' 字段")
        if priority is None:
            raise ValueError(f"后端 '{backend_name}' 缺少 'priority' 字段")

        # 类型转换
        if not isinstance(models, list):
            raise ValueError(f"后端 '{backend_name}' 的 'models' 必须是列表")

        try:
            priority = int(priority)
            timeout = float(timeout)
            stream_timeout = float(stream_timeout)
            max_retries = int(max_retries)
        except (ValueError, TypeError) as e:
            raise ValueError(f"后端 '{backend_name}' 配置类型错误: {e}")

        # 创建 BackendConfig 对象
        # 注意：BackendConfig 目前没有 stream_timeout 字段，我们将其存储在额外属性中
        config = BackendConfig(
            name=name,
            base_url=base_url,
            priority=priority,
            models=models,
            enabled=enabled,
            timeout=timeout,
            max_retries=max_retries,
        )

        # 临时存储 stream_timeout 和 api_key（直到 BackendConfig 添加这些字段）
        # 使用 object.__setattr__ 绕过 dataclass 的限制
        object.__setattr__(config, "stream_timeout", stream_timeout)
        if api_key:  # [FIX 2026-01-24] 存储 API Key（如果有）
            object.__setattr__(config, "api_key", api_key)

        configs[backend_name] = config

    return configs


def load_model_routing_config(config_path: str = None) -> Dict[str, ModelRoutingRule]:
    """
    从 YAML 文件加载模型特定路由配置

    Args:
        config_path: 配置文件路径（默认为 config/gateway.yaml）

    Returns:
        模型路由规则字典 {model_name: ModelRoutingRule}

    Examples:
        >>> rules = load_model_routing_config()
        >>> sonnet_rule = rules.get("claude-sonnet-4.5")
        >>> if sonnet_rule and sonnet_rule.enabled:
        ...     print(f"Sonnet 4.5 backends: {sonnet_rule.backends}")
        Sonnet 4.5 backends: ['kiro-gateway', 'antigravity']
    """
    # 默认配置文件路径
    if config_path is None:
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "gateway.yaml"
    else:
        config_path = Path(config_path)

    # 检查文件是否存在
    if not config_path.exists():
        return {}  # 配置文件不存在时返回空字典

    # 读取 YAML 文件
    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    if not isinstance(raw_config, dict):
        return {}

    # 获取 model_routing 配置节
    model_routing_raw = raw_config.get("model_routing", {})
    if not isinstance(model_routing_raw, dict):
        return {}

    # 转换为 ModelRoutingRule 对象
    rules: Dict[str, ModelRoutingRule] = {}

    for model_name, rule_data in model_routing_raw.items():
        if not isinstance(rule_data, dict):
            continue

        # 展开环境变量
        expanded_data = expand_env_vars(rule_data)

        # 提取字段
        backends_raw = expanded_data.get("backends", [])
        fallback_on_raw = expanded_data.get("fallback_on", [])
        enabled = expanded_data.get("enabled", True)

        # 处理 backends：支持两种格式
        # 1. 字符串格式（旧）: ["kiro-gateway", "gcli2api-antigravity"]
        # 2. 对象格式（新）: [{"backend": "kiro-gateway", "model": "claude-sonnet-4.5"}]
        backend_chain: List[BackendEntry] = []
        for item in backends_raw:
            if isinstance(item, str):
                # 旧格式：字符串，使用原始模型名作为目标模型
                backend_chain.append(BackendEntry(
                    backend=item,
                    model=model_name.lower()  # 使用配置的模型名作为默认目标
                ))
            elif isinstance(item, dict):
                # 新格式：{backend: "name", model: "target_model"}
                backend_name = item.get("backend", "")
                target_model = item.get("model", model_name.lower())
                if backend_name:
                    backend_chain.append(BackendEntry(
                        backend=backend_name,
                        model=target_model
                    ))

        # 处理 fallback_on：转换为 set，支持整数和字符串
        fallback_on = set()
        for item in fallback_on_raw:
            if isinstance(item, int):
                fallback_on.add(item)
            elif isinstance(item, str):
                # 尝试转换为整数（HTTP 状态码）
                try:
                    fallback_on.add(int(item))
                except ValueError:
                    # 保留字符串（特殊条件如 timeout, connection_error）
                    fallback_on.add(item.lower())

        # 创建规则对象
        rule = ModelRoutingRule(
            model=model_name.lower(),
            backend_chain=backend_chain,
            fallback_on=fallback_on,
            enabled=enabled,
        )

        rules[model_name.lower()] = rule

    return rules


# 全局缓存：避免重复加载配置
_model_routing_cache: Dict[str, ModelRoutingRule] = None


def get_model_routing_rule(model: str, config_path: str = None) -> ModelRoutingRule:
    """
    获取指定模型的路由规则

    Args:
        model: 模型名称
        config_path: 配置文件路径（可选）

    Returns:
        模型路由规则，如果不存在则返回 None
    
    作者: 浮浮酱 (Claude Sonnet 4.5)
    更新: 2026-01-24 - 添加模型名称映射，支持 Cursor 格式
    """
    global _model_routing_cache

    if _model_routing_cache is None:
        _model_routing_cache = load_model_routing_config(config_path)

    # ✅ [FIX 2026-01-24] 先进行模型名称映射（Cursor 格式 -> Antigravity 格式）
    # 例如：claude-4.5-opus-high-thinking -> claude-opus-4-5-thinking
    try:
        from akarins_gateway.converters.model_config import model_mapping
        mapped_model = model_mapping(model)
        log.debug(f"[GATEWAY] Model mapping: '{model}' -> '{mapped_model}'", tag="GATEWAY")
    except ImportError:
        log.warning("[GATEWAY] Cannot import model_mapping, using original model name", tag="GATEWAY")
        mapped_model = model

    # 规范化模型名称
    model_lower = mapped_model.lower()

    # 1. 精确匹配（原样）
    rule = _model_routing_cache.get(model_lower)
    if rule:
        log.debug(f"[GATEWAY] Found exact routing rule for '{model_lower}'", tag="GATEWAY")
        return rule

    # 2. 基础模糊匹配：移除 -thinking/-extended/-preview/-latest 后缀和日期后缀
    normalized = re.sub(r'-(thinking|extended|preview|latest)$', '', model_lower)
    normalized = re.sub(r'-\d{8}$', '', normalized)

    rule = _model_routing_cache.get(normalized)
    if rule:
        log.debug(f"[GATEWAY] Found normalized routing rule for '{normalized}' (from '{model}')", tag="GATEWAY")
        return rule

    # 3. 针对 Claude 4.5 版本号写法的额外兼容：
    #    - 配置中通常使用 4.5（点号）
    #    - 实际模型名可能使用 4-5 或带日期后缀，如 claude-sonnet-4-5-20250929
    #    这里对 4.5/4-5 和 4.6/4-6 做双向归一化，以便命中配置键。
    claude_variants = set()
    claude_variants.add(normalized)
    claude_variants.add(normalized.replace("4-5", "4.5"))
    claude_variants.add(normalized.replace("4.5", "4-5"))
    claude_variants.add(normalized.replace("4-6", "4.6"))
    claude_variants.add(normalized.replace("4.6", "4-6"))

    for key in claude_variants:
        if key in _model_routing_cache:
            log.debug(f"[GATEWAY] Found variant routing rule '{key}' for '{model}'", tag="GATEWAY")
            return _model_routing_cache[key]

    log.info(f"[GATEWAY] No routing rule found for '{model}' (mapped: '{mapped_model}')", tag="GATEWAY")
    return None


def reload_model_routing_config(config_path: str = None) -> None:
    """
    重新加载所有路由配置（清除所有缓存）

    用于配置文件更新后刷新配置

    [REFACTOR 2026-02-21] 扩展：同时清除 Phase B 新增的所有缓存
    """
    global _model_routing_cache
    global _raw_yaml_cache
    global _backend_capabilities_cache
    global _default_routing_rules_cache, _default_routing_catch_all_cache
    global _cross_model_fallback_cache
    global _copilot_model_mapping_yaml_cache
    global _final_fallback_cache

    # 原子清除所有缓存
    # ⚠️ Thread Safety Note: 依赖 Python GIL + asyncio 单线程事件循环保证安全。
    # 如果 reload 从线程池调用，需添加 threading.Lock。[C1: code review]
    _raw_yaml_cache = None
    _backend_capabilities_cache = None
    _default_routing_rules_cache = None
    _default_routing_catch_all_cache = None
    _cross_model_fallback_cache = None
    _copilot_model_mapping_yaml_cache = None
    _final_fallback_cache = None

    # 重新加载 model_routing
    _model_routing_cache = load_model_routing_config(config_path)

    # [NEW 2026-03-10] Notify ModelRegistry to reload static config
    try:
        from .model_registry import get_model_registry
        registry = get_model_registry()
        if registry.initialized:
            registry.reload_static()
    except Exception:
        pass  # Registry not yet initialized or import failed — safe to ignore


# ==================== Phase B: Pattern Matching Engine ====================
# [REFACTOR 2026-02-21] fnmatch 模式匹配引擎，支持版本号双向归一化

# 模块级常量（避免热路径重复分配） [FIX I3: code review]
_VERSION_NORMALIZATIONS = (
    ("4-5", "4.5"),
    ("4-6", "4.6"),
    ("2-5", "2.5"),
    ("3-1", "3.1"),
    ("5-1", "5.1"),
    ("5-2", "5.2"),
)

_CLAUDE_VARIANTS = ("opus", "sonnet", "haiku")


def match_model_pattern(model: str, pattern: str) -> bool:
    """
    使用 fnmatch 模式匹配模型名称

    支持:
    - 4.5/4-5、4.6/4-6、2.5/2-5、3.1/3-1 等版本号双向归一化
    - Claude 模型名反转格式规范化（如 claude-4.6-opus → claude-opus-4.6）
    - fnmatch 为 C 实现，性能 <1μs/次

    Args:
        model: 模型名称（如 "claude-opus-4-6-thinking"）
        pattern: fnmatch 模式（如 "claude-*opus*4.6*"）

    Returns:
        是否匹配

    Examples:
        >>> match_model_pattern("claude-opus-4.6", "claude-*opus*4.6*")
        True
        >>> match_model_pattern("claude-4.6-opus", "claude-*opus*4.6*")
        True
        >>> match_model_pattern("claude-opus-4-6-thinking", "claude-*opus*4.6*")
        True

    作者: 浮浮酱 (Claude Opus 4.6)
    创建日期: 2026-02-21
    """
    m = model.lower()
    p = pattern.lower()

    def _try_match(model_str: str, pat: str) -> bool:
        """尝试直接匹配 + 版本号归一化匹配"""
        if fnmatch.fnmatch(model_str, pat):
            return True
        for dash_form, dot_form in _VERSION_NORMALIZATIONS:
            if dash_form in model_str:
                if fnmatch.fnmatch(model_str.replace(dash_form, dot_form), pat):
                    return True
            if dot_form in model_str:
                if fnmatch.fnmatch(model_str.replace(dot_form, dash_form), pat):
                    return True
        return False

    # 1. 直接匹配（含版本归一化）
    if _try_match(m, p):
        return True

    # 2. Claude 反转格式规范化
    #    将 claude-{version}-{variant} 重排为 claude-{variant}-{version}
    #    例如: claude-4.6-opus → claude-opus-4.6
    #          claude-4-5-sonnet-thinking → claude-sonnet-4-5-thinking
    if m.startswith("claude-"):
        parts = m.split("-")
        for variant in _CLAUDE_VARIANTS:
            if variant in parts:
                v_idx = parts.index(variant)
                if v_idx > 1:  # variant 不在 claude- 之后的第一个位置，说明是反转格式
                    # 将 variant 移到 claude- 之后
                    new_parts = [parts[0], parts[v_idx]] + parts[1:v_idx] + parts[v_idx + 1:]
                    canonical = "-".join(new_parts)
                    if _try_match(canonical, p):
                        return True
                break  # 只检查第一个匹配到的 variant

    return False


# ==================== Phase B: Raw YAML Config Helper ====================

_raw_yaml_cache = None


def _get_raw_yaml_config(config_path: str = None) -> dict:
    """
    加载原始 YAML 配置（带缓存）

    内部辅助函数，为所有 Phase B 加载函数提供统一的 YAML 读取入口。
    避免每个 load 函数各自打开文件，减少 I/O。
    """
    global _raw_yaml_cache

    if _raw_yaml_cache is not None:
        return _raw_yaml_cache

    # Auto-discover ZeroGravity port before any env var expansion
    _ensure_zerogravity_port_env()

    if config_path is None:
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "gateway.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        log.warning(f"[CONFIG_LOADER] gateway.yaml root has unexpected type: {type(raw).__name__}, expected dict")
        return {}

    _raw_yaml_cache = raw
    return raw


# ==================== Phase B: Section Loaders ====================

def load_backend_capabilities(config_path: str = None) -> Dict[str, BackendCapability]:
    """
    加载后端能力声明

    从 YAML 的 backend_capabilities 节解析后端支持的模型模式

    Returns:
        {backend_name: BackendCapability}
    """
    raw = _get_raw_yaml_config(config_path)
    section = raw.get("backend_capabilities", {})
    if not isinstance(section, dict):
        log.warning(f"[CONFIG_LOADER] backend_capabilities section has unexpected type: {type(section).__name__}, expected dict")
        return {}

    result = {}
    for backend_name, cap_data in section.items():
        if not isinstance(cap_data, dict):
            continue
        result[backend_name] = BackendCapability(
            include_patterns=tuple(cap_data.get("include_patterns", [])),
            exclude_patterns=tuple(cap_data.get("exclude_patterns", [])),
        )
    return result


def load_default_routing(config_path: str = None) -> Tuple[List[DefaultRoutingRule], Optional[DefaultRoutingRule]]:
    """
    加载默认路由链配置

    从 YAML 的 default_routing 节解析模式匹配路由规则

    Returns:
        (rules, catch_all) 元组
    """
    raw = _get_raw_yaml_config(config_path)
    section = raw.get("default_routing", {})
    if not isinstance(section, dict):
        log.warning(f"[CONFIG_LOADER] default_routing section has unexpected type: {type(section).__name__}, expected dict")
        return [], None

    def _parse_chain(chain_data: list) -> Tuple[DefaultRoutingEntry, ...]:
        """解析路由链条目"""
        chain = []
        for entry_data in (chain_data or []):
            if isinstance(entry_data, dict):
                chain.append(DefaultRoutingEntry(backend=entry_data.get("backend", "")))
            elif isinstance(entry_data, str):
                chain.append(DefaultRoutingEntry(backend=entry_data))
        return tuple(chain)

    def _parse_fallback_on(fallback_data: list) -> frozenset:
        """解析降级条件"""
        result = set()
        for item in (fallback_data or []):
            if isinstance(item, int):
                result.add(item)
            elif isinstance(item, str):
                result.add(item)
        return frozenset(result)

    # 解析 rules
    rules = []
    for rule_data in section.get("rules", []):
        if not isinstance(rule_data, dict):
            continue
        rules.append(DefaultRoutingRule(
            pattern=rule_data.get("pattern", ""),
            chain=_parse_chain(rule_data.get("chain", [])),
            fallback_on=_parse_fallback_on(rule_data.get("fallback_on", [])),
        ))

    # 解析 catch_all
    catch_all = None
    catch_all_data = section.get("catch_all")
    if isinstance(catch_all_data, dict):
        catch_all = DefaultRoutingRule(
            pattern="*",
            chain=_parse_chain(catch_all_data.get("chain", [])),
            fallback_on=_parse_fallback_on(catch_all_data.get("fallback_on", [])),
        )

    return rules, catch_all


def load_cross_model_fallback(config_path: str = None) -> List[CrossModelFallbackRule]:
    """
    加载跨模型降级配置

    从 YAML 的 cross_model_fallback 节解析 Claude→Gemini 等降级规则
    """
    raw = _get_raw_yaml_config(config_path)
    section = raw.get("cross_model_fallback", {})
    if not isinstance(section, dict) or not section.get("enabled", True):
        if not isinstance(section, dict):
            log.warning(f"[CONFIG_LOADER] cross_model_fallback section has unexpected type: {type(section).__name__}, expected dict")
        return []

    rules = []
    for rule_data in section.get("rules", []):
        if not isinstance(rule_data, dict):
            continue
        rules.append(CrossModelFallbackRule(
            pattern=rule_data.get("pattern", ""),
            fallback_model=rule_data.get("fallback_model", ""),
            backend=rule_data.get("backend", ""),
        ))
    return rules


def load_copilot_model_mapping(config_path: str = None) -> Dict[str, str]:
    """
    加载 Copilot 模型名映射

    从 YAML 的 copilot_model_mapping 节解析显式名称映射
    """
    raw = _get_raw_yaml_config(config_path)
    section = raw.get("copilot_model_mapping", {})
    if not isinstance(section, dict):
        log.warning(f"[CONFIG_LOADER] copilot_model_mapping section has unexpected type: {type(section).__name__}, expected dict")
        return {}
    return {str(k).lower(): str(v) for k, v in section.items()}


def load_final_fallback(config_path: str = None) -> Optional[FinalFallbackConfig]:
    """
    加载最终兜底配置

    从 YAML 的 final_fallback 节解析 Copilot 最终兜底设置
    """
    raw = _get_raw_yaml_config(config_path)
    section = raw.get("final_fallback", {})
    if not isinstance(section, dict):
        log.warning(f"[CONFIG_LOADER] final_fallback section has unexpected type: {type(section).__name__}, expected dict")
        return None

    return FinalFallbackConfig(
        enabled=section.get("enabled", True),
        backend=section.get("backend", "copilot"),
        respect_circuit_breaker=section.get("respect_circuit_breaker", True),
    )


# ==================== Phase B: Global Caches ====================

_backend_capabilities_cache: Optional[Dict[str, BackendCapability]] = None
_default_routing_rules_cache: Optional[List[DefaultRoutingRule]] = None
_default_routing_catch_all_cache: Optional[DefaultRoutingRule] = None
_cross_model_fallback_cache: Optional[List[CrossModelFallbackRule]] = None
_copilot_model_mapping_yaml_cache: Optional[Dict[str, str]] = None
_final_fallback_cache: Optional[FinalFallbackConfig] = None


# ==================== Phase B: Getter Functions ====================

def get_default_routing_rule(model: str) -> Optional[DefaultRoutingRule]:
    """
    根据模型名获取匹配的默认路由规则

    按规则顺序匹配，第一个命中的规则生效。
    注意：model_routing 显式规则优先级高于 default_routing，
    调用者应先检查 get_model_routing_rule()。

    Args:
        model: 模型名称

    Returns:
        匹配的路由规则，如果无匹配则返回 None
    """
    global _default_routing_rules_cache, _default_routing_catch_all_cache

    if _default_routing_rules_cache is None:
        _default_routing_rules_cache, _default_routing_catch_all_cache = load_default_routing()

    for rule in _default_routing_rules_cache:
        if match_model_pattern(model, rule.pattern):
            return rule

    return None


def get_catch_all_routing() -> Optional[DefaultRoutingRule]:
    """
    获取 catch_all 兜底路由规则

    Returns:
        catch_all 规则，如果未配置则返回 None
    """
    global _default_routing_rules_cache, _default_routing_catch_all_cache

    if _default_routing_rules_cache is None:
        _default_routing_rules_cache, _default_routing_catch_all_cache = load_default_routing()

    return _default_routing_catch_all_cache


def is_backend_capable(backend: str, model: str) -> bool:
    """
    检查指定后端是否支持该模型（基于 YAML backend_capabilities 配置）

    使用 exclude_patterns 优先于 include_patterns 的策略：
    1. 如果匹配任一 exclude_pattern → 不支持
    2. 如果匹配任一 include_pattern → 支持
    3. 都不匹配 → 不支持

    Args:
        backend: 后端名称（如 "gcli2api-antigravity"）
        model: 模型名称（如 "claude-opus-4.6"）

    Returns:
        是否支持
    """
    global _backend_capabilities_cache

    if _backend_capabilities_cache is None:
        _backend_capabilities_cache = load_backend_capabilities()

    cap = _backend_capabilities_cache.get(backend)
    if cap is None:
        # Backend not defined in backend_capabilities → assume supports everything (backward-compatible)
        return True

    # 检查 exclude_patterns（优先于 include）
    for pattern in cap.exclude_patterns:
        if match_model_pattern(model, pattern):
            return False

    # 检查 include_patterns
    for pattern in cap.include_patterns:
        if match_model_pattern(model, pattern):
            return True

    return False


def get_cross_model_fallback(model: str) -> Optional[CrossModelFallbackRule]:
    """
    获取跨模型降级规则

    按规则顺序匹配，第一个命中的规则生效

    Args:
        model: 当前失败的模型名称

    Returns:
        匹配的降级规则，包含 fallback_model 和 backend
    """
    global _cross_model_fallback_cache

    if _cross_model_fallback_cache is None:
        _cross_model_fallback_cache = load_cross_model_fallback()

    for rule in _cross_model_fallback_cache:
        if match_model_pattern(model, rule.pattern):
            return rule

    return None


def get_copilot_model_mapping_yaml() -> Dict[str, str]:
    """
    获取 YAML 中的 Copilot 模型名映射（显式映射部分）

    注意：这只是显式映射，模糊推断逻辑仍在 config.py 的 map_model_for_copilot() 中

    Returns:
        {原始模型名: Copilot 模型名}
    """
    global _copilot_model_mapping_yaml_cache

    if _copilot_model_mapping_yaml_cache is None:
        _copilot_model_mapping_yaml_cache = load_copilot_model_mapping()

    return _copilot_model_mapping_yaml_cache


def get_final_fallback() -> Optional[FinalFallbackConfig]:
    """
    获取最终兜底配置

    Returns:
        FinalFallbackConfig 对象，如果未配置则返回 None
    """
    global _final_fallback_cache

    if _final_fallback_cache is None:
        _final_fallback_cache = load_final_fallback()

    return _final_fallback_cache


def get_backend_config(backend_name: str, config_path: str = None) -> BackendConfig:
    """
    获取指定后端的配置

    Args:
        backend_name: 后端名称
        config_path: 配置文件路径（可选）

    Returns:
        后端配置对象

    Raises:
        KeyError: 后端不存在

    Examples:
        >>> config = get_backend_config("gcli2api-antigravity")
        >>> print(config.priority)
        1
    """
    configs = load_gateway_config(config_path)
    if backend_name not in configs:
        raise KeyError(f"后端 '{backend_name}' 不存在于配置文件中")
    return configs[backend_name]


def list_enabled_backends(config_path: str = None) -> List[str]:
    """
    列出所有启用的后端名称

    Args:
        config_path: 配置文件路径（可选）

    Returns:
        启用的后端名称列表（按优先级排序）

    Examples:
        >>> backends = list_enabled_backends()
        >>> print(backends)
        ['antigravity', 'copilot']
    """
    configs = load_gateway_config(config_path)
    enabled = [
        (name, config.priority)
        for name, config in configs.items()
        if config.enabled
    ]
    # 按优先级排序
    enabled.sort(key=lambda x: x[1])
    return [name for name, _ in enabled]


if __name__ == "__main__":
    # 测试代码
    try:
        configs = load_gateway_config()
        print("成功加载配置:")
        for name, config in configs.items():
            print(f"\n后端: {name}")
            print(f"  - 启用: {config.enabled}")
            print(f"  - 优先级: {config.priority}")
            print(f"  - URL: {config.base_url}")
            print(f"  - 模型: {config.models}")
            print(f"  - 超时: {config.timeout}s")
            if hasattr(config, "stream_timeout"):
                print(f"  - 流式超时: {config.stream_timeout}s")
            print(f"  - 最大重试: {config.max_retries}")

        print("\n启用的后端（按优先级）:")
        for backend in list_enabled_backends():
            print(f"  - {backend}")

    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
