"""
Hypercorn Server Launcher for Akarin's Gateway.

Handles:
  - Port availability pre-flight check (Windows-aware)
  - Automatic fallback to adjacent ports on PermissionError
  - Hypercorn configuration (100MB body, 5min timeouts)

Usage:
    python -m akarins_gateway
    # or
    python akarins_gateway/server.py

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-02-27
"""

import asyncio
import platform
import socket
import subprocess

from akarins_gateway.core.config import get_server_host, get_server_port
from akarins_gateway.core.log import log


def _check_port(host: str, port: int) -> int:
    """
    Pre-flight port availability check.

    Returns the available port (may differ from input if fallback was needed).
    Raises SystemExit if no port is available.
    """
    bind_host = "127.0.0.1" if host == "0.0.0.0" else host
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        # SO_REUSEADDR on Windows allows binding to ports already in use,
        # masking real conflicts. Only enable on non-Windows platforms.
        if platform.system() != "Windows":
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        test_sock.bind((bind_host, port))
        log.info(f"[STARTUP] Port {port} available")
        return port

    except PermissionError:
        log.error(f"[STARTUP] Port {port} bind failed: permission denied")
        log.error("[STARTUP] Possible cause: port in Windows TCP exclusion range")
        log.error(f"[STARTUP] Try: netsh int ipv4 show excludedportrange protocol=tcp")

        # Auto-fallback to adjacent ports
        for offset in range(1, 10):
            try_port = port + offset
            try:
                sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock2.bind((bind_host, try_port))
                sock2.close()
                log.warning(f"[STARTUP] Falling back to port {try_port}")
                return try_port
            except OSError:
                try:
                    sock2.close()
                except Exception:
                    pass

        log.error(f"[STARTUP] Ports {port}~{port + 9} all unavailable")
        raise SystemExit(1)

    except OSError as e:
        if hasattr(e, "winerror") and e.winerror == 10048:
            log.error(f"[STARTUP] Port {port} already in use (WinError 10048)")
            try:
                result = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        log.error(f"[STARTUP]   -> {line.strip()}")
            except Exception:
                pass
        else:
            log.error(f"[STARTUP] Port {port} bind failed: {e}")
        raise SystemExit(1)

    finally:
        test_sock.close()


async def serve():
    """Start the Hypercorn server with the gateway app."""
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config

    from akarins_gateway.app import app

    host = get_server_host()
    port = get_server_port()

    # Pre-flight port check
    port = _check_port(host, port)

    log.info("=" * 60)
    log.info("  Akarin's Gateway")
    log.info("=" * 60)
    log.info(f"  Server:  http://{host}:{port}")
    log.info(f"  OpenAI:  http://127.0.0.1:{port}/v1/chat/completions")
    log.info(f"  Models:  http://127.0.0.1:{port}/v1/models")
    log.info(f"  Augment: http://127.0.0.1:{port}/gateway/chat-stream")
    log.info("=" * 60)

    # Hypercorn configuration
    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = "-"
    config.errorlog = "-"
    config.loglevel = "INFO"

    # 100MB body limit
    config.max_request_body_size = 100 * 1024 * 1024

    # 5-minute timeouts
    config.keep_alive_timeout = 300
    config.read_timeout = 300
    config.write_timeout = 300

    # 2-minute startup timeout (for slow backend probes)
    config.startup_timeout = 120

    await hypercorn_serve(app, config)


def main():
    """Synchronous entry point."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
