"""
Gateway Management Panel REST API

Provides endpoints for the embedded admin dashboard:
- Backend management (list, toggle, reorder, SCID, API keys, circuit breaker)
- Model routing (list, update fallback chain)
- Health monitoring (backend health, request stats)
- Config persistence (save to gateway.yaml)

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-03-14
"""

import os
import time
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel

from akarins_gateway.core.auth import verify_panel_token
from akarins_gateway.core.log import log
from akarins_gateway.gateway.config import BACKENDS
from akarins_gateway.gateway.circuit_breaker import (
    get_circuit_breaker,
    get_all_circuit_breakers,
)

router = APIRouter(prefix="/api/panel", tags=["Panel"])

__all__ = ["router"]


# ==================== Pydantic Models ====================

class BackendUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    timeout: Optional[float] = None
    stream_timeout: Optional[float] = None
    max_retries: Optional[int] = None


class ReorderRequest(BaseModel):
    order: List[str]  # List of backend keys in new priority order


class AddKeyRequest(BaseModel):
    key: str


class RoutingUpdateRequest(BaseModel):
    backend_chain: List[Dict[str, str]]  # [{"backend": "...", "model": "..."}]
    fallback_on: Optional[List] = None


class RoutingReorderRequest(BaseModel):
    order: List[Dict[str, str]]  # [{"backend": "...", "model": "..."}]


class ClientFeatureUpdateRequest(BaseModel):
    feature: str   # sanitization, cross_pool_fallback, stateless, signature_recovery_only, scid
    enabled: bool


# [NEW 2026-03-14] Pydantic models for hidden-rule CRUD

class CrossModelFallbackUpdateRequest(BaseModel):
    enabled: bool = True
    rules: List[Dict[str, str]]  # [{"pattern": "...", "fallback_model": "...", "backend": "..."}]


class DefaultRoutingUpdateRequest(BaseModel):
    rules: List[Dict[str, Any]]  # [{"pattern": "...", "chain": [...], "fallback_on": [...]}]
    catch_all: Optional[Dict[str, Any]] = None  # {"chain": [...], "fallback_on": [...]}


class FinalFallbackUpdateRequest(BaseModel):
    enabled: bool = True
    backend: str = "copilot"
    respect_circuit_breaker: bool = True


class BackendCapabilitiesUpdateRequest(BaseModel):
    capabilities: Dict[str, Dict[str, List[str]]]  # {"backend": {"include_patterns": [...], "exclude_patterns": [...]}}


class CopilotModelMappingUpdateRequest(BaseModel):
    mapping: Dict[str, str]  # {"source_model": "target_model"}


# [REFACTOR 2026-03-14] Runtime flags (AnyRouter env var migration)
class RuntimeFlagsUpdateRequest(BaseModel):
    flags: Dict[str, bool]  # {"ANYROUTER_CURSOR_PROFILE_FALLBACK": true, ...}


# [NEW 2026-03-14] Add backend request
class AddBackendRequest(BaseModel):
    key: str
    name: str
    base_url: str
    priority: int = 99
    timeout: float = 60.0
    stream_timeout: float = 300.0
    max_retries: int = 2
    api_format: str = "openai"
    enabled: bool = True
    api_keys: List[str] = []


# ==================== Helper Functions ====================

