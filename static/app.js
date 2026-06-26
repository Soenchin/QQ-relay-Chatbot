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
const WS_BASE = `ws://${location.host}/ws`;
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
    envConfig: { group_mode: {}, fallback_mode: 'direct', group_mode_raw: '' },
    ws: null,
    wsReconnectTimer: null,
    wsReconnectDelay: 1000,
    activeView: 'dashboard',
};

// ============ Utilities ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const escapeHtml = (s) => {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
};
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
        state.ws = null; scheduleReconnect();
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
            return `<div class="feed-entry reply"><span class="time">${t}</span>${gidTag}<span class="reply-icon">&#x1F4AC;</span><span class="reply-text">${escapeHtml(item.text)}</span></div>`;
        case 'manual':
            return `<div class="feed-entry manual"><span class="time">${t}</span>${gidTag}<span class="system-text">&#x1F4E8; ${escapeHtml(item.text)}</span></div>`;
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
        if (state.activeView === 'send') renderSend();
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
    $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === route.view));

    switch (route.view) {
        case 'dashboard': renderDashboard(); break;
        case 'group-config': renderGroupConfig(); break;
        case 'pipe-status': renderPipeStatus(); break;
        case 'knowledge': renderKnowledge(); break;
        case 'plugins': renderPlugins(); break;
        case 'send': renderSend(); break;
        default: view.innerHTML = '<div class="empty-state">&#x1F50D; 页面不存在</div>';
    }
}

// ============ Dashboard ============
function renderDashboard() {
    const status = state.bot;
    const groups = state.groups;
    const pipeData = state.pipeState;

    const totalGroups = groups.length;
    const totalMsg = groups.reduce((s, g) => s + g.history_count, 0);
    const pipeCount = groups.filter(g => g.mode === 'pipe').length;

    const cardsHtml = `
        <div class="card-grid">
            <div class="card">
                <h3>机器人状态</h3>
                <div class="value ${status.connected ? 'online' : 'offline'}">${status.connected ? '在线' : '离线'}</div>
                <div class="sub">QQ: ${status.bot_qq || '未登录'} - 在线订阅 ${status.subscribers || 0}</div>
            </div>
            <div class="card">
                <h3>运行时长</h3>
                <div class="value accent">${formatUptime(status.uptime)}</div>
                <div class="sub">${new Date().toLocaleString('zh-CN')}</div>
            </div>
            <div class="card">
                <h3>已接入群</h3>
                <div class="value accent">${totalGroups}</div>
                <div class="sub">${pipeCount} 管道群 - ${totalGroups - pipeCount} 直调群</div>
            </div>
            <div class="card">
                <h3>对话总条数</h3>
                <div class="value accent">${totalMsg}</div>
                <div class="sub">上限 50 条/群</div>
            </div>
        </div>`;

    const feedItems = state.liveFeed.slice(0, 50).map(item => feedItemHTML(item)).join('');
    const feedHtml = `
        <h3 style="margin-bottom: 14px; font-size: 18px; font-weight: 600;">
            &#x1F4CD; 实时消息流 <span class="text-sm text-muted">(${BOT_NAME} 回复 <span style="color:var(--success)">绿色高亮</span>)</span>
        </h3>
        <div class="feed-container" id="live-feed">
            ${feedItems || '<div class="empty-state">&#x1F4A4; 暂无消息，等待中...</div>'}
        </div>`;

    const maxHistory = Math.max(1, ...groups.map(g => g.history_count));
    const barRows = groups.map(g => {
        const pct = Math.max(2, (g.history_count / maxHistory) * 100);
        const barClass = g.mode === 'pipe' ? 'pipe-bar' : 'direct-bar';
        return `
            <div class="bar-row">
                <span class="bar-label">群 ${g.gid} <span class="text-muted">(${g.mode})</span></span>
                <div class="bar-track"><div class="bar-fill ${barClass}" style="width:${pct}%"></div></div>
                <span class="bar-value">${g.history_count}</span>
            </div>`;
    }).join('');

    const chartHtml = groups.length ? `
        <div class="card bar-chart">
            <h3>&#x1F4CA; 各群对话数量</h3>
            ${barRows}
        </div>` : '';

    let pipePreview = '';
    if (pipeData.length) {
        const pipeCards = pipeData.map(p => {
            const pct = p.threshold > 0 ? Math.min(100, (p.counter / p.threshold) * 100) : 0;
            const fillClass = pct < 50 ? 'low' : pct < 80 ? 'mid' : 'high';
            return `
                <div class="card" style="cursor:pointer" onclick="navigate('pipe-status')">
                    <h3>&#x1F527; 群 ${p.gid}</h3>
                    <div class="value accent">${p.counter} / ${p.threshold}</div>
                    <div class="sub" style="margin-top:10px">
                        <div class="progress-bar" style="height:8px;background:var(--bg-secondary);border-radius:4px;overflow:hidden;margin-bottom:6px">
                            <div class="progress-fill ${fillClass}" style="width:${pct}%"></div>
                        </div>
                        最近 ${p.recent_count} 条待触发消息
                    </div>
                </div>`;
        }).join('');
        pipePreview = `
            <h3 style="margin: 28px 0 14px; font-size: 18px; font-weight: 600;">
                &#x1F527; 管道状态速览 <span class="text-sm text-muted" style="cursor:pointer" onclick="navigate('pipe-status')">查看详情 &#x2192;</span>
            </h3>
            <div class="card-grid" style="grid-template-columns: repeat(auto-fill, minmax(240px, 1fr))">${pipeCards}</div>`;
    }

    $('#app-view').innerHTML = cardsHtml + feedHtml + chartHtml + pipePreview;
    const feed = $('#live-feed');
    if (feed) feed.scrollTop = 0;
}

