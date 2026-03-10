# Antigravity Backend — Isolated Module

## Purpose

This directory contains Antigravity-related backend code that depends on
gcli2api, consolidated here for easy removal in the future.

**NOTE:** `AntigravityToolsBackend` has been moved OUT of this isolation zone
(2026-02-28). It is an independent external service proxy (port 9046) and
lives at `backends/antigravity_tools.py`, alongside `copilot.py` and `kiro.py`.

## Feature Flag

Controlled by `ENABLE_ANTIGRAVITY` environment variable (default: `false`).

When disabled, `AntigravityBackend` is replaced with a `None` stub — the rest
of the gateway operates without any gcli2api dependencies.

## Files

| File | Origin | Description |
|------|--------|-------------|
| `__init__.py` | New | Feature flag + conditional re-export |
| `backend.py` | `src/gateway/backends/antigravity.py` | Main Antigravity GatewayBackend (gcli2api dependent) |

## Removal Plan

See `docs/antigravity-removal-plan.md` for the full removal timeline.

Quick steps:
1. Delete this entire directory (`backends/antigravity/`)
2. Remove `AntigravityBackend` from `backends/__init__.py`
3. Remove `antigravity` entry from `config/gateway.yaml`
4. Remove `ENABLE_ANTIGRAVITY` from `.env.example` and `core/config.py`
5. Remove try/except stubs in `proxy.py` that reference `antigravity.*`

**Note:** Removing this directory does NOT affect `AntigravityToolsBackend`
(`backends/antigravity_tools.py`), which is independent.

## Author

fufu-chan (Claude Opus 4.6) — 2026-02-27
Refactored: 2026-02-28 — AntigravityToolsBackend moved to independent backend
