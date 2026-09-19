/* A stream block: server-sent events from GET /api/<module>/stream/<name>
   (routers/streams.py), one line per event, prepended to a list.

   The server names each event's type in the SSE "event:" field. A browser
   EventSource only hands "message" events to onmessage and needs one
   listener per known type, so an open-ended stream (every timeline type)
   would be silent. This client reads the same wire format through fetch and
   a ReadableStream instead: same id / event / data fields, same resume
   with Last-Event-ID, every type delivered, an AbortController to close it,
   and the page's query string forwarded like every other request. It
   reconnects with a short backoff until closed; a 404 module_disabled
   stops it and says so.

   mountStream(host, block, ctx) renders into host and returns
   {open, close, closed}: the spec view opens it on show and closes it on
   hide. ctx = {payload, renderLine(line) -> html} where the renderer
   builds one escaped row from the block's row expressions. */
import {API_BASE,withPageQuery} from './api.js';
import {esc} from './ui.js';
import * as kit from './shadcn.js';

const DEFAULT_LIMIT=200;
const BACKOFF_MS=[1000,2000,5000,10000];

/* Parse SSE text incrementally: returns the events completed by `chunk`
   and keeps the tail in state.rest. An event is {id, event, data}. */
export function parseSse(state,chunk){
  state.rest=(state.rest||'')+chunk;
  const events=[];
  let at;
  while((at=state.rest.search(/\r?\n\r?\n/))>=0){
    const raw=state.rest.slice(0,at);
    state.rest=state.rest.slice(at).replace(/^\r?\n\r?\n/,'');
    const event={id:null,event:'message',data:[]};
    for(const line of raw.split(/\r?\n/)){
      if(!line||line.startsWith(':'))continue;
      const colon=line.indexOf(':');
      const field=colon<0?line:line.slice(0,colon);
      const value=colon<0?'':line.slice(colon+1).replace(/^ /,'');
      if(field==='id')event.id=value;
      else if(field==='event')event.event=value||'message';
      else if(field==='data')event.data.push(value);
    }
    if(event.data.length)events.push({id:event.id,event:event.event,data:event.data.join('\n')});
  }
  return events;
}

export function mountStream(host,block,ctx){
  const limit=Math.max(1,Number(block.limit)||DEFAULT_LIMIT);
  const lines=[];
  let controller=null,lastId=null,attempt=0,closed=true,timer=null;
  host.innerHTML='<div class="spec-stream-status" data-slot="stream-status"></div><ol class="spec-stream" data-slot="stream-lines"></ol>';
  const status=host.querySelector('[data-slot="stream-status"]');
  const list=host.querySelector('[data-slot="stream-lines"]');

  function paint(){
    if(!lines.length){
      list.innerHTML='';
      status.innerHTML=kit.empty({title:esc(block.empty||'Nothing yet.'),description:'Lines appear here as they happen.',size:'sm'});
      return;
    }
    status.innerHTML='';
    list.innerHTML=lines.map(line=>'<li class="spec-stream-line" data-type="'+esc(line.type||'')+'">'+ctx.renderLine(line)+'</li>').join('');
  }
  function note(text,variant='default'){
    status.innerHTML=kit.alert({variant,description:esc(text)});
  }
  function push(line){
    lines.unshift(line);
    if(lines.length>limit)lines.length=limit;
    paint();
  }

  async function connect(){
    if(closed)return;
    controller=new AbortController();
    const headers={};
    if(lastId)headers['Last-Event-ID']=lastId;
    let response;
    try{
      response=await fetch(withPageQuery(API_BASE+block.stream),{headers,cache:'no-store',signal:controller.signal});
    }catch(err){
      if(closed)return;
      return retry();
    }
    if(!response.ok){
      if(response.status===404){
        let code=null;
        try{code=(await response.json())?.detail?.code||null;}catch(e){}
        if(code==='module_disabled'){note('This stream is off in Setup, under Modules.');return;}
      }
      return retry();
    }
    attempt=0;
    const reader=response.body.getReader();
    const decoder=new TextDecoder();
    const state={rest:''};
    try{
      while(!closed){
        const{value,done}=await reader.read();
        if(done)break;
        for(const event of parseSse(state,decoder.decode(value,{stream:true}))){
          if(event.id)lastId=event.id;
          let line=null;
          try{line=JSON.parse(event.data);}catch(e){line={type:event.event,detail:event.data};}
          if(line&&typeof line==='object'){
            if(!line.type)line.type=event.event;
            push(line);
          }
        }
      }
    }catch(err){
      if(closed)return;
    }
    if(!closed)retry();
  }
  function retry(){
    if(closed)return;
    const wait=BACKOFF_MS[Math.min(attempt++,BACKOFF_MS.length-1)];
    if(!lines.length)note('Reconnecting to the stream…');
    timer=setTimeout(connect,wait);
  }

  paint();
  return{
    open(){
      if(!closed)return;
      closed=false;attempt=0;
      connect();
    },
    close(){
      closed=true;
      clearTimeout(timer);timer=null;
      if(controller){controller.abort();controller=null;}
    },
    get closed(){return closed;},
    get lines(){return lines.slice();},
  };
}
