/**
 * SortableJS Helper Functions
 *
 * Encapsulates SortableJS initialization for backend cards
 * and fallback chain nodes.
 *
 * Author: fufu-chan (Claude Opus 4.6)
 * Date: 2026-03-14
 */

/**
 * Initialize drag-sort for backend cards.
 * @param {string} containerId - DOM element ID of the container
 * @param {Function} onReorder - Callback with new order array of backend keys
 */
function initBackendSortable(containerId, onReorder) {
    const el = document.getElementById(containerId);
    if (!el) return null;

    return new Sortable(el, {
        handle: '.drag-handle',
        animation: 200,
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        onEnd: function (evt) {
            // Collect new order from data-key attributes
            const items = el.querySelectorAll('[data-key]');
            const order = Array.from(items).map(item => item.dataset.key);
            if (onReorder) onReorder(order);
        },
    });
}

/**
 * Initialize drag-sort for fallback chain nodes.
 *
 * Uses the "DOM revert" strategy to avoid conflicts with Alpine.js x-for:
 * 1. SortableJS physically moves the DOM node during drag
 * 2. In onEnd, we REVERT the DOM change (put element back)
 * 3. Then update Alpine's reactive data via callback
 * 4. Alpine re-renders the list from data, producing correct DOM order
 *
 * @param {string} containerId - DOM element ID of the chain container
 * @param {string|null} selectedModel - Currently selected model (triggers re-init on change)
 * @param {Function} onReorder - Callback with new chain order array
 */
let _chainSortableInstance = null;

function initChainSortable(containerId, selectedModel, onReorder) {
    // Destroy previous instance on model switch or re-init
    if (_chainSortableInstance) {
        _chainSortableInstance.destroy();
        _chainSortableInstance = null;
    }

    if (!selectedModel) return;

    // Use requestAnimationFrame to ensure Alpine has finished rendering
    requestAnimationFrame(() => {
        const el = document.getElementById(containerId);
        if (!el) return;

        _chainSortableInstance = new Sortable(el, {
            handle: '.drag-handle',
            animation: 200,
            ghostClass: 'sortable-ghost',
            draggable: '.chain-item',
            onEnd: function (evt) {
                const { item, from, oldIndex, newIndex } = evt;

                if (oldIndex === newIndex) return;

                // Step 1: Revert SortableJS's DOM manipulation
                // Put the dragged item back to its original position
                // so Alpine's virtual DOM stays consistent
                from.removeChild(item);
                const refNode = from.children[oldIndex] || null;
                from.insertBefore(item, refNode);

                // Step 2: Build the new order by applying the move
                const items = el.querySelectorAll('.chain-item');
                const currentOrder = Array.from(items).map(it => ({
                    backend: it.dataset.backend,
                    model: it.dataset.model,
                }));
                const newOrder = [...currentOrder];
                const [moved] = newOrder.splice(oldIndex, 1);
                newOrder.splice(newIndex, 0, moved);

                // Step 3: Notify Alpine via callback — Alpine will re-render
                if (onReorder) onReorder(newOrder);
            },
        });
    });
}
