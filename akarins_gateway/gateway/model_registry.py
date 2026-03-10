"""
ModelRegistry: Dynamic Model Discovery with Three-Layer Defense

Three-layer architecture:
  Layer 1: Static Baseline (YAML)     -- Always available, never lost
  Layer 2: Dynamic Discovery (async)  -- Periodic refresh, extends Layer 1
  Layer 3: Request-Time Probing       -- Deferred to future PR

Core Principle: "Only-Add-Never-Remove"
Dynamic discovery ADDS to routing knowledge. It NEVER removes entries that
exist in Layer 1 (YAML) or were previously discovered. This prevents
temporary backend issues from breaking routing.

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-03-10
"""

import asyncio
import os
import time
from typing import Dict, Set, Optional, Any, List

# Safety limits to prevent unbounded memory growth
MAX_MODELS_PER_BACKEND = 500
MAX_TOTAL_DYNAMIC_MODELS = 5000
MAX_MODEL_ID_LENGTH = 256

# Allowed fields in model objects from backend responses (cache poisoning defense)
_ALLOWED_MODEL_FIELDS = frozenset({
    "id", "object", "owned_by", "created", "name",
    "display_name", "displayName", "type",
})

# Lazy imports to avoid circular dependencies
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# ==================== Singleton ====================

_registry_instance: Optional["ModelRegistry"] = None


