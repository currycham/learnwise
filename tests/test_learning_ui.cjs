const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('app/templates/learn.html', 'utf8');
const cardSource = html.slice(html.indexOf('const cards = Object.assign'), html.indexOf('// —— 练习：'));
const tick = () => new Promise(resolve => setImmediate(resolve));
async function cardTest() {
  const note = {innerHTML: ''}, button = {addEventListener(type, fn) {this.onclick = fn;}};
  let fail = true, calls = 0;
  const ctx = vm.createContext({LW: {cards: {}, api: true, pageId: 1}, online: true, P: {chunks: [1]},
    store: {get: () => ({'0-0': 'good'}), set() {}}, esc: String, markDone() {},
    document: {querySelectorAll: () => [], getElementById: id => id === 'cards-note' ? note : note.innerHTML.includes('id="cards-sync"') ? button : null},
    api: async () => {calls++; if (fail) throw Error('network'); return {};}});
  vm.runInContext(cardSource, ctx);
  assert(note.innerHTML.includes('1 张本机记录未同步'));
  assert(!note.innerHTML.includes('已有 1 张卡片进入'));
  await button.onclick();
  assert(note.innerHTML.includes('保存失败')); assert(note.innerHTML.includes('重试保存'));
  fail = false; await button.onclick();
  assert(note.innerHTML.includes('已有 1 张卡片进入')); assert(!note.innerHTML.includes('未同步'));
  assert.equal(calls, 2);
  console.log('PASS cards local records / failed save / retry / server-confirmed count');
}
async function quizTest() {
  let result, retry = {}, callCount=0, deferred;
  const box = {set innerHTML(value) {if(result) result.isConnected=false; result={isConnected:true,p:{textContent:''},status:{innerHTML:'', querySelector:()=>retry}, insertAdjacentHTML(){},querySelector(sel){return sel==='p'?this.p:this.status}};}, querySelector:()=>result};
  const source = html.slice(html.indexOf('function finish()'),html.indexOf('function light()'));
  const ctx = vm.createContext({res:Array(8).fill(true), quizAttempt:0, online:true, box,
    P:{quiz:Array(8),predict:[]},preds:{},LW:{pageId:1,api:true,domainUrl:'/d/1'},C:{level:'进阶'},cur:'知识点',
    store:{set(){}},markDone(){},esc:String,md:String,light(){ctx.lit=true;},renderQ(){result.isConnected=false;},
    document:{getElementById:()=>retry},api:async()=>{callCount++;if(callCount===1)throw Error('network');if(callCount===2)return {next:null};return new Promise(r=>deferred=r);}});
  vm.runInContext(source+'\nfinish();',ctx); await tick();
  assert(result.status.innerHTML.includes('保存失败')); assert(!ctx.lit);
  await retry.onclick(); assert(result.status.innerHTML.includes('成绩已保存')); assert(ctx.lit);
  vm.runInContext('finish()',ctx); const previous=result;
  retry.onclick(); // 再闯一次：令本次请求失效
  deferred({next:{url:'/next',name:'下一页'}}); await tick();
  assert(!previous.status.innerHTML.includes('/next'));
  console.log('PASS quiz failed save / retry without retake / stale response isolation');
}
(async()=>{await cardTest();await quizTest();})().catch(e=>{console.error(e);process.exit(1);});
