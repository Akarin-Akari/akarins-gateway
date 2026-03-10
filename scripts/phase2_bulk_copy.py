#!/usr/bin/env python3
"""
Phase 2 Bulk Copy Script for akarins-gateway extraction.
Copies mid-layer modules from gcli2api to akarins-gateway with import refactoring.

Usage: python scripts/phase2_bulk_copy.py

Author: 浮浮酱 (Claude Opus 4.6)
Date: 2026-02-27
"""

import os
import re

SRC_BASE = r"F:\antigravity2api\gcli2api\src"
DST_BASE = r"F:\antigravity2api\akarins-gateway\akarins_gateway"

# ====================== Copy Targets ======================

# Directories to copy entirely: (src_subdir, dst_subdir)
DIR_COPIES = [
    ("ide_compat", "ide_compat"),
    ("cache", "cache"),
    ("augment_compat", "augment_compat"),
    ("converters", "converters"),
]

# Standalone files to copy: (src_file_relative, dst_file_relative)
FILE_COPIES = [
    ("signature_cache.py", "signature_cache.py"),
    ("context_truncation.py", "context_truncation.py"),
]

# ====================== Import Replacement Rules ======================
# Applied in order; first match wins per line.
# Each rule: (regex_pattern_on_stripped_line, replacement_string)

IMPORT_RULES = [
    # --- Core module imports (top priority) ---
    (r'^from log import ', 'from akarins_gateway.core.log import '),
    (r'^import log\b', 'from akarins_gateway.core import log'),
    (r'^from config import ', 'from akarins_gateway.core.config import '),

    # --- src.core-mapped modules ---
    (r'^from src\.httpx_client import ', 'from akarins_gateway.core.httpx_client import '),
    (r'^from src\.tls_impersonate import ', 'from akarins_gateway.core.tls_impersonate import '),
    (r'^from src\.retry_utils import ', 'from akarins_gateway.core.retry_utils import '),
    (r'^from src\.rate_limiter import ', 'from akarins_gateway.core.rate_limiter import '),

    # --- src.* → akarins_gateway.* direct mappings ---
    (r'^from src\.signature_cache import ', 'from akarins_gateway.signature_cache import '),
    (r'^from src\.context_truncation import ', 'from akarins_gateway.context_truncation import '),

    # --- src.ide_compat ---
    (r'^from src\.ide_compat\.', 'from akarins_gateway.ide_compat.'),
    (r'^from src\.ide_compat import ', 'from akarins_gateway.ide_compat import '),

    # --- src.cache ---
    (r'^from src\.cache\.', 'from akarins_gateway.cache.'),
    (r'^from src\.cache import ', 'from akarins_gateway.cache import '),

    # --- src.converters ---
    (r'^from src\.converters\.', 'from akarins_gateway.converters.'),
    (r'^from src\.converters import ', 'from akarins_gateway.converters import '),

    # --- src.augment_compat ---
    (r'^from src\.augment_compat\.', 'from akarins_gateway.augment_compat.'),
    (r'^from src\.augment_compat import ', 'from akarins_gateway.augment_compat import '),

    # --- src.anthropic_converter → converters.anthropic_constants ---
    (r'^from src\.anthropic_converter import ', 'from akarins_gateway.converters.anthropic_constants import '),

    # --- src.context_calibrator (not available, stub it) ---
    (r'^from src\.context_calibrator import (.+)',
     r'# STUB: context_calibrator not extracted to akarins-gateway\ntry:\n    from akarins_gateway.context_calibrator import \1\nexcept ImportError:\n    \1 = None  # graceful degradation'),

    # --- src.utils split into core.constants + core.auth + core.log ---
    # This needs special handling per import; the catch-all marks it for review
    (r'^from src\.utils import log\b', 'from akarins_gateway.core.log import log'),
    (r'^from src\.utils import (.+)', r'from akarins_gateway.core.constants import \1  # TODO: verify — some may belong in core.auth'),

    # --- Bare "from cache." in test files → absolute import ---
    (r'^from cache\.', 'from akarins_gateway.cache.'),

    # --- src.gateway.* (shouldn't appear in Phase 2 but just in case) ---
    (r'^from src\.gateway\.', 'from akarins_gateway.gateway.'),

    # --- Generic src.* catch-all (flag for manual review) ---
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
                    # Add proper indentation to each line
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


def copy_directory(src_dir: str, dst_dir: str) -> tuple:
    """Copy a directory recursively, refactoring .py imports.

    Returns:
        (total_files, total_changes, all_details)
    """
    total_files = 0
    total_changes = 0
    all_details = []

    for root, dirs, files in os.walk(src_dir):
        # Skip __pycache__
        dirs[:] = [d for d in dirs if d != '__pycache__']

        for filename in sorted(files):
            if not filename.endswith('.py'):
                continue

            src_path = os.path.join(root, filename)
            rel_path = os.path.relpath(src_path, src_dir)
            dst_path = os.path.join(dst_dir, rel_path)

            changes, details = copy_file_with_refactor(src_path, dst_path)
            total_files += 1
            total_changes += changes

            status = f"({changes} changes)" if changes else "(clean)"
            mark = "[EDIT]" if changes else "[COPY]"
            print(f"    {mark} {rel_path} {status}")
            if details:
                all_details.extend(details)

    return total_files, total_changes, all_details


def main():
    print("=" * 70)
    print("  Phase 2: Bulk Copy with Import Refactoring")
    print("  gcli2api/src → akarins-gateway/akarins_gateway")
    print("=" * 70)

    grand_files = 0
    grand_changes = 0
    all_changed = []

    # --- Copy directories ---
    for src_name, dst_name in DIR_COPIES:
        src_dir = os.path.join(SRC_BASE, src_name)
        dst_dir = os.path.join(DST_BASE, dst_name)
        print(f"\n  [DIR] {src_name}/ -> {dst_name}/")

        if not os.path.isdir(src_dir):
            print(f"    [WARN] Source directory not found: {src_dir}")
            continue

        files, changes, details = copy_directory(src_dir, dst_dir)
        grand_files += files
        grand_changes += changes
        all_changed.extend(details)
        print(f"    -- {files} files, {changes} imports changed")

    # --- Copy standalone files ---
    print()
    for src_name, dst_name in FILE_COPIES:
        src_path = os.path.join(SRC_BASE, src_name)
        dst_path = os.path.join(DST_BASE, dst_name)
        print(f"  [FILE] {src_name} -> {dst_name}")

        if not os.path.isfile(src_path):
            print(f"    [WARN] Source file not found: {src_path}")
            continue

        changes, details = copy_file_with_refactor(src_path, dst_path)
        grand_files += 1
        grand_changes += changes
        all_changed.extend(details)
        status = f"({changes} changes)" if changes else "(clean)"
        print(f"    {status}")

    # --- Summary ---
    print(f"\n{'=' * 70}")
    print(f"  [OK] Phase 2 Bulk Copy Complete!")
    print(f"     Files copied:       {grand_files}")
    print(f"     Imports refactored: {grand_changes}")
    print(f"{'=' * 70}")

    if all_changed:
        print(f"\n  Changed imports ({len(all_changed)} total):")
        for detail in all_changed:
            print(f"    {detail}")

    # Check for TODOs
    todo_count = sum(1 for d in all_changed if 'TODO' in d)
    if todo_count:
        print(f"\n  [WARN] {todo_count} imports flagged with TODO - manual review needed!")

    return grand_files, grand_changes


if __name__ == "__main__":
    main()
