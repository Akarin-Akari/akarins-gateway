# 2026-03-14 Model Routing Chain Reorder Bug

## Status: FIXED (2026-03-14)

## Summary

The Model Routing tab (Tab 2) in the Panel has two reorder mechanisms, both currently broken:

1. **SortableJS drag-and-drop** — Alpine.js `x-for` template rendering conflicts with SortableJS DOM manipulation
2. **↑↓ move buttons** — `moveChainNode()` calls `updateRoutingChain()` which calls the API and reloads routing, but the UI doesn't reflect changes (likely Alpine reactivity issue with the routing rules object)

## Root Cause Analysis

### SortableJS + Alpine.js Conflict

Alpine.js `x-for` uses `<template>` tags that render children as direct DOM children of the parent container. When SortableJS reorders DOM nodes, Alpine.js is unaware of the change and may re-render from its own reactive data, undoing the drag.

Attempted fixes:
- `x-init` with `$nextTick` — only initializes once, doesn't survive model switches
- `x-effect` watching `selectedModel` — re-inits Sortable on model change, but drag still doesn't produce visible reorder

### ↑↓ Button Issue

`moveChainNode(model, idx, direction)` in `panel.js`:
```javascript
moveChainNode(model, index, direction) {
    const rule = this.routingRules[model];
    if (!rule) return;
    const chain = [...rule.backend_chain];
    const newIndex = index + direction;
    if (newIndex < 0 || newIndex >= chain.length) return;
    [chain[index], chain[newIndex]] = [chain[newIndex], chain[index]];
    this.updateRoutingChain(model, chain);
},
```

This calls `updateRoutingChain` which does a `PUT /api/panel/routing/{model}` then `loadRouting()`. The issue may be:
- The API response is correct but `loadRouting()` overwrites `routingRules` as a new object, causing Alpine to lose track of `selectedRule` computed property
- Or the `selectedRule` getter returns a stale reference after the rules object is replaced

## Relevant Files

| File | Purpose |
|------|---------|
| `frontend/index.html` L283-313 | Chain editor HTML with `x-for` loop |
| `frontend/js/panel.js` L220-260 | `updateRoutingChain`, `reorderChain`, `moveChainNode`, `removeChainNode` methods |
| `frontend/js/panel.js` L200-218 | `selectedRule` getter, `routingRules` data |
| `frontend/js/sortable-helpers.js` | `initChainSortable()` helper (currently unused) |
| `frontend/css/panel.css` L311-330 | `.btn-move` and `.chain-node` styles |
| `akarins_gateway/gateway/endpoints/panel.py` | `PUT /routing/{model}` and `POST /routing/{model}/reorder` API endpoints |

## Suggested Investigation Steps

1. **Check Alpine reactivity**: Open browser DevTools, go to Console, after clicking ↑↓ verify:
   - Does the API call succeed? (Network tab)
   - Does `Alpine.store('panel').routingRules` update correctly after `loadRouting()`?
   - Does `Alpine.store('panel').selectedRule` return the updated chain?

2. **Check if `loadRouting` replaces the object**: If `this.routingRules = data.rules` replaces the entire object, Alpine loses reactive tracking. Fix: use `Object.assign(this.routingRules, data.rules)` instead.

3. **For SortableJS**: Consider using a vanilla JS approach instead of Alpine `x-for`:
   - Render chain items with `innerHTML` manually
   - Let SortableJS own the DOM completely
   - On `onEnd`, read DOM order and call API
   - On model switch, re-render manually

4. **Alternative**: Use a lightweight framework-agnostic sortable like `@shopify/draggable` or just implement manual drag with HTML5 Drag and Drop API.

## Current Workaround

The ↑↓ buttons and drag handle are present in the UI but non-functional. Users must edit `gateway.yaml` directly to reorder fallback chains, then use "Save Config" is not applicable (save writes memory → YAML, not the other way).

For now, reorder must be done by editing `config/gateway.yaml` manually and restarting the gateway.
