// Copyright (C) 2026 Soenchin
// SPDX-License-Identifier: AGPL-3.0
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.

let BOT_NAME = 'QQ Bot';

/**
 * QQ Bot Relay WebUI - Frontend SPA
 * Features: Dashboard, Group Config (pipe/direct), Pipe Status, Knowledge Base, Manual Send
 */
const API_BASE = '';
const WS_BASE = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`;
const MAX_FEED = 200;

// ============ Global State ============
const state = {
    bot: { connected: false, bot_qq: null, uptime: 0 },
    groups: [],
    pipeState: [],
    liveFeed: [],
    knowledge: [],
    plugins: [],
    pluginGroups: [],
    envConfig: { group_mode: {}, fallback_mode: 'direct', group_mode_raw: '', group_vision: [] },
    ws: null,
    wsReconnectTimer: null,
    wsReconnectDelay: 1000,
    pipeRefreshTimer: null,
    activeView: 'dashboard',
};

// ============ Utilities ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
function timeStr(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function formatUptime(seconds) {
    if (!seconds || seconds <= 0) return '刚刚启动';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h} 时 ${m} 分`;
    if (m > 0) return `${m} 分`;
    return `${seconds} 秒`;
}
async function api(path, opts = {}) {
    const url = `${API_BASE}${path}`;
    const res = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
    });
    if (!res.ok) {
        const err = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${err.slice(0, 100)}`);
    }
    return res.json();
}

// ============ Routing ============
function getRoute() {
    const hash = location.hash.slice(1) || '/dashboard';
    const parts = hash.split('/').filter(Boolean);
    return { view: parts[0] || 'dashboard', params: parts.slice(1) };
}
function navigate(view, ...params) {
    location.hash = `#/${view}/${params.join('/')}`;
}
window.addEventListener('hashchange', renderRoute);

