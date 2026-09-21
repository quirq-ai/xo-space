/* Work > Inbox > one item. Left, the fact as it arrived, the transcript of
   its latest session and the outcome; right, the chat that continues the
   session and the actions on the item. Data: GET
   /api/inbox/{project_id}/{workitem_id} (the row with its fact, session,
   outcome and claim, the transcript pointer, running, can_reply, can_send),
   re-read every 2 s while a session runs and every 15 s otherwise, only
   while the page is shown; the transcript is GET
   /api/sessions/{session_id}/transcript for the session the detail names,
   read on the same cadence. The selection is the hash query
   (#/inbox/item?p=<project>&id=<item>, or ?s=<session> for a runtime
   session no work item owns, which shows the transcript alone), read on
   every show, so a reload or a Back keeps the item. Every field is untrusted
   (the fact came from a feeder or a POST, the transcript from an agent), so
   every string is escaped before it reaches innerHTML. */
import {API_BASE,apiFetch,failText} from '../core/api.js';
import {esc,rel,toast} from '../core/ui.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import {hasLink,openItemLink} from '../core/item-links.js?v=20260921-work3';
import {INBOX_ITEM_PAGE} from '../core/navigation.js?v=20260921-work2';

const OUTCOME_LABEL={reply_drafted:'reply drafted',task_proposed:'task proposed',needs_you:'asks you',fyi:'for your information',handled:'handled'};
const STATE_LABEL={new:'no session yet',running:'a session is working on it',waiting:'waiting for you',failed:'the session failed',closed:'closed'};
const RUNNING_POLL_MS=2000,IDLE_POLL_MS=15000;
const safeUrl=u=>typeof u==='string'&&/^https?:\/\//i.test(u)?u:'';
const dtfmt=iso=>{
  const t=iso?new Date(iso).getTime():NaN;
  return isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
};
/* The agent's answer ends with its outcome as a fenced json block: the
   outcome section shows it, the bubble does not. Only a block that closes
   the message goes; a fenced block cannot hold a backtick, so the match
   never reaches back past the last one. */
