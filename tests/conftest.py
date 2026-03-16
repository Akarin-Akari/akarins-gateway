"""
Shared pytest fixtures for akarins-gateway test suite.

Provides:
- tmp_gateway_yaml: Temporary gateway.yaml with controlled content
- mock_backends: Clean BACKENDS dict for testing
- async client fixtures for FastAPI testing

Author: fufu-chan (Gemini)
Date: 2026-03-17
"""

import asyncio
import copy
import os
import shutil
import textwrap
from pathlib import Path
from typing import Dict, Any
from unittest.mock import patch

import pytest
import yaml


# ===========================================================================
# Gateway YAML Fixtures
# ===========================================================================

MINIMAL_GATEWAY_YAML = textwrap.dedent("""\
    backends:
      test-backend-a:
        enabled: true
        priority: 1
        base_url: http://127.0.0.1:9001/v1
        timeout: 60.0
        stream_timeout: 300.0
        max_retries: 2
        api_key: sk-original-key-aaaa
        models:
          - claude-sonnet-4.5
      test-backend-b:
        enabled: false
        priority: 2
        base_url: http://127.0.0.1:9002/v1
        timeout: 30.0
        stream_timeout: 120.0
        max_retries: 1
        api_keys:
          - sk-key-b-001
          - sk-key-b-002
        models:
          - gpt-4
    model_routing:
      claude-sonnet-4.5:
        enabled: true
        backends:
          - backend: test-backend-a
            model: claude-sonnet-4.5
        fallback_on:
          - 429
          - timeout
""")


@pytest.fixture
def tmp_gateway_yaml(tmp_path: Path) -> Path:
    """Create a temporary gateway.yaml for testing.

    Returns the path to the temp config directory (not the file itself).
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    yaml_file = config_dir / "gateway.yaml"
    yaml_file.write_text(MINIMAL_GATEWAY_YAML, encoding="utf-8")
    return yaml_file


@pytest.fixture
def tmp_project_root(tmp_path: Path, tmp_gateway_yaml: Path) -> Path:
    """Return a fake project root with config/gateway.yaml inside."""
    # tmp_gateway_yaml lives at tmp_path/config/gateway.yaml
    # So "project root" is tmp_path
    return tmp_path


@pytest.fixture
def mock_backends() -> Dict[str, Dict[str, Any]]:
    """Return a clean BACKENDS dict for unit testing."""
    return {
        "test-backend-a": {
            "name": "Test Backend A",
            "base_url": "http://127.0.0.1:9001/v1",
            "priority": 1,
            "timeout": 60.0,
            "stream_timeout": 300.0,
            "max_retries": 2,
            "enabled": True,
            "api_key": "sk-original-key-aaaa",
            "supported_models": ["claude-sonnet-4.5"],
        },
        "test-backend-b": {
            "name": "Test Backend B",
            "base_url": "http://127.0.0.1:9002/v1",
            "priority": 2,
            "timeout": 30.0,
            "stream_timeout": 120.0,
            "max_retries": 1,
            "enabled": False,
            "api_keys": ["sk-key-b-001", "sk-key-b-002"],
            "supported_models": ["gpt-4"],
        },
    }


@pytest.fixture
def mock_backends_with_new_keys(mock_backends) -> Dict[str, Dict[str, Any]]:
    """BACKENDS dict where api_keys have been modified (simulating panel edits)."""
    backends = copy.deepcopy(mock_backends)
    # Simulate: user added a key to backend-a via panel
    backends["test-backend-a"]["api_keys"] = ["sk-original-key-aaaa", "sk-new-key-1234"]
    backends["test-backend-a"].pop("api_key", None)  # panel migrates to list format
    # Simulate: user deleted one key from backend-b
    backends["test-backend-b"]["api_keys"] = ["sk-key-b-001"]  # removed sk-key-b-002
    return backends
