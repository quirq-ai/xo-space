/* Projects chat: the workspace channel and one channel per project, each
   with a coordinator, plus the threads the coordinators start. Reads and
   writes /api/projects-chat/* (routers/cowork_agent/projects_chat.py).

   Layout: channel list | the selected channel's timeline and composer |
   thread cards grouped by what they need, or one open thread. Live updates
   come from one EventSource per open channel (closed on hide and on channel
   switch); when the stream errors the page polls every 5 s instead.

   Every agent or owner string goes through chatMarkdown(), which escapes
   first and then emits a fixed, attribute-controlled subset. The pure
   helpers are exported for tests/test_space_projects_chat.py. */
import {API_BASE,apiFetch,failText,withPageQuery} from '../core/api.js';
import {esc,rel} from '../core/ui.js';
import {projectPage} from '../core/navigation.js?v=20261013-chat1';

const BASE='/api/projects-chat';
const WORKSPACE='workspace';
const POLL_MS=5000;
const CHANNEL_KEY='space.projectsChat.channel';
const THREAD_ID=/^cmsg_[A-Za-z0-9]+$/;

/* ── pure helpers ─────────────────────────────────────────────────────── */

/* Escape-first inline markdown: `code`, **bold**, [label](#cmsg_…) as a
   thread link, [label](http(s)://…) as an external link. Code spans are
   lifted out before the other transforms so their contents stay literal. */