def _mask_key(key: str) -> str:
    """Mask an API key for display: sk-***last4"""
    if not key or len(key) < 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def _get_backend_runtime_info(key: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Build backend info dict with runtime state."""
    cb = get_circuit_breaker(key)
    cb_status = cb.get_status()

    # Get API keys (masked)
    api_keys_raw = config.get("api_keys", [])
    if not api_keys_raw:
        # Single key format
        single_key = config.get("api_key", "")
        if single_key:
            api_keys_raw = [single_key]

    masked_keys = [_mask_key(k) for k in api_keys_raw]

    # Get supported models
    supported_models = config.get("supported_models", config.get("models", []))

    return {
        "key": key,
        "name": config.get("name", key),
        "base_url": config.get("base_url", ""),
        "priority": config.get("priority", 999),
        "enabled": config.get("enabled", True),
        "timeout": config.get("timeout", 60.0),
        "stream_timeout": config.get("stream_timeout", 300.0),
        "max_retries": config.get("max_retries", 2),
        "api_format": config.get("api_format", "openai"),
        "supported_models": supported_models if isinstance(supported_models, list) else [],
        "api_keys_masked": masked_keys,
        "api_keys_count": len(api_keys_raw),
        "scid_enabled": config.get("scid_enabled", False),
        "thinking_enabled": config.get("thinking_enabled", False),
        "circuit_breaker": {
            "state": cb_status["state"],
            "failure_count": cb_status["failure_count"],
            "total_failures": cb_status["total_failures"],
            "total_successes": cb_status["total_successes"],
            "remaining_timeout": cb_status["remaining_timeout"],
        },
    }


# ==================== Backend Management API ====================

@router.get("/backends")
async def list_backends(_token: str = Depends(verify_panel_token)):
    """Get all backend configurations with runtime state."""
    backends = []
    for key, config in BACKENDS.items():
        backends.append(_get_backend_runtime_info(key, config))

    # Sort by priority
    backends.sort(key=lambda b: b["priority"])

    return {"backends": backends, "timestamp": time.time()}


@router.post("/backends")
async def add_backend(
    req: AddBackendRequest,
    _token: str = Depends(verify_panel_token),
):
    """Create a new backend configuration."""
    if req.key in BACKENDS:
        raise HTTPException(status_code=409, detail=f"Backend '{req.key}' already exists")

    # Build the config dict matching existing BACKENDS structure
    new_config = {
        "name": req.name,
        "base_url": req.base_url,
        "priority": req.priority,
        "timeout": req.timeout,
        "stream_timeout": req.stream_timeout,
        "max_retries": req.max_retries,
        "enabled": req.enabled,
        "api_format": req.api_format,
        "api_keys": req.api_keys,
        "supported_models": [],
        "scid_enabled": False,
    }

    BACKENDS[req.key] = new_config

    # Initialize circuit breaker for the new backend
    get_circuit_breaker(req.key)

    log.info(f"Panel: Created new backend '{req.key}' ({req.name}) -> {req.base_url}")

    return _get_backend_runtime_info(req.key, new_config)


@router.put("/backends/{backend_key}")
async def update_backend(
    backend_key: str,
    req: BackendUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update backend configuration fields."""
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    config = BACKENDS[backend_key]
    updated_fields = []

    if req.enabled is not None:
        config["enabled"] = req.enabled
        updated_fields.append("enabled")
    if req.base_url is not None:
        config["base_url"] = req.base_url
        updated_fields.append("base_url")
    if req.timeout is not None:
        config["timeout"] = req.timeout
        updated_fields.append("timeout")
    if req.stream_timeout is not None:
        config["stream_timeout"] = req.stream_timeout
        updated_fields.append("stream_timeout")
    if req.max_retries is not None:
        config["max_retries"] = req.max_retries
        updated_fields.append("max_retries")

    log.info(f"[PANEL] Updated backend '{backend_key}': {updated_fields}")
    return {
        "backend": backend_key,
        "updated": updated_fields,
        "config": _get_backend_runtime_info(backend_key, config),
    }


@router.post("/backends/{backend_key}/toggle")
async def toggle_backend(
    backend_key: str,
    _token: str = Depends(verify_panel_token),
):
    """Toggle backend enabled/disabled."""
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    BACKENDS[backend_key]["enabled"] = not BACKENDS[backend_key].get("enabled", True)
    new_state = BACKENDS[backend_key]["enabled"]
    log.info(f"[PANEL] Toggled backend '{backend_key}' -> {'enabled' if new_state else 'disabled'}")

    return {"backend": backend_key, "enabled": new_state}


@router.post("/backends/reorder")
async def reorder_backends(
    req: ReorderRequest,
    _token: str = Depends(verify_panel_token),
):
    """Reorder backends by updating priority values based on drag order."""
    for idx, key in enumerate(req.order):
        if key in BACKENDS:
            BACKENDS[key]["priority"] = idx
    log.info(f"[PANEL] Reordered backends: {req.order}")
    return {"order": req.order, "success": True}


@router.post("/backends/{backend_key}/scid")
async def toggle_scid(
    backend_key: str,
    _token: str = Depends(verify_panel_token),
):
    """Toggle SCID feature for a backend."""
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    current = BACKENDS[backend_key].get("scid_enabled", False)
    BACKENDS[backend_key]["scid_enabled"] = not current
    new_state = BACKENDS[backend_key]["scid_enabled"]
    log.info(f"[PANEL] Toggled SCID for '{backend_key}' -> {new_state}")

    return {"backend": backend_key, "scid_enabled": new_state}


@router.get("/backends/{backend_key}/keys")
async def get_api_keys(
    backend_key: str,
    _token: str = Depends(verify_panel_token),
):
    """Get API keys for a backend (masked)."""
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    config = BACKENDS[backend_key]
    api_keys_raw = config.get("api_keys", [])
    if not api_keys_raw:
        single_key = config.get("api_key", "")
        if single_key:
            api_keys_raw = [single_key]

    return {
        "backend": backend_key,
        "keys": [{"index": i, "masked": _mask_key(k)} for i, k in enumerate(api_keys_raw)],
    }


@router.post("/backends/{backend_key}/keys")
async def add_api_key(
    backend_key: str,
    req: AddKeyRequest,
    _token: str = Depends(verify_panel_token),
):
    """Add an API key to a backend."""
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    config = BACKENDS[backend_key]
    if "api_keys" not in config:
        # Migrate from single key to list
        existing = config.pop("api_key", "")
        config["api_keys"] = [existing] if existing else []

    config["api_keys"].append(req.key)
    log.info(f"[PANEL] Added API key to '{backend_key}' (total: {len(config['api_keys'])})")

    return {"backend": backend_key, "keys_count": len(config["api_keys"])}


@router.delete("/backends/{backend_key}/keys/{index}")
async def delete_api_key(
    backend_key: str,
    index: int,
    _token: str = Depends(verify_panel_token),
):
    """Delete an API key from a backend by index."""
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    config = BACKENDS[backend_key]
    api_keys = config.get("api_keys", [])

    if index < 0 or index >= len(api_keys):
        raise HTTPException(status_code=400, detail=f"Key index {index} out of range")

    removed = api_keys.pop(index)
    log.info(f"[PANEL] Removed API key from '{backend_key}' at index {index}")

    return {"backend": backend_key, "removed_masked": _mask_key(removed), "keys_count": len(api_keys)}


@router.post("/backends/{backend_key}/circuit-breaker/reset")
async def reset_circuit_breaker(
    backend_key: str,
    _token: str = Depends(verify_panel_token),
):
    """Reset circuit breaker for a backend."""
    cb = get_circuit_breaker(backend_key)
    cb.reset()
    log.info(f"[PANEL] Reset circuit breaker for '{backend_key}'")

    return {"backend": backend_key, "circuit_breaker": cb.get_status()}


# [NEW 2026-03-17] Fetch models from a backend's /models endpoint
@router.post("/backends/{backend_key}/fetch-models")
async def fetch_backend_models(
    backend_key: str,
    _token: str = Depends(verify_panel_token),
):
    """
    Fetch the real model list from a backend's /models endpoint.
    Updates supported_models in-memory and returns the model list.
    """
    if backend_key not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"Backend '{backend_key}' not found")

    config = BACKENDS[backend_key]
    if not config.get("enabled", True):
        raise HTTPException(status_code=400, detail=f"Backend '{backend_key}' is disabled")

    # Get base_url
    from akarins_gateway.gateway.routing import get_backend_base_url
    base_url = get_backend_base_url(config)
    if not base_url:
        raise HTTPException(status_code=400, detail=f"Backend '{backend_key}' has no base_url")

    # Skip anthropic-format backends — they don't have /models
    if config.get("api_format") == "anthropic":
        raise HTTPException(
            status_code=400,
            detail=f"Backend '{backend_key}' uses anthropic format (no /models endpoint)",
        )

    # Build auth headers
    api_key = config.get("api_key") or ""
    if not api_key:
        api_keys = config.get("api_keys")
        if api_keys and isinstance(api_keys, list) and len(api_keys) > 0:
            api_key = api_keys[0]
    if not api_key:
        api_key = "dummy"

    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        from akarins_gateway.core.httpx_client import http_client

        async with http_client.get_client(timeout=15.0) as client:
            response = await client.get(f"{base_url}/models", headers=headers)

            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Backend returned HTTP {response.status_code}",
                )

            data = response.json()
            model_ids = []

            for item in data.get("data", []):
                if isinstance(item, dict):
                    model_id = item.get("id")
                    if model_id and isinstance(model_id, str):
                        model_ids.append(model_id)
                elif isinstance(item, str) and item:
                    model_ids.append(item)

            if not model_ids:
                raise HTTPException(
                    status_code=502,
                    detail="Backend returned empty model list",
                )

            # Sort for consistent display
            model_ids = sorted(set(model_ids))

            # Update in-memory config
            config["supported_models"] = model_ids
            config["models"] = model_ids

            log.info(
                f"[PANEL] Fetched {len(model_ids)} models from '{backend_key}'"
            )

            return {
                "backend": backend_key,
                "models": model_ids,
                "count": len(model_ids),
            }

    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"[PANEL] Failed to fetch models from '{backend_key}': {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to backend: {e}",
        )


