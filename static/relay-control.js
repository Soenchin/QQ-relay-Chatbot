// Console lifecycle controls; the console stays connected while the relay is stopped.
let relayControlBusy = false;
let relayControlState = null;
function applyRelayStatus(data) {
    relayControlState = data;
    const managed = data.managed !== false && data.state !== 'unmanaged';
    const names = {stopped:'已停止', starting:'启动中', running:'运行中', reconnecting:'重连中', stopping:'停止中', error:'需要处理', unmanaged:'未托管'};
    document.querySelector('#relay-phase').textContent = names[data.state] || '状态未知';
    document.querySelector('#relay-message').textContent = data.message || (managed ? '' : '当前服务未启用启停管理');
    document.querySelector('#relay-data-dir').textContent = data.data_dir || '—';
    document.querySelector('#relay-memory-dir').textContent = data.memory_dir || '—';
    const transitional = ['starting','stopping'].includes(data.state);
    document.querySelector('#relay-start').disabled = !managed || relayControlBusy || transitional || !data.can_start;
    document.querySelector('#relay-stop').disabled = !managed || relayControlBusy || data.state === 'stopping' || data.state === 'stopped' || data.can_start;
    document.querySelector('#relay-restart').disabled = !managed || relayControlBusy || transitional || data.state === 'stopped';
}
async function refreshRelayStatus() {
    try {
        const response = await fetch('/api/relay', {cache:'no-store'});
        if (!response.ok) throw Error();
        applyRelayStatus(await response.json());
    } catch {
        document.querySelector('#relay-phase').textContent = '控制台连接中断';
        document.querySelector('#relay-message').textContent = '请检查服务是否运行；不会把连接失败当作已停止。';
        for (const id of ['relay-start','relay-stop','relay-restart']) document.getElementById(id).disabled = true;
    }
}
function initRelayControl() {
    for (const action of ['start','stop','restart']) document.querySelector(`#relay-${action}`).addEventListener('click', async () => {
        if (relayControlBusy) return;
        if (action !== 'start' && !confirm(action === 'stop'
            ? '停止中继会中断正在处理的消息和 Claude 任务，控制台和日志仍可使用。继续？'
            : '重启会中断当前任务并读取已保存配置；未保存的修改不会应用。继续？')) return;
        relayControlBusy = true;
        if (relayControlState) applyRelayStatus(relayControlState);
        const error = document.querySelector('#relay-action-error');
        error.textContent = '';
        try {
            const response = await fetch(`/api/relay/${action}`, {method:'POST'});
            const data = await response.json();
            if (!response.ok) throw Error(data.detail || '操作失败，请查看运行日志');
            applyRelayStatus(data);
            await Promise.all([fetchStatus(), fetchGroups(), fetchPipeState()]);
            const config = await api('/api/config');
            BOT_NAME = config.bot_name || BOT_NAME;
            document.querySelector('.sidebar-header h1').textContent = BOT_NAME + ' Relay';
            document.title = BOT_NAME + ' Relay - WebUI';
        } catch (failure) {
            error.textContent = failure.message || '操作失败，请查看运行日志';
        } finally {
            relayControlBusy = false;
            await refreshRelayStatus();
        }
    });
    async function poll() {
        await refreshRelayStatus();
        setTimeout(poll, 2500);
    }
    poll();
}