function inline(escaped){
  const codes=[];
  let s=escaped.replace(/\x00/g,'').replace(/`([^`\n]+)`/g,(_m,code)=>{codes.push('<code>'+code+'</code>');return'\x00'+(codes.length-1)+'\x00';});
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
    .replace(/\[([^\]\n]+)\]\(#(cmsg_[A-Za-z0-9]+)\)/g,
      '<a href="#" class="pc-thread-link" data-pc-thread="$2">$1</a>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)<]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\x00(\d+)\x00/g,(_m,i)=>codes[+i]);
}

/* Paragraphs split on blank lines; single newlines become <br>. */
export function chatMarkdown(src){
  const text=String(src??'').replace(/\r\n?/g,'\n').trim();
  if(!text)return'';
  return text.split(/\n{2,}/).map(block=>'<p>'+inline(esc(block)).replace(/\n/g,'<br>')+'</p>').join('');
}

export const GROUPS=Object.freeze([
  ['waiting','Waiting on you'],['working','Working'],['ready','Ready'],['failed','Failed'],['resolved','Resolved'],
]);

/* A resolved thread is Resolved whatever its last card said; otherwise the
   card's state decides, falling back to the thread's own state. */
export function threadGroup(thread){
  if(thread?.state==='resolved')return'resolved';
  const state=thread?.card?.state||thread?.state;
  if(state==='needs_reply'||state==='needs_approval')return'waiting';
  if(state==='done')return'ready';
  if(state==='failed')return'failed';
  return'working';
}

const activity=t=>Date.parse(t?.last_activity_at||t?.created_at||'')||0;

/* [{key,label,threads}] in the fixed order above, newest activity first,
   with empty groups left out. */
export function groupThreads(threads){
  const by=new Map(GROUPS.map(([key])=>[key,[]]));
  for(const t of Array.isArray(threads)?threads:[]){if(t&&t.ts)by.get(threadGroup(t)).push(t);}
  return GROUPS.filter(([key])=>by.get(key).length)
    .map(([key,label])=>({key,label,threads:by.get(key).sort((a,b)=>activity(b)-activity(a))}));
}

/* Whether the composer may send, and why not. `result` is the apiFetch
   result of GET /status (or null while it loads). */
export function composerState(result){
  if(!result)return{disabled:true,reason:'Loading…'};
  if(!result.ok)return{disabled:true,reason:result.notImplemented
    ?'Projects chat is not available for the active agent.'
    :'Projects chat status is unavailable: '+failText(result)+'.'};
  const s=result.data||{};
  if(s.supported===false)return{disabled:true,reason:'Projects chat is not available for the active agent'
    +(s.agent?' ('+s.agent+')':'')+'.'};
  if(s.enabled===false)return{disabled:true,
    reason:'The router is off (XO_ROUTER_MODE=off), so this chat is read-only. Turn the router on to send messages.'};
  return{disabled:false,reason:''};
}

/* The wording for a failed POST: 409 is the router being off, 501 the
   backend lacking the coordinator capability. */
export function postFailText(res){
  if(res.status===409)return'The router is off, so the message was not sent. Turn the router on (XO_ROUTER_MODE) to chat.';
  if(res.notImplemented)return'Not sent: projects chat is not available for the active agent.';
  return'Not sent: '+failText(res);
}

/* The thread's status.md checklist: lines led by ✱ (in progress), ○ (to do)
   or ✓ (done) get a glyph and a state class; any other line is a note. */
export function checklistHtml(status){
  const lines=String(status??'').split('\n').map(line=>line.trim()).filter(Boolean);
  if(!lines.length)return'';
  const KIND={'✱':'is-active','○':'is-todo','✓':'is-done'};
  return'<ul class="pc-checklist">'+lines.map(line=>{
    const body=line.replace(/^[-*]\s+/,'');
    const glyph=[...body][0];
    if(KIND[glyph])return'<li class="pc-check '+KIND[glyph]+'"><span class="pc-glyph" aria-hidden="true">'+glyph
      +'</span><span>'+inline(esc(body.slice(glyph.length).trim()))+'</span></li>';
    return'<li class="pc-check is-note"><span>'+inline(esc(body))+'</span></li>';
  }).join('')+'</ul>';
}

export const channelLabel=ch=>ch===WORKSPACE?'Workspace':ch;
const enc=encodeURIComponent;
const stateLabel={needs_reply:'Needs your reply',needs_approval:'Needs approval',working:'Working',done:'Ready',
  failed:'Failed',resolved:'Resolved'};

/* Workspace first, then projects alphabetically. */
export function sortChannels(list){
  return(Array.isArray(list)?list:[]).filter(c=>c&&typeof c.channel==='string')
    .sort((a,b)=>(a.channel===WORKSPACE?-1:b.channel===WORKSPACE?1:a.channel.localeCompare(b.channel)));
}

export function messageHtml(m,threadTitles=new Map()){
  const kind=['owner','agent','system'].includes(m?.author_kind)?m.author_kind:'system';
  const time=m.written_at?'<time datetime="'+esc(m.written_at)+'" title="'+esc(new Date(m.written_at).toLocaleString())+'">'
    +esc(rel(m.written_at))+'</time>':'';
  if(kind==='system')return'<li class="pc-msg is-system" data-id="'+esc(m.id)+'"><div class="pc-system">'
    +chatMarkdown(m.text)+'</div>'+time+'</li>';
  const who=kind==='owner'?'You':m.author_id==='thread'?'Thread agent':'Coordinator';
  const forwarded=m.forwarded_from?'<span class="pc-forwarded">forwarded from workspace</span>':'';
  const title=threadTitles.get(m.id);
  const chip=title!==undefined?'<a href="#" class="pc-thread-chip" data-pc-thread="'+esc(m.id)+'">Thread: '
    +esc(title||'untitled')+' →</a>':'';
  return'<li class="pc-msg is-'+kind+'" data-id="'+esc(m.id)+'"><div class="pc-meta"><b>'+esc(who)+'</b>'+forwarded+time+'</div>'
    +'<div class="pc-bubble">'+chatMarkdown(m.text)+'</div>'+chip+'</li>';
}

export function cardHtml(t,openTs=''){
  const card=t.card||{};
  const state=t.state==='resolved'?'resolved':card.state||t.state||'working';
  const headline=card.headline||t.title||'Untitled thread';
  const detail=(card.needs_you||card.happened||'').replace(/`/g,'');
  return'<li><button type="button" class="pc-card'+(t.ts===openTs?' is-open':'')+'" data-pc-thread="'+esc(t.ts)+'">'
    +'<span class="pc-card-head"><b>'+esc(headline)+'</b><span class="pc-state st-'+esc(state)+'">'+esc(stateLabel[state]||state)+'</span></span>'
    +(detail?'<span class="pc-card-detail">'+esc(detail)+'</span>':'')
    +'<span class="pc-card-foot">'+(t.branch?'<code>'+esc(t.branch)+'</code>':'')
    +'<span>'+esc(rel(t.last_activity_at||t.created_at))+'</span></span></button></li>';
}

