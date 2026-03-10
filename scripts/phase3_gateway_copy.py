#!/usr/bin/env python3
"""
Phase 3 Gateway Core Copy Script for akarins-gateway extraction.
Copies src/gateway/ module with full import path refactoring.

Handles Phase 3 (core), Phase 4 (backends), Phase 5 (endpoints/augment/sse).
Skips: adapter.py, compat.py (legacy bridge code).

Usage: python scripts/phase3_gateway_copy.py

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-02-27
"""

import os
import re

SRC_BASE = r"F:\antigravity2api\gcli2api\src\gateway"
DST_BASE = r"F:\antigravity2api\akarins-gateway\akarins_gateway\gateway"

# Files to skip (legacy bridge code)
SKIP_FILES = {"adapter.py", "compat.py"}

# ====================== Import Replacement Rules ======================
# Applied in order; first match wins per line.
# Each rule: (regex_pattern_on_stripped_line, replacement_string)

IMPORT_RULES = [
    # === Top priority: bare log imports ===
    (r'^from log import log$', 'from akarins_gateway.core.log import log'),
    (r'^from log import ', 'from akarins_gateway.core.log import '),

    # === src.utils → split into core.log + core.constants ===
    (r'^from src\.utils import log, ANTIGRAVITY_USER_AGENT$',
     'from akarins_gateway.core.log import log\nfrom akarins_gateway.core.constants import ANTIGRAVITY_USER_AGENT'),
    (r'^from src\.utils import log$', 'from akarins_gateway.core.log import log'),
    (r'^from src\.utils import (.+)', r'from akarins_gateway.core.constants import \1'),

    # === src.auth → core.auth ===
    (r'^from src\.auth import ', 'from akarins_gateway.core.auth import '),

    # === src.httpx_client → core.httpx_client ===
    (r'^from src\.httpx_client import ', 'from akarins_gateway.core.httpx_client import '),

    # === src.tls_impersonate → core.tls_impersonate ===
    (r'^from src\.tls_impersonate import ', 'from akarins_gateway.core.tls_impersonate import '),

    # === src.retry_utils → core.retry_utils ===
    (r'^from src\.retry_utils import ', 'from akarins_gateway.core.retry_utils import '),

    # === src.rate_limiter → core.rate_limiter ===
    (r'^from src\.rate_limiter import ', 'from akarins_gateway.core.rate_limiter import '),

    # === src.ide_compat.* → akarins_gateway.ide_compat.* ===
    (r'^from src\.ide_compat\.', 'from akarins_gateway.ide_compat.'),
    (r'^from src\.ide_compat import ', 'from akarins_gateway.ide_compat import '),

    # === src.cache.* → akarins_gateway.cache.* ===
    (r'^from src\.cache\.', 'from akarins_gateway.cache.'),
    (r'^from src\.cache import ', 'from akarins_gateway.cache import '),

    # === src.signature_cache → akarins_gateway.signature_cache ===
    (r'^from src\.signature_cache import ', 'from akarins_gateway.signature_cache import '),

    # === src.anthropic_converter → akarins_gateway.converters.anthropic_constants ===
    (r'^from src\.anthropic_converter import ', 'from akarins_gateway.converters.anthropic_constants import '),

    # === src.converters.* → akarins_gateway.converters.* ===
    (r'^from src\.converters\.', 'from akarins_gateway.converters.'),
    (r'^from src\.converters import ', 'from akarins_gateway.converters import '),

    # === src.augment_compat.* → akarins_gateway.augment_compat.* ===
    (r'^from src\.augment_compat\.', 'from akarins_gateway.augment_compat.'),
    (r'^from src\.augment_compat import ', 'from akarins_gateway.augment_compat import '),

    # === src.context_truncation → akarins_gateway.context_truncation ===
    (r'^from src\.context_truncation import ', 'from akarins_gateway.context_truncation import '),

    # === Triple-dot relative: ...streaming_constants → absolute ===
    (r'^from \.\.\.streaming_constants import ', 'from akarins_gateway.streaming_constants import '),

    # === src.gateway.* self-references → absolute akarins_gateway.gateway.* ===
    # (using absolute imports avoids relative-depth complexity)
    (r'^from src\.gateway\.backends\.public_station import ',
     'from akarins_gateway.gateway.backends.public_station import '),
    (r'^from src\.gateway\.backends\.public_station\.',
     'from akarins_gateway.gateway.backends.public_station.'),
    (r'^from src\.gateway\.backends\.interface import ',
     'from akarins_gateway.gateway.backends.interface import '),
    (r'^from src\.gateway\.backends\.', 'from akarins_gateway.gateway.backends.'),
    (r'^from src\.gateway\.config_loader import ', 'from akarins_gateway.gateway.config_loader import '),
    (r'^from src\.gateway\.config import ', 'from akarins_gateway.gateway.config import '),
    (r'^from src\.gateway\.routing import ', 'from akarins_gateway.gateway.routing import '),
    (r'^from src\.gateway\.scid_generator import ', 'from akarins_gateway.gateway.scid_generator import '),
    (r'^from src\.gateway\.augment\.state import ', 'from akarins_gateway.gateway.augment.state import '),
    (r'^from src\.gateway\.compat import ', '# REMOVED: legacy compat bridge'),
    (r'^from src\.gateway\.', 'from akarins_gateway.gateway.'),

    # === Antigravity-specific: stub with try/except ===
    (r'^from src\.services\.antigravity_service import (.+)',
     r'# STUB: antigravity_service (ENABLE_ANTIGRAVITY feature flag)\ntry:\n    from akarins_gateway.gateway.backends.antigravity.service import \1\nexcept ImportError:\n    \1 = None'),
    (r'^from src\.antigravity_anthropic_router import (.+)',
     r'# STUB: antigravity_anthropic_router (ENABLE_ANTIGRAVITY feature flag)\ntry:\n    from akarins_gateway.gateway.backends.antigravity.router import \1\nexcept ImportError:\n    \1 = None'),

    # === Credential manager: stub ===
    (r'^from src\.credential_manager import (.+)',
     r'# STUB: credential_manager not extracted to akarins-gateway\ntry:\n    from akarins_gateway.credential_manager import \1\nexcept ImportError:\n    \1 = None'),

    # === Diagnostics: optional, stub ===
    (r'^from src\.diagnostics\.(.+) import (.+)',
     r'# STUB: diagnostics not extracted to akarins-gateway\ntry:\n    from akarins_gateway.diagnostics.\1 import \2\nexcept ImportError:\n    \2 = None'),

    # === fallback_manager: stub ===
    (r'^from src\.fallback_manager import (.+)',
     r'# STUB: fallback_manager not extracted to akarins-gateway\ntry:\n    from akarins_gateway.fallback_manager import \1\nexcept ImportError:\n    \1 = None'),

    # === unified_gateway_router: should not appear (skipped files) ===
    (r'^from src\.unified_gateway_router import ', '# REMOVED: unified_gateway_router (legacy) — '),

    # === Generic src.* catch-all ===
    (r'^from src\.(\w+) import (.+)', r'from akarins_gateway.\1 import \2  # TODO: verify import path'),
    (r'^from src\.(\w+)', r'from akarins_gateway.\1  # TODO: verify import path'),
]