// ============ Group Config ============
function renderGroupConfig() {
    const env = state.envConfig;
    const groupMode = env.group_mode || {};
    const fallback = env.fallback_mode || 'direct';

    const rows = Object.entries(groupMode).map(([gid, mode]) => `
        <tr data-gid="${gid}">
            <td><input type="text" value="${gid}" class="group-id-input" readonly style="background:var(--bg-hover);color:var(--text-muted);cursor:not-allowed;" /></td>
            <td>
                <select class="group-mode-select">
                    <option value="direct" ${mode === 'direct' ? 'selected' : ''}>直调 (direct)</option>
                    <option value="pipe" ${mode === 'pipe' ? 'selected' : ''}>管道 (pipe)</option>
                </select>
            </td>
            <td><button class="btn btn-danger btn-sm" onclick="removeGroupRow(this)">&#x1F5D1; 删除</button></td>
        </tr>
    `).join('');

    const groupTable = `
        <table class="group-table">
            <thead>
                <tr>
                    <th style="width:200px">群号</th>
                    <th>模式</th>
                    <th style="width:100px">操作</th>
                </tr>
            </thead>
            <tbody id="group-config-tbody">
                ${rows || '<tr class="empty-row"><td colspan="3">&#x1F4ED; 暂无配置，请添加群</td></tr>'}
            </tbody>
        </table>`;

    $('#app-view').innerHTML = `
        <div class="page-header">
            <h2>&#x2699;&#xFE0F; 群设置</h2>
            <p>管理每个群的模式（直调 / 管道）。保存后需重启 relay 才能生效。</p>
        </div>
        <div class="group-config-panel">
            <div class="panel-section">
                <div id="group-config-alert" style="display:none"></div>
            </div>

            <div class="panel-section">
                <h3>&#x1F4E5; 默认模式（fallback）</h3>
                <p class="text-muted text-sm mb-16">未在下方表格中指定的群，默认使用此模式。</p>
                <div class="form-group" style="max-width:300px">
                    <select id="fallback-mode-select">
                        <option value="direct" ${fallback === 'direct' ? 'selected' : ''}>直调 (direct) - 仅 @ 时回复</option>
                        <option value="pipe" ${fallback === 'pipe' ? 'selected' : ''}>管道 (pipe) - 可主动发言</option>
                    </select>
                </div>
            </div>

            <div class="panel-section">
                <h3>&#x1F4CB; 群模式列表</h3>
                <div class="add-group-row mb-20">
                    <div class="form-group">
                        <label>群号</label>
                        <input type="text" id="new-group-id" placeholder="123456789" />
                    </div>
                    <div class="form-group">
                        <label>模式</label>
                        <select id="new-group-mode">
                            <option value="direct">直调 (direct)</option>
                            <option value="pipe" selected>管道 (pipe)</option>
                        </select>
                    </div>
                    <button class="btn btn-success" id="btn-add-group">&#x2795; 添加</button>
                </div>
                ${groupTable}
            </div>

            <div class="panel-section">
                <h3>&#x1F4DD; 配置预览</h3>
                <p class="text-muted text-sm mb-16">保存后写入 .env 文件的实际内容。</p>
                <div class="json-preview" id="config-preview"></div>
            </div>

            <div class="panel-section mt-24">
                <button class="btn btn-primary btn-lg" id="btn-save-group-config">
                    &#x1F4BE; 保存到 .env
                </button>
                <span id="save-group-indicator" class="save-indicator" style="margin-left:16px">已保存</span>
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
            showGroupAlert('&#x26A0; 请输入有效的群号（纯数字）', 'warning');
            return;
        }
        const gidInt = parseInt(gid);
        if (state.envConfig.group_mode[gidInt]) {
            showGroupAlert('&#x26A0; 该群号已存在', 'warning');
            return;
        }

        state.envConfig.group_mode[gidInt] = mode;
        gidInput.value = '';
        renderGroupConfig();
        showGroupAlert('&#x2705; 已添加，请保存到 .env', 'success');
    });

    // Save to .env
    $('#btn-save-group-config')?.addEventListener('click', async () => {
        const groupMode = state.envConfig.group_mode;
        const fallback = $('#fallback-mode-select')?.value || 'direct';

        // Collect from table (in case user edited modes in-place)
        const tbody = $('#group-config-tbody');
        if (tbody) {
            tbody.querySelectorAll('tr').forEach(tr => {
                const gidInput = tr.querySelector('.group-id-input');
                const modeSelect = tr.querySelector('.group-mode-select');
                if (gidInput && modeSelect) {
                    const gid = parseInt(gidInput.value);
                    if (!isNaN(gid)) {
                        groupMode[gid] = modeSelect.value;
                    }
                }
            });
        }

        const groupModeJson = JSON.stringify(groupMode, null, 0);

        try {
            const result = await api('/api/env-config', {
                method: 'PUT',
                body: JSON.stringify({ group_mode: groupModeJson, fallback_mode: fallback }),
            });

            const indicator = $('#save-group-indicator');
            if (indicator) indicator.classList.add('visible');
            setTimeout(() => indicator?.classList.remove('visible'), 3000);

            if (result.restart_required) {
                showGroupAlert('&#x2705; 已保存到 .env。<strong>请重启 relay 才能生效。</strong>', 'warning');
            } else {
                showGroupAlert('&#x2705; 保存成功', 'success');
            }
        } catch (e) {
            showGroupAlert('&#x274C; 保存失败: ' + e.message, 'danger');
        }
    });

    // Fallback change updates preview
    $('#fallback-mode-select')?.addEventListener('change', updateConfigPreview);

    // Table mode changes update preview
    document.querySelectorAll('.group-mode-select').forEach(el => {
        el.addEventListener('change', updateConfigPreview);
    });
}

function removeGroupRow(btn) {
    const tr = btn.closest('tr');
    const gid = tr?.dataset.gid;
    if (gid) {
        delete state.envConfig.group_mode[gid];
        tr.remove();
        updateConfigPreview();
    }
}

function updateConfigPreview() {
    const preview = $('#config-preview');
    if (!preview) return;

    const groupMode = {};
    const tbody = $('#group-config-tbody');
    if (tbody) {
        tbody.querySelectorAll('tr').forEach(tr => {
            const gidInput = tr.querySelector('.group-id-input');
            const modeSelect = tr.querySelector('.group-mode-select');
            if (gidInput && modeSelect) {
                groupMode[gidInput.value] = modeSelect.value;
            }
        });
    }

    const fallback = $('#fallback-mode-select')?.value || 'direct';
    const raw = {
        GROUP_MODE: JSON.stringify(groupMode),
        FALLBACK_MODE: fallback,
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

    if (!pipeData.length) {
        $('#app-view').innerHTML = `
            <div class="page-header"><h2>&#x1F527; 管道状态</h2><p>管理管道群的主动发言配置</p></div>
            <div class="empty-state">&#x1F4A4; 暂无管道群，或机器人未启动</div>`;
        return;
    }

    const cardsHtml = pipeData.map(p => {
        const pct = p.threshold > 0 ? Math.min(100, (p.counter / p.threshold) * 100) : 0;
        const fillClass = pct < 50 ? 'low' : pct < 80 ? 'mid' : 'high';
        const recentHtml = p.recent && p.recent.length
            ? p.recent.map(r => `<div class="recent-item">${escapeHtml(r)}</div>`).join('')
            : '<div class="recent-item text-muted">（暂无最近消息）</div>';

        return `
        <div class="pipe-card">
            <h3>群 ${p.gid} <span class="pipe-mode-badge">PIPE</span></h3>

            <div class="pipe-progress">
                <div class="progress-label"><span>计数进度</span><span>${p.counter} / ${p.threshold}</span></div>
                <div class="progress-bar">
                    <div class="progress-fill ${fillClass}" style="width:${pct}%"></div>
                </div>
            </div>

            <div class="pipe-slider">
                <label><span>&#x1F3AF; 发言阈值</span><span class="save-indicator" id="saved-${p.gid}">已保存</span></label>
                <div class="slider-row">
                    <span class="text-sm text-muted">1</span>
                    <input type="range" id="slider-${p.gid}" min="1" max="30" value="${p.threshold}" data-gid="${p.gid}">
                    <span class="text-sm text-muted">30</span>
                    <span class="slider-value" id="slider-val-${p.gid}">${p.threshold}</span>
                </div>
            </div>

            <div class="pipe-recent">
                <div class="recent-title">最近消息窗口（${p.recent_count} 条）</div>
                ${recentHtml}
            </div>
        </div>`;
    }).join('');

    $('#app-view').innerHTML = `
        <div class="page-header">
            <h2>&#x1F527; 管道状态</h2>
            <p>拖拽滑块调整主动发言阈值，实时生效 - 计数到达阈值后自动触发 ${BOT_NAME} 发言</p>
        </div>
        <div class="pipe-grid">${cardsHtml}</div>`;

    pipeData.forEach(p => {
        const slider = $(`#slider-${p.gid}`);
        const valDisplay = $(`#slider-val-${p.gid}`);
        const indicator = $(`#saved-${p.gid}`);
        if (!slider || !valDisplay) return;

        let saveTimer = null;
        slider.addEventListener('input', () => {
            valDisplay.textContent = slider.value;
            if (indicator) { indicator.classList.remove('visible'); }
            if (saveTimer) clearTimeout(saveTimer);
            saveTimer = setTimeout(async () => {
                try {
                    await api(`/api/groups/${p.gid}`, {
                        method: 'PUT',
                        body: JSON.stringify({ pipe_threshold: parseInt(slider.value) }),
                    });
                    if (indicator) { indicator.classList.add('visible'); }
                    fetchPipeState();
                } catch (e) {
                    console.warn('Save threshold failed', e);
                }
            }, 400);
        });
    });
}

