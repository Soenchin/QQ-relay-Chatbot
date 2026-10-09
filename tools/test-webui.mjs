// Browser regression checks against isolated in-memory fixtures. No npm packages required.
// Run: node tools/test-webui.mjs [path-to-Chromium-or-Edge]
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,readFile,writeFile,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {startPreview} from './preview.mjs';

const browserPath=process.argv[2]||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const profile=await mkdtemp(join(tmpdir(),'relay-ui-test-'));
const artifacts=process.env.UI_ARTIFACTS||join(tmpdir(),'relay-ui-artifacts');
await mkdir(artifacts,{recursive:true});
const preview=await startPreview(0);
const browser=spawn(browserPath,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',`--user-data-dir=${profile}`,'--remote-debugging-port=0','about:blank'],{stdio:'ignore'});
let socket;
let sequence=0;
const pending=new Map();
const errors=[];
const results=[];
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function waitFor(fn,label) {
    for(let i=0;i<100;i++){try{const result=await fn();if(result)return result;}catch{}await delay(80);}
    throw Error('Timed out: '+label);
}
function call(method,params={}) {
    const id=++sequence;
    return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},15000);
        pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
        socket.send(JSON.stringify({id,method,params}));
    });
}
async function evaluate(expression) {
    const result=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(result.exceptionDetails)throw Error(result.exceptionDetails.text+': '+JSON.stringify(result.exceptionDetails.exception));
    return result.result.value;
}
const check=async(expression,label)=>{assert.equal(await evaluate(expression),true,label);results.push(label);};
async function route(name,selector){await evaluate(`location.hash='#/${name}'`);await waitFor(()=>evaluate(`!!document.querySelector(${JSON.stringify(selector)})`),name);}
async function shot(name){await delay(650);const {data}=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});await writeFile(join(artifacts,name+'.png'),Buffer.from(data,'base64'));}
try {
    const port=await waitFor(async()=>Number((await readFile(join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]),'browser startup');
    const targets=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
    socket.onmessage=event=>{
        const message=JSON.parse(event.data);
        if(message.id){const p=pending.get(message.id);if(p){pending.delete(message.id);message.error?p.reject(Error(message.error.message)):p.resolve(message.result);}}
        if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);
        if(message.method==='Page.javascriptDialogOpening')call('Page.handleJavaScriptDialog',{accept:true,promptText:'测试新知识.md'}).catch(()=>{});
    };
    await call('Runtime.enable');await call('Page.enable');
    await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await call('Page.navigate',{url:preview.url});
    await waitFor(()=>evaluate(`document.querySelectorAll('.feed-entry').length===6 && document.querySelectorAll('[data-pipe]').length===3`),'dashboard fixtures');
    await evaluate(`document.fonts.ready.then(()=>true)`);
    await check(`document.fonts.check('400 16px "Relay Source Han Sans SC"', '群聊知识库') && [...document.fonts].some(font=>font.family==='Relay Source Han Sans SC' && font.status==='loaded')`,'Embedded Source Han Sans font file loads');
    await check(`getComputedStyle(document.body).fontSize==='16px' && getComputedStyle(document.querySelector('.feed-entry')).fontSize==='16px' && getComputedStyle(document.querySelector('.feed-entry .time')).fontSize==='14px'`,'Readable 16px body and 14px secondary text');
    await call('DOM.enable'); await call('CSS.enable');
    const {root}=await call('DOM.getDocument');
    const {nodeId}=await call('DOM.querySelector',{nodeId:root.nodeId,selector:'.page-header h2'});
    const {fonts}=await call('CSS.getPlatformFontsForNode',{nodeId});
    assert.ok(fonts.some(font=>font.isCustomFont && font.glyphCount>0 && font.familyName.includes('Source Han Sans')),JSON.stringify(fonts));
    results.push('Chinese glyphs actually render with bundled Source Han Sans, not system fallback');
    await check(`getComputedStyle(document.body).backgroundColor==='rgb(243, 245, 242)'`,'Sage light theme loaded');
    await check(`document.querySelectorAll('.nav-item .ui-icon').length===7`,'All navigation uses vector icons');
    await check(`document.querySelectorAll('#history-chart .bar-row').length===4`,'History chart updates after asynchronous group fetch');
    await evaluate(`window.counterBaselineSpread = (selector) => {
        const row = document.querySelector(selector);
        const targets = [...row.children].map(child => {
            if (child.tagName !== 'ROLLING-NUMBER') return child;
            const cell = child.querySelector('.number-cell');
            return cell.querySelector('.number-wheel').children[10 + Number(cell.dataset.digit)];
        });
        const baselines = targets.map(target => {
            const marker = document.createElement('i');
            marker.style.cssText = 'display:inline-block;width:0;height:0;padding:0;margin:0;vertical-align:baseline';
            target.append(marker);
            const baseline = marker.getBoundingClientRect().top;
            marker.remove();
            return baseline;
        });
        return Math.max(...baselines) - Math.min(...baselines);
    }`);
    await check(`counterBaselineSpread('.preview-count') < 1`, 'Preview digits, slash and unit share a text baseline');
    await check(`counterBaselineSpread('.metrics > div:last-child .value') < 1`, 'Session count and unit share a text baseline');
    await shot('dashboard-desktop');
    await evaluate(`window.savedCounter=document.querySelector('[data-pipe="100001"] rolling-number');window.savedFeed=document.querySelector('#live-feed');`);
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({counter:9,threshold:12})}).then(()=>fetchPipeState())`);
    await check(`savedCounter===document.querySelector('[data-pipe="100001"] rolling-number') && savedCounter.getAttribute('value')==='9' && savedFeed===document.querySelector('#live-feed')`,'Polling preserves counters and feed DOM');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({counter:10})}).then(()=>fetchPipeState())`);
    await check(`savedCounter.children.length===2 && savedCounter.getAttribute('aria-label')==='10' && savedCounter.querySelector('[data-place="0"]').dataset.digit==='0'`,'Rolling number handles 9 to 10');
    await evaluate(`document.getAnimations().forEach(animation => animation.finish())`);
    await check(`counterBaselineSpread('.preview-count') < 1`, 'Two-digit counters retain baseline alignment');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({counter:0})}).then(()=>fetchPipeState())`);
    await check(`savedCounter.children.length===1 && savedCounter.getAttribute('aria-label')==='0'`,'Counter reset removes extra digit');
    await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    await evaluate(`savedCounter.setAttribute('value',8)`);
    await check(`savedCounter.getAnimations({subtree:true}).length===0`,'Reduced motion disables number animation');
    await call('Emulation.setEmulatedMedia',{features:[]});
    await route('pipe-status','#pipe-grid');
    await check(`counterBaselineSpread('.pipe-count') < 1`, 'Pipe detail counters and slash share a text baseline');
    await evaluate(`window.savedSlider=document.querySelector('input[type=range]');savedSlider.focus();savedSlider.value=17;savedSlider.dispatchEvent(new Event('input'));`);
    await evaluate(`fetchPipeState()`);
    await check(`savedSlider===document.querySelector('input[type=range]') && savedSlider.value==='17'`,'Polling does not interrupt threshold input');
    await evaluate(`savedSlider.dispatchEvent(new Event('change'))`);
    await waitFor(()=>evaluate(`savedSlider.dataset.saving==='false'`),'threshold save');
    await check(`state.pipeState[0].threshold===17`,'Existing threshold API remains functional');
    await shot('pipe-desktop');
    await route('group-config','#group-config-tbody');
    await shot('groups-desktop');
    await evaluate(`document.querySelector('#new-group-id').value='100009';document.querySelector('#btn-add-group').click();document.querySelector('#btn-save-group-config').click();`);
    await waitFor(()=>evaluate(`document.querySelector('#save-group-indicator').classList.contains('visible')`),'group save');
    await check(`state.envConfig.group_mode['100009']==='pipe'`,'Group configuration retained');
    await route('plugins','.plugin-card');
    await shot('plugins-desktop');
    await check(`document.querySelectorAll('.plugin-toggle[aria-label]').length===2`,'Plugin toggles have accessible labels');
    await route('settings','#settings-form');
    await check(`!document.querySelector('.nav-item[data-view="send"]') && document.querySelector('.nav-item[data-view="settings"]').textContent.includes('连接与配置')`, 'Settings replaces manual-send navigation');
    await check(`document.querySelector('#setting-DEEPSEEK_API_KEY').value==='' && document.querySelector('#setting-DEEPSEEK_API_KEY').type==='password' && document.querySelector('#setting-DEEPSEEK_API_KEY').placeholder.includes('已配置')`, 'Secret inputs are blank and only show presence');
    await shot('settings-desktop');
    await evaluate(`document.querySelector('#setting-BOT_NAME').value='预览机器人';document.querySelector('#settings-form').requestSubmit()`);
    await waitFor(()=>evaluate(`document.querySelector('#settings-result').textContent.includes('请重启中继')`),'settings save');
    await check(`document.querySelector('#setting-DEEPSEEK_API_KEY').placeholder.includes('已配置')`, 'Saving identity preserves stored API key');
    let settingsWrites=(await(await fetch(preview.url+'/__preview/writes')).json()).filter(w=>w.path==='/api/settings');
    assert.deepEqual(settingsWrites.at(-1).body,{values:{BOT_NAME:'预览机器人'},clear_secrets:[]});
    results.push('Settings submits only changed fields, not blank secrets');
    await evaluate(`document.querySelector('#setting-DEEPSEEK_API_KEY').value='mock-secret-new';document.querySelector('#settings-form').requestSubmit()`);
    await waitFor(()=>evaluate(`document.querySelector('#setting-DEEPSEEK_API_KEY').value===''`),'secret scrub after save');
    await check(`!document.querySelector('#settings-form').textContent.includes('mock-secret-new')`, 'Successful save scrubs typed secret from UI');
    await evaluate(`document.querySelector('[data-clear-secret="NAPCAT_TOKEN"]').click();document.querySelector('#settings-form').requestSubmit()`);
    await waitFor(()=>evaluate(`document.querySelector('#setting-NAPCAT_TOKEN').placeholder==='尚未配置'`),'explicit clear token');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({failSettings:true})})`);
    await evaluate(`document.querySelector('#setting-BOT_NAME').value='失败时保留草稿';document.querySelector('#settings-form').requestSubmit()`);
    await waitFor(()=>evaluate(`document.querySelector('#settings-result').textContent.includes('保存失败')`),'failed settings save');
    await check(`document.querySelector('#setting-BOT_NAME').value==='失败时保留草稿' && !document.querySelector('#settings-fields').disabled`, 'Failed settings save retains draft and enables retry');
    await evaluate(`fetchGroups();fetchPipeState();fetchEnvConfig()`);
    await check(`document.querySelector('#setting-BOT_NAME').value==='失败时保留草稿'`, 'Background refresh does not replace settings draft');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({failSettings:false})})`);
    await evaluate(`document.querySelector('#setting-WEBUI_HOST').value='0.0.0.0';document.querySelector('#settings-form').requestSubmit()`);
    await waitFor(()=>evaluate(`document.querySelector('#setting-WEBUI_HOST').getAttribute('aria-invalid')==='true'`),'field error');
    await check(`document.querySelector('#setting-WEBUI_HOST').closest('details').open && document.activeElement.id==='setting-WEBUI_HOST'`, 'Advanced validation errors expand and focus invalid field');
    await evaluate(`document.querySelector('#setting-WEBUI_HOST').value='127.0.0.1';document.querySelector('#settings-form').requestSubmit()`);
    await waitFor(()=>evaluate(`document.querySelector('#settings-result').textContent.includes('请重启中继')`),'settings retry');
    await shot('settings-advanced-desktop');
    await route('logs','.log-row');
    await check(`document.querySelectorAll('.log-row').length===8 && document.querySelector('#logs-path').textContent.includes('relay.log')`, 'Log page displays initial history and file location');
    await check(`!window.logInjection && !document.querySelector('.log-message img') && document.querySelector('#logs-stream').textContent.includes('<img')`, 'Log messages render as text, never executable HTML');
    await shot('logs-desktop');
    await evaluate(`document.querySelector('#logs-level').value='ERROR';document.querySelector('#logs-level').dispatchEvent(new Event('change'));document.querySelector('#logs-search').value='连接';document.querySelector('#logs-search').dispatchEvent(new Event('input'))`);
    await check(`document.querySelectorAll('.log-row').length===1 && document.querySelector('.log-row').dataset.level==='ERROR'`, 'Log level and text filters combine');
    await evaluate(`Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.logCopied=text}}});document.querySelector('#logs-copy').click()`);
    await waitFor(()=>evaluate(`!!window.logCopied`),'log copy');
    await check(`logCopied.includes('连接失败') && !logCopied.includes('初始化完成')`, 'Copy includes only filtered log results');
    await evaluate(`Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('blocked')}}});document.querySelector('#logs-copy').click()`);
    await waitFor(()=>evaluate(`!document.querySelector('#logs-copy-fallback').hidden`),'clipboard fallback');
    await check(`document.querySelector('#logs-copy-fallback').selectionEnd===document.querySelector('#logs-copy-fallback').value.length`, 'Clipboard denial offers selected manual-copy text');
    await evaluate(`document.querySelector('#logs-level').value='';document.querySelector('#logs-level').dispatchEvent(new Event('change'));document.querySelector('#logs-search').value='';document.querySelector('#logs-search').dispatchEvent(new Event('input'));document.querySelector('#logs-follow').click();document.querySelector('#logs-stream').scrollTop=0`);
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({appendLog:'实时追加测试'})})`);
    await waitFor(()=>evaluate(`document.querySelector('#logs-stream').textContent.includes('实时追加测试')`),'incremental log');
    await check(`document.querySelectorAll('.log-row').length===9 && document.querySelector('#logs-stream').scrollTop===0`, 'Incremental logs append without duplicates or forced scrolling when paused');
    await evaluate(`document.querySelector('#logs-clear').click()`);
    await check(`document.querySelectorAll('.log-row').length===0 && document.querySelector('#logs-notice').textContent.includes('没有删除')`, 'Clear display is local-only');
    const storedLogs=await(await fetch(preview.url+'/__preview/log-polls')).json();
    assert.equal(storedLogs.count,9);results.push('Clearing view does not erase server history');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({appendLog:'清空后的新日志'})})`);
    await waitFor(()=>evaluate(`document.querySelector('#logs-stream').textContent.includes('清空后的新日志')`),'log after clear');
    await check(`document.querySelectorAll('.log-row').length===1`, 'Clear does not replay old cursor history');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({failLogs:true})})`);
    await waitFor(()=>evaluate(`document.querySelector('#logs-connection').textContent.includes('中断')`),'log failure');
    await check(`document.querySelectorAll('.log-row').length===1`, 'Connection failure preserves displayed logs');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({failLogs:false,resetLogs:true})})`);
    await waitFor(()=>evaluate(`document.querySelector('#logs-stream').textContent.includes('新进程已启动')`),'log restart');
    await check(`document.querySelector('#logs-notice').textContent.includes('重启') && document.querySelectorAll('.log-row').length===1`, 'Restart resets cursor and explains retained disk history');
    await route('knowledge','.knowledge-file');
    const pollsBefore=(await(await fetch(preview.url+'/__preview/log-polls')).json()).polls;
    await delay(1800);
    assert.equal((await(await fetch(preview.url+'/__preview/log-polls')).json()).polls,pollsBefore);
    results.push('Leaving log page stops polling');
    await evaluate(`document.querySelector('.knowledge-file').click()`);
    await waitFor(()=>evaluate(`!!document.querySelector('.delete-trigger')`),'knowledge editor');
    await check(`document.querySelector('.delete-options').hidden`,'Delete confirmation initially hidden');
    await evaluate(`document.querySelector('.delete-trigger').click()`);
    await check(`document.querySelector('.delete-trigger').getAttribute('aria-expanded')==='true' && document.querySelector('.delete-feedback').textContent.includes('无法撤销')`,'Delete explains irreversible file action');
    await shot('knowledge-confirm-desktop');
    await evaluate(`document.querySelector('.delete-cancel').click()`);
    await check(`document.activeElement.matches('.delete-trigger') && document.querySelector('.delete-options').hidden`,'Cancel restores trigger focus');
    await evaluate(`document.querySelector('.delete-trigger').click();document.querySelector('.delete-trigger').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`);
    await check(`document.querySelector('.delete-options').hidden`,'Escape cancels confirmation');
    const before=await(await fetch(preview.url+'/__preview/writes')).json();
    assert.equal(before.filter(w=>w.method==='DELETE').length,0);results.push('Cancel and Escape never issue DELETE');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({failDelete:true})})`);
    await evaluate(`document.querySelector('.delete-trigger').click();document.querySelector('.delete-confirm').click();document.querySelector('.delete-confirm').click()`);
    await check(`document.querySelector('.delete-tile').dataset.state==='pending' && document.querySelector('.delete-confirm').disabled && document.querySelector('#btn-save-knowledge').disabled`,'Delete pending prevents repeat submission and save');
    await waitFor(()=>evaluate(`document.querySelector('.delete-feedback').textContent.includes('删除失败')`),'delete failure');
    await check(`document.querySelector('#knowledge-textarea').value.includes('群聊说明') && !document.querySelector('#knowledge-textarea').readOnly && !document.querySelector('#btn-save-knowledge').disabled`,'Failed delete preserves editable file');
    await shot('knowledge-failure-desktop');
    await evaluate(`fetch('/__preview/control',{method:'POST',body:JSON.stringify({failDelete:false})})`);
    await evaluate(`document.querySelector('.delete-confirm').click()`);
    await waitFor(()=>evaluate(`document.querySelector('.delete-tile')?.dataset.state==='done'`),'delete success');
    await shot('knowledge-success-desktop');
    await waitFor(()=>evaluate(`document.querySelectorAll('.knowledge-file').length===1 && !document.querySelector('.delete-trigger')`),'knowledge refresh');
    results.push('Successful delete shows check only after server response, then refreshes list');
    await evaluate(`document.querySelector('#btn-new-knowledge').click()`);
    await check(`!!document.querySelector('#knowledge-textarea') && !document.querySelector('.delete-trigger')`,'Unsaved new file has no destructive file action');
    await evaluate(`document.querySelector('#knowledge-textarea').value='# 新文件';document.querySelector('#btn-save-knowledge').click()`);
    await waitFor(()=>evaluate(`document.querySelectorAll('.knowledge-file').length===2`),'new file saved');
    results.push('Knowledge creation and save retained');
    for(const width of [375,768,900,1440]) {
        await call('Emulation.setDeviceMetricsOverride',{width,height:width===375?812:1000,deviceScaleFactor:1,mobile:false});
        for(const [name,selector] of [['dashboard','#dashboard-overview'],['group-config','#group-config-tbody'],['pipe-status','#pipe-grid'],['knowledge','.knowledge-file'],['plugins','.plugin-card'],['settings','#settings-form'],['logs','.logs-panel']]) {
            await route(name,selector);
            if(name==='knowledge') {await evaluate(`document.querySelector('.knowledge-file').click()`);await waitFor(()=>evaluate(`!!document.querySelector('.delete-trigger')`),'mobile editor');await evaluate(`document.querySelector('.delete-trigger').click()`);}
            await check(`document.documentElement.scrollWidth<=innerWidth`,`${name}: no horizontal page overflow at ${width}px`);
            await check(`document.querySelector('.sidebar-nav').getBoundingClientRect().height>0`,`${name}: navigation reachable at ${width}px`);
            if(width===375)await shot(name+'-mobile');
        }
    }
    await call('Emulation.setDeviceMetricsOverride',{width:812,height:375,deviceScaleFactor:1,mobile:false});
    await route('dashboard','#dashboard-overview');
    await check(`document.documentElement.scrollWidth<=innerWidth`,'Landscape layout fits');
    await evaluate(`document.documentElement.style.zoom='2'`);
    await check(`document.documentElement.scrollWidth<=innerWidth`,'200% zoom layout fits');
    assert.equal(errors.length,0,JSON.stringify(errors));
    const writes=await(await fetch(preview.url+'/__preview/writes')).json();
    assert.equal(writes.filter(w=>w.method==='DELETE').length,2);results.push('Exactly two DELETE requests: one failed, one retry');
    console.log(JSON.stringify({passed:results.length,results,artifacts},null,2));
    await writeFile(join(artifacts,'results.json'),JSON.stringify({passed:results.length,results,errors},null,2));
} finally {
    if(socket?.readyState===1) {await call('Browser.close').catch(()=>{});socket.close();}
    browser.kill();
    await preview.close();
    await delay(500);
    await rm(profile,{recursive:true,force:true,maxRetries:3}).catch(()=>{});
}
