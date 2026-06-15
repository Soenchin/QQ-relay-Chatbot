let BOT_NAME = 'QQ Bot';

/**
 * QQ Bot Relay WebUI — 前端 SPA
 * 功能：仪表盘（实时消息流+统计图表）、管道状态面板、知识库管理、手动发消息
 */
const API_BASE = '';
const WS_BASE = `ws://${location.host}/ws`;
const MAX_FEED = 200;

// ============ 全局状态 ============
const state = {
    bot: { connected: false, bot_qq: null, uptime: 0 },
    groups: [],
    pipeState: [],
    liveFeed: [],
    knowledge: [],
    ws: null,
    wsReconnectTimer: null,
    wsReconnectDelay: 1000,
    activeView: 'dashboard',
};

// ============ 工具函数 ============
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

// ============ 路由 ============
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
        console.log('[WS] 已连接');
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
            // 自动刷新管道状态（回复后计数归零）
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
            return `<div class="feed-entry reply"><span class="time">${t}</span>${gidTag}<span class="reply-icon">💬</span><span class="reply-text">${escapeHtml(item.text)}</span></div>`;
        case 'manual':
            return `<div class="feed-entry manual"><span class="time">${t}</span>${gidTag}<span class="system-text">📨 ${escapeHtml(item.text)}</span></div>`;
        default:
            return `<div class="feed-entry"><span class="time">${t}</span>${gidTag}${nick}<span class="msg-text">${escapeHtml(item.text)}</span></div>`;
    }
}

// ============ 状态更新 ============
function updateStatusDot(connected) {
    const dot = $('#statusDot');
    const text = $('#statusText');
    if (dot) dot.className = `status-dot ${connected ? 'online' : 'offline'}`;
    if (text) text.textContent = connected ? `在线 · ${state.bot.bot_qq || '?'}` : '离线';
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

// ============ 路由渲染 ============
function renderRoute() {
    const route = getRoute();
    state.activeView = route.view;
    const view = $('#app-view');
    $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === route.view));

    switch (route.view) {
        case 'dashboard': renderDashboard(); break;
        case 'pipe-status': renderPipeStatus(); break;
        case 'knowledge': renderKnowledge(); break;
        case 'send': renderSend(); break;
        default: view.innerHTML = '<div class="empty-state">页面不存在</div>';
    }
}

