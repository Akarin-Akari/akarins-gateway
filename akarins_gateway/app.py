"""
FastAPI Application Factory for Akarin's Gateway.

Simplified lifespan — removes gcli2api-specific dependencies:
  - No CredentialManager
  - No Antigravity version checker
  - No token_stats database
  - No legacy routers (openai_router, gemini_router, antigravity_router, web_router)

Retains:
  - Backend health probing on startup
  - IDE compatibility middleware
  - Signature cache migration mode
  - CORS middleware
  - Gateway + Augment routers

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-02-27
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from akarins_gateway.core.log import log


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown hooks."""

    log.info("Starting Akarin's Gateway")

    # ---------- Signature cache migration (DUAL_WRITE) ----------
    try:
        from akarins_gateway.signature_cache import (
            enable_migration_mode,
            set_migration_phase,
            get_migration_status,
        )
        enable_migration_mode()
        set_migration_phase("DUAL_WRITE")
        status = get_migration_status()
        log.info(f"[CACHE] Dual-write migration enabled: {status}")
    except Exception as e:
        log.warning(f"[CACHE] Dual-write init failed (non-fatal): {e}")

    # ---------- Default password warning ----------
    try:
        from akarins_gateway.core.config import get_api_password
        if get_api_password() == "pwd":
            log.warning("=" * 60)
            log.warning("  WARNING: Using default API password 'pwd'!")
            log.warning("  Set API_PASSWORD or PASSWORD env var for production.")
            log.warning("=" * 60)
    except Exception:
        pass

    # ---------- Backend health probing ----------
    # [FIX 2026-03-02] 等待 30 秒再探测，给其他后端服务留足启动时间
    # 网关通常先于 zerogravity、copilot、kiro-gateway 等后端启动完成，
    # 过早探测会导致所有后端被误冻结 5 分钟
    startup_probe_delay = float(os.environ.get("STARTUP_PROBE_DELAY", "30"))
    log.info(f"[STARTUP] Waiting {startup_probe_delay}s for backends to initialize...")
    await asyncio.sleep(startup_probe_delay)

    try:
        from akarins_gateway.gateway.proxy import probe_backends_on_startup
        probe_results = await probe_backends_on_startup()
        reachable = sum(1 for v in probe_results.values() if v)
        total = len(probe_results)
        log.info(f"[STARTUP] Backend probe complete: {reachable}/{total} reachable")
    except Exception as e:
        log.warning(f"[STARTUP] Backend probe failed (non-fatal): {e}")

    # ---------- Model Registry initialization ----------
    refresh_task = None
    try:
        from akarins_gateway.gateway.model_registry import get_model_registry
        from akarins_gateway.gateway.config import BACKENDS
        registry = get_model_registry()
        if registry.enabled:
            registry.initialize(BACKENDS)
            log.info("[STARTUP] ModelRegistry initialized with static config")

            # First dynamic refresh (after backend probe)
            await registry.refresh_all()
            log.info("[STARTUP] ModelRegistry first refresh complete")

            # Start periodic refresh task
            refresh_interval = float(
                os.environ.get("MODEL_REGISTRY_REFRESH_INTERVAL", "300")
            )

            async def _periodic_refresh():
                while True:
                    await asyncio.sleep(refresh_interval)
                    try:
                        await registry.refresh_all()
                    except Exception as exc:
                        log.warning(
                            f"[MODEL_REGISTRY] Periodic refresh failed: {exc}"
                        )

            refresh_task = asyncio.create_task(_periodic_refresh())
            log.info(
                f"[STARTUP] ModelRegistry periodic refresh started "
                f"(interval={refresh_interval}s)"
            )
        else:
            log.info("[STARTUP] ModelRegistry disabled via env")
    except Exception as e:
        log.warning(f"[STARTUP] ModelRegistry init failed (non-fatal): {e}")

    yield

    # ---------- Shutdown ----------
    if refresh_task is not None:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
    log.info("Shutting down Akarin's Gateway")


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        Configured FastAPI instance with all routes mounted.
    """
    app = FastAPI(
        title="Akarin's Gateway",
        description="Multi-backend API Gateway with IDE compatibility",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ---- CORS ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- IDE Compatibility Middleware ----
    try:
        from akarins_gateway.ide_compat import IDECompatMiddleware
        app.add_middleware(IDECompatMiddleware)
        log.info("[STARTUP] IDE compatibility middleware enabled")
    except Exception as e:
        log.warning(f"[STARTUP] IDE compat middleware failed (non-fatal): {e}")

    # ---- Gateway Router (primary) ----
    from akarins_gateway.gateway.endpoints import create_gateway_router
    gateway_router = create_gateway_router()

    # Dual-prefix mount:
    #   prefix=""         -> /v1/chat/completions (primary)
    #   prefix="/gateway" -> /gateway/v1/chat/completions (backward compat)
    app.include_router(gateway_router, prefix="", tags=["Gateway"])
    app.include_router(gateway_router, prefix="/gateway", tags=["Gateway (compat)"])

    # ---- Augment Code Compatibility Router ----
    from akarins_gateway.gateway.augment import create_augment_router
    augment_router = create_augment_router()
    app.include_router(augment_router, prefix="/gateway", tags=["Augment Code"])

    # ---- Keepalive endpoint ----
    @app.head("/keepalive")
    async def keepalive() -> Response:
        return Response(status_code=200)

    return app


# Module-level app instance for Hypercorn / uvicorn
app = create_app()
