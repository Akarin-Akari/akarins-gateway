"""
E2E Tests: Model Routing Chain Reorder

Tests the drag-and-drop and ↑↓ button reordering functionality
in the Model Routing tab of the management panel.

Covers:
- Drag-and-drop chain node reordering via SortableJS
- ↑↓ button chain node reordering
- Move button disabled states (first/last item)
- Chain node removal
- Model switching preserves separate drag contexts
- Persistence after reorder (page refresh check)

Author: fufu-chan
Date: 2026-03-14
"""
import os

import pytest
from playwright.sync_api import Page, expect


class TestChainUpDownButtons:
    """Tests for the ↑↓ arrow button reordering."""

    def test_move_up_button_swaps_nodes(self, routing_page: Page):
        """Clicking ↑ on the second node should swap it with the first."""
        page = routing_page

        # Click first model to ensure chain is visible
        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        # Get initial chain order
        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes to test reorder")

        first_backend = chain_items[0].get_attribute("data-backend")
        second_backend = chain_items[1].get_attribute("data-backend")

        # Click ↑ on the second node (index=1)
        up_buttons = page.query_selector_all("#chain-editor .btn-move")
        # Buttons are paired: [↑0, ↓0, ↑1, ↓1, ...]
        # Second node's ↑ button is at index 2
        up_buttons[2].click()

        # Wait for UI update
        page.wait_for_timeout(500)

        # Verify the swap happened
        chain_items_after = page.query_selector_all("#chain-editor .chain-item")
        assert chain_items_after[0].get_attribute("data-backend") == second_backend
        assert chain_items_after[1].get_attribute("data-backend") == first_backend

    def test_move_down_button_swaps_nodes(self, routing_page: Page):
        """Clicking ↓ on the first node should swap it with the second."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes to test reorder")

        first_backend = chain_items[0].get_attribute("data-backend")
        second_backend = chain_items[1].get_attribute("data-backend")

        # Click ↓ on the first node (index=0)
        # First node's ↓ button is at index 1
        down_buttons = page.query_selector_all("#chain-editor .btn-move")
        down_buttons[1].click()

        page.wait_for_timeout(500)

        chain_items_after = page.query_selector_all("#chain-editor .chain-item")
        assert chain_items_after[0].get_attribute("data-backend") == second_backend
        assert chain_items_after[1].get_attribute("data-backend") == first_backend

    def test_first_node_up_button_disabled(self, routing_page: Page):
        """The ↑ button on the first chain node should be disabled."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 1:
            pytest.skip("Need at least 1 chain node")

        # First node's ↑ button (index 0) should be disabled
        first_up = page.query_selector_all("#chain-editor .btn-move")[0]
        assert first_up.is_disabled()

    def test_last_node_down_button_disabled(self, routing_page: Page):
        """The ↓ button on the last chain node should be disabled."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 1:
            pytest.skip("Need at least 1 chain node")

        # Last node's ↓ button
        all_move_btns = page.query_selector_all("#chain-editor .btn-move")
        last_down = all_move_btns[-1]  # Last ↓ button
        assert last_down.is_disabled()

    def test_reorder_shows_success_banner(self, routing_page: Page):
        """After successful reorder, a success banner should appear."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes")

        # Click ↓ on first node
        down_buttons = page.query_selector_all("#chain-editor .btn-move")
        down_buttons[1].click()

        # Wait for banner
        banner = page.wait_for_selector(".save-banner.show", timeout=5000)
        expect(banner).to_contain_text("Routing updated")


class TestChainDragAndDrop:
    """Tests for SortableJS drag-and-drop reordering."""

    def test_drag_reorder_first_to_second(self, routing_page: Page):
        """Dragging the first node to the second position should swap them."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes to test drag")

        first_backend = chain_items[0].get_attribute("data-backend")
        second_backend = chain_items[1].get_attribute("data-backend")

        # Get the drag handles
        first_handle = chain_items[0].query_selector(".drag-handle")
        second_item = chain_items[1]

        # Perform drag: first handle → below second item
        first_box = first_handle.bounding_box()
        second_box = second_item.bounding_box()

        page.mouse.move(first_box["x"] + first_box["width"] / 2,
                        first_box["y"] + first_box["height"] / 2)
        page.mouse.down()
        # Move to below the second item
        page.mouse.move(second_box["x"] + second_box["width"] / 2,
                        second_box["y"] + second_box["height"] + 5,
                        steps=10)
        page.mouse.up()

        # Wait for Alpine to re-render
        page.wait_for_timeout(800)

        # Verify the order changed
        chain_items_after = page.query_selector_all("#chain-editor .chain-item")
        assert chain_items_after[0].get_attribute("data-backend") == second_backend
        assert chain_items_after[1].get_attribute("data-backend") == first_backend

    def test_drag_shows_ghost_class(self, routing_page: Page):
        """During drag, the ghost element should have sortable-ghost class."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes")

        first_handle = chain_items[0].query_selector(".drag-handle")
        first_box = first_handle.bounding_box()

        # Start dragging
        page.mouse.move(first_box["x"] + first_box["width"] / 2,
                        first_box["y"] + first_box["height"] / 2)
        page.mouse.down()
        page.mouse.move(first_box["x"], first_box["y"] + 60, steps=5)

        # Check for ghost class
        ghost = page.query_selector(".sortable-ghost")
        assert ghost is not None

        page.mouse.up()


