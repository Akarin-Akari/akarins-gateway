/**
 * Akarin's Gateway — i18n (Internationalization)
 * Alpine.js Store for Chinese/English language switching
 *
 * Author: fufu-chan (Gemini 2.5 Pro)
 * Date: 2026-03-14
 */

document.addEventListener('alpine:init', () => {

    const translations = {
        en: {
            // ---- Login ----
            'login.title': "Akarin's Gateway",
            'login.subtitle': 'Management Panel',
            'login.placeholder': 'Enter panel password',
            'login.signin': 'Sign In',
            'login.authenticating': 'Authenticating...',

            // ---- Header ----
            'header.title': "Akarin's Gateway",
            'header.subtitle': 'Management Panel',
            'header.save': 'Save Config',
            'header.saving': 'Saving...',
            'header.logout': 'Logout',

            // ---- Tabs ----
            'tab.backends': 'Backends',
            'tab.routing': 'Model Routing',
            'tab.health': 'Health Monitor',
            'tab.clients': 'Client Settings',

            // ---- Backends Tab ----
            'backends.endpoint': 'Endpoint',
            'backends.timeout': 'Timeout (s)',
            'backends.stream_timeout': 'Stream Timeout (s)',
            'backends.max_retries': 'Max Retries',
            'backends.api_keys': 'API Keys',
            'backends.add_key': '+ Add',
            'backends.remove_key': 'Remove key',
            'backends.supported_models': 'Supported Models',
            'backends.all_models': 'All models (*)',
            'backends.circuit_breaker': 'Circuit Breaker:',
            'backends.reset_cb': 'Reset CB',
            'backends.drag_reorder': 'Drag to reorder',
            'backends.add_backend': '+ Add Backend',
            'backends.add_backend_title': 'Add New Backend',
            'backends.key': 'Key (unique ID)',
            'backends.name': 'Name',
            'backends.base_url': 'Base URL',
            'backends.priority': 'Priority',
            'backends.api_format': 'API Format',
            'backends.enabled': 'Enabled',
            'backends.submit': 'Create Backend',
            'backends.cancel': 'Cancel',

            // ---- Routing Tab ----
            'routing.models': 'MODELS',
            'routing.add_model': '+ Add Model',
            'routing.fallback_chain': 'Fallback Chain (drag to reorder)',
            'routing.add': '+ Add',
            'routing.backend_key_placeholder': 'backend key (e.g. copilot)',
            'routing.target_model_placeholder': 'target model (optional)',
            'routing.fallback_on': 'Fallback On (click to toggle)',
            'routing.select_model': 'Select a model from the list',
            'routing.model_placeholder': 'claude-new-model',

            // ---- Cross-Model Fallback ----
            'xmodel.title': '🔄 Cross-Model Fallback',
            'xmodel.desc': "When all backends in a model's chain fail, try a different model on a different backend",
            'xmodel.enabled': 'Enabled',
            'xmodel.save': 'Save',
            'xmodel.source_pattern': 'Source Pattern',
            'xmodel.fallback_model': 'Fallback Model',
            'xmodel.backend': 'Backend',
            'xmodel.add_rule': '+ Add Rule',

            // ---- Default Routing ----
            'defroute.title': '🛤️ Default Routing',
            'defroute.desc': 'Pattern-based fallback chains for models without explicit routing rules',
            'defroute.save': 'Save',
            'defroute.pattern': 'Pattern:',
            'defroute.chain': 'Chain:',
            'defroute.add_rule': '+ Add Rule',
            'defroute.catch_all': '🚨 Catch-All (last resort)',

            // ---- Final Fallback ----
            'finalfb.title': '🛟 Final Fallback',
            'finalfb.desc': 'Ultimate last-resort backend when everything else fails',
            'finalfb.enabled': 'Enabled',
            'finalfb.respect_cb': 'Respect CB',
            'finalfb.save': 'Save',

            // ---- Backend Capabilities ----
            'caps.title': '🎯 Backend Capabilities',
            'caps.desc': 'Declare which model patterns each backend supports (include/exclude)',
            'caps.save': '💾 Save Capabilities',
            'caps.include': '✅ Include:',
            'caps.exclude': '❌ Exclude:',

            // ---- Copilot Model Mapping ----
            'mapping.title': '🔄 Copilot Model Mapping',
            'mapping.desc': 'Map model aliases to canonical names (e.g. claude-3-sonnet → claude-sonnet-4)',
            'mapping.entries': 'entries',
            'mapping.save': '💾 Save Mapping',
            'mapping.source_placeholder': 'Source model (e.g. claude-3-sonnet)',
            'mapping.target_placeholder': 'Target model (e.g. claude-sonnet-4)',
            'mapping.add': '+ Add',
            'mapping.col_source': 'Source',
            'mapping.col_target': 'Target',

            // ---- Runtime Flags ----
            'flags.title': '⚙️ Runtime Flags',
            'flags.desc': 'AnyRouter behavioral switches. Precedence: YAML > env var > default.',
            'flags.save': 'Save Flags',

            // ---- Health Tab ----
            'health.total': 'Total',
            'health.enabled': 'Enabled',
            'health.healthy': 'Healthy',
            'health.uptime': 'Uptime',
            'health.success_rate': 'Success Rate',
            'health.requests': 'Requests:',
            'health.avg': 'Avg:',
            'health.no_traffic': 'No traffic recorded',
            'health.loading': 'Loading health data...',
            'health.disabled': 'DISABLED',
            'health.reset': 'Reset',

            // ---- Client Settings Tab ----
            'clients.desc': 'Configure per-client behavior for each IDE and CLI tool. Changes take effect immediately for new requests.',
            'clients.feat_sanitization': 'Message sanitization (clean up IDE-mangled thinking blocks)',
            'clients.feat_cross_pool': 'Cross-pool fallback (allow fallback across backend pools)',
            'clients.feat_stateless': 'Stateless mode (bypass SCID architecture, client manages own state)',
            'clients.feat_sig_recovery': 'Signature recovery only (lightweight mode, skip full SCID)',
            'clients.feat_scid': 'Full SCID (server-managed conversation state, signature caching)',

            // ---- Banner Messages ----
            'msg.backend_updated': "Backend '{key}' updated",
            'msg.toggle_failed': 'Toggle failed: {error}',
            'msg.scid_failed': 'SCID toggle failed: {error}',
            'msg.reorder_failed': 'Reorder failed: {error}',
            'msg.key_added': 'API key added',
            'msg.key_add_failed': 'Add key failed: {error}',
            'msg.key_removed': 'API key removed',
            'msg.key_remove_failed': 'Delete key failed: {error}',
            'msg.cb_reset': "Circuit breaker reset for '{key}'",
            'msg.cb_reset_failed': 'Reset failed: {error}',
            'msg.routing_updated': "Routing updated for '{model}'",
            'msg.routing_failed': 'Routing update failed: {error}',
            'msg.model_exists': "Model '{model}' already exists",
            'msg.model_added': "Model '{model}' added",
            'msg.model_add_failed': 'Add model failed: {error}',
            'msg.client_toggled': '{client}: {feature} → {state}',
            'msg.client_toggle_failed': 'Toggle failed: {error}',
            'msg.xmodel_saved': 'Cross-model fallback rules updated',
            'msg.xmodel_failed': 'Failed to update cross-model fallback: {error}',
            'msg.defroute_saved': 'Default routing rules updated',
            'msg.defroute_failed': 'Failed to update default routing: {error}',
            'msg.finalfb_saved': 'Final fallback config updated',
            'msg.finalfb_failed': 'Failed to update final fallback: {error}',
            'msg.caps_saved': 'Backend capabilities updated',
            'msg.caps_failed': 'Failed to save backend capabilities: {error}',
            'msg.mapping_saved': 'Copilot model mapping updated',
            'msg.mapping_failed': 'Failed to save copilot model mapping: {error}',
            'msg.flags_saved': 'Runtime flags updated',
            'msg.flags_failed': 'Failed to save runtime flags: {error}',
            'msg.config_saved': 'Configuration saved to gateway.yaml',
            'msg.config_failed': 'Save failed: {error}',
            'msg.auth_failed': 'Authentication failed',
            'msg.backend_added': "Backend '{key}' created",
            'msg.backend_add_failed': 'Create backend failed: {error}',
            'msg.backend_key_exists': "Backend '{key}' already exists",
            'msg.backend_key_required': 'Backend key is required',
        },
        zh: {
            // ---- 登录页 ----
            'login.title': "阿卡林的网关",
            'login.subtitle': '管理面板',
            'login.placeholder': '请输入面板密码',
            'login.signin': '登 录',
            'login.authenticating': '认证中...',

            // ---- 顶栏 ----
            'header.title': "阿卡林的网关",
            'header.subtitle': '管理面板',
            'header.save': '保存配置',
            'header.saving': '保存中...',
            'header.logout': '退出',

            // ---- Tab ----
            'tab.backends': '后端管理',
            'tab.routing': '模型路由',
            'tab.health': '健康监控',
            'tab.clients': '客户端设置',

            // ---- 后端管理 Tab ----
            'backends.endpoint': '端点 URL',
            'backends.timeout': '超时 (秒)',
            'backends.stream_timeout': '流式超时 (秒)',
            'backends.max_retries': '最大重试',
            'backends.api_keys': 'API 密钥',
            'backends.add_key': '+ 添加',
            'backends.remove_key': '删除密钥',
            'backends.supported_models': '支持的模型',
            'backends.all_models': '全部模型 (*)',
            'backends.circuit_breaker': '熔断器:',
            'backends.reset_cb': '重置熔断',
            'backends.drag_reorder': '拖拽排序',
            'backends.add_backend': '+ 添加后端',
            'backends.add_backend_title': '添加新后端',
            'backends.key': '标识 (唯一 ID)',
            'backends.name': '名称',
            'backends.base_url': '基础 URL',
            'backends.priority': '优先级',
            'backends.api_format': 'API 格式',
            'backends.enabled': '启用',
            'backends.submit': '创建后端',
            'backends.cancel': '取消',

            // ---- 路由 Tab ----
            'routing.models': '模型列表',
            'routing.add_model': '+ 添加模型',
            'routing.fallback_chain': '降级链 (拖拽排序)',
            'routing.add': '+ 添加',
            'routing.backend_key_placeholder': '后端标识 (如 copilot)',
            'routing.target_model_placeholder': '目标模型 (可选)',
            'routing.fallback_on': '降级条件 (点击切换)',
            'routing.select_model': '请从左侧列表选择模型',
            'routing.model_placeholder': 'claude-new-model',

            // ---- 跨模型降级 ----
            'xmodel.title': '🔄 跨模型降级',
            'xmodel.desc': '当模型链路中所有后端失败时，尝试使用不同后端的另一个模型',
            'xmodel.enabled': '启用',
            'xmodel.save': '保存',
            'xmodel.source_pattern': '源模式',
            'xmodel.fallback_model': '降级模型',
            'xmodel.backend': '后端',
            'xmodel.add_rule': '+ 添加规则',

            // ---- 默认路由 ----
            'defroute.title': '🛤️ 默认路由',
            'defroute.desc': '基于模式匹配的降级链，用于没有显式路由规则的模型',
            'defroute.save': '保存',
            'defroute.pattern': '模式:',
            'defroute.chain': '链路:',
            'defroute.add_rule': '+ 添加规则',
            'defroute.catch_all': '🚨 兜底规则 (最后手段)',

            // ---- 最终降级 ----
            'finalfb.title': '🛟 最终降级',
            'finalfb.desc': '当所有其他策略都失败时的终极兜底后端',
            'finalfb.enabled': '启用',
            'finalfb.respect_cb': '遵守熔断',
            'finalfb.save': '保存',

            // ---- 后端能力声明 ----
            'caps.title': '🎯 后端能力声明',
            'caps.desc': '声明每个后端支持哪些模型模式 (包含/排除)',
            'caps.save': '💾 保存能力',
            'caps.include': '✅ 包含:',
            'caps.exclude': '❌ 排除:',

            // ---- Copilot 模型映射 ----
            'mapping.title': '🔄 Copilot 模型映射',
            'mapping.desc': '将模型别名映射到规范名称 (如 claude-3-sonnet → claude-sonnet-4)',
            'mapping.entries': '条',
            'mapping.save': '💾 保存映射',
            'mapping.source_placeholder': '源模型 (如 claude-3-sonnet)',
            'mapping.target_placeholder': '目标模型 (如 claude-sonnet-4)',
            'mapping.add': '+ 添加',
            'mapping.col_source': '源',
            'mapping.col_target': '目标',

            // ---- 运行时开关 ----
            'flags.title': '⚙️ 运行时开关',
            'flags.desc': 'AnyRouter 行为开关。优先级: YAML > 环境变量 > 默认值。',
            'flags.save': '保存开关',

            // ---- 健康监控 Tab ----
            'health.total': '总数',
            'health.enabled': '已启用',
            'health.healthy': '健康',
            'health.uptime': '运行时间',
            'health.success_rate': '成功率',
            'health.requests': '请求数:',
            'health.avg': '平均:',
            'health.no_traffic': '暂无流量记录',
            'health.loading': '加载健康数据中...',
            'health.disabled': '已禁用',
            'health.reset': '重置',

            // ---- 客户端设置 Tab ----
            'clients.desc': '为每个 IDE 和 CLI 工具配置客户端行为。更改会立即对新请求生效。',
            'clients.feat_sanitization': '消息清洗（清理 IDE 损坏的 thinking 块）',
            'clients.feat_cross_pool': '跨池降级（允许跨后端池降级）',
            'clients.feat_stateless': '无状态模式（绕过 SCID 架构，客户端自行管理状态）',
            'clients.feat_sig_recovery': '仅签名恢复（轻量模式，跳过完整 SCID）',
            'clients.feat_scid': '完整 SCID（服务端管理会话状态，签名缓存）',

            // ---- Banner 消息 ----
            'msg.backend_updated': "后端 '{key}' 已更新",
            'msg.toggle_failed': '切换失败: {error}',
            'msg.scid_failed': 'SCID 切换失败: {error}',
            'msg.reorder_failed': '排序失败: {error}',
            'msg.key_added': 'API 密钥已添加',
            'msg.key_add_failed': '添加密钥失败: {error}',
            'msg.key_removed': 'API 密钥已删除',
            'msg.key_remove_failed': '删除密钥失败: {error}',
            'msg.cb_reset': "后端 '{key}' 的熔断器已重置",
            'msg.cb_reset_failed': '重置失败: {error}',
            'msg.routing_updated': "模型 '{model}' 的路由已更新",
            'msg.routing_failed': '路由更新失败: {error}',
            'msg.model_exists': "模型 '{model}' 已存在",
            'msg.model_added': "模型 '{model}' 已添加",
            'msg.model_add_failed': '添加模型失败: {error}',
            'msg.client_toggled': '{client}: {feature} → {state}',
            'msg.client_toggle_failed': '切换失败: {error}',
            'msg.xmodel_saved': '跨模型降级规则已更新',
            'msg.xmodel_failed': '跨模型降级更新失败: {error}',
            'msg.defroute_saved': '默认路由规则已更新',
            'msg.defroute_failed': '默认路由更新失败: {error}',
            'msg.finalfb_saved': '最终降级配置已更新',
            'msg.finalfb_failed': '最终降级更新失败: {error}',
            'msg.caps_saved': '后端能力声明已更新',
            'msg.caps_failed': '后端能力保存失败: {error}',
            'msg.mapping_saved': 'Copilot 模型映射已更新',
            'msg.mapping_failed': 'Copilot 模型映射保存失败: {error}',
            'msg.flags_saved': '运行时开关已更新',
            'msg.flags_failed': '运行时开关保存失败: {error}',
            'msg.config_saved': '配置已保存到 gateway.yaml',
            'msg.config_failed': '保存失败: {error}',
            'msg.auth_failed': '认证失败',
            'msg.backend_added': "后端 '{key}' 已创建",
            'msg.backend_add_failed': '创建后端失败: {error}',
            'msg.backend_key_exists': "后端 '{key}' 已存在",
            'msg.backend_key_required': '后端标识不能为空',
        },
    };

    Alpine.store('i18n', {
        locale: localStorage.getItem('panel_locale') || 'en',

        /**
         * Translate a key. Supports parameter interpolation: {key} → value.
         * @param {string} key  Translation key
         * @param {Object} params  Optional interpolation params, e.g. {key: 'copilot'}
         * @returns {string}
         */
        t(key, params) {
            const dict = translations[this.locale] || translations.en;
            let text = dict[key] ?? translations.en[key] ?? key;
            if (params) {
                for (const [k, v] of Object.entries(params)) {
                    text = text.replaceAll(`{${k}}`, v);
                }
            }
            return text;
        },

        setLocale(lang) {
            this.locale = lang;
            localStorage.setItem('panel_locale', lang);
        },

        get isEn() { return this.locale === 'en'; },
        get isZh() { return this.locale === 'zh'; },
    });
});
