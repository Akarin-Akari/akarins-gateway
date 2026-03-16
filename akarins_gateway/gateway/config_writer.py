"""
Gateway Config Writer

Uses ruamel.yaml for round-trip YAML editing that preserves comments.
Supports atomic writes with backup.

[FIX 2026-03-14] Windows compatibility:
- Close mkstemp fd immediately to avoid file locking
- Fall back to shutil.copy2 + unlink when Path.replace() fails (WinError 32)
- Clean up orphaned .yaml.tmp files

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-03-14
"""

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from akarins_gateway.core.log import log

__all__ = ["save_gateway_config", "get_config_path"]

# Concurrency lock for config writes
_write_lock = asyncio.Lock()

_IS_WINDOWS = sys.platform == "win32"


def get_config_path() -> Path:
    """Get the gateway.yaml config file path."""
    project_root = Path(__file__).parent.parent.parent
    return project_root / "config" / "gateway.yaml"


async def save_gateway_config(
    backends: Dict[str, Dict[str, Any]],
    model_routing: Optional[Dict[str, Any]] = None,
    cross_model_fallback: Optional[Dict[str, Any]] = None,
    default_routing: Optional[Dict[str, Any]] = None,
    final_fallback: Optional[Dict[str, Any]] = None,
    backend_capabilities: Optional[Dict[str, Any]] = None,
    copilot_model_mapping: Optional[Dict[str, str]] = None,
    runtime_flags: Optional[Dict[str, bool]] = None,
) -> Path:
    """
    Save current configuration to gateway.yaml.

    Uses ruamel.yaml to preserve comments and formatting.
    Creates a backup before writing.

    Args:
        backends: The BACKENDS dict from config.py
        model_routing: MODEL_ROUTING dict (ModelRoutingRule objects)
        cross_model_fallback: Cross-model fallback rules
        default_routing: Default routing rules + catch_all
        final_fallback: Final fallback config
        backend_capabilities: Backend capability declarations
        copilot_model_mapping: Copilot model name mapping

    Returns:
        Path to the backup file

    Raises:
        RuntimeError: If save fails
    """
    async with _write_lock:
        return await asyncio.get_event_loop().run_in_executor(
            None, _sync_save, backends, model_routing,
            cross_model_fallback, default_routing, final_fallback,
            backend_capabilities, copilot_model_mapping, runtime_flags,
        )


def _sync_save(
    backends: Dict[str, Dict[str, Any]],
    model_routing: Optional[Dict[str, Any]] = None,
    cross_model_fallback: Optional[Dict[str, Any]] = None,
    default_routing: Optional[Dict[str, Any]] = None,
    final_fallback: Optional[Dict[str, Any]] = None,
    backend_capabilities: Optional[Dict[str, Any]] = None,
    copilot_model_mapping: Optional[Dict[str, str]] = None,
    runtime_flags: Optional[Dict[str, bool]] = None,
) -> Path:
    """Synchronous save implementation."""
    config_path = get_config_path()

    if not config_path.exists():
        raise RuntimeError(f"Config file not found: {config_path}")

    # Clean up any orphaned tmp files from previous failed saves
    _cleanup_tmp_files(config_path.parent)

    extra_sections = {
        "model_routing": model_routing,
        "cross_model_fallback": cross_model_fallback,
        "default_routing": default_routing,
        "final_fallback": final_fallback,
        "backend_capabilities": backend_capabilities,
        "copilot_model_mapping": copilot_model_mapping,
        "runtime_flags": runtime_flags,
    }

    # Try ruamel.yaml first (preserves comments), fall back to PyYAML
    try:
        return _save_with_ruamel(config_path, backends, extra_sections)
    except ImportError:
        log.warning("[CONFIG_WRITER] ruamel.yaml not available, falling back to PyYAML (comments will be lost)")
        return _save_with_pyyaml(config_path, backends, extra_sections)


