/* Plain-language jobs over the scheduler's own fields. A job is Manual when
   every_seconds is null; a schedule is every_seconds plus an optional
   first_run_at anchor, so "every day at 02:00" is 86400 anchored at the next
   02:00 local time. Nothing here touches the DOM or the network, so Setup and
   Inbox describe a job the same way. */

export const UNITS=Object.freeze({seconds:1,minutes:60,hours:3600,days:86400});
export const WEEKDAYS=Object.freeze(['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday']);
const HOUR=3600,DAY=86400,WEEK=604800;
const SINGULAR={seconds:'second',minutes:'minute',hours:'hour',days:'day'};

const pad2=n=>String(n).padStart(2,'0');
const hhmm=d=>pad2(d.getHours())+':'+pad2(d.getMinutes());
/* An empty input is missing, not zero. */
const num=value=>value==null||String(value).trim()===''?NaN:Number(value);

export const isScheduled=job=>job?.every_seconds!=null;

/* The browser's offset for a date, e.g. "+05:30". */
export function utcOffset(date=new Date()){
  const minutes=-date.getTimezoneOffset(),abs=Math.abs(minutes);
  return (minutes<0?'-':'+')+pad2(Math.floor(abs/60))+':'+pad2(abs%60);
}

/* Local wall time with that date's offset, which the scheduler requires. */
export function toOffsetIso(d){
  return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate())
    +'T'+pad2(d.getHours())+':'+pad2(d.getMinutes())+':00'+utcOffset(d);
}

/* "5 minutes", "1 hour", "90 seconds". */
export function durationText(value,unit){
  return value+' '+(Number(value)===1?SINGULAR[unit]:unit);
}

/* The largest unit that divides evenly, so 300 s edits as 5 minutes and 90 s
   stays 90 seconds. Fractional seconds (a 0.5 s timeout) stay in seconds. */
export function splitDuration(seconds,units=['days','hours','minutes','seconds']){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<=0)return{value:'',unit:units.includes('minutes')?'minutes':units[units.length-1]};
  for(const unit of units){
    if(Number.isInteger(n/UNITS[unit]))return{value:n/UNITS[unit],unit};
  }
  return{value:n,unit:'seconds'};
}

function parseTime(value){
  const match=/^(\d{1,2}):(\d{2})$/.exec(String(value||''));
  if(!match)return null;
  const hour=Number(match[1]),minute=Number(match[2]);
  return hour<24&&minute<60?{hour,minute}:null;
}

/* The next local instant strictly after now for an hourly, daily or weekly
   pattern. The scheduler also accepts a past anchor, but a future one makes
   the preview's "next run" true without a round trip. */
function nextOccurrence(now,{kind,hour=0,minute=0,weekday=0}){
  const d=new Date(now.getTime());
  d.setSeconds(0,0);
  if(kind==='hourly'){
    d.setMinutes(minute);
    if(d<=now)d.setHours(d.getHours()+1);
    return d;
  }
  d.setHours(hour,minute);
  if(kind==='weekly')d.setDate(d.getDate()+((weekday-d.getDay()+7)%7));
  if(d<=now)d.setDate(d.getDate()+(kind==='weekly'?7:1));
  return d;
}

/* A form choice → {every_seconds, first_run_at}, or {error} in plain words.
   choice.kind is custom | hourly | daily | weekly. A custom choice keeps an
   edited job's existing anchor so saving does not shift its grid. */
export function scheduleToFields(choice,now=new Date()){
  const kind=choice?.kind;
  if(kind==='custom'){
    const every=num(choice.every);
    if(!Number.isInteger(every)||every<1)return{error:'Enter how often it runs as a whole number, such as 30.'};
    if(!UNITS[choice.unit])return{error:'Choose seconds, minutes, hours or days.'};
    return{every_seconds:every*UNITS[choice.unit],first_run_at:choice.anchor||null};
  }
  if(kind==='hourly'){
    const minute=num(choice.minute);
    if(!Number.isInteger(minute)||minute<0||minute>59)return{error:'Enter a minute from 0 to 59.'};
    return{every_seconds:HOUR,first_run_at:toOffsetIso(nextOccurrence(now,{kind,minute}))};
  }
  if(kind==='daily'||kind==='weekly'){
    const time=parseTime(choice.time);
    if(!time)return{error:'Choose a time of day.'};
    const weekday=num(choice.weekday);
    if(kind==='weekly'&&!(Number.isInteger(weekday)&&weekday>=0&&weekday<7))return{error:'Choose a day of the week.'};
    const every=kind==='daily'?DAY:WEEK;
    return{every_seconds:every,first_run_at:toOffsetIso(nextOccurrence(now,{kind,...time,weekday}))};
  }
  return{error:'Choose how often the job runs.'};
}

/* A saved job → the form choice that reproduces it, or null for Manual. The
   upcoming slot (next_run) is read before the anchor, so the local time shown
   is today's, even when the anchor sits on the other side of a DST change. */
export function jobToSchedule(job){
  if(!isScheduled(job))return null;
  const every=Number(job.every_seconds);
  const at=new Date(job.next_run||job.first_run_at||NaN);
  const onMinute=!Number.isNaN(at.getTime())&&at.getSeconds()===0&&at.getMilliseconds()===0;
  if(onMinute&&every===HOUR)return{kind:'hourly',minute:at.getMinutes()};
  if(onMinute&&every===DAY)return{kind:'daily',time:hhmm(at)};
  if(onMinute&&every===WEEK)return{kind:'weekly',weekday:at.getDay(),time:hhmm(at)};
  const {value,unit}=splitDuration(every);
  return{kind:'custom',every:value,unit,anchor:job.first_run_at||null};
}

export function describeChoice(choice){
  if(!choice)return'Manual';
  switch(choice.kind){
    case'hourly':return'Every hour at :'+pad2(choice.minute);
    case'daily':return'Every day at '+choice.time;
    case'weekly':return'Every '+WEEKDAYS[choice.weekday]+' at '+choice.time;
    default:return Number(choice.every)===1?'Every '+SINGULAR[choice.unit]:'Every '+durationText(choice.every,choice.unit);
  }
}

/* "Every day at 02:00", "Every 30 minutes", "Manual". */
export const describeSchedule=job=>describeChoice(jobToSchedule(job));

const STATUS_TEXT={ok:'Succeeded',failed:'Failed',timed_out:'Timed out',missing_binary:'Command not found',
  error:'Could not start',skipped:'Skipped (still running)',lost:'Interrupted'};
export const statusText=status=>STATUS_TEXT[status]||String(status||'Unknown result');