def get_model_registry() -> "ModelRegistry":
    """Get the singleton ModelRegistry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ModelRegistry()
    return _registry_instance


class ModelRegistry:
    """
    Three-layer model discovery and caching registry.

    Singleton — access via get_model_registry().

    Data flow:
      initialize() loads Layer 1 (static from YAML/BACKENDS)
      refresh_all() populates Layer 2 (dynamic from /v1/models)
      _rebuild_cache() builds pre-formatted responses for /v1/models endpoints
    """

    def __init__(self):
        # Layer 1: Static models from YAML config
        self._static_models: Dict[str, Set[str]] = {}  # model_id -> {backend_keys}

        # Layer 2: Dynamically discovered models
        self._dynamic_models: Dict[str, Set[str]] = {}  # model_id -> {backend_keys}

        # Raw model data per backend (for cache rebuild)
        self._backend_raw_models: Dict[str, Set[str]] = {}  # backend_key -> {model_ids}

        # Full model objects from backend responses (for richer cache)
        self._dynamic_model_objects: Dict[str, dict] = {}  # model_id -> model object

        # Wildcard backends (models: ["*"])
        self._wildcard_backends: Set[str] = set()

        # Refresh tracking
        self._last_refresh: Dict[str, float] = {}  # backend_key -> monotonic timestamp
        self._refresh_lock = asyncio.Lock()

        # Pre-built response caches
        self._cached_models_response: Optional[dict] = None
        self._cached_augment_response: Optional[list] = None
        self._cache_built_at: float = 0.0

        # Background refresh task reference (prevent GC of fire-and-forget tasks)
        self._background_refresh_tasks: Set[asyncio.Task] = set()

        # Configuration
        self._stale_threshold = float(
            os.environ.get("MODEL_REGISTRY_STALE_THRESHOLD", "600")
        )
        self._enabled = (
            os.environ.get("MODEL_REGISTRY_ENABLED", "true").lower() == "true"
        )

        # Initialization flag
        self._initialized = False

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def initialized(self) -> bool:
        return self._initialized

    # ------------------------------------------------------------------ #
    #  Layer 1: Static Initialization                                     #
    # ------------------------------------------------------------------ #

    def initialize(self, backends_config: Dict[str, Any]) -> None:
        """
        Load Layer 1 (static) from BACKENDS configuration.

        Reads both 'models' and 'supported_models' keys from backend configs.
        Backends with models: ["*"] are marked as wildcard backends.

        Uses atomic swap: builds new dicts in local vars, then assigns,
        so concurrent readers never see a half-populated state.
        """
        new_static: Dict[str, Set[str]] = {}
        new_wildcards: Set[str] = set()

        for backend_key, config in backends_config.items():
            if not config.get("enabled", True):
                continue

            # Check both keys — YAML uses 'models', BACKENDS dict may use 'supported_models'
            models = config.get("models") or config.get("supported_models") or []

            if models == ["*"] or models == "*":
                new_wildcards.add(backend_key)
                log.debug(
                    f"[MODEL_REGISTRY] {backend_key}: wildcard backend (accepts all)"
                )
                continue

            for model_id in models:
                if isinstance(model_id, str) and model_id:
                    new_static.setdefault(model_id, set()).add(backend_key)

        # Atomic swap — no window where data structures are empty
        self._static_models = new_static
        self._wildcard_backends = new_wildcards
        self._initialized = True

        total_models = len(self._static_models)
        total_backends = (
            len(set(b for bs in self._static_models.values() for b in bs))
            + len(self._wildcard_backends)
        )
        log.info(
            f"[MODEL_REGISTRY] Initialized: {total_models} static models, "
            f"{total_backends} backends, "
            f"{len(self._wildcard_backends)} wildcard"
        )

    # ------------------------------------------------------------------ #
    #  Layer 2: Dynamic Discovery                                         #
    # ------------------------------------------------------------------ #

    async def refresh_all(self) -> None:
        """
        Refresh all enabled backends (Layer 2).

        Uses asyncio.Lock to prevent concurrent refresh storms.
        Fetches /models from each backend in parallel.
        """
        if not self._enabled:
            return

        if self._refresh_lock.locked():
            log.debug("[MODEL_REGISTRY] Refresh already in progress, skipping")
            return

        async with self._refresh_lock:
            # Lazy import to avoid circular dependencies
            from .routing import get_sorted_backends

            tasks = []
            for bk, bc in get_sorted_backends():
                if bc.get("enabled", True):
                    tasks.append(self._refresh_backend(bk, bc))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                succeeded = sum(1 for r in results if r is True)
                failed = len(tasks) - succeeded
                log.info(
                    f"[MODEL_REGISTRY] Refresh complete: "
                    f"{succeeded}/{len(tasks)} backends succeeded"
                    + (f", {failed} failed" if failed else "")
                )

            self._rebuild_cache()

    async def _refresh_backend(
        self, backend_key: str, backend_config: Dict[str, Any]
    ) -> bool:
        """
        Fetch a single backend's /models endpoint.

        Returns True on success, False on failure.
        Implements Only-Add-Never-Remove: new models are added,
        existing entries are never removed.
        """
        # Lazy import
        from .routing import get_backend_base_url

        base_url = get_backend_base_url(backend_config)
        if not base_url:
            log.debug(f"[MODEL_REGISTRY] {backend_key}: no base_url, skipping")
            return False

        # Skip anthropic-format backends — they likely don't have /models
        if backend_config.get("api_format") == "anthropic":
            log.debug(
                f"[MODEL_REGISTRY] {backend_key}: anthropic format, skipping"
            )
            return False

        # Build auth headers with real API key when available
        api_key = backend_config.get("api_key") or ""
        if not api_key:
            api_keys = backend_config.get("api_keys")
            if api_keys and isinstance(api_keys, list) and len(api_keys) > 0:
                api_key = api_keys[0]
        if not api_key:
            api_key = "dummy"

        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            from akarins_gateway.core.httpx_client import http_client

            async with http_client.get_client(timeout=10.0) as client:
                response = await client.get(
                    f"{base_url}/models", headers=headers
                )

                if response.status_code != 200:
                    log.warning(
                        f"[MODEL_REGISTRY] {backend_key} returned "
                        f"{response.status_code}"
                    )
                    return False

                data = response.json()
                raw_models: Set[str] = set()
                model_objects: Dict[str, dict] = {}

                for item in data.get("data", []):
                    if isinstance(item, dict):
                        model_id = item.get("id")
                        if model_id and isinstance(model_id, str):
                            raw_models.add(model_id)
                            model_objects[model_id] = item
                    elif isinstance(item, str) and item:
                        raw_models.add(item)
                        model_objects[item] = {
                            "id": item,
                            "object": "model",
                        }

                if not raw_models:
                    log.warning(
                        f"[MODEL_REGISTRY] {backend_key} returned empty "
                        f"model list, keeping stale data"
                    )
                    return False

                # Validate and cap model IDs
                valid_models: Set[str] = set()
                for mid in raw_models:
                    if (
                        len(mid) <= MAX_MODEL_ID_LENGTH
                        and mid.isprintable()
                        and "\n" not in mid
                        and "\r" not in mid
                    ):
                        valid_models.add(mid)
                raw_models = valid_models

                if len(raw_models) > MAX_MODELS_PER_BACKEND:
                    log.warning(
                        f"[MODEL_REGISTRY] {backend_key} returned "
                        f"{len(raw_models)} models, capping at "
                        f"{MAX_MODELS_PER_BACKEND}"
                    )
                    raw_models = set(list(raw_models)[:MAX_MODELS_PER_BACKEND])

                # Only-Add-Never-Remove: additive update for both structures
                self._backend_raw_models.setdefault(
                    backend_key, set()
                ).update(raw_models)

                # Global cap on total dynamic models
                if len(self._dynamic_models) > MAX_TOTAL_DYNAMIC_MODELS:
                    log.warning(
                        "[MODEL_REGISTRY] Total dynamic models exceeds "
                        f"{MAX_TOTAL_DYNAMIC_MODELS}, skipping add"
                    )
                    return False

                for model_id in raw_models:
                    self._dynamic_models.setdefault(model_id, set()).add(
                        backend_key
                    )
                    # Sanitize and prefer model objects with more fields
                    sanitized = {
                        k: v
                        for k, v in model_objects.get(model_id, {}).items()
                        if k in _ALLOWED_MODEL_FIELDS
                        and isinstance(v, (str, int, float, bool))
                    }
                    sanitized["id"] = model_id  # Ensure id is always present
                    existing = self._dynamic_model_objects.get(model_id)
                    if existing is None or len(sanitized) > len(existing):
                        self._dynamic_model_objects[model_id] = sanitized

                self._last_refresh[backend_key] = time.monotonic()
                log.info(
                    f"[MODEL_REGISTRY] Refreshed {backend_key}: "
                    f"{len(raw_models)} models"
                )
                return True

        except Exception as e:
            log.warning(
                f"[MODEL_REGISTRY] Failed to refresh {backend_key}: {e}"
            )
            return False

    # ------------------------------------------------------------------ #
    #  Query Methods                                                      #
    # ------------------------------------------------------------------ #

    def get_backends_for_model(self, model_id: str) -> List[str]:
        """
        Get all backends that can serve a given model.

        Returns union of Layer 1 (static) + Layer 2 (dynamic),
        plus wildcard backends. The caller handles priority sorting.
        """
        backends: Set[str] = set()

        # Layer 1: Static
        if model_id in self._static_models:
            backends.update(self._static_models[model_id])

        # Layer 2: Dynamic
        if model_id in self._dynamic_models:
            backends.update(self._dynamic_models[model_id])

        # Also try normalized matching
        try:
            from .config import normalize_model_name

            normalized = normalize_model_name(model_id)
            if normalized != model_id:
                if normalized in self._static_models:
                    backends.update(self._static_models[normalized])
                if normalized in self._dynamic_models:
                    backends.update(self._dynamic_models[normalized])
        except Exception:
            pass

        # Wildcard backends always match
        backends.update(self._wildcard_backends)

        return list(backends)

    def is_model_known_to_backend(
        self, model_id: str, backend_key: str
    ) -> bool:
        """
        Check if the registry knows this model-backend pair.

        Checks:
          1. Wildcard backend
          2. Exact match in static models
          3. Exact match in dynamic models
          4. Normalized match in both
        """
        # Wildcard backends accept everything
        if backend_key in self._wildcard_backends:
            return True

        # Exact match — static
        if (
            model_id in self._static_models
            and backend_key in self._static_models[model_id]
        ):
            return True

        # Exact match — dynamic
        if (
            model_id in self._dynamic_models
            and backend_key in self._dynamic_models[model_id]
        ):
            return True

        # Normalized match
        try:
            from .config import normalize_model_name

            normalized = normalize_model_name(model_id)
            if normalized != model_id:
                if (
                    normalized in self._static_models
                    and backend_key in self._static_models[normalized]
                ):
                    return True
                if (
                    normalized in self._dynamic_models
                    and backend_key in self._dynamic_models[normalized]
                ):
                    return True
        except Exception:
            pass

        return False

    def get_all_known_models(self) -> Set[str]:
        """Get union of all models across all layers."""
        return set(self._static_models.keys()) | set(
            self._dynamic_models.keys()
        )

    # ------------------------------------------------------------------ #
    #  Cache: Pre-built Responses                                         #
    # ------------------------------------------------------------------ #

    def get_cached_models_response(self) -> Optional[dict]:
        """
        Get pre-built OpenAI-format response for /v1/models.

        Returns None if cache is not available (cold start).
        Uses stale-while-revalidate: returns stale cache immediately
        and triggers async refresh in background.
        """
        if not self._enabled or self._cached_models_response is None:
            return None

        # Stale-while-revalidate
        if self._is_cache_stale():
            self._trigger_background_refresh()

        return self._cached_models_response

    def get_cached_augment_models_response(self) -> Optional[list]:
        """
        Get pre-built Augment-format response for /usage/api/get-models.

        Returns None if cache is not available (cold start).
        """
        if not self._enabled or self._cached_augment_response is None:
            return None

        if self._is_cache_stale():
            self._trigger_background_refresh()

        return self._cached_augment_response

    def _is_cache_stale(self) -> bool:
        """Check if the cache is older than the stale threshold."""
        if self._cache_built_at == 0.0:
            return True
        return (time.monotonic() - self._cache_built_at) > self._stale_threshold

    def _trigger_background_refresh(self) -> None:
        """Trigger an async background refresh (fire-and-forget).

        Stores task reference in _background_refresh_tasks to prevent GC
        from collecting the task before it completes.
        Checks lock first to avoid creating unnecessary tasks under load.
        """
        # Debounce: don't create tasks if a refresh is already in progress
        if self._refresh_lock.locked():
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._safe_refresh())
            self._background_refresh_tasks.add(task)
            task.add_done_callback(self._background_refresh_tasks.discard)
        except RuntimeError:
            pass  # No running loop

    async def _safe_refresh(self) -> None:
        """Safe wrapper for refresh_all that catches exceptions."""
        try:
            await self.refresh_all()
        except Exception as e:
            log.warning(f"[MODEL_REGISTRY] Background refresh failed: {e}")

    def _rebuild_cache(self) -> None:
        """
        Rebuild pre-built response caches from current registry state.

        Called after every successful refresh_all().
        """
        all_models = self.get_all_known_models()

        # -- OpenAI-format response --
        model_data = []
        for model_id in sorted(all_models):
            if model_id in self._dynamic_model_objects:
                obj = dict(self._dynamic_model_objects[model_id])
                obj.setdefault("object", "model")
                obj.setdefault("owned_by", "gateway")
                model_data.append(obj)
            else:
                model_data.append(
                    {
                        "id": model_id,
                        "object": "model",
                        "owned_by": "gateway",
                    }
                )

        self._cached_models_response = {
            "object": "list",
            "data": model_data,
        }

        # -- Augment-format response --
        augment_list = []
        for model_id in sorted(all_models):
            obj = self._dynamic_model_objects.get(model_id, {})
            augment_model = {
                "id": model_id,
                "name": obj.get("name", model_id),
                "displayName": (
                    obj.get("display_name")
                    or obj.get("displayName")
                    or model_id
                ),
            }
            if "object" in obj:
                augment_model["object"] = obj["object"]
            if "owned_by" in obj:
                augment_model["owned_by"] = obj["owned_by"]
            if "type" in obj:
                augment_model["type"] = obj["type"]
            augment_list.append(augment_model)

        self._cached_augment_response = augment_list
        self._cache_built_at = time.monotonic()

        log.info(f"[MODEL_REGISTRY] Cache rebuilt: {len(model_data)} models")

    # ------------------------------------------------------------------ #
    #  Reload / Maintenance                                               #
    # ------------------------------------------------------------------ #

    def reload_static(self) -> None:
        """
        Reload Layer 1 from current BACKENDS config.

        Called when YAML config is reloaded via reload_model_routing_config().
        """
        try:
            from .config import BACKENDS

            self.initialize(BACKENDS)
            self._rebuild_cache()
            log.info("[MODEL_REGISTRY] Static config reloaded")
        except Exception as e:
            log.warning(
                f"[MODEL_REGISTRY] Failed to reload static config: {e}"
            )

    def get_stats(self) -> dict:
        """Get registry statistics for debugging/monitoring."""
        now = time.monotonic()
        return {
            "enabled": self._enabled,
            "initialized": self._initialized,
            "static_models_count": len(self._static_models),
            "dynamic_models_count": len(self._dynamic_models),
            "total_known_models": len(self.get_all_known_models()),
            "wildcard_backends": sorted(self._wildcard_backends),
            "backends_refreshed": {
                k: round(now - v, 1) for k, v in self._last_refresh.items()
            },
            "cache_age_seconds": (
                round(now - self._cache_built_at, 1)
                if self._cache_built_at > 0
                else None
            ),
            "cache_stale": self._is_cache_stale(),
        }