def _update_yaml_backends(yaml_backends: dict, backends: Dict[str, Dict[str, Any]]) -> None:
    """Update backend nodes in parsed YAML data from in-memory BACKENDS dict.

    [FIX 2026-03-14] Changed from skip-if-missing to upsert:
    - Existing YAML backends: update in-place (enabled, priority, timeout, etc.)
    - New backends (panel-added): create new YAML entry with all fields
    """
    for key, config in backends.items():
        if key not in yaml_backends:
            # [FIX 2026-03-14] Upsert: create new backend entry in YAML
            yaml_backends[key] = {}
            backend_node = yaml_backends[key]
            # Write all essential fields for a new backend
            if config.get("name"):
                backend_node["name"] = config["name"]
            if config.get("base_url"):
                backend_node["base_url"] = config["base_url"]
            if config.get("api_format"):
                backend_node["api_format"] = config["api_format"]
            if config.get("api_keys"):
                backend_node["api_keys"] = config["api_keys"]
            if config.get("supported_models"):
                backend_node["models"] = config["supported_models"]

        backend_node = yaml_backends[key]

        # Update enabled
        if "enabled" in config:
            backend_node["enabled"] = config["enabled"]

        # Update priority
        if "priority" in config:
            backend_node["priority"] = config["priority"]

        # Update base_url (only if YAML value is not an env var reference)
        base_url = config.get("base_url", "")
        if base_url and not str(backend_node.get("base_url", "")).startswith("${"):
            backend_node["base_url"] = base_url

        # Update timeout fields
        if "timeout" in config:
            backend_node["timeout"] = config["timeout"]
        if "stream_timeout" in config:
            backend_node["stream_timeout"] = config["stream_timeout"]
        if "max_retries" in config:
            backend_node["max_retries"] = config["max_retries"]

        # [FIX 2026-03-17] Update API keys (support both list and single-key formats)
        if "api_keys" in config and config["api_keys"]:
            backend_node["api_keys"] = list(config["api_keys"])
            # Remove legacy single-key field to avoid ambiguity
            if "api_key" in backend_node:
                del backend_node["api_key"]
        elif "api_key" in config and config["api_key"]:
            backend_node["api_key"] = config["api_key"]


def _update_yaml_model_routing(yaml_data: dict, model_routing: Dict[str, Any]) -> None:
    """
    Update model_routing section in YAML from in-memory MODEL_ROUTING dict.

    Converts ModelRoutingRule objects to YAML-serializable format.
    """
    if not model_routing:
        return

    # Build new model_routing section from in-memory rules
    new_section = {}
    for model_name, rule in model_routing.items():
        # rule is a ModelRoutingRule dataclass — extract fields
        rule_data = {}

        # enabled field
        if hasattr(rule, 'enabled'):
            rule_data["enabled"] = rule.enabled

        # backend chain
        if hasattr(rule, 'backend_chain') and rule.backend_chain:
            backends_list = []
            for entry in rule.backend_chain:
                backends_list.append({
                    "backend": entry.backend,
                    "model": entry.model,
                })
            rule_data["backends"] = backends_list

        # fallback_on
        if hasattr(rule, 'fallback_on') and rule.fallback_on:
            rule_data["fallback_on"] = list(rule.fallback_on)

        new_section[model_name] = rule_data

    yaml_data["model_routing"] = new_section
    log.info(f"[CONFIG_WRITER] Updated model_routing: {len(new_section)} models")


def _update_yaml_cross_model_fallback(yaml_data: dict, cross_model_fallback: Dict[str, Any]) -> None:
    """
    Update cross_model_fallback section in YAML.

    Args:
        cross_model_fallback: Dict with 'enabled' bool and 'rules' list of dicts
    """
    if cross_model_fallback is None:
        return
    yaml_data["cross_model_fallback"] = cross_model_fallback
    rules_count = len(cross_model_fallback.get("rules", []))
    log.info(f"[CONFIG_WRITER] Updated cross_model_fallback: {rules_count} rules")


def _update_yaml_default_routing(yaml_data: dict, default_routing: Dict[str, Any]) -> None:
    """
    Update default_routing section in YAML.

    Args:
        default_routing: Dict with 'rules' list and optional 'catch_all' dict
    """
    if default_routing is None:
        return
    yaml_data["default_routing"] = default_routing
    rules_count = len(default_routing.get("rules", []))
    has_catch_all = "catch_all" in default_routing
    log.info(f"[CONFIG_WRITER] Updated default_routing: {rules_count} rules, catch_all={'yes' if has_catch_all else 'no'}")


def _update_yaml_final_fallback(yaml_data: dict, final_fallback: Dict[str, Any]) -> None:
    """
    Update final_fallback section in YAML.

    Args:
        final_fallback: Dict with 'enabled', 'backend', 'respect_circuit_breaker'
    """
    if final_fallback is None:
        return
    yaml_data["final_fallback"] = final_fallback
    log.info(f"[CONFIG_WRITER] Updated final_fallback: backend={final_fallback.get('backend', '?')}")


