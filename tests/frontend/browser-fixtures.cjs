/* Shared browser surface; run the production login and skill-loading flow. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const catalog = {skills:[
  {id:'ecommerce-analysis',label:'运营分析'}, {id:'report-writing',label:'报告写作'},
  {id:'research',label:'信息调研'}, {id:'document-analysis',label:'文档分析'},
  {id:'data-analysis',label:'数据分析'}], max_selected:5,
  required_skills:{ordinary:[],internal_external:['ecommerce-analysis']}};
const response = (data,status=200) => ({ok:status<400,status,json:async()=>data});
async function harness(saved) {
  const elements = new Map(), calls = [], streams = [], timers = new Map();
  const element = selector => {
    if (!elements.has(selector)) {
      const listeners = new Map();
      elements.set(selector, {value:'', checked:false, innerHTML:'', textContent:'', hidden:false,
        scrollHeight:0,scrollTop:0,clientHeight:0,listeners,
        addEventListener(type,fn){listeners.set(type,fn);}, setAttribute(){},
        querySelector(){return element(selector+' child');},
        querySelectorAll(query){
          if (selector !== '#skillChoices' || !query.startsWith('input[name="skill_id"]')) return [];
          const inputs = catalog.skills.map(item=>element('[value="'+item.id+'"]'));
          return query.endsWith(':checked') ? inputs.filter(input=>input.checked) : inputs;
        }});
    }
    return elements.get(selector);
  };
  element('#modeSelect').value='ordinary';
  element('#modeSelect').options=[{value:'ordinary'},{value:'internal_external'}];
  for (const item of catalog.skills) element('[value="'+item.id+'"]').value=item.id;
  let stored=saved ? JSON.stringify(saved) : null, loggedIn=false;
  class Stream {
    constructor(threadId,runId,onEvent,onState) {Object.assign(this,{threadId,runId,onEvent,onState,seen:new Set(),afterSeq:0});streams.push(this);}
    connect(){this.connected=true;}
    close(){this.closed=true;}
  }
  class Channel {postMessage(){} close(){}}
  const context={document:{querySelector:element},localStorage:{getItem(){return stored;},setItem(k,v){stored=v;}},
    BroadcastChannel:Channel,Headers,RunEventStream:Stream,WebSocket:{OPEN:1},console,URLSearchParams,
    setTimeout(fn){const id=timers.size+1;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    reply:()=>response({files:[],jobs:[]})};
  context.fetch=async(url,options={})=>{
    calls.push({url,options});
    if(url==='/api/auth/session') return loggedIn ? response({csrf_token:'offline-csrf',expires_at:4102444800}) : response({},401);
    if(url==='/api/auth/login') {assert.equal(options.headers['X-Login-Intent'],'login');loggedIn=true;return response({csrf_token:'offline-csrf',expires_at:4102444800});}
    assert.equal(loggedIn,true);
    if(url==='/api/skills') return response(catalog);
    return context.reply(url,options);
  };
  vm.createContext(context);
  const source=fs.readFileSync(path.join(__dirname,'../../api/static/app.js'),'utf8');
  vm.runInContext(source+';this.ui={sessions,applyEvent,displayStatus,describeEvent,createSession,syncSession,sendTask,cancelRun,restoreSessions,closeSession,escapeHtml,refreshFiles,callGroups,renderCallGroups};',context);
  await new Promise(setImmediate);
  element('#accessPassword').value='offline-browser-password';
  await element('#loginForm').listeners.get('submit')({preventDefault(){}});
  assert.equal(element('#workspace').hidden,false);
  assert.equal(element('#sendBtn').disabled,false);
  return {context,ui:context.ui,element,calls,streams,stored:()=>JSON.parse(stored)};
}
module.exports={harness,response};
