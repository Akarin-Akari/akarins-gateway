# Antigravity Removal Plan

## Context

The Antigravity backend in `akarins-gateway` has been superseded by ZeroGravity.
All Antigravity code is isolated in `akarins_gateway/gateway/backends/antigravity/`
and disabled by default via `ENABLE_ANTIGRAVITY=false`.

**IMPORTANT:** `AntigravityToolsBackend` is NOT part of the Antigravity isolation.
It is an independent external service proxy (port 9046) living at
`backends/antigravity_tools.py`. Removing Antigravity does NOT affect it.

## Current State (2026-02-28)

- **Feature flag**: `ENABLE_ANTIGRAVITY=false` (default OFF)
- **Backend files**: `backend.py` in isolated directory (`backends/antigravity/`)
- **AntigravityToolsBackend**: Independent file at `backends/antigravity_tools.py` (NOT affected by removal)
- **Stub references**: `proxy.py` has try/except stubs for:
  - `antigravity.service.handle_openai_chat_completions`
  - `antigravity.router.get_credential_manager`
- **Config entries**: `config/gateway.yaml` has `antigravity` backend definition
  (will be skipped when backend is frozen/disabled)

## Removal Steps

### Step 1: Confirm No Users Depend on Antigravity

- [ ] Check production logs for any `ENABLE_ANTIGRAVITY=true` deployments
- [ ] Verify all users have migrated to ZeroGravity backend

### Step 2: Remove Backend Code

- [ ] Delete `akarins_gateway/gateway/backends/antigravity/` directory
- [ ] Remove from `backends/__init__.py`:
  - `AntigravityBackend` from `__all__`
  - Corresponding `__getattr__` entry
- [ ] Remove `ENABLE_ANTIGRAVITY` from `core/config.py`
- [ ] Remove `ENABLE_ANTIGRAVITY` from `.env.example`

**Do NOT touch:** `backends/antigravity_tools.py` — it is independent.

### Step 3: Clean Up Proxy Stubs

In `proxy.py`, remove all try/except blocks referencing:
- [ ] `from akarins_gateway.gateway.backends.antigravity.service import ...`
- [ ] `from akarins_gateway.gateway.backends.antigravity.router import ...`
- [ ] Any `_get_ag_cm`, `_get_ag_cred_mgr` stub variables
- [ ] `_on_antigravity_credential_change()` callback (~15 references)
- [ ] Antigravity backend startup probe with credential check
- [ ] Antigravity pre-filtering with credential check

**Do NOT touch:** `_inject_thinking_blocks_for_antigravity_tools()` and
`backend_key == "antigravity-tools"` references — they belong to the
independent AntigravityToolsBackend.

### Step 4: Clean Up Config

- [ ] Remove `antigravity` backend entry from `config/gateway.yaml`

**Do NOT touch:** `antigravity-tools` config entry — it is independent.

### Step 5: Clean Up Converters

- [ ] Check `converters/message_converter.py` for AG-specific functions
- [ ] Check `conversion.py` for `antigravity_contents_to_openai_messages` usage

### Step 6: Verify

- [ ] Run full compile check: `py_compile` all files
- [ ] Run smoke test: create app, verify routes
- [ ] Run unit tests
- [ ] Confirm no `antigravity` string remains in isolation code (except in docs/history)
- [ ] Confirm `antigravity-tools` references remain intact and functional

## Timeline

| Milestone | Target Date | Status |
|-----------|-------------|--------|
| Isolation complete | 2026-02-27 | Done |
| AT independence refactor | 2026-02-28 | Done |
| Monitor for 2 weeks | 2026-03-13 | Pending |
| Remove if no issues | 2026-03-20 | Pending |

## Author

fufu-chan (Claude Opus 4.6) — 2026-02-27
Updated: 2026-02-28 — AntigravityToolsBackend independence clarification