def _safe_write_to_target(tmp_path_str: str, config_path: Path) -> None:
    """
    Move tmp file content to target config file.

    Strategy:
    1. Try atomic Path.replace() (works on Linux, may fail on Windows)
    2. Fall back to shutil.copy2 + delete tmp (Windows WinError 32 workaround)
    3. Last resort: direct write to target
    """
    tmp_path = Path(tmp_path_str)
    try:
        tmp_path.replace(config_path)
        log.info(f"[CONFIG_WRITER] Config saved (atomic rename) to {config_path}")
        return
    except OSError as e:
        if not _IS_WINDOWS:
            raise
        log.warning(f"[CONFIG_WRITER] Atomic rename failed ({e}), using copy fallback")

    # Windows fallback: copy content over the target, then remove tmp
    try:
        shutil.copy2(tmp_path_str, str(config_path))
        log.info(f"[CONFIG_WRITER] Config saved (copy fallback) to {config_path}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _save_with_ruamel(
    config_path: Path,
    backends: Dict[str, Dict[str, Any]],
    extra_sections: Optional[Dict[str, Any]] = None,
) -> Path:
    """Save using ruamel.yaml (preserves comments)."""
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True

    # Read existing config (preserves comments)
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.load(f)

    if data is None:
        data = {}

    # Update backend configs in-place
    if "backends" not in data:
        data["backends"] = {}
    _update_yaml_backends(data["backends"], backends)

    # [FIX 2026-03-14] Update additional sections (model_routing, cross_model_fallback, etc.)
    if extra_sections:
        if extra_sections.get("model_routing"):
            _update_yaml_model_routing(data, extra_sections["model_routing"])
        if extra_sections.get("cross_model_fallback") is not None:
            _update_yaml_cross_model_fallback(data, extra_sections["cross_model_fallback"])
        if extra_sections.get("default_routing") is not None:
            _update_yaml_default_routing(data, extra_sections["default_routing"])
        if extra_sections.get("final_fallback") is not None:
            _update_yaml_final_fallback(data, extra_sections["final_fallback"])
        # [P2 2026-03-14] Raw dict passthrough (no transformation needed)
        if extra_sections.get("backend_capabilities") is not None:
            data["backend_capabilities"] = extra_sections["backend_capabilities"]
        if extra_sections.get("copilot_model_mapping") is not None:
            data["copilot_model_mapping"] = extra_sections["copilot_model_mapping"]
        # [REFACTOR 2026-03-14] Runtime flags passthrough
        if extra_sections.get("runtime_flags") is not None:
            data["runtime_flags"] = extra_sections["runtime_flags"]

    # Create backup
    backup_path = _create_backup(config_path)

    # Write to temp file first
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent, suffix=".yaml.tmp"
    )
    # [FIX 2026-03-14] Close the fd immediately — mkstemp returns an open fd
    # which prevents rename/copy on Windows
    os.close(tmp_fd)

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        _safe_write_to_target(tmp_path, config_path)
    except Exception:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return backup_path


def _save_with_pyyaml(
    config_path: Path,
    backends: Dict[str, Dict[str, Any]],
    extra_sections: Optional[Dict[str, Any]] = None,
) -> Path:
    """Fallback: save using PyYAML (loses comments)."""
    import yaml

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if "backends" not in data:
        data["backends"] = {}

    # [FIX 2026-03-14] Use shared upsert logic (same as ruamel path)
    _update_yaml_backends(data["backends"], backends)

    # [FIX 2026-03-14] Update additional sections
    if extra_sections:
        if extra_sections.get("model_routing"):
            _update_yaml_model_routing(data, extra_sections["model_routing"])
        if extra_sections.get("cross_model_fallback") is not None:
            _update_yaml_cross_model_fallback(data, extra_sections["cross_model_fallback"])
        if extra_sections.get("default_routing") is not None:
            _update_yaml_default_routing(data, extra_sections["default_routing"])
        if extra_sections.get("final_fallback") is not None:
            _update_yaml_final_fallback(data, extra_sections["final_fallback"])
        # [P2 2026-03-14] Raw dict passthrough
        if extra_sections.get("backend_capabilities") is not None:
            data["backend_capabilities"] = extra_sections["backend_capabilities"]
        if extra_sections.get("copilot_model_mapping") is not None:
            data["copilot_model_mapping"] = extra_sections["copilot_model_mapping"]
        # [REFACTOR 2026-03-14] Runtime flags passthrough
        if extra_sections.get("runtime_flags") is not None:
            data["runtime_flags"] = extra_sections["runtime_flags"]

    backup_path = _create_backup(config_path)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent, suffix=".yaml.tmp"
    )
    os.close(tmp_fd)  # [FIX] Close fd immediately

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _safe_write_to_target(tmp_path, config_path)
    except Exception:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return backup_path


def _create_backup(config_path: Path) -> Path:
    """Create a timestamped backup of the config file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.with_name(f"gateway.yaml.backup.{timestamp}")
    shutil.copy2(config_path, backup_path)
    log.info(f"[CONFIG_WRITER] Backup created: {backup_path}")
    return backup_path


def _cleanup_tmp_files(directory: Path) -> None:
    """Remove orphaned .yaml.tmp files from previous failed saves."""
    try:
        for tmp_file in directory.glob("*.yaml.tmp"):
            try:
                tmp_file.unlink()
                log.info(f"[CONFIG_WRITER] Cleaned up orphaned tmp: {tmp_file.name}")
            except OSError:
                pass  # File still locked, skip
    except Exception:
        pass
