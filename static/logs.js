// Live log view owns its polling timer; leaving the route aborts the request.
function renderLogs() {
    const view = document.querySelector('#app-view');
    view.innerHTML = `<div class="page-header"><h2>运行日志</h2><p>查看启动、连接与异常输出。密钥已做脱敏，分享前仍请检查群消息和个人信息。</p></div>
        <section class="card logs-panel">
            <div class="logs-toolbar">
                <div class="form-group"><label for="logs-level">级别</label><select id="logs-level"><option value="">全部级别</option><option>ERROR</option><option>WARNING</option><option>INFO</option><option>DEBUG</option><option>CRITICAL</option></select></div>
                <div class="form-group logs-search"><label for="logs-search">搜索内容</label><input id="logs-search" type="search" placeholder="例如：管道、连接失败、群号" autocomplete="off"></div>
                <button type="button" class="btn btn-outline" id="logs-follow" aria-pressed="true">暂停滚动</button>
                <button type="button" class="btn btn-outline" id="logs-copy">复制筛选结果</button>
                <button type="button" class="btn btn-outline" id="logs-clear">清空显示</button>
            </div>
            <div class="logs-status"><span id="logs-connection" role="status">正在读取日志…</span><span id="logs-count">0 条</span></div>
            <p class="logs-notice" id="logs-notice" role="status" hidden></p>
            <div class="logs-stream" id="logs-stream" role="region" aria-label="运行日志列表" tabindex="0"><div class="empty-state">正在读取…</div></div>
            <div id="logs-copy-status" role="status" class="field-help"></div>
            <textarea id="logs-copy-fallback" aria-label="手动复制日志" readonly hidden></textarea>
        </section>
        <section class="card logs-storage"><h3>本地日志文件</h3><p id="logs-path">读取存储状态中…</p><p id="logs-retention" class="field-help"></p><p class="field-help">“清空显示”只影响当前页面，不删除文件。网页只展示本次进程的最近 1000 条；重启前的记录请查看日志文件。</p></section>`;
    const panel = view.querySelector('.logs-panel');
    const stream = view.querySelector('#logs-stream');
    const connection = view.querySelector('#logs-connection');
    const notice = view.querySelector('#logs-notice');
    const followButton = view.querySelector('#logs-follow');
    const controller = new AbortController();
    let timer;
    let stopped = false;
    let busy = false;
    let follow = true;
    let cursor;
    let session;
    let lines = [];
    let visible = [];
    function render() {
        const level = view.querySelector('#logs-level').value;
        const search = view.querySelector('#logs-search').value.trim().toLowerCase();
        visible = lines.filter(line => (!level || line.level === level) && (!search || line.msg.toLowerCase().includes(search)));
        const top = stream.scrollTop;
        const fragment = document.createDocumentFragment();
        for (const line of visible) {
            const row = document.createElement('div');
            row.className = 'log-row';
            row.dataset.level = ['ERROR','WARNING','INFO','DEBUG','CRITICAL'].includes(line.level) ? line.level : 'INFO';
            row.dataset.id = line.id;
            const time = document.createElement('time');
            time.dateTime = line.time;
            time.textContent = new Date(line.time).toLocaleTimeString('zh-CN', {hour12: false});
            const tag = document.createElement('span');
            tag.className = 'log-level';
            tag.textContent = line.level;
            const message = document.createElement('span');
            message.className = 'log-message';
            message.textContent = line.msg;
            row.append(time, tag, message);
            fragment.append(row);
        }
        if (!visible.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            empty.textContent = lines.length ? '没有匹配的日志，试试其他级别或关键词。' : '暂无日志，等待新的运行输出。';
            fragment.append(empty);
        }
        stream.replaceChildren(fragment);
        stream.scrollTop = follow ? stream.scrollHeight : top;
        view.querySelector('#logs-count').textContent = `${visible.length} / ${lines.length} 条`;
    }
    function showNotice(text) {
        notice.textContent = text;
        notice.hidden = !text;
    }
    async function poll() {
        if (stopped || busy) return;
        busy = true;
        try {
            const query = new URLSearchParams({limit: '1000'});
            if (cursor !== undefined) query.set('after', cursor);
            if (session) query.set('session', session);
            const response = await fetch(`/api/logs?${query}`, {signal: controller.signal, cache: 'no-store'});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (stopped) return;
            if (data.reset || (session && session !== data.session)) {
                lines = [];
                showNotice('中继已重启，正在显示新进程日志。旧记录仍保留在磁盘文件中。');
            } else if (data.gap) {
                lines = [];
                showNotice('日志增长超过内存窗口，已跳到最近记录；完整保留范围请查看本地文件。');
            }
            const previous = lines.at(-1)?.id ?? -1;
            const incoming = data.lines.filter(line => line.id > previous);
            lines.push(...incoming);
            if (lines.length > 1000) lines.splice(0, lines.length - 1000);
            cursor = data.cursor;
            session = data.session;
            connection.textContent = follow ? '实时更新 · 自动滚动' : '实时更新 · 滚动已暂停';
            if (incoming.length || data.reset || data.gap || !stream.querySelector('.log-row')) render();
            const storage = data.storage;
            view.querySelector('#logs-path').textContent = storage ? `${storage.directory} / ${storage.filename}` : '存储状态不可用';
            view.querySelector('#logs-retention').textContent = storage?.available
                ? `UTF-8 文本 · 每份 ${storage.max_bytes / 1024 / 1024} MB · 最多 ${storage.backups} 份轮换备份（.1 为最新备份）`
                : storage?.error || '文件记录不可用，当前仅展示内存日志。';
        } catch (error) {
            if (stopped || error.name === 'AbortError') return;
            connection.textContent = '日志连接中断，将自动重试。请在中继所在电脑访问 WebUI。';
        } finally {
            busy = false;
            if (!stopped) timer = setTimeout(poll, 1500);
        }
    }
    view.querySelector('#logs-level').addEventListener('change', render);
    view.querySelector('#logs-search').addEventListener('input', render);
    followButton.addEventListener('click', () => {
        follow = !follow;
        followButton.textContent = follow ? '暂停滚动' : '跟随最新';
        followButton.setAttribute('aria-pressed', String(follow));
        connection.textContent = follow ? '实时更新 · 自动滚动' : '实时更新 · 滚动已暂停';
        if (follow) stream.scrollTop = stream.scrollHeight;
    });
    view.querySelector('#logs-clear').addEventListener('click', () => {
        lines = [];
        render();
        showNotice('已清空当前显示，后续新日志仍会出现；没有删除本地文件。');
    });
    view.querySelector('#logs-copy').addEventListener('click', async () => {
        const status = view.querySelector('#logs-copy-status');
        const fallback = view.querySelector('#logs-copy-fallback');
        const text = visible.map(line => `${line.time} [${line.level}] [${line.stream}] ${line.msg}`).join('\n');
        if (!text) { status.textContent = '没有可复制的日志'; return; }
        try {
            await navigator.clipboard.writeText(text);
            if (!panel.isConnected) return;
            status.textContent = `已复制 ${visible.length} 条日志，分享前请检查个人信息。`;
            fallback.hidden = true;
            fallback.value = '';
        } catch {
            if (!panel.isConnected) return;
            fallback.hidden = false;
            fallback.value = text;
            fallback.focus();
            fallback.select();
            status.textContent = '浏览器未允许写入剪贴板，已选中日志，可按 Ctrl+C 复制。';
        }
    });
    poll();
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
}