def apply_import_rules(content: str) -> tuple:
    """Apply import replacement rules to file content.

    Returns:
        (new_content, change_count, changed_lines)
    """
    lines = content.split('\n')
    new_lines = []
    changes = 0
    changed_details = []

    for i, line in enumerate(lines, 1):
        original = line
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]

        matched = False
        for pattern, replacement in IMPORT_RULES:
            if re.match(pattern, stripped):
                new_stripped = re.sub(pattern, replacement, stripped)
                # Handle multi-line replacements (e.g., try/except stubs)
                if '\n' in new_stripped:
                    sub_lines = new_stripped.split('\n')
                    line = '\n'.join(indent + sl for sl in sub_lines)
                else:
                    line = indent + new_stripped

                if line != original:
                    changes += 1
                    changed_details.append(f"  L{i}: {stripped.strip()}")
                matched = True
                break

        new_lines.append(line)

    return '\n'.join(new_lines), changes, changed_details


def copy_file_with_refactor(src_path: str, dst_path: str) -> tuple:
    """Copy a single .py file with import refactoring.

    Returns:
        (change_count, changed_details)
    """
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    with open(src_path, 'r', encoding='utf-8') as f:
        content = f.read()

    new_content, changes, details = apply_import_rules(content)

    with open(dst_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(new_content)

    return changes, details


def main():
    print("=" * 70)
    print("  Phase 3: Gateway Core Copy with Import Refactoring")
    print("  gcli2api/src/gateway -> akarins-gateway/akarins_gateway/gateway")
    print("=" * 70)

    grand_files = 0
    grand_changes = 0
    all_changed = []
    skipped = []

    for root, dirs, files in os.walk(SRC_BASE):
        # Skip __pycache__
        dirs[:] = [d for d in dirs if d != '__pycache__']

        for filename in sorted(files):
            if not filename.endswith('.py'):
                continue

            # Skip legacy files
            rel_path = os.path.relpath(os.path.join(root, filename), SRC_BASE)
            if filename in SKIP_FILES and os.path.dirname(rel_path) == '':
                skipped.append(rel_path)
                continue

            src_path = os.path.join(root, filename)
            dst_path = os.path.join(DST_BASE, rel_path)

            changes, details = copy_file_with_refactor(src_path, dst_path)
            grand_files += 1
            grand_changes += changes

            status = f"({changes} changes)" if changes else "(clean)"
            mark = "[EDIT]" if changes else "[COPY]"
            print(f"    {mark} {rel_path} {status}")
            if details:
                all_changed.extend(details)

    # Summary
    print(f"\n{'=' * 70}")
    print(f"  [OK] Phase 3 Gateway Copy Complete!")
    print(f"     Files copied:       {grand_files}")
    print(f"     Files skipped:      {len(skipped)} ({', '.join(skipped)})")
    print(f"     Imports refactored: {grand_changes}")
    print(f"{'=' * 70}")

    if all_changed:
        print(f"\n  Changed imports ({len(all_changed)} total):")
        for detail in all_changed:
            print(f"    {detail}")

    # Check for TODOs
    todo_count = sum(1 for d in all_changed if 'TODO' in d)
    stub_count = sum(1 for d in all_changed if 'STUB' in d or 'REMOVED' in d)
    if todo_count:
        print(f"\n  [WARN] {todo_count} imports flagged with TODO - manual review needed!")
    if stub_count:
        print(f"  [INFO] {stub_count} imports stubbed (try/except or removed)")

    return grand_files, grand_changes


if __name__ == "__main__":
    main()