# ==================== Model Routing API ====================

@router.get("/routing")
async def get_routing(_token: str = Depends(verify_panel_token)):
    """Get all model routing rules from gateway.yaml config."""
    from akarins_gateway.gateway.config_loader import load_model_routing_config

    rules = load_model_routing_config()
    result = {}

    for model_name, rule in rules.items():
        result[model_name] = {
            "model": rule.model,
            "enabled": rule.enabled,
            "backend_chain": [
                {"backend": entry.backend, "model": entry.model}
                for entry in rule.backend_chain
            ],
            "fallback_on": list(rule.fallback_on),
        }

    return {"routing": result, "timestamp": time.time()}


@router.put("/routing/{model}")
async def update_routing(
    model: str,
    req: RoutingUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update routing rule for a model (in-memory only, use /config/save to persist)."""
    from akarins_gateway.gateway.config_loader import (
        BackendEntry,
        ModelRoutingRule,
        _model_routing_cache,
    )
    from akarins_gateway.gateway.config import MODEL_ROUTING

    model_lower = model.lower()

    # Build new backend chain
    new_chain = []
    for entry in req.backend_chain:
        new_chain.append(BackendEntry(
            backend=entry.get("backend", ""),
            model=entry.get("model", model_lower),
        ))

    # Build fallback_on set
    new_fallback_on = set()
    if req.fallback_on:
        for item in req.fallback_on:
            if isinstance(item, int):
                new_fallback_on.add(item)
            elif isinstance(item, str):
                try:
                    new_fallback_on.add(int(item))
                except ValueError:
                    new_fallback_on.add(item.lower())

    # Create or update rule — preserve existing enabled state
    existing_enabled = True
    if _model_routing_cache and model_lower in _model_routing_cache:
        existing_enabled = _model_routing_cache[model_lower].enabled
    elif model_lower in MODEL_ROUTING:
        existing_enabled = MODEL_ROUTING[model_lower].enabled

    new_rule = ModelRoutingRule(
        model=model_lower,
        backend_chain=new_chain,
        fallback_on=new_fallback_on,
        enabled=existing_enabled,
    )

    # Update both caches
    # [FIX 2026-03-16] Codex #10: Initialize cache on cold-start so Panel writes always take effect
    if _model_routing_cache is None:
        from akarins_gateway.gateway import config_loader as _cl
        _cl._model_routing_cache = {}
        _cl._model_routing_cache[model_lower] = new_rule
    else:
        _model_routing_cache[model_lower] = new_rule
    MODEL_ROUTING[model_lower] = new_rule

    log.info(f"[PANEL] Updated routing for '{model_lower}': {[e.backend for e in new_chain]}")

    return {
        "model": model_lower,
        "backend_chain": [{"backend": e.backend, "model": e.model} for e in new_chain],
        "fallback_on": list(new_fallback_on),
    }


@router.post("/routing/{model}/reorder")
async def reorder_routing(
    model: str,
    req: RoutingReorderRequest,
    _token: str = Depends(verify_panel_token),
):
    """Reorder fallback chain for a model."""
    # Preserve existing fallback_on when only reordering
    from akarins_gateway.gateway.config_loader import _model_routing_cache
    from akarins_gateway.gateway.config import MODEL_ROUTING

    model_lower = model.lower()
    existing_fallback_on = None
    if _model_routing_cache and model_lower in _model_routing_cache:
        existing_fallback_on = list(_model_routing_cache[model_lower].fallback_on)
    elif model_lower in MODEL_ROUTING:
        existing_fallback_on = list(MODEL_ROUTING[model_lower].fallback_on)

    update_req = RoutingUpdateRequest(
        backend_chain=req.order,
        fallback_on=existing_fallback_on,
    )
    return await update_routing(model, update_req, _token)


# ==================== Health Monitoring API ====================

@router.get("/health")
async def get_health(_token: str = Depends(verify_panel_token)):
    """Get all backend health status with circuit breaker info."""
    from akarins_gateway.gateway.routing import check_backend_health

    health_data = {}
    for key, config in BACKENDS.items():
        is_healthy = await check_backend_health(key)
        cb = get_circuit_breaker(key)
        cb_status = cb.get_status()

        health_data[key] = {
            "name": config.get("name", key),
            "enabled": config.get("enabled", True),
            "healthy": is_healthy,
            "circuit_breaker": cb_status,
        }

    total = len(health_data)
    enabled = sum(1 for v in health_data.values() if v["enabled"])
    healthy = sum(1 for v in health_data.values() if v["healthy"] and v["enabled"])

    return {
        "summary": {
            "total": total,
            "enabled": enabled,
            "healthy": healthy,
        },
        "backends": health_data,
        "timestamp": time.time(),
    }


@router.get("/stats")
async def get_stats(_token: str = Depends(verify_panel_token)):
    """Get request statistics for all backends."""
    from akarins_gateway.gateway.stats_collector import get_stats_collector

    collector = get_stats_collector()
    return {
        "stats": collector.get_all_stats(),
        "timestamp": time.time(),
    }


# ==================== Config Persistence API ====================

@router.post("/config/save")
async def save_config(_token: str = Depends(verify_panel_token)):
    """Save current in-memory config to gateway.yaml (preserves comments).

    [FIX 2026-03-14] Now saves ALL configurable sections:
    - backends (always)
    - model_routing (P0 fix: was previously lost on restart)
    - cross_model_fallback, default_routing, final_fallback (P1: newly exposed)
    """
    from akarins_gateway.gateway.config_writer import save_gateway_config
    from akarins_gateway.gateway.config import MODEL_ROUTING
    from akarins_gateway.gateway.config_loader import _get_raw_yaml_config

    # Read hidden rules from raw YAML cache (updated by PUT endpoints)
    raw = _get_raw_yaml_config()
    cross_model_fb = raw.get("cross_model_fallback") if raw else None
    default_rt = raw.get("default_routing") if raw else None
    final_fb = raw.get("final_fallback") if raw else None
    backend_caps = raw.get("backend_capabilities") if raw else None
    copilot_mapping = raw.get("copilot_model_mapping") if raw else None
    runtime_fl = raw.get("runtime_flags") if raw else None

    try:
        backup_path = await save_gateway_config(
            backends=BACKENDS,
            model_routing=MODEL_ROUTING,
            cross_model_fallback=cross_model_fb,
            default_routing=default_rt,
            final_fallback=final_fb,
            backend_capabilities=backend_caps,
            copilot_model_mapping=copilot_mapping,
            runtime_flags=runtime_fl,
        )
        log.info(f"[PANEL] Config saved to gateway.yaml (backup: {backup_path})")
        return {"success": True, "backup": str(backup_path)}
    except Exception as e:
        log.error(f"[PANEL] Failed to save config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save config: {e}")


# ==================== Client Settings API ====================

@router.get("/clients")
async def get_client_settings(_token: str = Depends(verify_panel_token)):
    """Get all IDE/CLI client feature settings."""
    from akarins_gateway.ide_compat.client_detector import ClientTypeDetector

    return {
        "clients": ClientTypeDetector.get_all_client_settings(),
        "features": {
            "sanitization": "Message sanitization (clean up IDE-mangled thinking blocks)",
            "cross_pool_fallback": "Cross-pool fallback (allow fallback across backend pools)",
            "stateless": "Stateless mode (bypass SCID architecture, client manages own state)",
            "signature_recovery_only": "Signature recovery only (lightweight mode, skip full SCID)",
            "scid": "Full SCID (server-managed conversation state, signature caching)",
        },
        "timestamp": time.time(),
    }


@router.put("/clients/{client_type}")
async def update_client_feature(
    client_type: str,
    req: ClientFeatureUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Toggle a feature for a specific IDE/CLI client type."""
    from akarins_gateway.ide_compat.client_detector import ClientTypeDetector

    success = ClientTypeDetector.set_client_feature(client_type, req.feature, req.enabled)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid client type '{client_type}' or feature '{req.feature}'",
        )

    log.info(f"[PANEL] Client '{client_type}' feature '{req.feature}' -> {req.enabled}")
    return {
        "client": client_type,
        "feature": req.feature,
        "enabled": req.enabled,
    }


