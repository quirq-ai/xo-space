/* Expressions for page specs (services/schema/page.schema.json).

   An expression is a dotted path followed by pipes separated by "|":

     connections|where:enabled|count      rows with enabled truthy, counted
     last_ok_at|rel                       "3m ago"
     events_total|plural:event            "12 events"
     warnings|join:,
     signed_in|map                        through the block's or item's map

   The path resolves into the row first when a row is given, then into the
   page payload; "page.x" always means the payload. A missing path is null,
   never an error: a spec cannot throw. Pure: no DOM, no fetch, so the
   tests run this file under node. */
import {rel} from './ui.js';

const SEGMENT=/^[A-Za-z0-9_$-]+$/;

/* Walk a dotted path into a value; undefined when any step is missing. */
export function get(source,path){
  if(source===null||source===undefined)return undefined;
  if(path===''||path==='.')return source;
  let value=source;
  for(const part of String(path).split('.')){
    if(value===null||value===undefined||typeof value!=='object')return undefined;
    if(Array.isArray(value)&&/^\d+$/.test(part))value=value[Number(part)];
    else value=Object.prototype.hasOwnProperty.call(value,part)?value[part]:undefined;
  }
  return value;
}

/* The row first, then the payload; "page.x" is the payload. */
export function resolve(path,payload,row){
  const p=String(path??'').trim();
  if(p===''||p==='.')return row!==undefined&&row!==null?row:payload;
  if(p==='page')return payload;
  if(p.startsWith('page.'))return get(payload,p.slice(5));
  if(row!==undefined&&row!==null){
    const inRow=get(row,p);
    if(inRow!==undefined)return inRow;
    const head=p.split('.')[0];
    if(row&&typeof row==='object'&&Object.prototype.hasOwnProperty.call(row,head))return undefined;
  }
  return get(payload,p);
}

/* What "when" and "where:" call true: not null, false, 0, "", an empty
   array or an empty object. */
export function truthy(value){
  if(value===null||value===undefined||value===false||value===0||value==='')return false;
  if(Array.isArray(value))return value.length>0;
  if(typeof value==='object')return Object.keys(value).length>0;
  return true;
}

const mapKey=value=>value===null||value===undefined?'null':typeof value==='boolean'?String(value):String(value);

function applyPipe(pipe,value,map){
  const at=pipe.indexOf(':');
  const name=(at<0?pipe:pipe.slice(0,at)).trim();
  const arg=at<0?'':pipe.slice(at+1);
  switch(name){
    case 'rel':return rel(value);
    case 'date':{
      if(!value)return'';
      const t=new Date(value).getTime();
      return isFinite(t)?new Date(t).toLocaleDateString(undefined,{dateStyle:'medium'}):'';
    }
    case 'datetime':{
      if(!value)return'';
      const t=new Date(value).getTime();
      return isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
    }
    case 'count':
      if(Array.isArray(value))return value.length;
      if(value&&typeof value==='object')return Object.keys(value).length;
      if(typeof value==='number')return value;
      return 0;
    case 'sum':
      if(!Array.isArray(value))return 0;
      return value.reduce((total,row)=>total+(Number(get(row,arg.trim()))||0),0);
    case 'where':{
      if(!Array.isArray(value))return[];
      const field=arg.trim();
      const negate=field.startsWith('!');
      const key=negate?field.slice(1):field;
      return value.filter(row=>negate?!truthy(get(row,key)):truthy(get(row,key)));
    }
    case 'map':{
      if(!map||typeof map!=='object')return value;
      const key=mapKey(value);
      if(Object.prototype.hasOwnProperty.call(map,key))return map[key];
      if(Object.prototype.hasOwnProperty.call(map,'*'))return map['*'];
      return value;
    }
    case 'plural':{
      if(value===null||value===undefined||value==='')return'';
      const n=Number(value);
      if(!isFinite(n))return'';
      const word=arg.trim();
      return n+' '+word+(n===1?'':'s');
    }
    case 'join':{
      if(Array.isArray(value))return value.filter(v=>v!==null&&v!==undefined&&v!=='').map(String).join(arg);
      return value===null||value===undefined?'':String(value);
    }
    case 'entries':
      if(Array.isArray(value))return value;
      if(value&&typeof value==='object')return Object.entries(value).map(([key,entry])=>(
        entry&&typeof entry==='object'&&!Array.isArray(entry)?{key,...entry}:{key,value:entry}));
      return[];
    case 'not':return!truthy(value);
    case 'first':return Array.isArray(value)?(value.length?value[0]:null):value;
    case 'last':return Array.isArray(value)?(value.length?value[value.length-1]:null):value;
    default:return value;
  }
}

/* evaluate(expr, payload, row, map): the value of one expression. A
   non-string expression is a literal and comes back as is. */
export function evaluate(expr,payload,row,map){
  if(expr===undefined||expr===null)return null;
  if(typeof expr!=='string')return expr;
  const parts=expr.split('|');
  let value=resolve(parts[0],payload,row);
  for(const pipe of parts.slice(1))value=applyPipe(pipe,value,map);
  return value===undefined?null:value;
}

/* Fill "{field}" and "{page.x}" in a template from the row and the payload.
   With encode, each value is URI-encoded (path and query templates); a
   missing value fills as "". */
export function fill(template,payload,row,encode=false){
  return String(template??'').replace(/\{([^{}]+)\}/g,(_m,path)=>{
    const value=evaluate(path.trim(),payload,row);
    const text=value===null||value===undefined?'':typeof value==='object'?JSON.stringify(value):String(value);
    return encode?encodeURIComponent(text):text;
  });
}

/* "tasks.poller.enabled" with false becomes {tasks:{poller:{enabled:false}}}. */
export function pathObject(path,value){
  const parts=String(path).split('.').map(p=>p.trim()).filter(Boolean);
  if(!parts.length)return{};
  const out={};
  let cursor=out;
  parts.forEach((part,i)=>{
    if(!SEGMENT.test(part))return;
    if(i===parts.length-1)cursor[part]=value;
    else{cursor[part]={};cursor=cursor[part];}
  });
  return out;
}