export const stripOutcome=s=>String(s??'').replace(/\s*```json[^`]*```\s*$/,'');
/* the selection the hash carries: {project,id} for a work item, {session}
   for a session alone, null when it names neither */
export function readSelection(hash){
  const s=String(hash??''),at=s.indexOf('?');
  if(at<0)return null;
  const q=new URLSearchParams(s.slice(at+1));
  const session=q.get('s'),project=q.get('p'),id=q.get('id');
  if(session)return{session};
  if(project&&id)return{project,id};
  return null;
}
const sameSelection=(a,b)=>!!a&&!!b&&a.session===b.session&&a.project===b.project&&a.id===b.id;

let root=null,switchTo=()=>{};
let selected=null;      /* {project,id} or {session} */
let data=null;          /* the last good detail (never set for a session alone) */
let failed=null;        /* the last failed detail read, shown above the fact */
let transcript=null;    /* the last good transcript */
let transcriptFor='';   /* the session id that transcript belongs to */
let transcriptFailed=null;
let token=0;            /* race guard: only the newest read may paint */
let shown=false;
let busy=false;         /* a reply or an action in flight */
let draft='';           /* what is typed in the chat, kept across repaints */

const itemPath=(sel,suffix='')=>API_BASE+'/api/inbox/'+encodeURIComponent(sel.project)+'/'+encodeURIComponent(sel.id)+suffix;
const transcriptPath=id=>API_BASE+'/api/sessions/'+encodeURIComponent(id)+'/transcript';

/* the detail's shape, read defensively: the row's fields at the top, or
   under `row`, with the sidecars beside them */
const rowOf=()=>data&&data.row&&typeof data.row==='object'?data.row:(data||{});
const factOf=()=>(data&&data.fact&&typeof data.fact==='object'?data.fact:rowOf().fact)||{};
const sessionOf=()=>(data&&data.session&&typeof data.session==='object'?data.session:rowOf().session)||null;
const outcomeOf=()=>(data&&data.outcome&&typeof data.outcome==='object'?data.outcome:rowOf().outcome)||null;
const sessionId=()=>selected&&selected.session?selected.session
  :String(data&&data.transcript&&typeof data.transcript==='object'&&data.transcript.session_id||'');

function reset(next){
  selected=next;data=null;failed=null;transcript=null;transcriptFor='';transcriptFailed=null;draft='';busy=false;token++;
}
async function load(){
  if(!selected)return;
  const mine=++token,sel=selected;
  if(!sel.session){
    const res=await apiFetch(itemPath(sel));
    if(mine!==token)return; /* a newer read, or another item, owns the page */
    if(res.ok&&res.data){data=res.data;failed=null;}
    else failed=res;
  }
  const id=sessionId();
  if(id){
    const res=await apiFetch(transcriptPath(id));
    if(mine!==token)return;
    if(res.ok&&res.data){transcript=res.data;transcriptFor=id;transcriptFailed=null;}
    else{transcriptFailed=res;if(transcriptFor!==id){transcript=null;transcriptFor='';}}
  }else{transcript=null;transcriptFor='';transcriptFailed=null;}
  render();schedule();
}
function schedule(){
  clearSlottedInterval('work-item-poll');
  if(!shown||!selected)return;
  setSlottedInterval('work-item-poll',load,data&&data.running?RUNNING_POLL_MS:IDLE_POLL_MS);
}

/* The chat: one POST, then the detail and the transcript are re-read; the
   agent's answer lands on the transcript as its turn runs, which the 2 s
   poll picks up. Every action is the same shape: one POST, one re-read. */
async function send(){
  const text=draft.trim();
  if(!selected||selected.session||busy||!text)return;
  busy=true;render();
  const res=await apiFetch(itemPath(selected,'/reply'),{method:'POST',body:{text}});
  busy=false;
  if(!res.ok){toast('could not send: '+failText(res));render();return;}
  draft='';
  await load();
}
async function act(suffix,body,verb){
  if(!selected||selected.session||busy)return;
  busy=true;render();
  const res=await apiFetch(itemPath(selected,suffix),body===undefined?{method:'POST'}:{method:'POST',body});
  busy=false;
  if(!res.ok)toast('could not '+verb+': '+failText(res));
  await load();
}
const start=retry=>act('/start'+(retry?'?retry=true':''),undefined,retry?'retry':'start');
const sendDraft=()=>act('/send',undefined,'send the draft');
const archive=()=>act('/archive',{},'archive');
const reopen=()=>act('/reopen',undefined,'reopen');

/* ── painting ────────────────────────────────────────────────────────── */
function chip(text,cls=''){return'<span class="wi-chip'+(cls?' '+cls:'')+'">'+esc(text)+'</span>';}
function titleOf(){
  if(selected.session)return transcript&&transcript.title?String(transcript.title):selected.session;
  return factOf().title||rowOf().title||selected.id;
}
function factHTML(){
  const it=rowOf(),f=factOf();
  const url=safeUrl(f.url);
  const link={link:f.link,kind:f.kind,project_id:it.project_id};
  return'<article class="wi-msg is-item">'
    +'<div class="wi-msg-head"><b>'+esc(f.title||it.title||selected.id)+'</b>'
      +'<span class="wi-when" title="'+esc(dtfmt(f.ts))+'">'+esc(rel(f.ts))+'</span></div>'
    +'<div class="wi-chips">'+(it.section||f.section?chip(it.section||f.section):'')+(it.entity||f.entity?chip(it.entity||f.entity):'')
      +(f.kind?chip(f.kind):'')+(it.project_id?chip(it.project_id):'')+'</div>'
    +(f.body?'<pre class="wi-text">'+esc(f.body)+'</pre>':'<p class="wi-note">No details came with this item.</p>')
    +'<div class="wi-acts">'
      +(hasLink(link)?'<button class="inb-btn" type="button" data-act="open-link">Open in Space</button>':'')
      /* a real link, and only for an http(s) url: safeUrl ran above */
      +(url?'<a class="inb-btn" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">Open link</a>':'')
    +'</div>'
  +'</article>';
}
function turnHTML(m,runtime){
  const role=String(m&&m.role||'');
  const person=role==='person'||role==='user';
  const who=person?'You':role==='assistant'?(runtime||'Agent'):(role||'Note');
  const text=role==='assistant'?stripOutcome(m.content):String(m&&m.content||'');
  return'<li class="wi-turn is-'+(person?'person':role==='assistant'?'assistant':'system')+'">'
    +'<div class="wi-turn-head"><b>'+esc(who)+'</b></div>'
    +'<pre class="wi-text">'+esc(text)+'</pre></li>';
}
function transcriptHTML(){
  const id=sessionId();
  if(!id)return'';
  const session=sessionOf(),runtime=session&&session.runtime?String(session.runtime):'';
  if(!transcript){
    if(transcriptFailed)return'<div class="inb-fail">'+esc(failText(transcriptFailed))+'</div>';
    return'<div class="wi-note">Loading the transcript…</div>';
  }
  const stale=transcriptFailed?'<div class="inb-fail">'+esc(failText(transcriptFailed))+' · showing the last good read</div>':'';
  const turns=Array.isArray(transcript.messages)?transcript.messages:[];
  if(!turns.length)return stale+'<p class="wi-note">Nothing on the transcript yet.</p>';
  return stale+'<ol class="wi-turns">'+turns.map(m=>turnHTML(m,runtime)).join('')+'</ol>';
}
function outcomeHTML(o){
  if(!o||typeof o!=='object')return'';
  const task=o.task&&typeof o.task==='object'?o.task:null;
  const acted=Array.isArray(o.acted)?o.acted:[];
  return'<section class="wi-outcome" aria-label="Outcome">'
    +'<div class="wi-outcome-head">'+chip(OUTCOME_LABEL[o.kind]||o.kind,'is-outcome')+'<span class="wi-when">'+esc(rel(o.at))+'</span></div>'
    +(o.summary?'<p>'+esc(o.summary)+'</p>':'')
    +(o.question?'<p class="wi-question">'+esc(o.question)+'</p>':'')
    +(o.draft?'<p class="wi-note">Draft: <code>'+esc(o.draft)+'</code></p>':'')
    +(task&&task.title?'<p class="wi-note">Proposed task: '+esc(task.title)+(task.assignee?' for '+esc(task.assignee):'')+'</p>':'')
    +(acted.length?'<p class="wi-note">Acted: '+esc(acted.join('; '))+'</p>':'')
  +'</section>';
}
function threadHTML(){
  if(selected.session)return transcriptHTML();
  if(!data){
    if(failed)return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
    return'<div class="wi-note">Loading the item…</div>';
  }
  const session=sessionOf(),runtime=session&&session.runtime?String(session.runtime):'';
  const stale=failed?'<div class="inb-fail">'+esc(failText(failed))+' · showing the last good read</div>':'';
  return stale+factHTML()+transcriptHTML()
    +(data.running?'<div class="wi-typing" role="status"><i class="wi-dot" aria-hidden="true"></i>'+esc(runtime||'The agent')+' is working on it…</div>':'')
    +outcomeHTML(outcomeOf());
}
function statusHTML(){
  const it=rowOf(),session=sessionOf(),state=String(it.state||'new');
  const bits=[STATE_LABEL[state]||state];
  if(session){
    if(session.runtime)bits.push(String(session.runtime));
    if(session.attempt)bits.push('attempt '+Number(session.attempt));
    if(state==='failed'&&session.exit&&session.exit.message)bits.push(String(session.exit.message));
  }
  return'<p class="wi-status" role="status">'+esc(bits.join(' · '))+'</p>'
    +(!data.can_reply?'<p class="wi-note">Sessions are off for this section, so nothing answers here.</p>':'');
}
/* the actions on the item: Start only before a session exists, Retry only
   after a failed one, Send only when the policy allows the draft to go,
   Archive while open, Reopen once closed */
function actionsHTML(){
  const it=rowOf(),state=String(it.state||'new'),closed=it.status==='closed'||state==='closed';
  const off=busy||!!data.running?' disabled':'';
  const button=(act,label)=>'<button class="inb-btn" type="button" data-act="'+act+'"'+off+'>'+label+'</button>';
  return'<div class="wi-actions">'
    +(!closed&&!sessionOf()&&data.can_reply?button('start','Start a session'):'')
    +(state==='failed'&&data.can_reply?button('retry','Retry'):'')
    +(data.can_send?button('send-draft','Send the draft'):'')
    +(closed?button('reopen','Reopen'):button('archive','Archive'))
  +'</div>';
}
function chatHTML(){
  if(!data)return failed?'':'<p class="wi-note">Loading…</p>';
  const running=!!data.running,can=!!data.can_reply;
  const placeholder=running?'The agent is working on it; your next message can wait a moment.'
    :sessionOf()?'Reply to the agent…':'Tell the agent what to do with this item…';
  return'<div class="wi-chat-status">'+statusHTML()+'</div>'
    +'<textarea class="wi-input" rows="5" aria-label="Message to the agent" placeholder="'+esc(placeholder)+'"'+(!can||running||busy?' disabled':'')+'></textarea>'
    +'<div class="wi-chat-foot"><span class="wi-hint">'+(can?'⌘↩ sends. The answer lands on the transcript.':'')+'</span>'
      +'<button class="inb-btn wi-send" type="button" data-act="send"'+(!can||running||busy?' disabled':'')+'>'+(busy?'Sending…':'Send')+'</button></div>'
    +actionsHTML();
}
function render(){
  if(!root)return;
  const input=root.querySelector('.wi-input');
  const hadFocus=!!input&&document.activeElement===input;
  if(!selected){
    root.innerHTML='<div class="wi"><header class="wi-head"><a class="wi-back" href="#/inbox/items">Inbox</a><h1>Item</h1></header>'
      +'<div class="inb-empty"><b>No item open.</b><p>Open an item from the Inbox to read its session and talk to the agent about it.</p></div></div>';
    return;
  }
  root.innerHTML='<div class="wi">'
    +'<header class="wi-head"><a class="wi-back" href="#/inbox/items">Inbox</a><h1>'+esc(titleOf())+'</h1>'
      +'<div class="wi-head-acts"><button class="inb-btn" type="button" data-act="refresh">Refresh</button></div></header>'
    +'<div class="wi-body'+(selected.session?' is-session':'')+'"><section class="wi-thread" aria-label="Thread">'+threadHTML()+'</section>'
    +(selected.session?'':'<aside class="wi-chat" aria-label="Chat">'+chatHTML()+'</aside>')+'</div></div>';
  const box=root.querySelector('.wi-input');
  if(box){box.value=draft;if(hadFocus&&!box.disabled)box.focus({preventScroll:true});}
}

function onClick(e){
  const b=e.target.closest('button[data-act]');
  if(!b||b.disabled)return;
  switch(b.dataset.act){
    case'refresh':load();break;
    case'send':send();break;
    case'start':start(false);break;
    case'retry':start(true);break;
    case'send-draft':sendDraft();break;
    case'archive':archive();break;
    case'reopen':reopen();break;
    case'open-link':{
      const f=factOf(),it=rowOf();
      if(!data||!openItemLink(switchTo,{link:f.link,kind:f.kind,project_id:it.project_id}))toast('This item has no link to open.');
      break;
    }
  }
}

export default {
  ...INBOX_ITEM_PAGE,
  mount(el,ctx){
    root=el;switchTo=ctx.switchTo;
    el.addEventListener('click',onClick);
    el.addEventListener('input',e=>{if(e.target.classList.contains('wi-input'))draft=e.target.value;});
    el.addEventListener('keydown',e=>{
      if(e.target.classList.contains('wi-input')&&e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();send();}
    });
    render();
  },
  /* the hash names the item: another one replaces the page, the same one is re-read */
  show(){
    shown=true;
    const next=readSelection(location.hash);
    if(!next){reset(null);clearSlottedInterval('work-item-poll');render();return;}
    if(!sameSelection(next,selected))reset(next);
    render();load();
  },
  hide(){shown=false;clearSlottedInterval('work-item-poll');token++;},
  refresh:()=>load(),
};