// ============ Knowledge ============
function renderKnowledge() {
    (async () => {
        let files;
        try {
            files = await api('/api/knowledge');
            state.knowledge = files;
        } catch (e) {
            $('#app-view').innerHTML = `<div class="page-header"><h2>&#x1F4DA; 知识库</h2><p>加载失败: ${e.message}</p></div>`;
            return;
        }

        const fileList = files.map(f =>
            `<div class="knowledge-file" data-filename="${f.name}">${f.name}</div>`).join('');

        $('#app-view').innerHTML = `
            <div class="page-header flex justify-between items-center">
                <div>
                    <h2>&#x1F4DA; 知识库</h2>
                    <p>管理群聊知识库 Markdown 文件（${files.length} 个文件）</p>
                </div>
                <button class="btn btn-success btn-sm" id="btn-new-knowledge">&#x2795; 新建</button>
            </div>
            <div class="knowledge-layout">
                <div class="knowledge-sidebar" id="knowledge-filelist">
                    ${fileList || '<div class="empty-state">&#x1F4ED; 知识库为空</div>'}
                </div>
                <div class="knowledge-editor" id="knowledge-editor-panel">
                    <div class="empty-state" style="flex:1;display:flex;align-items:center;justify-content:center">
                        &#x1F4C1; 选择一个文件或新建
                    </div>
                    <div class="knowledge-toolbar" id="knowledge-toolbar" style="display:none">
                        <span id="knowledge-filename-label" style="font-size:14px;color:var(--text-secondary);flex:1"></span>
                        <button class="btn btn-primary btn-sm" id="btn-save-knowledge">&#x1F4BE; 保存</button>
                        <button class="btn btn-danger btn-sm" id="btn-delete-knowledge">&#x1F5D1; 删除</button>
                    </div>
                </div>
            </div>`;
        attachKnowledgeEvents();
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
                const panel = $('#knowledge-editor-panel');
                panel.innerHTML = `
                    <textarea id="knowledge-textarea">${escapeHtml(data.content)}</textarea>
                    <div class="knowledge-toolbar">
                        <span style="font-size:14px;color:var(--text-secondary);flex:1">${name}</span>
                        <button class="btn btn-primary btn-sm" id="btn-save-knowledge">&#x1F4BE; 保存</button>
                        <button class="btn btn-danger btn-sm" id="btn-delete-knowledge">&#x1F5D1; 删除</button>
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
            <textarea id="knowledge-textarea" placeholder="&#x1F4DD; 在此输入 Markdown 内容..."></textarea>
            <div class="knowledge-toolbar">
                <span style="font-size:14px;color:var(--text-secondary);flex:1">${name}</span>
                <button class="btn btn-primary btn-sm" id="btn-save-knowledge">&#x1F4BE; 保存</button>
                <button class="btn btn-danger btn-sm" id="btn-delete-knowledge">&#x1F5D1; 删除</button>
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
            alert('已保存 &#x2705;');
            renderKnowledge();
        } catch (e) { alert('保存失败: ' + e.message); }
    });
    $('#btn-delete-knowledge')?.addEventListener('click', async () => {
        if (!confirm(`确定删除 "${filename}"？`)) return;
        try {
            await api(`/api/knowledge/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            alert('已删除 &#x2705;');
            renderKnowledge();
        } catch (e) { alert('删除失败: ' + e.message); }
    });
}

