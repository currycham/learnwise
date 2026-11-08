// Real Chrome mobile-layout smoke test. Uses a temporary DB and no model workers.
const {spawn, spawnSync} = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'learnwise-browser-'));
const python = path.join(root, '.venv/bin/python'), env = {...process.env, DB_PATH:path.join(tmp,'test.db'), JOB_CONCURRENCY:'0', DEEPSEEK_API_KEY:'', BOCHA_API_KEY:''};
const seed = spawnSync(python, ['-c', `
import json
from app import db
from pathlib import Path
db.init()
db.run("INSERT INTO domains(id,name,key,status) VALUES(1,'Python 编程','python','ready')")
for i,name in enumerate(['递归','调用栈'],1):
 db.run('INSERT INTO concepts(id,name,key) VALUES(?,?,?)',i,name,str(i))
 db.run('INSERT INTO domain_nodes(domain_id,concept_id,stage,ord) VALUES(1,?,0,?)',i,i)
data=Path('fixtures/递归.json').read_text()
db.run("INSERT INTO pages(id,domain_id,concept_id,level,status,data) VALUES(1,1,1,'进阶','ready',?)",data)
`],{cwd:root,env,encoding:'utf8'});
assert.equal(seed.status,0,seed.stderr);
let server,chrome,ws, seq=0; const pending=new Map(), errors=[];
const delay=ms=>new Promise(r=>setTimeout(r,ms));
function cdp(method,params={}) {return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression) {const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;}
async function go(base,url) {await cdp('Page.navigate',{url:base+url});for(let i=0;i<60;i++){await delay(100);if(await evaluate(`location.pathname===${JSON.stringify(url.split('?')[0])} && document.readyState==='complete'`))break;}await delay(400);}
async function checkLayout(name) {
 const d=await evaluate(`({width:innerWidth,scroll:document.documentElement.scrollWidth})`);
 assert(d.scroll<=d.width+1,`${name}: overflow ${JSON.stringify(d)}`);
 const r=await cdp('Page.captureScreenshot',{format:'png'});fs.mkdirSync('/tmp/learnwise-browser-check',{recursive:true});fs.writeFileSync(`/tmp/learnwise-browser-check/${name}.png`,Buffer.from(r.data,'base64'));
 console.log('PASS layout',name,d);
}
(async()=>{
 const log=fs.openSync(path.join(tmp,'process.log'),'a');
 server=spawn(python,['-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18406','--workers','1'],{cwd:root,env,stdio:['ignore',log,log]});
 const base='http://127.0.0.1:18406';
 for(let i=0;i<80;i++){try{if((await fetch(base+'/healthz')).ok)break;}catch{}await delay(100);}
 chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--no-first-run','--no-default-browser-check','--remote-debugging-port=0',`--user-data-dir=${tmp}/chrome`,'--use-angle=swiftshader','--enable-unsafe-swiftshader','about:blank'],{stdio:['ignore',log,log]});
 const active=path.join(tmp,'chrome/DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await delay(100);
 const port=fs.readFileSync(active,'utf8').split('\n')[0];const targets=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r));
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);});
 await cdp('Page.enable');await cdp('Runtime.enable');
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await go(base,'/');await checkLayout('mobile-home');
 await evaluate(`document.getElementById('topic').value='概率论';document.getElementById('goal').value='看懂数据';document.getElementById('ask').dispatchEvent(new Event('submit',{cancelable:true}));`);
 await delay(500);assert.equal(await evaluate('location.pathname'),'/login');
 await go(base,'/');assert.deepEqual(await evaluate(`[document.getElementById('topic').value,document.getElementById('goal').value]`),['概率论','看懂数据']);console.log('PASS login roundtrip preserves draft');
 await go(base,'/d/1');await checkLayout('mobile-map');
 await go(base,'/p/1');await checkLayout('mobile-learn');
 assert((await evaluate(`document.getElementById('next-learning').innerText`)).includes('调用栈'));
 assert((await evaluate(`document.getElementById('next-learning').innerText`)).includes('准备中'));
 await evaluate('scrollTo(0,document.body.scrollHeight)');await delay(100);
 assert(await evaluate(`(()=>{const r=document.getElementById('next-learning').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+1})()`));console.log('PASS next-page status and fixed action');
 await go(base,'/graph');await delay(1500);await checkLayout('mobile-graph');
 await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});await go(base,'/p/1');await checkLayout('desktop-learn');
 assert.deepEqual(errors,[],'browser JS errors');console.log('PASS browser runtime (no uncaught JS errors)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();if(chrome)chrome.kill();if(server)server.kill();await delay(500);process.exit(process.exitCode||0);});
