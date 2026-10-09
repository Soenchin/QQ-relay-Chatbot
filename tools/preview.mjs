// Isolated UI preview. All API writes affect in-memory fixtures only; no relay import.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

export async function startPreview(port = 8812) {
    const groups = [100001,100002,100003,100004].map((gid, i) => ({gid, mode: i < 3 ? 'pipe' : 'direct', history_count: 12 + i * 7}));
    const pipes = groups.slice(0,3).map((g, i) => ({gid:g.gid, counter:[3,5,1][i], threshold:[6,8,4][i], recent_count:3,
        recent:['小林：这个周末一起开黑吗？','阿青：我晚一点到，先帮我留个位置。','小林：没问题，群里喊你。']}));
    const files = new Map([['群聊说明.md','# 群聊说明\n\n欢迎来到开发讨论群。\n\n## 对话约定\n\n- 分享你正在做的东西\n- 遇到问题，把复现步骤一起贴出来\n- 留一点空间给轻松的聊天\n'],['游戏备忘.md','# 游戏备忘\n\n周末一起开黑。']]);
    let config = {group_mode:{100001:'pipe',100002:'pipe',100003:'pipe',100004:'direct'},fallback_mode:'direct',group_vision:[100001]};
    let failLogs = false;
    let logSession = 'preview-log-session';
    let logPolls = 0;
    let logSequence = 0;
    const logLines = [];
    const addLog = (msg, level='INFO') => logLines.push({id:++logSequence,time:new Date().toISOString(),level,stream:level==='ERROR'?'stderr':'stdout',msg});
    addLog('[中继] 初始化完成');
    addLog('[WebUI] 本机控制台已启动');
    addLog('[中继] 已连接 OneBot，开始监听');
    addLog('[管道] 收到新消息，累计 3 / 6');
    addLog('[读图] 临时文件已清理');
    addLog('[连接] 请求超时，稍后重试', 'WARNING');
    addLog('[管道] 连接失败：Authorization: [REDACTED]', 'ERROR');
    addLog('<img src=x onerror="window.logInjection=true"> — 这只是文本');
    let failDelete = false;
    let failSettings = false;
    let appSettings = {
        BOT_NAME:'QQ Bot', MASTER_QQ:'0', NAPCAT_WS_URL:'ws://127.0.0.1:3001',
        DEEPSEEK_BASE_URL:'https://api.deepseek.com/anthropic', DEEPSEEK_MODEL:'deepseek-v4-flash',
        CLAUDE_CMD:'claude.cmd', MEM_DIR:'./memory', PIPE_ADD_DIR:'./memory',
        MEME_SERVER_PORT:'8801', WEBUI_ENABLED:'true', WEBUI_HOST:'127.0.0.1', WEBUI_PORT:'8800',
    };
    const secretPresence = {DEEPSEEK_API_KEY:{configured:true},NAPCAT_TOKEN:{configured:true}};
    const settingsResponse = () => ({values:appSettings,secrets:secretPresence,environment_overrides:[],env_exists:true});
    const plugins = [{name:'link_summary',desc:'识别群聊中的网页链接，自动整理简短摘要。',default:true,groups:{100001:true}},
        {name:'welcome',desc:'在新成员加入时送上一句欢迎。',default:false,groups:{}}];
    const writes = [];
    const staticFiles = {'/':'static/index.html','/static/app.css':'static/app.css','/static/ui.js':'static/ui.js','/static/app.js':'static/app.js','/static/logs.js':'static/logs.js','/static/fonts/SourceHanSansSC-VF.woff2':'static/fonts/SourceHanSansSC-VF.woff2'};
    const server = http.createServer(async (req,res) => {
        const url = new URL(req.url,'http://localhost');
        const path = url.pathname;
        const send = (data,status=200) => {res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(data));};
        try {
            if (staticFiles[path]) {
                const file = staticFiles[path];
                res.writeHead(200,{'Content-Type':file.endsWith('.woff2')?'font/woff2':file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':'text/html; charset=utf-8'});
                res.end(await readFile(new URL('../'+file,import.meta.url)));return;
            }
            if (req.method === 'GET') {
                if(path==='/__preview/log-polls') return send({polls:logPolls,count:logLines.length});
                if(path==='/api/logs') {
                    logPolls++;
                    if(failLogs) return send({detail:'模拟日志连接失败'},503);
                    const after=Number(url.searchParams.get('after')||0);
                    const session=url.searchParams.get('session');
                    const reset=!!session && session!==logSession;
                    const rows=reset?logLines:logLines.filter(line=>line.id>after);
                    return send({lines:rows,cursor:logSequence,session:logSession,reset,gap:false,storage:{directory:'./memory/logs',filename:'relay.log',max_bytes:2097152,backups:1,available:true,error:null}});
                }
                if(path==='/api/settings') return send(settingsResponse());
                if(path==='/api/config') return send({bot_name:'QQ Bot',preview:true});
                if(path==='/api/status') return send({connected:true,bot_qq:123456,uptime:45360,subscribers:1});
                if(path==='/api/groups') return send(groups);
                if(path==='/api/pipe-state') return send({groups:pipes});
                if(path==='/api/env-config') return send(config);
                if(path==='/api/plugins') return send({plugins,groups:groups.map(g=>g.gid)});
                if(path==='/api/knowledge') return send([...files.keys()].map(name=>({name})));
                if(path.startsWith('/api/knowledge/')) {
                    const name=decodeURIComponent(path.slice('/api/knowledge/'.length));
                    return files.has(name)?send({content:files.get(name)}):send({detail:'文件不存在'},404);
                }
                if(path==='/__preview/writes') return send(writes);
            }
            let raw=''; for await(const chunk of req) raw+=chunk;
            const body=raw?JSON.parse(raw):{};
            if(path==='/__preview/control') {
                if('failLogs' in body) failLogs=body.failLogs;
                if(body.resetLogs) {logLines.length=0;logSequence=0;logSession+='-restart';addLog('新进程已启动');}
                if(body.appendLog) addLog(body.appendLog,body.logLevel||'INFO');
                if('failDelete' in body) failDelete=body.failDelete;
                if('failSettings' in body) failSettings=body.failSettings;
                if('counter' in body) pipes[0].counter=body.counter;
                if('threshold' in body) pipes[0].threshold=body.threshold;
                return send({ok:true});
            }
            writes.push({method:req.method,path,body});
            if(path==='/api/settings' && req.method==='PUT') {
                if(failSettings) return send({detail:'模拟配置保存失败，原配置未更新'},500);
                if(body.values?.WEBUI_HOST==='0.0.0.0') return send({detail:'请检查标出的配置项',errors:{WEBUI_HOST:'只允许本机地址'}},422);
                for(const [key,value] of Object.entries(body.values||{})) {
                    if(key in secretPresence) {if(value) secretPresence[key]={configured:true};}
                    else appSettings[key]=value;
                }
                for(const key of body.clear_secrets||[]) if(key in secretPresence) secretPresence[key]={configured:false};
                return send({...settingsResponse(),ok:true,updated:Object.keys(body.values||{}),restart_required:true});
            }
            if(path.startsWith('/api/knowledge/')) {
                const name=decodeURIComponent(path.slice('/api/knowledge/'.length));
                if(req.method==='DELETE') {
                    await new Promise(resolve=>setTimeout(resolve,400));
                    if(failDelete) return send({detail:'模拟网络失败，文件未删除'},503);
                    files.delete(name);return send({ok:true});
                }
                if(req.method==='PUT') {files.set(name,body.content);return send({ok:true});}
            }
            if(path.startsWith('/api/groups/') && req.method==='PUT') {
                const p=pipes.find(p=>String(p.gid)===path.split('/').pop());
                if(p && 'pipe_threshold' in body) p.threshold=body.pipe_threshold;
                return send({ok:true});
            }
            if(path==='/api/env-config' && req.method==='PUT') {
                config={group_mode:JSON.parse(body.group_mode),fallback_mode:body.fallback_mode,group_vision:JSON.parse(body.group_vision)};
                return send({ok:true,restart_required:false});
            }
            if(path.startsWith('/api/plugins/') && req.method==='PUT') {
                const p=plugins.find(p=>p.name===path.split('/').pop());
                if(p && 'default' in body) p.default=body.default;
                if(p && body.group) {if(body.group_enabled===null) delete p.groups[body.group];else p.groups[body.group]=body.group_enabled;}
                return send({ok:true});
            }
            if(['/api/send','/api/plugins/reload','/api/pipe-state/reload','/api/persona/load'].includes(path)) return send({ok:true});
            return send({detail:'预览中无此接口'},404);
        } catch(error) {send({detail:error.message},500);}
    });
    // A fixture WebSocket keeps the production subscription path active, without any QQ connection.
    const {createHash}=await import('node:crypto');
    const sockets=new Set();
    server.on('upgrade',(req,socket)=>{
        sockets.add(socket);socket.on('close',()=>sockets.delete(socket));socket.on('error',()=>{});
        const accept=createHash('sha1').update(req.headers['sec-websocket-key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
        socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n');
        const messages=[['小林','这个周末一起开黑吗？'],['阿青','刚把新功能跑通了，晚点贴效果图。'],['小林','可以，记得把配置也一起发来。'],['机器人','收到，我会整理一下讨论要点。'],['阿青','这个排版舒服多了。'],['小林','先保留现有接口，外观慢慢打磨。']];
        messages.forEach(([nick,text],i)=>setTimeout(()=>{
            if(socket.destroyed)return;
            const data=Buffer.from(JSON.stringify({type:nick==='机器人'?'reply':'message',data:{gid:100001+i%3,nick,text}}));
            const header=data.length<126?Buffer.from([0x81,data.length]):Buffer.from([0x81,126,data.length>>8,data.length&255]);
            socket.write(Buffer.concat([header,data]));
        },120+i*80));
        socket.on('data',data=>{if((data[0]&15)===8)socket.end();});
    });
    await new Promise(resolve=>server.listen(port,'127.0.0.1',resolve));
    return {url:`http://127.0.0.1:${server.address().port}`,close:()=>{sockets.forEach(s=>s.destroy());return new Promise(resolve=>server.close(resolve));}};
}
if(process.argv[1]===fileURLToPath(import.meta.url)) {
    const preview=await startPreview(Number(process.argv[2]||8812));
    console.log(`Isolated fixture preview: ${preview.url} (no real files or QQ connection)`);
}