// ============ Send Message ============
function renderSend() {
    (async () => {
        let groups = state.groups;
        if (!groups.length) {
            try { groups = await api('/api/groups'); state.groups = groups; } catch (e) { /* ignore */ }
        }
        const options = groups.map(g => `<option value="${g.gid}">群 ${g.gid} (${g.mode})</option>`).join('');
        $('#app-view').innerHTML = `
            <div class="page-header">
                <h2>&#x2709;&#xFE0F; 手动发消息</h2>
                <p>通过机器人向指定群发送消息</p>
            </div>
            <div class="card" style="max-width:600px">
                <div class="form-group">
                    <label for="send-group">目标群</label>
                    <select id="send-group">${options || '<option value="">&#x1F4ED; 暂无可用群</option>'}</select>
                </div>
                <div class="form-group">
                    <label for="send-message">消息内容</label>
                    <textarea id="send-message" style="min-height:100px" placeholder="&#x1F4AC; 输入消息内容..."></textarea>
                </div>
                <button class="btn btn-primary" id="btn-send-message">&#x1F4E8; 发送</button>
                <div id="send-result" class="mt-20 text-sm"></div>
            </div>`;
        attachSendEvents();
    })();
}

function attachSendEvents() {
    $('#btn-send-message')?.addEventListener('click', async () => {
        const gid = parseInt($('#send-group')?.value || '0');
        const msg = $('#send-message')?.value?.trim();
        if (!gid || !msg) { $('#send-result').textContent = '&#x26A0; 请选择群并输入消息'; return; }
        try {
            await api('/api/send', {
                method: 'POST', body: JSON.stringify({ group_id: gid, message: msg }),
            });
            $('#send-result').innerHTML = '<span style="color:var(--success)">&#x2705; 已发送</span>';
            $('#send-message').value = '';
        } catch (e) {
            $('#send-result').innerHTML = `<span style="color:var(--danger)">&#x274C; 发送失败: ${e.message}</span>`;
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
            return `<span class="plugin-tag ${enabled ? 'on' : 'off'}" data-plugin="${p.name}" data-gid="${gid}">${gid} ${enabled ? 'ON' : 'OFF'} <span class="tag-close">&times;</span></span>`;
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
                    <input type="checkbox" class="plugin-toggle" data-plugin="${p.name}" ${p.default ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                </label>
            </div>
            <div class="plugin-groups">
                <div class="plugin-tags">${groupTags || '<span class="text-muted text-sm">暂无分群配置</span>'}</div>
                <div class="plugin-group-add">
                    <select class="plugin-gid-select">
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
            <h2>&#x1F9E9; 插件管理</h2>
            <p>管理已注册插件的默认开关和分群配置</p>
        </div>
        <div class="plugin-grid">
            ${cardsHtml || '<div class="empty-state">&#x1F4ED; 暂无已注册插件</div>'}
        </div>
        <div class="plugin-actions">
            <button class="btn btn-primary" id="btn-reload-plugins">&#x1F504; 重载插件配置</button>
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
            alert('人设+知识库已重载 &#x2705; 立即生效');
        } catch (e) {
            try {
                await api('/api/persona/load', { method: 'POST' });
                alert('人设+知识库已重载 &#x2705;');
            } catch (e2) {
                alert('重载失败: ' + e.message);
            }
        }
    });
}

// ============ Initialization ============
async function init() {
    try {
        const cfg = await api('/api/config');
        if (cfg.bot_name) {
            BOT_NAME = cfg.bot_name;
            document.title = BOT_NAME + ' Relay - WebUI';
            const h1 = document.querySelector('.sidebar-header h1');
            if (h1) h1.textContent = BOT_NAME + ' Relay';
        }
    } catch (e) { /* use default */ }

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
