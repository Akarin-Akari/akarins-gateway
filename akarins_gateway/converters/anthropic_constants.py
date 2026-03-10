"""
Anthropic Converter Constants & Utilities.

Extracted from gcli2api/src/anthropic_converter.py for akarins-gateway.
Contains:
- Thinking budget constants and mode system
- Token limit constants
- Effort-to-budget mapping
- clean_json_schema utility function

Author: Extracted by Phase 2 bulk copy
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from akarins_gateway.core.log import log


# ====================== Token Limit Constants ======================

# [FIX 2026-01-09] Bidirectional limit strategy
MAX_ALLOWED_TOKENS = 65535   # max_tokens absolute upper limit (Claude max)
MIN_OUTPUT_TOKENS = 16384    # Minimum guaranteed output space

# ====================== Thinking Budget Constants ======================

# [FIX 2026-02-04] Thinking Budget bidirectional limit strategy
MIN_THINKING_BUDGET = 24576  # Minimum, prevents API errors
MAX_THINKING_BUDGET = 65535  # Maximum, Claude API upper limit
try:
    DEFAULT_THINKING_BUDGET = int(os.getenv("THINKING_BUDGET", "24576"))
except (ValueError, TypeError):
    log.warning("[THINKING_BUDGET] Invalid THINKING_BUDGET env value, using default 24576")
    DEFAULT_THINKING_BUDGET = 24576
DEFAULT_TEMPERATURE = 0.4

# [NEW 2026-02-04] Model patterns that require thinking budget detection
THINKING_BUDGET_MODEL_PATTERNS = ["flash", "-thinking", "-search"]

# ====================== Thinking Budget Mode System ======================
#
# Four modes (ported from Antigravity-Manager Rust):
# 1. Auto       - Use default budget (MIN_THINKING_BUDGET)
# 2. Passthrough - Pass through client budget without limits
# 3. Custom     - Use custom fixed budget (from THINKING_BUDGET_CUSTOM env)
# 4. Adaptive   - Map effort levels (low/medium/high) to budgets
#

THINKING_BUDGET_MODE = os.getenv("THINKING_BUDGET_MODE", "auto").strip().lower()

_VALID_THINKING_BUDGET_MODES = {"auto", "passthrough", "custom", "adaptive"}
if THINKING_BUDGET_MODE not in _VALID_THINKING_BUDGET_MODES:
    log.warning(
        f"[THINKING_BUDGET] Invalid THINKING_BUDGET_MODE='{THINKING_BUDGET_MODE}', "
        f"valid modes: {_VALID_THINKING_BUDGET_MODES}. Falling back to 'auto'."
    )
    THINKING_BUDGET_MODE = "auto"

# Custom mode fixed budget
try:
    THINKING_BUDGET_CUSTOM = int(os.getenv("THINKING_BUDGET_CUSTOM", str(DEFAULT_THINKING_BUDGET)))
except (ValueError, TypeError):
    log.warning("[THINKING_BUDGET] Invalid THINKING_BUDGET_CUSTOM env value, using DEFAULT_THINKING_BUDGET")
    THINKING_BUDGET_CUSTOM = DEFAULT_THINKING_BUDGET

# Effort level to budget mapping
EFFORT_BUDGET_MAP = {
    "none": 0,           # No thinking (disabled)
    "low": 8192,         # Light thinking (fast response)
    "medium": 24576,     # Medium thinking (balanced, equals DEFAULT)
    "high": 65535,       # Deep thinking (max budget)
}

# Effort level to Gemini thinkingLevel mapping
EFFORT_THINKING_LEVEL_MAP = {
    "none": None,        # Disable thinking
    "low": "low",
    "medium": "medium",
    "high": "high",
}


# ====================== JSON Schema Cleaning ======================

def clean_json_schema(schema: Any) -> Any:
    """
    Clean JSON Schema by removing unsupported fields and appending
    validation requirements to description.

    This ensures compatibility with downstream APIs (Antigravity/Vertex/Gemini)
    that have limited JSON Schema support.
    """
    if not isinstance(schema, dict):
        return schema

    unsupported_keys = {
        "$schema", "$id", "$ref", "$defs", "definitions", "title",
        "example", "examples", "readOnly", "writeOnly", "default",
        "exclusiveMaximum", "exclusiveMinimum", "oneOf", "anyOf", "allOf",
        "const", "additionalItems", "contains", "patternProperties",
        "dependencies", "propertyNames", "if", "then", "else",
        "contentEncoding", "contentMediaType",
    }

    validation_fields = {
        "minLength": "minLength",
        "maxLength": "maxLength",
        "minimum": "minimum",
        "maximum": "maximum",
        "minItems": "minItems",
        "maxItems": "maxItems",
    }
    fields_to_remove = {"additionalProperties"}

    validations: List[str] = []
    for field, label in validation_fields.items():
        if field in schema:
            validations.append(f"{label}: {schema[field]}")

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported_keys or key in fields_to_remove or key in validation_fields:
            continue

        if key == "type" and isinstance(value, list):
            # Handle type: ["string", "null"] pattern
            has_null = any(
                isinstance(t, str) and t.strip() and t.strip().lower() == "null" for t in value
            )
            non_null_types = [
                t.strip()
                for t in value
                if isinstance(t, str) and t.strip() and t.strip().lower() != "null"
            ]
            cleaned[key] = non_null_types[0] if non_null_types else "string"
            if has_null:
                cleaned["nullable"] = True
            continue

        if key == "description" and validations:
            cleaned[key] = f"{value} ({', '.join(validations)})"
        elif key == "properties":
            if isinstance(value, dict):
                cleaned_properties: Dict[str, Any] = {}
                for prop_name, prop_schema in value.items():
                    if isinstance(prop_schema, dict):
                        cleaned_prop = clean_json_schema(prop_schema)
                        if cleaned_prop.get("type") == "object":
                            if "properties" not in cleaned_prop:
                                cleaned_prop["properties"] = {}
                            if "type" not in cleaned_prop:
                                cleaned_prop["type"] = "object"
                        cleaned_properties[prop_name] = cleaned_prop
                    elif isinstance(prop_schema, str) and prop_schema == "object":
                        cleaned_properties[prop_name] = {"type": "object", "properties": {}}
                    else:
                        cleaned_properties[prop_name] = prop_schema
                cleaned[key] = cleaned_properties
            else:
                cleaned[key] = value
        elif isinstance(value, dict):
            cleaned[key] = clean_json_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [clean_json_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value

    if validations and "description" not in cleaned:
        cleaned["description"] = f"Validation: {', '.join(validations)}"

    # Ensure properties with no explicit type get type: "object"
    if "properties" in cleaned and "type" not in cleaned:
        cleaned["type"] = "object"

    # Ensure non-empty schema always has a type field
    if cleaned and "type" not in cleaned:
        cleaned["type"] = "object"

    return cleaned