// ============ 📊 仪表盘 ============
function renderDashboard() {
    const status = state.bot;
    const groups = state.groups;
    const pipeData = state.pipeState;

    const totalGroups = groups.length;
    const totalMsg = groups.reduce((s, g) => s + g.history_count, 0);
    const pipeCount = groups.filter(g => g.mode === 'pipe').length;

    // 状态卡片
    const cardsHtml = `
        <div class="card-grid">
            <div class="card">
                <h3>机器人状态</h3>
                <div class="value ${status.connected ? 'online' : 'offline'}">${status.connected ? '在线' : '离线'}</div>
                <div class="sub">QQ: ${status.bot_qq || '未登录'} · 在线订阅 ${status.subscribers || 0}</div>
            </div>
            <div class="card">
                <h3>运行时长</h3>
                <div class="value accent">${formatUptime(status.uptime)}</div>
                <div class="sub">${new Date().toLocaleString('zh-CN')}</div>
            </div>
            <div class="card">
                <h3>已接入群</h3>
                <div class="value accent">${totalGroups}</div>
                <div class="sub">${pipeCount} 管道群 · ${totalGroups - pipeCount} 直调群</div>
            </div>
            <div class="card">
                <h3>对话总条数</h3>
                <div class="value accent">${totalMsg}</div>
                <div class="sub">上限 50 条/群</div>
            </div>
        </div>`;

    // 实时消息流
    const feedItems = state.liveFeed.slice(0, 50).map(item => feedItemHTML(item)).join('');
    const feedHtml = `
        <h3 style="margin-bottom: 12px">📍 实时消息流 <span class="text-sm text-muted">（${BOT_NAME} 回复 <span style="color:var(--success)">绿色高亮</span>）</span></h3>
        <div class="feed-container" id="live-feed">
            ${feedItems || '<div class="empty-state">暂无消息，等待中...</div>'}
        </div>`;

    // 统计柱状图
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
            <h3>📊 各群对话数量</h3>
            ${barRows}
        </div>` : '';

    // 管道状态速览
    let pipePreview = '';
    if (pipeData.length) {
        const pipeCards = pipeData.map(p => {
            const pct = p.threshold > 0 ? Math.min(100, (p.counter / p.threshold) * 100) : 0;
            const fillClass = pct < 50 ? 'low' : pct < 80 ? 'mid' : 'high';
            return `
                <div class="card" style="cursor:pointer" onclick="navigate('pipe-status')">
                    <h3>🔧 群 ${p.gid}</h3>
                    <div class="value accent">${p.counter} / ${p.threshold}</div>
                    <div class="sub" style="margin-top:8px">
                        <div class="progress-bar" style="height:6px;background:var(--bg-secondary);border-radius:3px;overflow:hidden;margin-bottom:4px">
                            <div class="progress-fill ${fillClass}" style="width:${pct}%"></div>
                        </div>
                        最近 ${p.recent_count} 条待触发消息
                    </div>
                </div>`;
        }).join('');
        pipePreview = `
            <h3 style="margin: 24px 0 12px">🔧 管道状态速览 <span class="text-sm text-muted" style="cursor:pointer" onclick="navigate('pipe-status')">查看详情 →</span></h3>
            <div class="card-grid" style="grid-template-columns: repeat(auto-fill, minmax(220px, 1fr))">${pipeCards}</div>`;
    }

    $('#app-view').innerHTML = cardsHtml + feedHtml + chartHtml + pipePreview;

    // 滚动消息流到顶部（最新）
    const feed = $('#live-feed');
    if (feed) feed.scrollTop = 0;
}

// ============ 🔧 管道状态面板 ============
function renderPipeStatus() {
    const pipeData = state.pipeState;

    if (!pipeData.length) {
        $('#app-view').innerHTML = `
            <div class="page-header"><h2>🔧 管道状态</h2><p>管理管道群的主动发言配置</p></div>
            <div class="empty-state">暂无管道群，或机器人未启动</div>`;
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
                <label><span>🎯 发言阈值</span><span class="save-indicator" id="saved-${p.gid}">已保存</span></label>
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
            <h2>🔧 管道状态</h2>
            <p>拖拽滑块调整主动发言阈值，实时生效 · 计数到达阈值后自动触发 ${BOT_NAME} 发言</p>
        </div>
        <div class="pipe-grid">${cardsHtml}</div>`;

    // 绑定滑块事件
    pipeData.forEach(p => {
        const slider = $(`#slider-${p.gid}`);
        const valDisplay = $(`#slider-val-${p.gid}`);
        const indicator = $(`#saved-${p.gid}`);
        if (!slider || !valDisplay) return;

        let saveTimer = null;
        slider.addEventListener('input', () => {
            valDisplay.textContent = slider.value;
            if (indicator) { indicator.classList.remove('visible'); }
            // 防抖保存
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
                    console.warn('保存阈值失败', e);
                }
            }, 400);
        });
    });
}

