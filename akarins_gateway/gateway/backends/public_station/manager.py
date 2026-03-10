"""
Public Station Manager - Central registry and dispatcher for all public stations.

Provides a single-point-of-contact for proxy.py and routing.py to interact
with any public station, replacing scattered if/elif chains.

Author: 浮浮酱 (Claude Opus 4.6)
Created: 2026-02-14
"""

from typing import Any, Dict, List, Optional

from .base import PublicStationBackend, PublicStationConfig


class PublicStationManager:
    """
    Central manager for all public stations.

    Replaces:
    - proxy.py: 4+ hardcoded if/elif chains for auth, UA, thinking, fallback
    - routing.py: 3 separate is_*_supported() functions
    - config.py: Inline BACKENDS dict entries per station
    """

    def __init__(self) -> None:
        self._stations: Dict[str, PublicStationBackend] = {}

    # ===== Registration =====

    def register(self, config: PublicStationConfig) -> None:
        """Register a public station from its config."""
        backend = PublicStationBackend(config)
        self._stations[config.name] = backend

    # ===== Lookup =====

    def get(self, backend_key: str) -> Optional[PublicStationBackend]:
        """Get a station backend by key, or None if not a public station."""
        return self._stations.get(backend_key)

    def is_public_station(self, backend_key: str) -> bool:
        """Check if a backend key is a registered public station."""
        return backend_key in self._stations

    def get_all(self) -> Dict[str, PublicStationBackend]:
        """Get all registered stations."""
        return dict(self._stations)

    def get_all_keys(self) -> List[str]:
        """Get all registered station backend keys."""
        return list(self._stations.keys())

    # ===== Unified Operations (replace proxy.py if/elif chains) =====

    def supports_model(self, backend_key: str, model: str) -> bool:
        """
        Check if a public station supports the given model.

        Replaces: is_ruoli_supported(), is_newapi_public_supported(),
                  is_anyrouter_supported() in routing.py.
        """
        station = self._stations.get(backend_key)
        if station is None:
            return False
        return station.supports_model(model)

    def prepare_headers(
        self,
        backend_key: str,
        request_headers: Dict[str, str],
        backend_config: Dict[str, Any],
    ) -> Dict[str, str]:
        """
        Apply station-specific header transformations.

        Replaces: proxy.py auth/UA/anthropic-version if/elif chains.
        Returns the original headers unchanged if backend_key is not a public station.
        """
        station = self._stations.get(backend_key)
        if station is None:
            return request_headers
        return station.prepare_headers(request_headers, backend_config)

    def prepare_body(self, backend_key: str, body: Any) -> Any:
        """
        Apply station-specific body transformations (thinking suffix stripping).

        Replaces: proxy.py thinking suffix stripping if/elif chain.
        Returns the original body unchanged if backend_key is not a public station.
        """
        station = self._stations.get(backend_key)
        if station is None:
            return body
        return station.prepare_body(body)

    def get_effective_url(
        self, backend_key: str, base_url: str, endpoint: str
    ) -> Optional[str]:
        """
        Get the effective URL for a public station request.

        For rotation-enabled stations, this uses the current rotation URL.
        Returns None if backend_key is not a public station (caller uses default URL logic).
        """
        station = self._stations.get(backend_key)
        if station is None:
            return None
        return station.get_effective_url(base_url, endpoint)

    def on_failure(self, backend_key: str) -> None:
        """
        Handle request failure for a public station (URL rotation, etc.).

        Replaces: proxy.py AnyRouter-specific rotation on failure.
        """
        station = self._stations.get(backend_key)
        if station is not None:
            station.on_failure()

    def needs_response_conversion(self, backend_key: str) -> bool:
        """Check if this station needs Anthropic → OpenAI response conversion."""
        station = self._stations.get(backend_key)
        if station is None:
            return False
        return station.config.needs_response_conversion

    def needs_request_conversion(self, backend_key: str) -> bool:
        """Check if this station needs OpenAI → Anthropic request conversion."""
        station = self._stations.get(backend_key)
        if station is None:
            return False
        return station.config.needs_request_conversion

    def is_anthropic_format(self, backend_key: str) -> bool:
        """Check if this station uses Anthropic API format."""
        station = self._stations.get(backend_key)
        if station is None:
            return False
        return station.config.api_format == "anthropic"

    def is_available(self, backend_key: str) -> bool:
        """Check if a public station is enabled and has valid credentials."""
        station = self._stations.get(backend_key)
        if station is None:
            return False
        return station.is_available()

    # ===== BACKENDS Dict Generation =====

    def generate_backends_entries(self) -> Dict[str, Dict[str, Any]]:
        """
        Generate BACKENDS dict entries for all registered public stations.

        This replaces the inline station entries in config.py BACKENDS dict.
        """
        entries: Dict[str, Dict[str, Any]] = {}
        for key, station in self._stations.items():
            entries[key] = station.to_backends_entry()
        return entries

    def inject_into_backends(self, backends: Dict[str, Dict[str, Any]]) -> None:
        """
        Inject public station entries into an existing BACKENDS dict.

        This is called during config initialization to auto-register
        all public stations without manual duplication.
        Overwrites placeholder entries (which only have 'name' key) with full config.
        """
        for key, station in self._stations.items():
            backends[key] = station.to_backends_entry()
