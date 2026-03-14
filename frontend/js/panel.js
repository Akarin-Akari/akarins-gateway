/**
 * Akarin's Gateway — Management Panel
 * Alpine.js Application Store
 *
 * Author: fufu-chan (Claude Opus 4.6)
 * Date: 2026-03-14
 */

document.addEventListener('alpine:init', () => {

    // i18n helper — shorthand
    const t = (key, params) => Alpine.store('i18n').t(key, params);

    Alpine.store('panel', {
        // ---- Auth State ----
        authenticated: false,
        token: '',
        loginError: '',
        loginLoading: false,

        // ---- UI State ----
        activeTab: 'backends',
        saveBanner: { show: false, type: 'success', message: '' },
        loading: false,

        // ---- Backend Data ----
        backends: [],
        expandedBackend: null,

        // ---- Add Backend Form ----
        showAddBackendForm: false,
        newBackend: {
            key: '', name: '', base_url: '', priority: 99,
            timeout: 60, stream_timeout: 300, max_retries: 2,
            api_format: 'openai', enabled: true,
        },

        // ---- Routing Data ----
        routingRules: {},
        selectedModel: null,
        routingModels: [],

        // ---- Health Data ----
        healthData: null,
        statsData: null,
        healthInterval: null,

        // ---- Client Settings Data ----
        clientSettings: {},
        clientFeatureDescriptions: {},

        // ---- Hidden Rules Data (P1: newly exposed) ----
        crossModelFallback: { enabled: true, rules: [] },
        defaultRouting: { rules: [], catch_all: null },
        finalFallback: { enabled: true, backend: 'copilot', respect_circuit_breaker: true },

        // ---- P2: Backend Capabilities + Copilot Model Mapping ----
        backendCapabilities: {},
        copilotModelMapping: {},
        newMappingSource: '',
        newMappingTarget: '',
        newCapPattern: {},  // { backendName: '' }

        // ---- [REFACTOR 2026-03-14] Runtime Flags (AnyRouter env var migration) ----
        runtimeFlags: {},  // { flagName: { effective, yaml_value, env_value, default } }

        // ---- New Key Input ----
        newKeyInput: '',

        // ==================== Auth ====================
        async login(password) {
            this.loginLoading = true;
            this.loginError = '';
            try {
                const res = await this._fetch('/api/panel/auth/verify', { method: 'POST' }, password);
                if (res.authenticated) {
                    this.authenticated = true;
                    this.token = password;
                    sessionStorage.setItem('panel_token', password);
                    await this.loadBackends();
                    await this.loadRouting();
                    await this.loadClientSettings();
                    await this.loadHiddenRules();
                    this.startHealthPolling();
                }
            } catch (e) {
                this.loginError = e.message || t('msg.auth_failed');
            } finally {
                this.loginLoading = false;
            }
        },

        tryRestoreSession() {
            const saved = sessionStorage.getItem('panel_token');
            if (saved) {
                this.token = saved;
                this.authenticated = true;
                this.loadBackends();
                this.loadRouting();
                this.loadClientSettings();
                this.loadHiddenRules();
                this.startHealthPolling();
            }
        },

        logout() {
            this.authenticated = false;
            this.token = '';
            sessionStorage.removeItem('panel_token');
            if (this.healthInterval) {
                clearInterval(this.healthInterval);
                this.healthInterval = null;
            }
        },

        // ==================== API Helper ====================
        async _fetch(path, options = {}, tokenOverride = null) {
            const tk = tokenOverride || this.token;
            const headers = {
                'Authorization': `Bearer ${tk}`,
                'Content-Type': 'application/json',
                ...(options.headers || {}),
            };
            const res = await fetch(path, { ...options, headers });
            if (res.status === 401 || res.status === 403) {
                this.logout();
                throw new Error(t('msg.auth_failed'));
            }
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                throw new Error(body.detail || `HTTP ${res.status}`);
            }
            return res.json();
        },

        // ==================== Backends ====================
        async loadBackends() {
            try {
                const data = await this._fetch('/api/panel/backends');
                this.backends = data.backends || [];
            } catch (e) {
                console.error('Failed to load backends:', e);
            }
        },

        async toggleBackend(key) {
            try {
                const res = await this._fetch(`/api/panel/backends/${key}/toggle`, { method: 'POST' });
                const b = this.backends.find(b => b.key === key);
                if (b) b.enabled = res.enabled;
            } catch (e) {
                this.showBanner('error', t('msg.toggle_failed', { error: e.message }));
            }
        },

        async toggleScid(key) {
            try {
                const res = await this._fetch(`/api/panel/backends/${key}/scid`, { method: 'POST' });
                const b = this.backends.find(b => b.key === key);
                if (b) b.scid_enabled = res.scid_enabled;
            } catch (e) {
                this.showBanner('error', t('msg.scid_failed', { error: e.message }));
            }
        },

        async updateBackend(key, updates) {
            try {
                await this._fetch(`/api/panel/backends/${key}`, {
                    method: 'PUT',
                    body: JSON.stringify(updates),
                });
                this.showBanner('success', t('msg.backend_updated', { key }));
            } catch (e) {
                this.showBanner('error', t('msg.toggle_failed', { error: e.message }));
            }
        },

        async reorderBackends(order) {
            try {
                await this._fetch('/api/panel/backends/reorder', {
                    method: 'POST',
                    body: JSON.stringify({ order }),
                });
                // Update local priorities
                order.forEach((key, idx) => {
                    const b = this.backends.find(b => b.key === key);
                    if (b) b.priority = idx;
                });
            } catch (e) {
                this.showBanner('error', t('msg.reorder_failed', { error: e.message }));
            }
        },

        async addApiKey(backendKey) {
            if (!this.newKeyInput.trim()) return;
            try {
                await this._fetch(`/api/panel/backends/${backendKey}/keys`, {
                    method: 'POST',
                    body: JSON.stringify({ key: this.newKeyInput.trim() }),
                });
                this.newKeyInput = '';
                await this.loadBackends();
                this.showBanner('success', t('msg.key_added'));
            } catch (e) {
                this.showBanner('error', t('msg.key_add_failed', { error: e.message }));
            }
        },

        async deleteApiKey(backendKey, index) {
            try {
                await this._fetch(`/api/panel/backends/${backendKey}/keys/${index}`, {
                    method: 'DELETE',
                });
                await this.loadBackends();
                this.showBanner('success', t('msg.key_removed'));
            } catch (e) {
                this.showBanner('error', t('msg.key_remove_failed', { error: e.message }));
            }
        },

        async resetCircuitBreaker(key) {
            try {
                await this._fetch(`/api/panel/backends/${key}/circuit-breaker/reset`, {
                    method: 'POST',
                });
                await this.loadBackends();
                this.showBanner('success', t('msg.cb_reset', { key }));
            } catch (e) {
                this.showBanner('error', t('msg.cb_reset_failed', { error: e.message }));
            }
        },

        toggleExpand(key) {
            this.expandedBackend = this.expandedBackend === key ? null : key;
        },

        // ==================== Add Backend ====================
        async addBackend() {
            const nb = this.newBackend;
            if (!nb.key.trim()) {
                this.showBanner('error', t('msg.backend_key_required'));
                return;
            }
            if (this.backends.find(b => b.key === nb.key.trim())) {
                this.showBanner('error', t('msg.backend_key_exists', { key: nb.key.trim() }));
                return;
            }
            try {
                await this._fetch('/api/panel/backends', {
                    method: 'POST',
                    body: JSON.stringify({
                        key: nb.key.trim(),
                        name: nb.name.trim() || nb.key.trim(),
                        base_url: nb.base_url.trim(),
                        priority: parseInt(nb.priority) || 99,
                        timeout: parseFloat(nb.timeout) || 60,
                        stream_timeout: parseFloat(nb.stream_timeout) || 300,
                        max_retries: parseInt(nb.max_retries) || 2,
                        api_format: nb.api_format || 'openai',
                        enabled: nb.enabled,
                    }),
                });
                this.showBanner('success', t('msg.backend_added', { key: nb.key.trim() }));
                this.resetNewBackendForm();
                await this.loadBackends();
            } catch (e) {
                this.showBanner('error', t('msg.backend_add_failed', { error: e.message }));
            }
        },

        resetNewBackendForm() {
            this.newBackend = {
                key: '', name: '', base_url: '', priority: 99,
                timeout: 60, stream_timeout: 300, max_retries: 2,
                api_format: 'openai', enabled: true,
            };
            this.showAddBackendForm = false;
        },

        // ==================== Routing ====================
        async loadRouting() {
            try {
                const data = await this._fetch('/api/panel/routing');
                const newRules = data.routing || {};
                // Remove models that no longer exist on server
                for (const key of Object.keys(this.routingRules)) {
                    if (!(key in newRules)) delete this.routingRules[key];
                }
                // Merge updated/new models into existing reactive object
                for (const [key, val] of Object.entries(newRules)) {
                    this.routingRules[key] = val;
                }
                this.routingModels = Object.keys(this.routingRules).sort();
                if (this.routingModels.length > 0 && !this.selectedModel) {
                    this.selectedModel = this.routingModels[0];
                }
            } catch (e) {
                console.error('Failed to load routing:', e);
            }
        },

        get selectedRule() {
            if (!this.selectedModel || !this.routingRules[this.selectedModel]) return null;
            return this.routingRules[this.selectedModel];
        },

        async updateRoutingChain(model, chain) {
            try {
                const rule = this.routingRules[model];
                // Optimistic local update for immediate UI feedback
                if (rule) rule.backend_chain = chain;
                await this._fetch(`/api/panel/routing/${model}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        backend_chain: chain,
                        fallback_on: rule ? rule.fallback_on : [],
                    }),
                });
                this.showBanner('success', t('msg.routing_updated', { model }));
            } catch (e) {
                // On error, reload from server to revert optimistic update
                await this.loadRouting();
                this.showBanner('error', t('msg.routing_failed', { error: e.message }));
            }
        },

        async reorderChain(model, newOrder) {
            await this.updateRoutingChain(model, newOrder);
        },

        removeChainNode(model, index) {
            const rule = this.routingRules[model];
            if (!rule) return;
            const chain = [...rule.backend_chain];
            chain.splice(index, 1);
            this.updateRoutingChain(model, chain);
        },

        moveChainNode(model, index, direction) {
            const rule = this.routingRules[model];
            if (!rule) return;
            const chain = [...rule.backend_chain];
            const newIndex = index + direction;
            if (newIndex < 0 || newIndex >= chain.length) return;
            [chain[index], chain[newIndex]] = [chain[newIndex], chain[index]];
            // Optimistic local update for instant UI feedback
            rule.backend_chain = chain;
            this.updateRoutingChain(model, chain);
        },

        addChainNode(model, backend, targetModel) {
            const rule = this.routingRules[model];
            if (!rule) return;
            const chain = [...rule.backend_chain];
            chain.push({ backend: backend, model: targetModel || model });
            this.updateRoutingChain(model, chain);
        },

        async addModel(modelName) {
            const model = modelName.toLowerCase();
            if (this.routingRules[model]) {
                this.showBanner('error', t('msg.model_exists', { model }));
                return;
            }
            // Create new empty rule via API
            try {
                await this._fetch(`/api/panel/routing/${model}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        backend_chain: [],
                        fallback_on: [429, 500, 502, 503, 504, 'timeout', 'connection_error', 'unavailable'],
                    }),
                });
                await this.loadRouting();
                this.selectedModel = model;
                this.showBanner('success', t('msg.model_added', { model }));
            } catch (e) {
                this.showBanner('error', t('msg.model_add_failed', { error: e.message }));
            }
        },

        toggleFallbackCondition(model, condition) {
            const rule = this.routingRules[model];
            if (!rule) return;
            const conditions = new Set(rule.fallback_on.map(c => String(c)));
            const condStr = String(condition);
            if (conditions.has(condStr)) {
                conditions.delete(condStr);
            } else {
                conditions.add(condStr);
            }
            // Update in-memory
            rule.fallback_on = Array.from(conditions).map(c => {
                const n = parseInt(c);
                return isNaN(n) ? c : n;
            });
            // Persist
            this._fetch(`/api/panel/routing/${model}`, {
                method: 'PUT',
                body: JSON.stringify({
                    backend_chain: rule.backend_chain,
                    fallback_on: rule.fallback_on,
                }),
            }).catch(e => this.showBanner('error', e.message));
        },

        isFallbackActive(model, condition) {
            const rule = this.routingRules[model];
            if (!rule) return false;
            return rule.fallback_on.map(String).includes(String(condition));
        },

        // ==================== Client Settings ====================
        async loadClientSettings() {
            try {
                const data = await this._fetch('/api/panel/clients');
                this.clientSettings = data.clients || {};
                this.clientFeatureDescriptions = data.features || {};
            } catch (e) {
                console.error('Failed to load client settings:', e);
            }
        },

        get clientTypes() {
            return Object.keys(this.clientSettings);
        },

        getClientDisplayName(clientType) {
            const s = this.clientSettings[clientType];
            return s ? s.display_name : clientType;
        },

        isClientFeatureEnabled(clientType, feature) {
            const s = this.clientSettings[clientType];
            return s ? !!s[feature] : false;
        },

        async toggleClientFeature(clientType, feature) {
            const current = this.isClientFeatureEnabled(clientType, feature);
            const newVal = !current;
            try {
                await this._fetch(`/api/panel/clients/${clientType}`, {
                    method: 'PUT',
                    body: JSON.stringify({ feature, enabled: newVal }),
                });
                // Update local state
                if (this.clientSettings[clientType]) {
                    this.clientSettings[clientType][feature] = newVal;
                }
                // Force Alpine reactivity by reassigning
                this.clientSettings = { ...this.clientSettings };
                this.showBanner('success', t('msg.client_toggled', {
                    client: this.getClientDisplayName(clientType),
                    feature: feature,
                    state: newVal ? 'ON' : 'OFF',
                }));
            } catch (e) {
                this.showBanner('error', t('msg.client_toggle_failed', { error: e.message }));
            }
        },

        featureLabel(feature) {
            const labels = {
                sanitization: 'Sanitize',
                cross_pool_fallback: 'Cross-Pool',
                stateless: 'Stateless',
                signature_recovery_only: 'Sig Recovery',
                scid: 'SCID',
            };
            return labels[feature] || feature;
        },

        featureTooltip(feature) {
            return this.clientFeatureDescriptions[feature] || feature;
        },

        clientIcon(ct) {
            const icons = {
                claude_code: '\u{1F4BB}',
                cursor: '\u{1F5B1}',
                augment: '\u{1F50C}',
                windsurf: '\u{1F3C4}',
                cline: '\u2328',
                continue_dev: '\u25B6',
                aider: '\u{1F916}',
                zed: '\u26A1',
                copilot: '\u2708',
                openai_api: '\u{1F310}',
            };
            return icons[ct] || '\u2753';
        },

        // ==================== Hidden Rules CRUD (P1) ====================

        async loadHiddenRules() {
            await Promise.all([
                this.loadCrossModelFallback(),
                this.loadDefaultRouting(),
                this.loadFinalFallback(),
                this.loadBackendCapabilities(),
                this.loadCopilotModelMapping(),
                this.loadRuntimeFlags(),
            ]);
        },

        async loadCrossModelFallback() {
            try {
                const data = await this._fetch('/api/panel/cross-model-fallback');
                this.crossModelFallback = {
                    enabled: data.enabled ?? true,
                    rules: data.rules || [],
                };
            } catch (e) {
                console.error('Failed to load cross-model fallback:', e);
            }
        },

        async saveCrossModelFallback() {
            try {
                await this._fetch('/api/panel/cross-model-fallback', {
                    method: 'PUT',
                    body: JSON.stringify(this.crossModelFallback),
                });
                this.showBanner('success', t('msg.xmodel_saved'));
            } catch (e) {
                this.showBanner('error', t('msg.xmodel_failed', { error: e.message }));
            }
        },

        addCrossModelRule() {
            this.crossModelFallback.rules.push({
                pattern: '*',
                fallback_model: 'gemini-3-pro-high',
                backend: 'gcli2api-antigravity',
            });
        },

        removeCrossModelRule(index) {
            this.crossModelFallback.rules.splice(index, 1);
        },

        async loadDefaultRouting() {
            try {
                const data = await this._fetch('/api/panel/default-routing');
                this.defaultRouting = {
                    rules: data.rules || [],
                    catch_all: data.catch_all || null,
                };
            } catch (e) {
                console.error('Failed to load default routing:', e);
            }
        },

        async saveDefaultRouting() {
            try {
                await this._fetch('/api/panel/default-routing', {
                    method: 'PUT',
                    body: JSON.stringify(this.defaultRouting),
                });
                this.showBanner('success', t('msg.defroute_saved'));
            } catch (e) {
                this.showBanner('error', t('msg.defroute_failed', { error: e.message }));
            }
        },

        addDefaultRoutingRule() {
            this.defaultRouting.rules.push({
                pattern: 'new-model-*',
                chain: [{ backend: 'copilot' }],
                fallback_on: ['429', '500', '502', '503', 'timeout', 'connection_error'],
            });
        },

        removeDefaultRoutingRule(index) {
            this.defaultRouting.rules.splice(index, 1);
        },

        addChainNodeToDefaultRule(ruleIndex) {
            this.defaultRouting.rules[ruleIndex].chain.push({ backend: 'copilot' });
        },

        removeChainNodeFromDefaultRule(ruleIndex, nodeIndex) {
            this.defaultRouting.rules[ruleIndex].chain.splice(nodeIndex, 1);
        },

        async loadFinalFallback() {
            try {
                const data = await this._fetch('/api/panel/final-fallback');
                this.finalFallback = {
                    enabled: data.enabled ?? true,
                    backend: data.backend || 'copilot',
                    respect_circuit_breaker: data.respect_circuit_breaker ?? true,
                };
            } catch (e) {
                console.error('Failed to load final fallback:', e);
            }
        },

        async saveFinalFallback() {
            try {
                await this._fetch('/api/panel/final-fallback', {
                    method: 'PUT',
                    body: JSON.stringify(this.finalFallback),
                });
                this.showBanner('success', t('msg.finalfb_saved'));
            } catch (e) {
                this.showBanner('error', t('msg.finalfb_failed', { error: e.message }));
            }
        },

        // ==================== P2: Backend Capabilities ====================

        async loadBackendCapabilities() {
            try {
                const data = await this._fetch('/api/panel/backend-capabilities');
                this.backendCapabilities = data.capabilities || {};
            } catch (e) {
                console.error('Failed to load backend capabilities:', e);
            }
        },

        async saveBackendCapabilities() {
            try {
                await this._fetch('/api/panel/backend-capabilities', {
                    method: 'PUT',
                    body: JSON.stringify({ capabilities: this.backendCapabilities }),
                });
                this.showBanner('success', t('msg.caps_saved'));
            } catch (e) {
                this.showBanner('error', t('msg.caps_failed', { error: e.message }));
            }
        },

        addCapabilityPattern(backend, type) {
            const key = `${backend}_${type}`;
            const pattern = (this.newCapPattern[key] || '').trim();
            if (!pattern) return;
            if (!this.backendCapabilities[backend]) {
                this.backendCapabilities[backend] = { include_patterns: [], exclude_patterns: [] };
            }
            if (!this.backendCapabilities[backend][type]) {
                this.backendCapabilities[backend][type] = [];
            }
            this.backendCapabilities[backend][type].push(pattern);
            this.newCapPattern[key] = '';
        },

        removeCapabilityPattern(backend, type, idx) {
            if (this.backendCapabilities[backend] && this.backendCapabilities[backend][type]) {
                this.backendCapabilities[backend][type].splice(idx, 1);
            }
        },

        // ==================== P2: Copilot Model Mapping ====================

        async loadCopilotModelMapping() {
            try {
                const data = await this._fetch('/api/panel/copilot-model-mapping');
                this.copilotModelMapping = data.mapping || {};
            } catch (e) {
                console.error('Failed to load copilot model mapping:', e);
            }
        },

        async saveCopilotModelMapping() {
            try {
                await this._fetch('/api/panel/copilot-model-mapping', {
                    method: 'PUT',
                    body: JSON.stringify({ mapping: this.copilotModelMapping }),
                });
                this.showBanner('success', t('msg.mapping_saved'));
            } catch (e) {
                this.showBanner('error', t('msg.mapping_failed', { error: e.message }));
            }
        },

        addMappingEntry() {
            const src = this.newMappingSource.trim().toLowerCase();
            const tgt = this.newMappingTarget.trim();
            if (!src || !tgt) return;
            this.copilotModelMapping[src] = tgt;
            this.newMappingSource = '';
            this.newMappingTarget = '';
        },

        removeMappingEntry(key) {
            delete this.copilotModelMapping[key];
            // Trigger Alpine reactivity
            this.copilotModelMapping = { ...this.copilotModelMapping };
        },

        // ==================== Runtime Flags (AnyRouter env var migration) ====================

        async loadRuntimeFlags() {
            try {
                const data = await this._fetch('/api/panel/runtime-flags');
                this.runtimeFlags = data.flags || {};
            } catch (e) {
                console.error('Failed to load runtime flags:', e);
            }
        },

        async saveRuntimeFlags() {
            try {
                // Build flags dict from effective values
                const flags = {};
                for (const [name, info] of Object.entries(this.runtimeFlags)) {
                    flags[name] = info.effective;
                }
                await this._fetch('/api/panel/runtime-flags', {
                    method: 'PUT',
                    body: JSON.stringify({ flags }),
                });
                this.showBanner('success', t('msg.flags_saved'));
                // Reload to get fresh effective values
                await this.loadRuntimeFlags();
            } catch (e) {
                this.showBanner('error', t('msg.flags_failed', { error: e.message }));
            }
        },

        toggleRuntimeFlag(name) {
            if (this.runtimeFlags[name]) {
                this.runtimeFlags[name].effective = !this.runtimeFlags[name].effective;
            }
        },

        // ==================== Health ====================
        async loadHealth() {
            try {
                const [health, stats] = await Promise.all([
                    this._fetch('/api/panel/health'),
                    this._fetch('/api/panel/stats'),
                ]);
                this.healthData = health;
                this.statsData = stats.stats;
            } catch (e) {
                console.error('Health poll failed:', e);
            }
        },

        startHealthPolling() {
            this.loadHealth();
            if (this.healthInterval) clearInterval(this.healthInterval);
            this.healthInterval = setInterval(() => this.loadHealth(), 10000);
        },

        getHealthColor(rate) {
            if (rate >= 95) return 'var(--accent-green)';
            if (rate >= 80) return 'var(--accent-yellow)';
            return 'var(--accent-red)';
        },

        // ==================== Config Save ====================
        async saveConfig() {
            try {
                this.loading = true;
                await this._fetch('/api/panel/config/save', { method: 'POST' });
                this.showBanner('success', t('msg.config_saved'));
            } catch (e) {
                this.showBanner('error', t('msg.config_failed', { error: e.message }));
            } finally {
                this.loading = false;
            }
        },

        // ==================== UI Helpers ====================
        showBanner(type, message) {
            this.saveBanner = { show: true, type, message };
            setTimeout(() => { this.saveBanner.show = false; }, 3000);
        },

        formatUptime(seconds) {
            if (!seconds) return '0s';
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            if (h > 0) return `${h}h ${m}m`;
            if (m > 0) return `${m}m`;
            return `${Math.floor(seconds)}s`;
        },

        cbStateLabel(state) {
            const labels = { closed: 'Closed', open: 'Open', half_open: 'Half-Open' };
            return labels[state] || state;
        },
    });
});