// ============ 📚 知识库 ============
function renderKnowledge() {
    (async () => {
        let files;
        try {
            files = await api('/api/knowledge');
            state.knowledge = files;
        } catch (e) {
            $('#app-view').innerHTML = `<div class="page-header"><h2>📚 知识库</h2><p>加载失败: ${e.message}</p></div>`;
            return;
        }

        const fileList = files.map(f =>
            `<div class="knowledge-file" data-filename="${f.name}">${f.name}</div>`).join('');

        $('#app-view').innerHTML = `
            <div class="page-header flex justify-between items-center">
                <div>
                    <h2>📚 知识库</h2>
                    <p>管理群聊知识库 Markdown 文件（${files.length} 个文件）</p>
                </div>
                <button class="btn btn-success btn-sm" id="btn-new-knowledge">➕ 新建</button>
            </div>
            <div class="knowledge-layout">
                <div class="knowledge-sidebar" id="knowledge-filelist">
                    ${fileList || '<div class="empty-state">知识库为空</div>'}
                </div>
                <div class="knowledge-editor" id="knowledge-editor-panel">
                    <div class="empty-state" style="flex:1;display:flex;align-items:center;justify-content:center">
                        选择一个文件或新建
                    </div>
                    <div class="knowledge-toolbar" id="knowledge-toolbar" style="display:none">
                        <span id="knowledge-filename-label" style="font-size:13px;color:var(--text-secondary);flex:1"></span>
                        <button class="btn btn-primary btn-sm" id="btn-save-knowledge">💾 保存</button>
                        <button class="btn btn-danger btn-sm" id="btn-delete-knowledge">🗑️ 删除</button>
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
                        <span style="font-size:13px;color:var(--text-secondary);flex:1">${name}</span>
                        <button class="btn btn-primary btn-sm" id="btn-save-knowledge">💾 保存</button>
                        <button class="btn btn-danger btn-sm" id="btn-delete-knowledge">🗑️ 删除</button>
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
            <textarea id="knowledge-textarea" placeholder="在此输入 Markdown 内容..."></textarea>
            <div class="knowledge-toolbar">
                <span style="font-size:13px;color:var(--text-secondary);flex:1">${name}</span>
                <button class="btn btn-primary btn-sm" id="btn-save-knowledge">💾 保存</button>
                <button class="btn btn-danger btn-sm" id="btn-delete-knowledge">🗑️ 删除</button>
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
            alert('已保存 ✅');
            renderKnowledge();
        } catch (e) { alert('保存失败: ' + e.message); }
    });
    $('#btn-delete-knowledge')?.addEventListener('click', async () => {
        if (!confirm(`确定删除 "${filename}"？`)) return;
        try {
            await api(`/api/knowledge/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            alert('已删除 ✅');
            renderKnowledge();
        } catch (e) { alert('删除失败: ' + e.message); }
    });
}

// ============ ✉️ 发消息 ============
function renderSend() {
    (async () => {
        let groups = state.groups;
        if (!groups.length) {
            try { groups = await api('/api/groups'); state.groups = groups; } catch (e) { /* ignore */ }
        }
        const options = groups.map(g => `<option value="${g.gid}">群 ${g.gid} (${g.mode})</option>`).join('');
        $('#app-view').innerHTML = `
            <div class="page-header">
                <h2>✉️ 手动发消息</h2>
                <p>通过机器人向指定群发送消息</p>
            </div>
            <div class="card" style="max-width:600px">
                <div class="form-group">
                    <label for="send-group">目标群</label>
                    <select id="send-group">${options || '<option value="">暂无可用群</option>'}</select>
                </div>
                <div class="form-group">
                    <label for="send-message">消息内容</label>
                    <textarea id="send-message" style="min-height:100px" placeholder="输入消息内容..."></textarea>
                </div>
                <button class="btn btn-primary" id="btn-send-message">📨 发送</button>
                <div id="send-result" class="mt-16 text-sm"></div>
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
            $('#send-result').innerHTML = '<span style="color:var(--success)">✅ 已发送</span>';
            $('#send-message').value = '';
        } catch (e) {
            $('#send-result').innerHTML = `<span style="color:var(--danger)">❌ 发送失败: ${e.message}</span>`;
        }
    });
}

// ============ 🌐 全局重载按钮 ============
function attachGlobalEvents() {
    $('#btn-reload-global')?.addEventListener('click', async () => {
        if (!confirm('确定重载人设+知识库？会立即生效。')) return;
        try {
            await api('/api/pipe-state/reload', { method: 'POST' });
            alert('人设+知识库已重载 ✅ 立即生效');
        } catch (e) {
            // 备用：用旧接口
            try {
                await api('/api/persona/load', { method: 'POST' });
                alert('人设+知识库已重载 ✅');
            } catch (e2) {
                alert('重载失败: ' + e.message);
            }
        }
    });
}

// ============ 启动 ============
async function init() {
    // 拉取远程配置
    try {
        const cfg = await api('/api/config');
        if (cfg.bot_name) {
            BOT_NAME = cfg.bot_name;
            document.title = BOT_NAME + ' Relay · WebUI';
            const h1 = document.querySelector('.sidebar-header h1');
            if (h1) h1.textContent = BOT_NAME + ' Relay';
        }
    } catch (e) { /* 使用默认值 */ }
    renderRoute();
    connectWS();
    attachGlobalEvents();
    setInterval(() => {
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
            fetchStatus(); fetchGroups(); fetchPipeState();
        }
    }, 15000);
    // 也定时刷新管道状态
    setInterval(fetchPipeState, 30000);
}
document.addEventListener('DOMContentLoaded', init);