// ============ WebSocket ============
function connectWS() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;
    try { state.ws = new WebSocket(WS_BASE); } catch (e) {
        scheduleReconnect(); return;
    }
    state.ws.onopen = () => {
        console.log('[WS] Connected');
        state.wsReconnectDelay = 1000;
        updateStatusDot(true);
        fetchStatus();
        fetchGroups();
        fetchPipeState();
    };
    state.ws.onmessage = (ev) => {
        try { handleWSEvent(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    };
    state.ws.onclose = () => {
        updateStatusDot(false); state.ws = null; scheduleReconnect();
    };
    state.ws.onerror = () => {
        // 不在这里置null，让 onclose 统一处理，避免幽灵订阅
    };
}
function scheduleReconnect() {
    if (state.wsReconnectTimer) return;
    const delay = Math.min(state.wsReconnectDelay, 30000);
    state.wsReconnectDelay = Math.min(state.wsReconnectDelay * 2, 30000);
    state.wsReconnectTimer = setTimeout(() => {
        state.wsReconnectTimer = null; connectWS();
    }, delay);
}
function handleWSEvent(event) {
    const { type, data } = event;
    switch (type) {
        case 'message':
            addFeedItem({ ...data, _type: 'message', _time: new Date().toISOString() });
            // Batch busy group traffic; counters come from the server, never inferred from the feed.
            if (!state.pipeRefreshTimer) state.pipeRefreshTimer = setTimeout(() => {
                state.pipeRefreshTimer = null;
                fetchPipeState();
            }, 350);
            break;
        case 'reply':
            addFeedItem({ ...data, _type: 'reply', _time: new Date().toISOString() });
            fetchPipeState();
            break;
        case 'pipe_trigger':
            fetchPipeState();
            break;
        case 'manual_send':
            addFeedItem({ gid: data.gid, nick: 'WebUI', text: data.text, _type: 'manual', _time: new Date().toISOString() });
            break;
        case 'ping': break;
    }
}
function addFeedItem(item) {
    state.liveFeed.unshift(item);
    if (state.liveFeed.length > MAX_FEED) state.liveFeed.pop();
    if (state.activeView === 'dashboard') {
        const container = $('#live-feed');
        if (container) prependFeedItem(container, item);
    }
}
function prependFeedItem(container, item) {
    container.querySelector('.empty-state')?.remove();
    const div = document.createElement('div');
    div.innerHTML = feedItemHTML(item);
    const el = div.firstElementChild;
    container.insertBefore(el, container.firstChild);
    while (container.children.length > 100) container.removeChild(container.lastChild);
}
function feedItemHTML(item) {
    const t = timeStr(item._time);
    const gidTag = `<span class="gid-tag">${item.gid}</span>`;
    const nick = item.nick ? `<span class="nick">${escapeHtml(item.nick)}</span>` : '';
    switch (item._type) {
        case 'reply':
            return `<div class="feed-entry reply"><span class="time">${t}</span>${gidTag}<span class="reply-icon">机器人</span><span class="reply-text">${escapeHtml(item.text)}</span></div>`;
        case 'manual':
            return `<div class="feed-entry manual"><span class="time">${t}</span>${gidTag}<span class="system-text">手动发送 · ${escapeHtml(item.text)}</span></div>`;
        default:
            return `<div class="feed-entry"><span class="time">${t}</span>${gidTag}${nick}<span class="msg-text">${escapeHtml(item.text)}</span></div>`;
    }
}

// ============ Status Updates ============
function updateStatusDot(connected) {
    const dot = $('#statusDot');
    const text = $('#statusText');
    if (dot) dot.className = `status-dot ${connected ? 'online' : 'offline'}`;
    if (text) text.textContent = connected ? `在线 - ${state.bot.bot_qq || '?'}` : '离线';
}
async function fetchStatus() {
    try {
        state.bot = await api('/api/status');
        updateStatusDot(state.bot.connected);
        if (state.activeView === 'dashboard') renderDashboard();
    } catch (e) { /* ignore */ }
}
async function fetchGroups() {
    try {
        state.groups = await api('/api/groups');
        if (state.activeView === 'dashboard') renderDashboard();
        if (state.activeView === 'pipe-status') renderPipeStatus();
        if (state.activeView === 'send' && !$('#send-message')) renderSend();
    } catch (e) { /* ignore */ }
}
async function fetchPipeState() {
    try {
        state.pipeState = (await api('/api/pipe-state')).groups || [];
        if (state.activeView === 'pipe-status') renderPipeStatus();
        if (state.activeView === 'dashboard') renderDashboard();
    } catch (e) { /* ignore */ }
}
async function fetchEnvConfig() {
    try {
        state.envConfig = await api('/api/env-config');
        if (state.activeView === 'group-config') renderGroupConfig();
    } catch (e) { console.warn('fetch env config failed', e); }
}

// ============ Route Rendering ============
function renderRoute() {
    const route = getRoute();
    state.activeView = route.view;
    const view = $('#app-view');
    view.dataset.view = route.view;
    $$('.nav-item').forEach(el => {
        const active = el.dataset.view === route.view;
        el.classList.toggle('active', active);
        if (active) {
            el.setAttribute('aria-current', 'page');
            $('#current-page').textContent = el.textContent.trim();
        } else el.removeAttribute('aria-current');
    });
    view.innerHTML = '';

    switch (route.view) {
        case 'dashboard': renderDashboard(); break;
        case 'group-config': renderGroupConfig(); break;
        case 'pipe-status': renderPipeStatus(); break;
        case 'knowledge': renderKnowledge(); break;
        case 'plugins': renderPlugins(); break;
        case 'send': renderSend(); break;
        default: view.innerHTML = '<div class="empty-state">页面不存在</div>';
    }
}

// ============ Dashboard ============
function renderDashboard() {
    const status = state.bot;
    const groups = state.groups;
    const pipeData = state.pipeState;

    const totalGroups = groups.length;
    const pipeCount = pipeData.length;
    if ($('#dashboard-overview')) {
        $('#metric-status').textContent = status.connected ? '在线' : '离线';
        $('#metric-status').className = `value ${status.connected ? 'online' : 'offline'}`;
        $('#metric-uptime').textContent = formatUptime(status.uptime);
        $('#metric-groups').textContent = groups.length;
        $('#metric-pipes').setAttribute('value', pipeCount);
        $('#metric-account').textContent = status.bot_qq ? `QQ ${status.bot_qq}` : '等待机器人连接';
        $('#dashboard-health').textContent = status.connected ? '运行正常' : '等待连接';
        renderPipePreview();
        renderHistoryChart();
        return;
    }

    const cardsHtml = `
        <div class="metrics" id="dashboard-overview">
            <div><h3>机器人状态</h3><div id="metric-status" class="value ${status.connected ? 'online' : 'offline'}">${status.connected ? '在线' : '离线'}</div><div class="sub" id="metric-account">${status.bot_qq ? `QQ ${status.bot_qq}` : '等待机器人连接'}</div></div>
            <div><h3>运行时长</h3><div class="value" id="metric-uptime">${formatUptime(status.uptime)}</div><div class="sub">当前进程持续运行</div></div>
            <div><h3>已接入群</h3><div class="value" id="metric-groups">${totalGroups}</div><div class="sub">管道与直调群聊</div></div>
            <div><h3>管道会话</h3><div class="value"><rolling-number id="metric-pipes" value="${pipeCount}"></rolling-number><span class="metric-unit">个</span></div><div class="sub">活跃的管道群</div></div>
        </div>`;

    const feedItems = state.liveFeed.slice(0, 50).map(item => feedItemHTML(item)).join('');
    const feedHtml = `<section class="feed-panel"><div class="panel-heading"><div><h3>实时消息</h3><p>群聊动态与机器人回复</p></div><span class="quiet-badge">实时订阅</span></div>
        <div class="feed-container" id="live-feed">${feedItems || '<div class="empty-state">暂无消息<br><small>收到的群消息会显示在这里</small></div>'}</div></section>`;

    $('#app-view').innerHTML = `<div class="page-header flex justify-between items-center"><div><h2>仪表盘</h2><p>查看运行状态与实时消息</p></div><span class="quiet-badge" id="dashboard-health">${status.connected ? '运行正常' : '等待连接'}</span></div>` + cardsHtml +
        `<div class="dashboard-columns">${feedHtml}<section class="pipe-preview-panel"><div class="panel-heading"><div><h3>管道概览</h3><p>下一次接话，还差几条？</p></div>${uiIcon('pipe')}</div><div id="pipe-preview"></div><a class="panel-link" href="#/pipe-status">查看全部管道 <span aria-hidden="true">→</span></a></section></div><div id="history-chart"></div>`;
    renderPipePreview();
    renderHistoryChart();
}

function renderHistoryChart() {
    const host = $('#history-chart');
    if (!host) return;
    const max = Math.max(1, ...state.groups.map(g => g.history_count || 0));
    host.innerHTML = state.groups.length ? `<div class="card bar-chart"><h3>各群对话数量 <span class="text-sm text-muted">· 已保留的上下文</span></h3>${state.groups.map(g => `<div class="bar-row"><span class="bar-label">群 ${g.gid} <span class="text-muted">(${g.mode})</span></span><div class="bar-track"><div class="bar-fill ${g.mode === 'pipe' ? 'pipe-bar' : 'direct-bar'}" style="width:${Math.max(0, (g.history_count || 0) / max * 100)}%"></div></div><span class="bar-value">${g.history_count || 0}</span></div>`).join('')}</div>` : '';
}

function renderPipePreview() {
    const host = $('#pipe-preview');
    if (!host) return;
    const ids = state.pipeState.map(p => String(p.gid)).join(',');
    if (host.dataset.ids !== ids || !host.children.length) {
        host.dataset.ids = ids;
        host.innerHTML = state.pipeState.map(p => `<div class="pipe-preview-item" data-pipe="${p.gid}">
            <div class="preview-title"><strong>群 ${p.gid}</strong><span class="quiet-badge">积累中</span></div>
            <div class="preview-count">${rollingNumber(p.counter, 'count')}<span class="text-muted"> / </span>${rollingNumber(p.threshold, 'threshold')}<span class="metric-unit">条消息</span></div>
            <div class="progress-bar"><div class="progress-fill"></div></div><p class="preview-recent"></p></div>`).join('') || '<div class="empty-state">暂无管道群</div>';
    }
    state.pipeState.forEach(p => updatePipeDisplay(host.querySelector(`[data-pipe="${p.gid}"]`), p));
}
function updatePipeDisplay(card, p) {
    if (!card) return;
    card.querySelector('[data-counter="count"]')?.setAttribute('value', p.counter);
    card.querySelector('[data-counter="threshold"]')?.setAttribute('value', p.threshold);
    const fill = card.querySelector('.progress-fill');
    if (fill) fill.style.width = `${p.threshold > 0 ? Math.max(0, Math.min(100, p.counter / p.threshold * 100)) : 0}%`;
    const recent = card.querySelector('.preview-recent');
    if (recent) recent.textContent = `最近上下文 ${p.recent_count} 条`;
}

// ============ Group Config ============
function renderGroupConfig() {
    const env = state.envConfig;
    const groupMode = env.group_mode || {};
    const fallback = env.fallback_mode || 'direct';
    const visionSet = new Set((env.group_vision || []).map(x => String(x)));
    const configuredCount = Object.keys(groupMode).length;
    const visionCount = visionSet.size;

    const rows = Object.entries(groupMode).map(([gid, mode]) => `
        <tr data-gid="${gid}">
            <td><span class="group-id-chip">${gid}</span><input type="hidden" value="${gid}" class="group-id-input" /></td>
            <td>
                <select class="group-mode-select" aria-label="群 ${gid} 的模式">
                    <option value="direct" ${mode === 'direct' ? 'selected' : ''}>直调 · 仅 @ 回复</option>
                    <option value="pipe" ${mode === 'pipe' ? 'selected' : ''}>管道 · 可主动发言</option>
                </select>
            </td>
            <td>
                <label class="vision-toggle" title="开启后保存群图片，并把中继侧图像描述带入管道上下文">
                    <input type="checkbox" class="group-vision-check" ${visionSet.has(String(gid)) ? 'checked' : ''} />
                    <span class="vision-toggle-track" aria-hidden="true"></span>
                    <span class="vision-toggle-text">读图</span>
                </label>
            </td>
            <td class="group-action-cell"><button class="btn btn-outline btn-sm group-delete-btn" onclick="removeGroupRow(this)">删除</button></td>
        </tr>
    `).join('');

    const groupTable = `
        <div class="group-table-wrap">
            <table class="group-table">
                <thead>
                    <tr>
                        <th>群号</th>
                        <th>对话模式</th>
                        <th>图片理解</th>
                        <th aria-label="操作"></th>
                    </tr>
                </thead>
                <tbody id="group-config-tbody">
                    ${rows || '<tr class="empty-row"><td colspan="4"><strong>还没有单独配置的群</strong><span>在上面填入群号后添加；其他群会使用默认模式。</span></td></tr>'}
                </tbody>
            </table>
        </div>`;

    $('#app-view').innerHTML = `
        <section class="settings-hero">
            <div>
                <span class="settings-eyebrow">BOT CONFIGURATION</span>
                <h2>群设置</h2>
                <p>给每个群单独安排对话方式和读图能力。模式改动需要重启；读图开关保存后会尽量即时生效。</p>
            </div>
            <div class="settings-stats" aria-label="群设置统计">
                <div><strong>${configuredCount}</strong><span>已配置群</span></div>
                <div><strong>${visionCount}</strong><span>开启读图</span></div>
            </div>
        </section>

        <div class="group-config-panel modern-group-config">
            <div id="group-config-alert" style="display:none"></div>

            <div class="settings-card fallback-card">
                <div class="settings-card-heading">
                    <div><span class="section-kicker">DEFAULT ROUTE</span><h3>默认对话模式</h3></div>
                    <span class="mode-status">未单独配置的群</span>
                </div>
                <p>机器人遇到未列在下方的群时，会按这里的规则响应。</p>
                <div class="fallback-control">
                    <label for="fallback-mode-select">默认行为</label>
                    <select id="fallback-mode-select">
                        <option value="direct" ${fallback === 'direct' ? 'selected' : ''}>直调模式 · 只有 @ 机器人时回复</option>
                        <option value="pipe" ${fallback === 'pipe' ? 'selected' : ''}>管道模式 · 会参考聊天上下文主动接话</option>
                    </select>
                </div>
            </div>

            <div class="settings-card group-list-card">
                <div class="settings-card-heading">
                    <div><span class="section-kicker">GROUP OVERRIDES</span><h3>单独配置群</h3></div>
                    <span class="mode-status">${configuredCount} 条规则</span>
                </div>
                <p>这里的规则优先于默认模式。读图只建议给管道群开，图会临时放入 inbox 并自动清理。</p>

                <div class="add-group-row">
                    <div class="form-group">
                        <label for="new-group-id">群号</label>
                        <input type="text" id="new-group-id" inputmode="numeric" placeholder="例如 123456789" />
                    </div>
                    <div class="form-group">
                        <label for="new-group-mode">模式</label>
                        <select id="new-group-mode">
                            <option value="direct">直调 · 仅 @ 回复</option>
                            <option value="pipe" selected>管道 · 可主动发言</option>
                        </select>
                    </div>
                    <button class="btn btn-success add-group-btn" id="btn-add-group">添加群</button>
                </div>
                ${groupTable}
            </div>

            <details class="settings-card config-preview-card">
                <summary><span><span class="section-kicker">ENV PREVIEW</span><strong>查看将写入 .env 的配置</strong></span><span class="summary-hint">展开</span></summary>
                <div class="json-preview" id="config-preview"></div>
            </details>

            <div class="settings-save-bar">
                <div><strong>确认后保存</strong><span>模式切换需重启 relay；读图开关会尝试热更新。</span></div>
                <div class="save-actions">
                    <span id="save-group-indicator" class="save-indicator">已保存</span>
                    <button class="btn btn-primary btn-lg" id="btn-save-group-config">保存群设置</button>
                </div>
            </div>
        </div>`;

    attachGroupConfigEvents();
    updateConfigPreview();
}

function attachGroupConfigEvents() {
    // Add new group
    $('#btn-add-group')?.addEventListener('click', () => {
        const gidInput = $('#new-group-id');
        const modeSelect = $('#new-group-mode');
        const gid = gidInput.value.trim();
        const mode = modeSelect.value;

        if (!gid || !/^\d+$/.test(gid)) {
            showGroupAlert('请输入有效的群号（纯数字）', 'warning');
            return;
        }
        updateConfigPreview();
        const gidInt = parseInt(gid);
        if (state.envConfig.group_mode[gidInt]) {
            showGroupAlert('该群号已存在', 'warning');
            return;
        }

        state.envConfig.group_mode[gidInt] = mode;
        gidInput.value = '';
        renderGroupConfig();
        showGroupAlert('已添加，请保存到 .env', 'success');
    });

    // Save to .env
    $('#btn-save-group-config')?.addEventListener('click', async () => {
        const groupMode = state.envConfig.group_mode;
        const fallback = $('#fallback-mode-select')?.value || 'direct';
        const visionIds = [];

        // Collect from table (in case user edited modes in-place)
        const tbody = $('#group-config-tbody');
        if (tbody) {
            tbody.querySelectorAll('tr').forEach(tr => {
                const gidInput = tr.querySelector('.group-id-input');
                const modeSelect = tr.querySelector('.group-mode-select');
                const visionCheck = tr.querySelector('.group-vision-check');
                if (gidInput && modeSelect) {
                    const gid = parseInt(gidInput.value);
                    if (!isNaN(gid)) {
                        groupMode[gid] = modeSelect.value;
                        if (visionCheck && visionCheck.checked) {
                            visionIds.push(gid);
                        }
                    }
                }
            });
        }

        state.envConfig.group_vision = visionIds;
        const groupModeJson = JSON.stringify(groupMode, null, 0);
        const groupVisionJson = JSON.stringify(visionIds);

        try {
            const result = await api('/api/env-config', {
                method: 'PUT',
                body: JSON.stringify({ group_mode: groupModeJson, fallback_mode: fallback, group_vision: groupVisionJson }),
            });

            const indicator = $('#save-group-indicator');
            if (indicator) indicator.classList.add('visible');
            setTimeout(() => indicator?.classList.remove('visible'), 3000);

            if (result.restart_required) {
                showGroupAlert('已保存到 .env。<strong>请重启 relay 才能生效。</strong>', 'warning');
            } else {
                showGroupAlert('保存成功', 'success');
            }
        } catch (e) {
            showGroupAlert('保存失败: ' + escapeHtml(e.message), 'danger');
        }
    });

    // Fallback change updates preview
    $('#fallback-mode-select')?.addEventListener('change', updateConfigPreview);

    // Table mode changes update preview
    document.querySelectorAll('.group-mode-select').forEach(el => {
        el.addEventListener('change', updateConfigPreview);
    });
    document.querySelectorAll('.group-vision-check').forEach(el => {
        el.addEventListener('change', updateConfigPreview);
    });
}

function removeGroupRow(btn) {
    const tr = btn.closest('tr');
    const gid = tr?.dataset.gid;
    if (gid) {
        updateConfigPreview();
        delete state.envConfig.group_mode[gid];
        tr.remove();
        updateConfigPreview();
    }
}

function updateConfigPreview() {
    const preview = $('#config-preview');
    if (!preview) return;

    const groupMode = {};
    const visionIds = [];
    const tbody = $('#group-config-tbody');
    if (tbody) {
        tbody.querySelectorAll('tr').forEach(tr => {
            const gidInput = tr.querySelector('.group-id-input');
            const modeSelect = tr.querySelector('.group-mode-select');
            const visionCheck = tr.querySelector('.group-vision-check');
            if (gidInput && modeSelect) {
                groupMode[gidInput.value] = modeSelect.value;
                if (visionCheck && visionCheck.checked) {
                    const gid = parseInt(gidInput.value);
                    if (!isNaN(gid)) visionIds.push(gid);
                }
            }
        });
    }

    const fallback = $('#fallback-mode-select')?.value || 'direct';
    state.envConfig.group_mode = groupMode;
    state.envConfig.fallback_mode = fallback;
    state.envConfig.group_vision = visionIds;
    const raw = {
        GROUP_MODE: JSON.stringify(groupMode),
        FALLBACK_MODE: fallback,
        GROUP_VISION: JSON.stringify(visionIds),
    };

    preview.textContent = JSON.stringify(raw, null, 2);
}

function showGroupAlert(html, type) {
    const alertBox = $('#group-config-alert');
    if (!alertBox) return;
    alertBox.innerHTML = `<div class="alert alert-${type}">${html}</div>`;
    alertBox.style.display = 'block';
    setTimeout(() => {
        alertBox.style.display = 'none';
        alertBox.innerHTML = '';
    }, 6000);
}

// ============ Pipe Status ============
function renderPipeStatus() {
    const pipeData = state.pipeState;
    const grid = $('#pipe-grid');
    const ids = pipeData.map(p => String(p.gid)).join(',');
    if (grid && grid.dataset.ids === ids) {
        pipeData.forEach(p => {
            const card = grid.querySelector(`[data-pipe="${p.gid}"]`);
            updatePipeDisplay(card, p);
            const slider = card.querySelector('input[type="range"]');
            if (document.activeElement !== slider && slider.dataset.saving !== 'true') {
                slider.value = p.threshold;
                card.querySelector('.slider-value').textContent = p.threshold;
            }
            card.querySelector('.recent-title').textContent = `最近消息窗口（${p.recent_count} 条）`;
            card.querySelector('.recent-items').innerHTML = recentMessagesHTML(p);
        });
        return;
    }
    if (!pipeData.length) {
        $('#app-view').innerHTML = `
            <div class="page-header"><h2>管道状态</h2><p>管理管道群的主动发言配置</p></div>
            <div class="card empty-state">暂无管道群，或机器人未启动</div>`;
        return;
    }

    const cardsHtml = pipeData.map(p => {
        const pct = p.threshold > 0 ? Math.min(100, (p.counter / p.threshold) * 100) : 0;
        const fillClass = pct < 50 ? 'low' : pct < 80 ? 'mid' : 'high';
        const recentHtml = recentMessagesHTML(p);

        return `
        <div class="pipe-card" data-pipe="${p.gid}">
            <h3>${uiIcon('pipe')} 群 ${p.gid} <span class="pipe-mode-badge">管道</span></h3>

            <div class="pipe-progress">
                <div class="progress-label"><span>消息积累</span><span class="pipe-count">${rollingNumber(p.counter, 'count')}<span class="text-muted"> / </span>${rollingNumber(p.threshold, 'threshold')}</span></div>
                <div class="progress-bar">
                    <div class="progress-fill ${fillClass}" style="width:${pct}%"></div>
                </div>
            </div>

            <div class="pipe-slider">
                <label for="slider-${p.gid}"><span>发言阈值</span><span class="save-indicator" id="saved-${p.gid}">已保存</span></label>
                <div class="slider-row">
                    <span class="text-sm text-muted">1</span>
                    <input type="range" id="slider-${p.gid}" aria-label="群 ${p.gid} 的发言阈值" min="1" max="100" value="${p.threshold}" data-gid="${p.gid}">
                    <span class="text-sm text-muted">100</span>
                    <span class="slider-value" id="slider-val-${p.gid}">${p.threshold}</span>
                </div>
            </div>

            <div class="pipe-recent">
                <div class="recent-title">最近消息窗口（${p.recent_count} 条）</div>
                <div class="recent-items">${recentHtml}</div>
            </div>
        </div>`;
    }).join('');

    $('#app-view').innerHTML = `
        <div class="page-header">
            <h2>管道状态</h2>
            <p>松开滑块后保存当前阈值；消息积累到阈值时触发 ${escapeHtml(BOT_NAME)} 发言。下一轮阈值由机器人重新设定。</p>
        </div>
        <div class="pipe-grid" id="pipe-grid" data-ids="${ids}">${cardsHtml}</div>`;

    pipeData.forEach(p => {
        const slider = $(`#slider-${p.gid}`);
        const valDisplay = $(`#slider-val-${p.gid}`);
        const indicator = $(`#saved-${p.gid}`);
        if (!slider || !valDisplay) return;

        slider.addEventListener('input', () => {
            valDisplay.textContent = slider.value;
            if (indicator) indicator.classList.remove('visible');
        });
        slider.addEventListener('change', async () => {
            slider.dataset.saving = 'true';
            slider.disabled = true;
            try {
                await api(`/api/groups/${p.gid}`, {
                    method: 'PUT', body: JSON.stringify({ pipe_threshold: parseInt(slider.value) }),
                });
                if (indicator) { indicator.textContent = '已保存'; indicator.classList.add('visible'); }
                await fetchPipeState();
            } catch (e) {
                const accepted = state.pipeState.find(item => item.gid === p.gid)?.threshold ?? p.threshold;
                slider.value = accepted;
                valDisplay.textContent = accepted;
                if (indicator) { indicator.textContent = '保存失败，请重试'; indicator.classList.add('visible'); }
            } finally {
                slider.dataset.saving = 'false';
                slider.disabled = false;
            }
        });
    });
}

function recentMessagesHTML(p) {
    return p.recent?.length ? p.recent.map(r => `<div class="recent-item">${escapeHtml(r)}</div>`).join('')
        : '<div class="recent-item text-muted">暂无最近消息</div>';
}

// ============ Knowledge ============
function renderKnowledge() {
    (async () => {
        const routeHash = location.hash;
        let files;
        try {
            files = await api('/api/knowledge');
            state.knowledge = files;
        } catch (e) {
            if (state.activeView === 'knowledge') $('#app-view').innerHTML = `<div class="page-header"><h2>知识库</h2><p>加载失败: ${escapeHtml(e.message)}</p></div>`;
            return;
        }

        if (state.activeView !== 'knowledge' || location.hash !== routeHash) return;
        const fileList = files.map(f =>
            `<button type="button" class="knowledge-file" data-filename="${escapeHtml(f.name)}">${uiIcon('book')}<span>${escapeHtml(f.name)}</span></button>`).join('');

        $('#app-view').innerHTML = `
            <div class="page-header flex justify-between items-center">
                <div>
                    <h2>知识库</h2>
                    <p>管理群聊知识库 Markdown 文件（${files.length} 个文件）</p>
                </div>
                <button class="btn btn-success btn-sm" id="btn-new-knowledge">${uiIcon('plus')} 新建文件</button>
            </div>
            <div class="knowledge-layout">
                <div class="knowledge-sidebar" id="knowledge-filelist">
                    ${fileList || '<div class="empty-state">知识库为空</div>'}
                </div>
                <div class="knowledge-editor" id="knowledge-editor-panel">
                    <div class="empty-state" style="flex:1;display:flex;align-items:center;justify-content:center">
                        选择一个文件，开始整理知识
                    </div>
                    <div class="knowledge-toolbar" id="knowledge-toolbar" style="display:none">
                        <span id="knowledge-filename-label" style="font-size:14px;color:var(--text-secondary);flex:1"></span>
                        <button class="btn btn-primary btn-sm" id="btn-save-knowledge">${uiIcon('save')} 保存</button>
                        <div id="knowledge-delete-control"></div>
                    </div>
                </div>
            </div>`;
        attachKnowledgeEvents();
        if (!document.activeElement || document.activeElement === document.body) $('#btn-new-knowledge')?.focus({ preventScroll: true });
    })();
}

function attachKnowledgeEvents() {
    let currentFile = null;

    $$('.knowledge-file').forEach(el => {
        el.addEventListener('click', async () => {
            const name = el.dataset.filename;
            $$('.knowledge-file').forEach(e => e.classList.remove('active'));
            el.classList.add('active');
            currentFile = name;
            try {
                const data = await api(`/api/knowledge/${encodeURIComponent(name)}`);
                if (state.activeView !== 'knowledge' || currentFile !== name || !el.isConnected) return;
                const panel = $('#knowledge-editor-panel');
                panel.innerHTML = `
                    <textarea id="knowledge-textarea" aria-label="知识文件内容">${escapeHtml(data.content)}</textarea>
                    <div class="knowledge-toolbar">
                        <span class="editor-filename">${escapeHtml(name)}</span>
                        <button class="btn btn-primary btn-sm" id="btn-save-knowledge">${uiIcon('save')} 保存</button>
                        <div id="knowledge-delete-control"></div>
                    </div>`;
                bindKnowledgeEditor(name);
            } catch (e) { alert('加载失败: ' + e.message); }
        });
    });

    $('#btn-new-knowledge')?.addEventListener('click', () => {
        const name = prompt('文件名（以 .md 结尾）:', '新知识.md');
        if (!name) return;
        if (!name.endsWith('.md')) { alert('文件名必须以 .md 结尾'); return; }
        currentFile = name;
        const panel = $('#knowledge-editor-panel');
        panel.innerHTML = `
            <textarea id="knowledge-textarea" aria-label="知识文件内容" placeholder="在此输入 Markdown 内容…"></textarea>
            <div class="knowledge-toolbar">
                <span class="editor-filename">${escapeHtml(name)}</span>
                <button class="btn btn-primary btn-sm" id="btn-save-knowledge">${uiIcon('save')} 保存</button>
            </div>`;
        bindKnowledgeEditor(name);
    });
}

function bindKnowledgeEditor(filename) {
    $('#btn-save-knowledge')?.addEventListener('click', async () => {
        const content = $('#knowledge-textarea')?.value;
        if (!content && !confirm('保存空内容？')) return;
        try {
            await api(`/api/knowledge/${encodeURIComponent(filename)}`, {
                method: 'PUT', body: JSON.stringify({ content: content || '' }),
            });
            alert('已保存');
            renderKnowledge();
        } catch (e) { alert('保存失败: ' + e.message); }
    });
    const host = $('#knowledge-delete-control');
    if (host) mountDeleteButton(host, filename,
        async () => {
            const save = $('#btn-save-knowledge');
            const editor = $('#knowledge-textarea');
            const fileControls = [...$$('.knowledge-file'), $('#btn-new-knowledge')].filter(Boolean);
            fileControls.forEach(button => { button.disabled = true; });
            if (save) save.disabled = true;
            if (editor) editor.readOnly = true;
            try {
                await api(`/api/knowledge/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            } catch (error) {
                fileControls.forEach(button => { if (button.isConnected) button.disabled = false; });
                if (save?.isConnected) save.disabled = false;
                if (editor?.isConnected) editor.readOnly = false;
                throw error;
            }
        }, () => renderKnowledge());
}

// ============ Send Message ============
function renderSend() {
    (async () => {
        let groups = state.groups;
        if (!groups.length) {
            try { groups = await api('/api/groups'); state.groups = groups; } catch (e) { /* ignore */ }
        }
        if (state.activeView !== 'send') return;
        const options = groups.map(g => `<option value="${g.gid}">群 ${g.gid} (${g.mode})</option>`).join('');
        $('#app-view').innerHTML = `
            <div class="page-header">
                <h2>发消息</h2>
                <p>通过机器人向指定群发送消息</p>
            </div>
            <div class="card" style="max-width:600px">
                <div class="form-group">
                    <label for="send-group">目标群</label>
                    <select id="send-group">${options || '<option value="">暂无可用群</option>'}</select>
                </div>
                <div class="form-group">
                    <label for="send-message">消息内容</label>
                    <textarea id="send-message" style="min-height:100px" placeholder="输入要发送的消息…"></textarea>
                </div>
                <button class="btn btn-primary" id="btn-send-message">${uiIcon('send')} 发送消息</button>
                <div id="send-result" class="mt-20 text-sm"></div>
            </div>`;
        attachSendEvents();
    })();
}

function attachSendEvents() {
    $('#btn-send-message')?.addEventListener('click', async () => {
        const gid = parseInt($('#send-group')?.value || '0');
        const msg = $('#send-message')?.value?.trim();
        if (!gid || !msg) { $('#send-result').textContent = '请选择群并输入消息'; return; }
        try {
            await api('/api/send', {
                method: 'POST', body: JSON.stringify({ group_id: gid, message: msg }),
            });
            $('#send-result').innerHTML = '<span style="color:var(--success)">已发送</span>';
            $('#send-message').value = '';
        } catch (e) {
            $('#send-result').innerHTML = `<span style="color:var(--danger)">发送失败: ${escapeHtml(e.message)}</span>`;
        }
    });
}

// ============ Plugin Management ============
async function fetchPlugins() {
    try {
        const data = await api('/api/plugins');
        state.plugins = data.plugins || [];
        state.pluginGroups = data.groups || [];
        if (state.activeView === 'plugins') renderPlugins();
    } catch (e) { /* ignore */ }
}

function renderPlugins() {
    const plugins = state.plugins;
    const groups = state.pluginGroups;
    const allGroups = [...new Set([...groups, ...(state.groups.map(g => g.gid))])].sort((a, b) => a - b);

    const cardsHtml = plugins.map(p => {
        const groupTags = Object.entries(p.groups || {}).map(([gid, enabled]) => {
            return `<span class="plugin-tag ${enabled ? 'on' : 'off'}" data-plugin="${p.name}" data-gid="${gid}">${gid} ${enabled ? 'ON' : 'OFF'} <button type="button" class="tag-close" aria-label="移除群 ${gid} 的插件覆盖配置">&times;</button></span>`;
        }).join('');

        const groupOptions = allGroups.map(gid => `<option value="${gid}">群 ${gid}</option>`).join('');

        return `
        <div class="plugin-card" data-plugin="${p.name}">
            <div class="plugin-header">
                <div>
                    <h3>${p.name}</h3>
                    <p class="plugin-desc">${escapeHtml(p.desc || '')}</p>
                </div>
                <label class="toggle-switch">
                    <input type="checkbox" class="plugin-toggle" aria-label="启用 ${escapeHtml(p.name)}" data-plugin="${p.name}" ${p.default ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                </label>
            </div>
            <div class="plugin-groups">
                <div class="plugin-tags">${groupTags || '<span class="text-muted text-sm">暂无分群配置</span>'}</div>
                <div class="plugin-group-add">
                    <select class="plugin-gid-select" aria-label="选择插件配置的群">
                        <option value="">选择群号...</option>
                        ${groupOptions}
                    </select>
                    <button class="btn btn-sm btn-success btn-enable-group" data-plugin="${p.name}">开启</button>
                    <button class="btn btn-sm btn-danger btn-disable-group" data-plugin="${p.name}">关闭</button>
                </div>
            </div>
        </div>`;
    }).join('');

    $('#app-view').innerHTML = `
        <div class="page-header">
            <h2>插件管理</h2>
            <p>管理已注册插件的默认开关和分群配置</p>
        </div>
        <div class="plugin-grid">
            ${cardsHtml || '<div class="empty-state">暂无已注册插件</div>'}
        </div>
        <div class="plugin-actions">
            <button class="btn btn-outline" id="btn-reload-plugins">${uiIcon('reload')} 重载插件配置</button>
        </div>`;

    // Toggle default
    $$('.plugin-toggle').forEach(el => {
        el.addEventListener('change', async () => {
            const name = el.dataset.plugin;
            const enabled = el.checked;
            try {
                await api(`/api/plugins/${encodeURIComponent(name)}`, {
                    method: 'PUT', body: JSON.stringify({ default: enabled }),
                });
            } catch (e) { console.warn('update plugin failed', e); }
        });
    });

    // Group enable/disable
    $$('.btn-enable-group').forEach(el => {
        el.addEventListener('click', async () => {
            const name = el.dataset.plugin;
            const card = el.closest('.plugin-card');
            const gid = card.querySelector('.plugin-gid-select')?.value;
            if (!gid) { alert('请选择群号'); return; }
            try {
                await api(`/api/plugins/${encodeURIComponent(name)}`, {
                    method: 'PUT', body: JSON.stringify({ group: gid, group_enabled: true }),
                });
                fetchPlugins();
            } catch (e) { console.warn('enable group failed', e); }
        });
    });
    $$('.btn-disable-group').forEach(el => {
        el.addEventListener('click', async () => {
            const name = el.dataset.plugin;
            const card = el.closest('.plugin-card');
            const gid = card.querySelector('.plugin-gid-select')?.value;
            if (!gid) { alert('请选择群号'); return; }
            try {
                await api(`/api/plugins/${encodeURIComponent(name)}`, {
                    method: 'PUT', body: JSON.stringify({ group: gid, group_enabled: false }),
                });
                fetchPlugins();
            } catch (e) { console.warn('disable group failed', e); }
        });
    });

    // Tag close (restore default)
    $$('.plugin-tag').forEach(el => {
        el.addEventListener('click', async (ev) => {
            if (ev.target.classList.contains('tag-close')) {
                const name = el.dataset.plugin;
                const gid = el.dataset.gid;
                try {
                    await api(`/api/plugins/${encodeURIComponent(name)}`, {
                        method: 'PUT', body: JSON.stringify({ group: gid, group_enabled: null }),
                    });
                    fetchPlugins();
                } catch (e) { console.warn('remove group config failed', e); }
            }
        });
    });

    // Reload
    $('#btn-reload-plugins')?.addEventListener('click', async () => {
        try {
            await api('/api/plugins/reload', { method: 'POST' });
            alert('插件配置已重载');
            fetchPlugins();
        } catch (e) { alert('重载失败: ' + e.message); }
    });
}

// ============ Global Reload Button ============
function attachGlobalEvents() {
    $('#btn-reload-global')?.addEventListener('click', async () => {
        if (!confirm('确定重载人设+知识库？会立即生效。')) return;
        try {
            await api('/api/pipe-state/reload', { method: 'POST' });
            alert('人设+知识库已重载，立即生效');
        } catch (e) {
            try {
                await api('/api/persona/load', { method: 'POST' });
                alert('人设+知识库已重载');
            } catch (e2) {
                alert('重载失败: ' + e.message);
            }
        }
    });
}

// ============ Initialization ============
async function init() {
    $$('[data-icon]').forEach(el => el.insertAdjacentHTML('afterbegin', uiIcon(el.dataset.icon)));
    $('#btn-reload-global').insertAdjacentHTML('afterbegin', uiIcon('reload'));
    try {
        const cfg = await api('/api/config');
        if (cfg.bot_name) {
            BOT_NAME = cfg.bot_name;
            document.title = BOT_NAME + ' Relay - WebUI';
            const h1 = document.querySelector('.sidebar-header h1');
            if (h1) h1.textContent = BOT_NAME + ' Relay';
        }
    } catch (e) { /* use default */ }

    fetchStatus();
    fetchGroups();
    fetchPipeState();
    fetchEnvConfig();
    fetchPlugins();
    renderRoute();
    connectWS();
    attachGlobalEvents();

    setInterval(() => {
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
            fetchStatus(); fetchGroups(); fetchPipeState();
        }
    }, 15000);
    setInterval(fetchPipeState, 30000);
    setInterval(fetchEnvConfig, 30000);
    setInterval(fetchPlugins, 30000);
}
document.addEventListener('DOMContentLoaded', init);