# [NEW 2026-03-17] Reset client settings to defaults
@router.post("/clients/reset-defaults")
async def reset_client_defaults(_token: str = Depends(verify_panel_token)):
    """Reset all client feature settings to their initial defaults."""
    from akarins_gateway.ide_compat.client_detector import ClientTypeDetector

    ClientTypeDetector.reset_to_defaults()
    log.info("[PANEL] Client settings reset to defaults")
    return {
        "clients": ClientTypeDetector.get_all_client_settings(),
        "message": "All client settings restored to defaults",
    }


# ==================== Cross-Model Fallback API ====================

@router.get("/cross-model-fallback")
async def get_cross_model_fallback_rules(_token: str = Depends(verify_panel_token)):
    """Get cross-model fallback rules from gateway.yaml config."""
    from akarins_gateway.gateway.config_loader import (
        load_cross_model_fallback,
        _get_raw_yaml_config,
    )

    raw = _get_raw_yaml_config()
    section = raw.get("cross_model_fallback", {})
    enabled = section.get("enabled", True) if isinstance(section, dict) else True

    rules = load_cross_model_fallback()
    return {
        "enabled": enabled,
        "rules": [
            {
                "pattern": r.pattern,
                "fallback_model": r.fallback_model,
                "backend": r.backend,
            }
            for r in rules
        ],
        "timestamp": time.time(),
    }