export function cardsHtml(threads,openTs=''){
  const groups=groupThreads(threads);
  if(!groups.length)return'<p class="pc-empty">No threads yet. When a coordinator starts work it appears here.</p>';
  return groups.map(g=>'<section class="pc-group" data-group="'+g.key+'"><h3>'+esc(g.label)
    +' <span>'+g.threads.length+'</span></h3><ul>'+g.threads.map(t=>cardHtml(t,openTs)).join('')+'</ul></section>').join('');
}

/* ── the view ─────────────────────────────────────────────────────────── */

export function createProjectsChatView({request=apiFetch,EventSourceImpl=globalThis.EventSource,pollMs=POLL_MS,
  storage=globalThis.localStorage}={}){
  let root=null,active=false,generation=0;
  let statusResult=null,channels=[],channelsError='',channel=null;
  let messages=[],seen=new Set(),lastId='',threads=new Map(),loadError='';
  let es=null,pollTimer=null,coordinatorBusy=false;
  let openTs='',threadResult=null,threadGeneration=0,threadReload=null;
  let sending=false,threadSending=false;
  const $=sel=>root.querySelector(sel);

  const remember=name=>{try{storage?.setItem(CHANNEL_KEY,name);}catch(_e){}};
  const recall=()=>{try{return storage?.getItem(CHANNEL_KEY)||'';}catch(_e){return'';}};
  const path=(suffix='')=>BASE+'/channels/'+enc(channel)+suffix;

  function shell(){
    return'<div class="pc">'
      +'<aside class="pc-channels" aria-label="Channels"><h2>Channels</h2><ul data-pc-channels></ul></aside>'
      +'<div class="pc-main"><header class="pc-main-head"><h2 data-pc-title>Chat</h2>'
      +'<span class="pc-busy" data-pc-busy hidden><span class="pc-dot" aria-hidden="true"></span>coordinator working…</span>'
      +'<span class="pc-live" data-pc-live></span></header>'
      +'<ol class="pc-timeline" data-pc-timeline aria-live="polite"></ol>'
      +'<form class="pc-composer" data-pc-composer>'
      +'<textarea rows="2" data-pc-input aria-label="Message the coordinator" placeholder="Message the coordinator…"></textarea>'
      +'<button type="submit" data-pc-send>Send</button>'
      +'<p class="pc-hint" data-pc-hint>Enter to send, Shift+Enter for a new line.</p>'
      +'<p class="pc-error" data-pc-error hidden></p></form></div>'
      +'<aside class="pc-side" aria-label="Threads">'
      +'<div data-pc-cards-pane><h2>Threads</h2><div data-pc-cards></div></div>'
      +'<div class="pc-thread" data-pc-thread-pane hidden>'
      +'<button type="button" class="pc-back" data-pc-close>← All threads</button>'
      +'<div data-pc-thread-body></div>'
      +'<form class="pc-composer is-thread" data-pc-thread-composer>'
      +'<textarea rows="2" data-pc-thread-input aria-label="Reply in this thread" placeholder="Reply in this thread…"></textarea>'
      +'<button type="submit" data-pc-thread-send>Send</button>'
      +'<p class="pc-error" data-pc-thread-error hidden></p></form></div>'
      +'</aside></div>';
  }

  /* ── rendering ── */
  function renderChannels(){
    const list=$('[data-pc-channels]');
    if(channelsError){list.innerHTML='<li class="pc-error">'+esc(channelsError)+'</li>';return;}
    list.innerHTML=channels.map(c=>{
      const n=Number.isInteger(c.open_threads)&&c.open_threads>0?'<span class="pc-count" title="Open threads">'+c.open_threads+'</span>':'';
      const on=c.channel===channel;
      return'<li><button type="button" data-pc-channel="'+esc(c.channel)+'"'+(on?' class="is-on" aria-current="true"':'')+'>'
        +'<span>'+(c.kind==='workspace'?'# ':'')+esc(channelLabel(c.channel))+'</span>'+n+'</button></li>';
    }).join('');
  }
  function threadTitles(){
    const titles=new Map();
    for(const t of threads.values())titles.set(t.ts,t.card?.headline||t.title||'');
    return titles;
  }
  function renderTimeline(){
    const list=$('[data-pc-timeline]');
    const pinned=list.scrollHeight-list.scrollTop-list.clientHeight<80;
    $('[data-pc-title]').textContent=channel?channelLabel(channel):'Chat';
    if(loadError)list.innerHTML='<li class="pc-error">'+esc(loadError)+'</li>';
    else if(!messages.length)list.innerHTML='<li class="pc-empty">'+(channel===WORKSPACE
      ?'Ask the workspace coordinator anything; it routes work to the right project.'
      :'No messages yet. Ask this project’s coordinator for something to start a thread.')+'</li>';
    else{const titles=threadTitles();list.innerHTML=messages.map(m=>messageHtml(m,titles)).join('');}
    if(pinned)list.scrollTop=list.scrollHeight;
  }
  function renderCards(){$('[data-pc-cards]').innerHTML=cardsHtml([...threads.values()],openTs);}
  function renderBusy(){$('[data-pc-busy]').hidden=!coordinatorBusy;}
  function renderLive(){$('[data-pc-live]').textContent=es?'live':pollTimer?'polling every '+Math.round(pollMs/1000)+'s':'';}
  function renderComposer(){
    const state=composerState(statusResult);
    const input=$('[data-pc-input]'),send=$('[data-pc-send]'),hint=$('[data-pc-hint]');
    input.disabled=state.disabled||!channel;
    send.disabled=state.disabled||sending||!channel;
    hint.textContent=state.disabled?state.reason:'Enter to send, Shift+Enter for a new line.';
    hint.classList.toggle('is-blocked',state.disabled);
    const tInput=$('[data-pc-thread-input]'),tSend=$('[data-pc-thread-send]');
    tInput.disabled=state.disabled;tSend.disabled=state.disabled||threadSending;
    tInput.placeholder=state.disabled?state.reason:'Reply in this thread…';
  }
  function renderThread(){
    const pane=$('[data-pc-thread-pane]'),cards=$('[data-pc-cards-pane]');
    pane.hidden=!openTs;cards.hidden=Boolean(openTs);
    if(!openTs)return;
    const body=$('[data-pc-thread-body]');
    if(!threadResult){body.innerHTML='<p class="pc-empty">Loading thread…</p>';return;}
    if(!threadResult.ok){body.innerHTML='<p class="pc-error">'+esc(threadResult.status===404
      ?'There is no such thread.':failText(threadResult))+'</p>';return;}
    const d=threadResult.data||{},t=d.thread||{},card=d.card||t.card||{},session=d.session||{};
    const state=t.state==='resolved'?'resolved':card.state||t.state||'working';
    const list=(d.messages||[]).map(m=>messageHtml(m)).join('');
    body.innerHTML='<header class="pc-thread-head"><h2>'+esc(t.title||card.headline||'Untitled thread')+'</h2>'
      +'<span class="pc-state st-'+esc(state)+'">'+esc(stateLabel[state]||state)+'</span>'
      +(d.busy?'<span class="pc-busy"><span class="pc-dot" aria-hidden="true"></span>working…</span>':'')+'</header>'
      +'<dl class="pc-facts">'+(t.branch?'<dt>Branch</dt><dd><code>'+esc(t.branch)+'</code></dd>':'')
      +(session.status?'<dt>Session</dt><dd>'+esc(session.status)+(Number.isInteger(session.turns)?' · '+session.turns+' turns':'')+'</dd>':'')
      +'</dl>'
      +(session.last_error?'<p class="pc-error">'+esc(session.last_error)+'</p>':'')
      +(card.needs_you?'<div class="pc-needs"><b>Needs you</b>'+chatMarkdown(card.needs_you)+'</div>':'')
      +(card.happened?'<div class="pc-happened">'+chatMarkdown(card.happened)+'</div>':'')
      +(d.status?'<h3>Checklist</h3>'+checklistHtml(d.status):'')
      +'<h3>Messages</h3><ol class="pc-timeline is-thread">'+(list||'<li class="pc-empty">No messages in this thread yet.</li>')+'</ol>';
  }
  function renderAll(){renderChannels();renderTimeline();renderCards();renderBusy();renderLive();renderComposer();renderThread();}

  /* ── data ── */
  function addMessages(rows){
    let added=false;
    for(const m of Array.isArray(rows)?rows:[]){
      if(!m||!m.id||seen.has(m.id))continue;
      seen.add(m.id);messages.push(m);added=true;
      if(m.id>lastId)lastId=m.id;
    }
    if(added)messages.sort((a,b)=>(a.written_at||a.ts||'').localeCompare(b.written_at||b.ts||'')||a.id.localeCompare(b.id));
    return added;
  }
  function upsertThread(t){
    if(!t||!t.ts)return;
    threads.set(t.ts,t);
    if(t.ts===openTs)scheduleThreadReload();
  }
  async function loadStatus(){
    statusResult=await request(API_BASE+BASE+'/status');
    if(root)renderComposer();
  }
  async function loadChannels(){
    const res=await request(API_BASE+BASE+'/channels');
    channelsError=res.ok?'':'Channels unavailable: '+failText(res);
    if(res.ok)channels=sortChannels(res.data?.channels);
    if(!channel||!channels.some(c=>c.channel===channel)){
      const saved=recall();
      channel=channels.some(c=>c.channel===saved)?saved:(channels[0]?.channel||WORKSPACE);
    }
    if(root)renderChannels();
  }
  async function loadChannel(){
    const gen=generation;
    const [msgs,list]=await Promise.all([request(API_BASE+path('/messages?limit=200')),request(API_BASE+path('/threads'))]);
    if(gen!==generation)return false;
    loadError=msgs.ok?'':'Messages unavailable: '+failText(msgs);
    if(msgs.ok)addMessages(msgs.data?.messages);
    if(list.ok){threads=new Map();for(const t of list.data?.threads||[])upsertThread(t);}
    renderTimeline();renderCards();
    return msgs.ok;
  }
  async function loadThread(){
    if(!openTs)return;
    const gen=++threadGeneration,ts=openTs;
    const res=await request(API_BASE+path('/threads/'+enc(ts)));
    if(gen!==threadGeneration||ts!==openTs)return;
    threadResult=res;renderThread();
  }
  function scheduleThreadReload(){
    if(threadReload)return;
    threadReload=setTimeout(()=>{threadReload=null;if(active)loadThread();},250);
  }

  /* ── live updates ── */
  function stopLive(){
    if(es){es.close();es=null;}
    if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
    if(threadReload){clearTimeout(threadReload);threadReload=null;}
    coordinatorBusy=false;
  }
  function startLive(){
    stopLive();
    if(!active||!channel)return;
    if(typeof EventSourceImpl!=='function'){startPolling();return;}
    const gen=generation;
    let source;
    try{source=new EventSourceImpl(withPageQuery(API_BASE+path('/stream'+(lastId?'?after='+enc(lastId):''))));}
    catch(_e){startPolling();return;}
    es=source;
    const data=e=>{try{return JSON.parse(e.data);}catch(_e){return null;}};
    const current=()=>gen===generation&&es===source;
    source.addEventListener('message',e=>{
      if(!current())return;
      if(addMessages([data(e)]))renderTimeline();
    });
    source.addEventListener('thread',e=>{
      if(!current())return;
      upsertThread(data(e));renderCards();renderTimeline();
    });
    source.addEventListener('heartbeat',e=>{
      if(!current())return;
      coordinatorBusy=Boolean(data(e)?.coordinator_busy);renderBusy();
    });
    source.onerror=()=>{
      if(!current())return;
      source.close();es=null;coordinatorBusy=false;renderBusy();
      startPolling();
    };
    renderLive();
  }
  function startPolling(){
    if(pollTimer||!active)return;
    const gen=generation;
    const tick=async()=>{
      if(!active||gen!==generation)return;
      const [msgs,list]=await Promise.all([
        request(API_BASE+path('/messages'+(lastId?'?after='+enc(lastId):'?limit=200'))),
        request(API_BASE+path('/threads'))]);
      if(!active||gen!==generation)return;
      if(msgs.ok&&addMessages(msgs.data?.messages))renderTimeline();
      if(list.ok){for(const t of list.data?.threads||[])upsertThread(t);renderCards();renderTimeline();}
      if(openTs)loadThread();
      pollTimer=setTimeout(tick,pollMs);
    };
    pollTimer=setTimeout(tick,pollMs);
    renderLive();
  }

  /* ── actions ── */
  /* keepThread: a refresh or re-show of the same channel leaves the open
     thread open (and rereads it) instead of closing the pane. */
  async function selectChannel(name,{keepThread=false}={}){
    if(!name)return;
    stopLive();
    const gen=++generation;
    const keep=keepThread&&name===channel?openTs:'';
    channel=name;remember(name);
    messages=[];seen=new Set();lastId='';threads=new Map();loadError='';
    openTs=keep;if(!keep){threadResult=null;threadGeneration++;}
    $('[data-pc-error]').hidden=true;
    renderAll();
    await loadChannel();
    if(gen!==generation)return;
    if(keep)loadThread();
    if(active)startLive();
    renderLive();
  }
  function openThread(ts){
    if(!THREAD_ID.test(ts||''))return;
    if(openTs!==ts){openTs=ts;threadResult=null;$('[data-pc-thread-input]').value='';$('[data-pc-thread-error]').hidden=true;}
    renderThread();renderCards();
    $('[data-pc-thread-pane]').scrollIntoView?.({block:'nearest'});
    loadThread();
  }
  function closeThread(){openTs='';threadResult=null;threadGeneration++;renderThread();renderCards();}

  async function send({thread=false}={}){
    const input=$(thread?'[data-pc-thread-input]':'[data-pc-input]');
    const errEl=$(thread?'[data-pc-thread-error]':'[data-pc-error]');
    const text=input.value.trim();
    if(!text||composerState(statusResult).disabled||(thread?threadSending:sending)||!channel)return;
    if(thread&&!openTs)return;
    const target=channel,ts=openTs;
    const body=thread?{text,thread_ts:ts}:{text};
    if(thread)threadSending=true;else sending=true;
    errEl.hidden=true;renderComposer();
    const res=await request(API_BASE+BASE+'/channels/'+enc(target)+'/messages',{method:'POST',body});
    if(thread)threadSending=false;else sending=false;
    renderComposer();
    if(!res.ok){
      errEl.textContent=postFailText(res);errEl.hidden=false;
      if(res.status===409||res.notImplemented)loadStatus();
      return;
    }
    if(channel!==target)return;
    input.value='';
    if(thread){if(ts===openTs)loadThread();}
    else if(addMessages([res.data?.message]))renderTimeline();
  }

  function wire(){
    root.addEventListener('click',event=>{
      const el=event.target?.closest?.('[data-pc-channel],[data-pc-thread],[data-pc-close]');
      if(!el)return;
      if(el.dataset.pcChannel!==undefined){if(el.dataset.pcChannel!==channel)selectChannel(el.dataset.pcChannel);return;}
      if(el.dataset.pcClose!==undefined){closeThread();return;}
      event.preventDefault();openThread(el.dataset.pcThread);
    });
    const enterSends=(sel,thread)=>$(sel).addEventListener('keydown',event=>{
      if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();send({thread});}
    });
    enterSends('[data-pc-input]',false);enterSends('[data-pc-thread-input]',true);
    $('[data-pc-composer]').addEventListener('submit',event=>{event.preventDefault();send();});
    $('[data-pc-thread-composer]').addEventListener('submit',event=>{event.preventDefault();send({thread:true});});
  }

  async function reload(){
    const previous=channel;
    await Promise.all([loadStatus(),loadChannels()]);
    if(!active)return;
    await selectChannel(channel,{keepThread:channel===previous});
  }

  return{
    ...projectPage('projects-chat'),toolbar:null,
    mount(el){
      root=el;root.innerHTML=shell();wire();renderAll();
    },
    async show(){active=true;await reload();},
    hide(){active=false;generation++;stopLive();},
    refresh:reload,
    /* for tests: the live state the page renders from */
    _state:()=>({channel,messages,threads,openTs,es,pollTimer,coordinatorBusy,statusResult}),
    _send:send,_selectChannel:selectChannel,_openThread:openThread,
  };
}

export default createProjectsChatView();