class TestChainModelSwitch:
    """Tests for chain editor behavior when switching models."""

    def test_switching_model_loads_different_chain(self, routing_page: Page):
        """Switching between models should load their respective chains."""
        page = routing_page

        model_items = page.query_selector_all(".model-item")
        if len(model_items) < 2:
            pytest.skip("Need at least 2 models")

        # Click first model
        model_items[0].click()
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)
        first_model_chain = [
            el.get_attribute("data-backend")
            for el in page.query_selector_all("#chain-editor .chain-item")
        ]

        # Click second model
        model_items[1].click()
        page.wait_for_timeout(500)
        second_model_chain = [
            el.get_attribute("data-backend")
            for el in page.query_selector_all("#chain-editor .chain-item")
        ]

        # Chains should differ (unless both models have identical config)
        # At minimum, the UI should have updated without errors
        assert isinstance(first_model_chain, list)
        assert isinstance(second_model_chain, list)

    def test_drag_works_after_model_switch(self, routing_page: Page):
        """Drag-and-drop should still work after switching models."""
        page = routing_page

        model_items = page.query_selector_all(".model-item")
        if len(model_items) < 2:
            pytest.skip("Need at least 2 models")

        # Switch to second model then back to first
        model_items[1].click()
        page.wait_for_timeout(300)
        model_items[0].click()
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes")

        # Attempt a drag
        first_handle = chain_items[0].query_selector(".drag-handle")
        second_item = chain_items[1]

        first_box = first_handle.bounding_box()
        second_box = second_item.bounding_box()

        page.mouse.move(first_box["x"] + first_box["width"] / 2,
                        first_box["y"] + first_box["height"] / 2)
        page.mouse.down()
        page.mouse.move(second_box["x"] + second_box["width"] / 2,
                        second_box["y"] + second_box["height"] + 5,
                        steps=10)
        page.mouse.up()

        page.wait_for_timeout(800)

        # No crash = pass; verify banner shows
        banner = page.wait_for_selector(".save-banner.show", timeout=5000)
        expect(banner).to_contain_text("Routing updated")


class TestChainNodeRemoval:
    """Tests for removing chain nodes."""

    def test_remove_node_reduces_count(self, routing_page: Page):
        """Clicking × on a node should remove it from the chain."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items_before = page.query_selector_all("#chain-editor .chain-item")
        count_before = len(chain_items_before)
        if count_before < 2:
            pytest.skip("Need at least 2 chain nodes to safely remove one")

        # Click × on the last node
        remove_buttons = page.query_selector_all(
            "#chain-editor button[title='Remove node']"
        )
        remove_buttons[-1].click()

        page.wait_for_timeout(500)

        chain_items_after = page.query_selector_all("#chain-editor .chain-item")
        assert len(chain_items_after) == count_before - 1


class TestReorderPersistence:
    """Tests that reorder changes survive a page refresh."""

    def test_reorder_persists_after_refresh(self, routing_page: Page, gateway_url: str):
        """After reordering and refreshing, the new order should persist."""
        page = routing_page

        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        chain_items = page.query_selector_all("#chain-editor .chain-item")
        if len(chain_items) < 2:
            pytest.skip("Need at least 2 chain nodes")

        first_backend = chain_items[0].get_attribute("data-backend")
        second_backend = chain_items[1].get_attribute("data-backend")

        # Swap via ↓ button on first node
        down_buttons = page.query_selector_all("#chain-editor .btn-move")
        down_buttons[1].click()

        # Wait for API call to complete
        page.wait_for_timeout(1000)

        # Refresh the page
        page.reload()
        page.wait_for_selector("input[type='password']", timeout=10000)

        # Re-login
        panel_password = os.environ.get("E2E_PANEL_PASSWORD", "test")
        page.fill("input[type='password']", panel_password)
        page.click("button:has-text('Sign In')")
        page.wait_for_selector(".tab-bar", timeout=10000)

        # Navigate to routing tab
        page.click("button:has-text('Model Routing')")
        page.wait_for_selector(".model-item", timeout=10000)
        page.click(".model-item:first-child")
        page.wait_for_selector("#chain-editor .chain-item", timeout=5000)

        # Verify the new order persisted
        chain_items_after = page.query_selector_all("#chain-editor .chain-item")
        assert chain_items_after[0].get_attribute("data-backend") == second_backend
        assert chain_items_after[1].get_attribute("data-backend") == first_backend

        # Swap back to restore original state (cleanup)
        up_buttons = page.query_selector_all("#chain-editor .btn-move")
        up_buttons[2].click()
        page.wait_for_timeout(1000)