@router.put("/cross-model-fallback")
async def update_cross_model_fallback(
    req: CrossModelFallbackUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update cross-model fallback rules (in-memory + cache clear)."""
    from akarins_gateway.gateway.config_loader import (
        CrossModelFallbackRule,
        reload_model_routing_config,
        _get_raw_yaml_config,
    )

    # Build new rules
    new_rules = []
    for r in req.rules:
        new_rules.append(CrossModelFallbackRule(
            pattern=r.get("pattern", ""),
            fallback_model=r.get("fallback_model", ""),
            backend=r.get("backend", ""),
        ))

    # Update the YAML raw cache and internal cache
    import akarins_gateway.gateway.config_loader as cl
    cl._cross_model_fallback_cache = new_rules

    # Also update raw YAML cache for save_config
    raw = _get_raw_yaml_config()
    if raw is not None:
        raw["cross_model_fallback"] = {
            "enabled": req.enabled,
            "rules": req.rules,
        }

    log.info(f"[PANEL] Updated cross_model_fallback: {len(new_rules)} rules, enabled={req.enabled}")
    return {
        "enabled": req.enabled,
        "rules": req.rules,
    }


# ==================== Default Routing API ====================

@router.get("/default-routing")
async def get_default_routing_rules(_token: str = Depends(verify_panel_token)):
    """Get default routing rules (pattern-based) and catch_all from config."""
    from akarins_gateway.gateway.config_loader import _get_raw_yaml_config

    raw = _get_raw_yaml_config()
    section = raw.get("default_routing", {})
    if not isinstance(section, dict):
        section = {}

    return {
        "rules": section.get("rules", []),
        "catch_all": section.get("catch_all", None),
        "timestamp": time.time(),
    }


@router.put("/default-routing")
async def update_default_routing(
    req: DefaultRoutingUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update default routing rules (in-memory + cache clear)."""
    from akarins_gateway.gateway.config_loader import (
        reload_model_routing_config,
        _get_raw_yaml_config,
    )

    # Update raw YAML cache
    raw = _get_raw_yaml_config()
    new_section = {"rules": req.rules}
    if req.catch_all is not None:
        new_section["catch_all"] = req.catch_all
    if raw is not None:
        raw["default_routing"] = new_section

    # Clear parsed caches so they reload from updated raw
    import akarins_gateway.gateway.config_loader as cl
    cl._default_routing_rules_cache = None
    cl._default_routing_catch_all_cache = None

    log.info(f"[PANEL] Updated default_routing: {len(req.rules)} rules")
    return {
        "rules": req.rules,
        "catch_all": req.catch_all,
    }


# ==================== Final Fallback API ====================

@router.get("/final-fallback")
async def get_final_fallback_config(_token: str = Depends(verify_panel_token)):
    """Get final fallback configuration."""
    from akarins_gateway.gateway.config_loader import get_final_fallback

    fb = get_final_fallback()
    if fb is None:
        return {
            "enabled": False,
            "backend": "copilot",
            "respect_circuit_breaker": True,
            "timestamp": time.time(),
        }

    return {
        "enabled": fb.enabled,
        "backend": fb.backend,
        "respect_circuit_breaker": fb.respect_circuit_breaker,
        "timestamp": time.time(),
    }


@router.put("/final-fallback")
async def update_final_fallback(
    req: FinalFallbackUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update final fallback config (in-memory + cache clear)."""
    from akarins_gateway.gateway.config_loader import (
        FinalFallbackConfig,
        _get_raw_yaml_config,
    )
    import akarins_gateway.gateway.config_loader as cl

    # Update parsed cache
    cl._final_fallback_cache = FinalFallbackConfig(
        enabled=req.enabled,
        backend=req.backend,
        respect_circuit_breaker=req.respect_circuit_breaker,
    )

    # Update raw YAML cache
    raw = _get_raw_yaml_config()
    if raw is not None:
        raw["final_fallback"] = {
            "enabled": req.enabled,
            "backend": req.backend,
            "respect_circuit_breaker": req.respect_circuit_breaker,
        }

    log.info(f"[PANEL] Updated final_fallback: enabled={req.enabled}, backend={req.backend}")
    return {
        "enabled": req.enabled,
        "backend": req.backend,
        "respect_circuit_breaker": req.respect_circuit_breaker,
    }


# ==================== Backend Capabilities API ====================

@router.get("/backend-capabilities")
async def get_backend_capabilities(_token: str = Depends(verify_panel_token)):
    """Get backend capability declarations (include/exclude patterns)."""
    from akarins_gateway.gateway.config_loader import _get_raw_yaml_config

    raw = _get_raw_yaml_config()
    section = raw.get("backend_capabilities", {})
    if not isinstance(section, dict):
        section = {}

    return {
        "capabilities": section,
        "timestamp": time.time(),
    }


@router.put("/backend-capabilities")
async def update_backend_capabilities(
    req: BackendCapabilitiesUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update backend capability declarations (in-memory + cache clear)."""
    from akarins_gateway.gateway.config_loader import _get_raw_yaml_config
    import akarins_gateway.gateway.config_loader as cl

    raw = _get_raw_yaml_config()
    if raw is not None:
        raw["backend_capabilities"] = req.capabilities

    # Clear parsed cache
    cl._backend_capabilities_cache = None

    log.info(f"[PANEL] Updated backend_capabilities: {len(req.capabilities)} backends")
    return {"capabilities": req.capabilities}


# ==================== Copilot Model Mapping API ====================

@router.get("/copilot-model-mapping")
async def get_copilot_model_mapping(_token: str = Depends(verify_panel_token)):
    """Get copilot model name mapping."""
    from akarins_gateway.gateway.config_loader import _get_raw_yaml_config

    raw = _get_raw_yaml_config()
    section = raw.get("copilot_model_mapping", {})
    if not isinstance(section, dict):
        section = {}

    return {
        "mapping": section,
        "timestamp": time.time(),
    }


@router.put("/copilot-model-mapping")
async def update_copilot_model_mapping(
    req: CopilotModelMappingUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update copilot model name mapping (in-memory + cache clear)."""
    from akarins_gateway.gateway.config_loader import _get_raw_yaml_config
    import akarins_gateway.gateway.config_loader as cl

    raw = _get_raw_yaml_config()
    if raw is not None:
        raw["copilot_model_mapping"] = req.mapping

    # Clear parsed cache
    cl._copilot_model_mapping_yaml_cache = None

    log.info(f"[PANEL] Updated copilot_model_mapping: {len(req.mapping)} entries")
    return {"mapping": req.mapping}


# ==================== Runtime Flags API ====================
# [REFACTOR 2026-03-14] Expose AnyRouter env var switches to Panel UI

@router.get("/runtime-flags")
async def get_runtime_flags(_token: str = Depends(verify_panel_token)):
    """Get all runtime flags with effective values and defaults."""
    from akarins_gateway.gateway.config_loader import (
        load_runtime_flags,
        get_runtime_flag,
        RUNTIME_FLAG_DEFAULTS,
    )

    yaml_flags = load_runtime_flags()
    result = {}
    for name, default_val in RUNTIME_FLAG_DEFAULTS.items():
        result[name] = {
            "effective": get_runtime_flag(name, default_val),
            "yaml_value": yaml_flags.get(name),
            "env_value": os.environ.get(name),
            "default": default_val,
        }

    return {
        "flags": result,
        "timestamp": time.time(),
    }


@router.put("/runtime-flags")
async def update_runtime_flags(
    req: RuntimeFlagsUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    """Update runtime flags (in-memory + cache clear)."""
    from akarins_gateway.gateway.config_loader import (
        _get_raw_yaml_config,
        reload_runtime_flags,
    )

    # Update raw YAML cache
    raw = _get_raw_yaml_config()
    if raw is not None:
        raw["runtime_flags"] = req.flags

    # Clear runtime flags cache so get_runtime_flag reads fresh values
    reload_runtime_flags()

    log.info(f"[PANEL] Updated runtime_flags: {len(req.flags)} flags")
    return {"flags": req.flags}


# ==================== Auth Check Endpoint ====================

@router.post("/auth/verify")
async def verify_auth(_token: str = Depends(verify_panel_token)):
    """Verify panel authentication token."""
    return {"authenticated": True, "timestamp": time.time()}
