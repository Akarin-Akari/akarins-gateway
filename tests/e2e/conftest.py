"""
E2E Test Fixtures for Akarin's Gateway Panel

Provides a running gateway server and authenticated browser page
for Playwright-based frontend tests.

Author: fufu-chan
Date: 2026-03-14
"""
import os
import time
import subprocess
import signal

import pytest
from playwright.sync_api import Page, expect


# ---- Configuration ----
GATEWAY_HOST = os.environ.get("E2E_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("E2E_GATEWAY_PORT", "9800"))
PANEL_PASSWORD = os.environ.get("E2E_PANEL_PASSWORD", "test")
BASE_URL = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}"


@pytest.fixture(scope="session")
def gateway_url():
    """
    Returns the base URL for the gateway.

    If E2E_GATEWAY_EXTERNAL=1 is set, assumes the gateway is already running
    externally (e.g. during development). Otherwise, you can extend this fixture
    to start the gateway process automatically.
    """
    if os.environ.get("E2E_GATEWAY_EXTERNAL", "1") == "1":
        # Gateway is already running externally
        return BASE_URL

    # Auto-start gateway (optional, requires proper setup)
    proc = subprocess.Popen(
        ["python", "-m", "akarins_gateway.server"],
        env={**os.environ, "GATEWAY_PORT": str(GATEWAY_PORT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to be ready
    time.sleep(3)
    yield BASE_URL
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)


@pytest.fixture(scope="function")
def panel_page(page: Page, gateway_url: str):
    """
    Navigate to the panel page and authenticate.
    Provides a logged-in panel page ready for testing.
    """
    page.goto(f"{gateway_url}/panel/")
    page.wait_for_selector("input[type='password']", timeout=10000)

    # Login
    page.fill("input[type='password']", PANEL_PASSWORD)
    page.click("button:has-text('Sign In')")

    # Wait for dashboard to load
    page.wait_for_selector(".tab-bar", timeout=10000)

    yield page


@pytest.fixture(scope="function")
def routing_page(panel_page: Page):
    """
    Navigate to the Model Routing tab and wait for it to load.
    Provides a panel page on the routing tab with models loaded.
    """
    # Click the Model Routing tab
    panel_page.click("button:has-text('Model Routing')")

    # Wait for model list to appear
    panel_page.wait_for_selector(".model-item", timeout=10000)

    yield panel_page
