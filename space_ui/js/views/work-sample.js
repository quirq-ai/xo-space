/* Sample data for the Work section while the API behind it is designed
   (docs/work-and-workitems.md). The shapes follow section 9 of that document,
   so replacing this module with GET /api/feed, GET /api/work/attention and
   GET /api/workspace/workitems is a data change, not a page change. Times
   are minutes before "now" so relative labels read naturally, and the work
   items and attention list are shared between the pages so an action on one
   page shows on the other. */
const ago=min=>new Date(Date.now()-min*60000).toISOString();
const ahead=min=>new Date(Date.now()+min*60000).toISOString();

export const ME='sharmasuraj0123';
export const PROJECTS=[
  {id:'xo-space',name:'xo-space'},
  {id:'xo-swarm',name:'xo-swarm'},
  {id:'xo-swarm-api',name:'xo-swarm-api'},
  {id:'xo-coworker',name:'xo-coworker'},
];
export const AGENTS=[{id:'claude_code',label:'Claude Code',color:'#d7a75d'},{id:'codex',label:'Codex',color:'var(--chart-1)'}];

const issue=(repo,number)=>({repo,number,url:'https://github.com/'+repo+'/issues/'+number});

let _workitems=null;
export function workitems(){
  if(_workitems)return _workitems;
  _workitems=[
    {id:'8f2c',title:'Fix parser timeout on large transcripts',project:'xo-space',status:'open',origin:'space',
      assignee:'me',in_progress:false,labels:['bug'],body:'Sessions over 40 MB stall the watcher for minutes. Either cap the transcript we read or stream it in chunks; the decision is yours.',
      created:ago(1500),updated:ago(120)},
    {id:'c7e0',title:'Space UI redesign: Work page',project:'xo-space',status:'open',origin:'github',issue:issue('sharmasuraj0123/xo-space',132),
      assignee:'me',in_progress:true,claim:{runtime:'claude_code',session:'6bd451cd'},labels:[],body:null,created:ago(1440),updated:ago(12)},
    {id:'a41d',title:'Rotate Composio secrets after the leak report',project:'xo-swarm-api',status:'open',origin:'github',issue:issue('sharmasuraj0123/xo-swarm-api',44),
      assignee:null,in_progress:false,labels:['security'],body:null,created:ago(45),updated:ago(45)},
    {id:'d2b9',title:'Ship the swarm poll-token mint (pairs with #41)',project:'xo-swarm',status:'open',origin:'space',
      assignee:'codex',in_progress:true,claim:{runtime:'codex',session:'b31f'},labels:[],body:'Blocked on the Clerk key until ops rotates it.',created:ago(2600),updated:ago(30)},
    {id:'e5a3',title:'Write the attention derivation tests',project:'xo-space',status:'open',origin:'space',
      assignee:'claude_code',in_progress:false,labels:['tests'],body:'One test per reason in section 8 of the design, plus the dismissal key rule.',created:ago(600),updated:ago(300)},
    {id:'f610',title:'Onboarding copy pass for xo-main',project:'xo-coworker',status:'open',origin:'space',
      assignee:null,in_progress:false,labels:[],body:'The title and meta description still carry typos.',created:ago(2000),updated:ago(2000)},
    {id:'0b77',title:'Agents page: hero stats and Configure cards',project:'xo-space',status:'closed',origin:'github',issue:issue('sharmasuraj0123/xo-space',131),
      assignee:'me',in_progress:false,state_reason:'completed',labels:[],body:null,created:ago(4000),updated:ago(1400)},
    {id:'19c4',title:'Move inbox.json under ~/.quirq/inbox/',project:'xo-space',status:'closed',origin:'space',
      assignee:'claude_code',in_progress:false,state_reason:'completed',labels:[],body:'Done in #123.',created:ago(6000),updated:ago(4300)},
    {id:'2ad8',title:'Gemini CLI adapter spike',project:'xo-space',status:'closed',origin:'github',issue:issue('sharmasuraj0123/xo-space',70),
      assignee:null,in_progress:false,state_reason:'not_planned',labels:[],body:null,created:ago(20000),updated:ago(9000)},
  ];
  return _workitems;
}

let _attention=null;
export function attention(){
  if(_attention)return _attention;
  _attention=[
    {key:'workitem:xo-space:8f2c',reason:'assigned_to_me',title:'Fix parser timeout on large transcripts',project:'xo-space',since:ago(120),ref:{workitem:'8f2c'},
      detail:'Assigned to you by Claude Code. Nobody is on it yet; Claim opens the project so a session can start.'},
    {key:'workitem:xo-swarm-api:a41d',reason:'unassigned',title:'Rotate Composio secrets after the leak report',project:'xo-swarm-api',since:ago(45),ref:{workitem:'a41d'},
      detail:'Opened as issue #44 by Sagar Rambade. Give it an owner, you or an agent, or it stays here.'},
    {key:'todo:xo-swarm:12',reason:'todo_blocked',title:'Deploy swarm #41: waiting on the Clerk key',project:'xo-swarm',since:ago(200),ref:{},
      detail:'Codex marked this todo blocked 3 hours ago: the Clerk key has to be rotated by ops before the deploy.'},
    {key:'connection:gmail:message:18c2',reason:'connection',title:'Invoice question from Sagar Rambade',project:null,source:'gmail',since:ago(35),
      ref:{url:'https://mail.google.com/mail/u/0/#inbox/18c2'},detail:'The June invoice shows 12 seats but we agreed on 10. Can you check before I forward it to finance?'},
    {key:'sharing:incoming:quirq-ai/xo-docs',reason:'share_pending',title:'quirq-ai/xo-docs shared with this Space',project:null,source:'sharing',since:ago(1440),ref:{repo:'quirq-ai/xo-docs'},
      detail:'Shared by ws_a7c1. Clone it into the projects root to start receiving its commits.'},
    {key:'sharing:behind:xo-swarm',reason:'commits_behind',title:'3 commits to apply on xo-swarm',project:'xo-swarm',source:'sharing',since:ago(58),ref:{repo:'sharmasuraj0123/xo-swarm'},
      detail:'origin/main is 3 ahead of your checkout. Apply fast-forwards it; nothing merges by hand.'},
    {key:'source:gmail',reason:'source_error',title:'Gmail: connection expired at Composio',project:null,source:'gmail',since:ago(1300),ref:{view:'setup/connectors'},
      detail:'Every poll since yesterday answered "no active connection". Only a reconnect from Setup fixes it.'},
  ];
  return _attention;
}

const E=(min,source,kind,title,extra={})=>({key:source+':'+kind+':'+min,ts:ago(min),source,kind,title,detail:'',project:null,actor:null,ref:{},tone:'info',...extra});
const files=(start,n,project,runtime)=>Array.from({length:n},(_,i)=>E(start+i,'timeline','file.edited',
  ['space_ui/js/views/work.js','space_ui/css/work.css','space_ui/js/core/navigation.js','docs/work-and-workitems.md','space_ui/js/views/work-work.js',
   'space_ui/js/views/work-sources.js','space_ui/js/core/shadcn.js','space_ui/css/shadcn.css','space_ui/index.html','space_ui/js/app.js',
   'tests/test_space_navigation.py','space_ui/js/core/command-palette.js','space_ui/js/views/wiki.js','space_ui/js/views/sharing.js'][i%14],
  {project,actor:{runtime,session:'6bd451cd'}}));

export function entries(){
  return[
    E(3,'timeline','workitem.claimed','Space UI redesign: Work page',{project:'xo-space',actor:{runtime:'claude_code',session:'6bd451cd'},ref:{workitem:'c7e0'}}),
    E(9,'connections','googlecalendar.event','Design sync with Sagar',{detail:'Today 15:00 to 15:45 · Google Meet',ref:{url:'https://calendar.google.com/event?eid=abc'},toolkit:'googlecalendar',starts:ahead(110)}),
    E(12,'timeline','session.started','Session started in xo-space',{project:'xo-space',actor:{runtime:'claude_code',session:'6bd451cd'}}),
    ...files(14,14,'xo-space','claude_code'),
    E(35,'connections','gmail.message','Invoice question from Sagar Rambade',{detail:'Hi Suraj, the June invoice shows 12 seats but we agreed on 10. Can you check before I forward it to finance?',
      ref:{url:'https://mail.google.com/mail/u/0/#inbox/18c2'},toolkit:'gmail',tone:'attention',key:'connection:gmail:message:18c2'}),
    E(41,'timeline','todo.completed','Read the Inbox implementation and the work item store',{project:'xo-space',actor:{runtime:'claude_code',session:'6bd451cd'}}),
    E(45,'issues','issue.opened','#44 Rotate Composio secrets after the leak report',{project:'xo-swarm-api',detail:'labels: security · opened by sagar-rambade',ref:{issue:issue('sharmasuraj0123/xo-swarm-api',44)}}),
    E(58,'sharing','sharing.fetched','3 new commits on sharmasuraj0123/xo-swarm',{project:'xo-swarm',detail:'origin/main is ahead by 3. Apply from Sharing.'}),
    E(75,'jobs','job.finished','nightly backup',{detail:'ok · 4.1 s · 212 files to xo-backups',ref:{job:'nightly-backup'}}),
    E(96,'posts','agent.note','Parser fix needs a decision: cap transcripts at 40 MB or stream them?',{project:'xo-space',actor:{runtime:'claude_code',session:'6bd451cd'},
      detail:'Capping is a 20-line change and drops the tail of huge sessions. Streaming keeps everything and touches the watcher loop. I lean streaming; say which and I continue.',ref:{workitem:'8f2c'}}),
    E(120,'timeline','workitem.assigned','Fix parser timeout on large transcripts',{project:'xo-space',detail:'assigned to you',ref:{workitem:'8f2c'}}),
    E(200,'timeline','todo.status_changed','Deploy swarm #41: waiting on the Clerk key',{project:'xo-swarm',actor:{runtime:'codex',session:'b31f'},detail:'blocked',tone:'attention',key:'todo:xo-swarm:12'}),
    E(230,'timeline','workitem.claimed','Ship the swarm poll-token mint (pairs with #41)',{project:'xo-swarm',actor:{runtime:'codex',session:'b31f'},ref:{workitem:'d2b9'}}),
    E(260,'jobs','job.failed','lint sweep',{detail:'exit 1 · ruff found 3 errors in services/connections/poller.py',tone:'error',ref:{job:'lint-sweep'}}),
    E(1400,'timeline','workitem.closed','Agents page: hero stats and Configure cards',{project:'xo-space',actor:{runtime:'claude_code',session:'0be16ecb'},detail:'completed',ref:{workitem:'0b77'}}),
    E(1440,'sharing','sharing.shared_with_you','quirq-ai/xo-docs shared with this Space',{detail:'Clone it from Sharing to start receiving commits.'}),
    E(1500,'timeline','workitem.created','Fix parser timeout on large transcripts',{project:'xo-space',ref:{workitem:'8f2c'}}),
    E(1520,'timeline','session.started','Session started in xo-swarm-api',{project:'xo-swarm-api',actor:{runtime:'codex',session:'b31f'}}),
    E(1600,'connections','googlecalendar.event','Investor update call',{detail:'Yesterday 17:00 to 17:30',toolkit:'googlecalendar',ref:{url:'https://calendar.google.com/event?eid=def'}}),
    E(1700,'timeline','peer.sync.applied','12 files applied from dev@xo.builders',{project:'xo-swarm'}),
    E(1900,'connections','slack.mention','#eng: @suraj can you look at the swarm 422s?',{detail:'Sagar Rambade in #eng',toolkit:'slack',tone:'attention',ref:{url:'https://xolabs.slack.com/archives/C01/p1'}}),
    E(2900,'timeline','project.created','xo-coworker',{project:'xo-coworker'}),
    E(3000,'issues','issue.closed','#131 Agents page: hero stats and Configure cards',{project:'xo-space',detail:'completed',ref:{issue:issue('sharmasuraj0123/xo-space',131)}}),
    E(3100,'jobs','job.finished','telemetry rebuild',{detail:'ok · 12.8 s',ref:{job:'telemetry-rebuild'}}),
  ];
}

export function olderEntries(){
  return[
    E(4300,'timeline','workitem.closed','Move inbox.json under ~/.quirq/inbox/',{project:'xo-space',actor:{runtime:'claude_code',session:'a9cd1ddc'},detail:'completed',ref:{workitem:'19c4'}}),
    E(4400,'timeline','file.created','services/storage/layout.py',{project:'xo-space',actor:{runtime:'claude_code',session:'a9cd1ddc'}}),
    E(5000,'timeline','session.started','Session started in xo-space',{project:'xo-space',actor:{runtime:'claude_code',session:'a9cd1ddc'}}),
    E(6100,'sharing','sharing.error','Sync failed for sharmasuraj0123/xo-swarm',{project:'xo-swarm',detail:'fetch: could not resolve host',tone:'error'}),
    E(9000,'issues','issue.closed','#70 Gemini CLI adapter spike',{project:'xo-space',detail:'not planned',ref:{issue:issue('sharmasuraj0123/xo-space',70)}}),
  ];
}

/* The person's marks, the part of work.json the pages write (design section
   10): shared, so a Track on Activity shows in the Inbox at once. */
export const marks={dismissed:new Set(),acked:new Set(),pinned:new Set(),tracked:new Map()};

/* What is running now: the watcher's presence snapshot and heartbeat, and
   the connection pollers' state. */
export function sessions(){
  return[
    {id:'6bd451cd',agent:'claude_code',project:'xo-space',opened:ago(12),last:ago(1),turns:42,working:'Build the Work pages on sample data'},
    {id:'b31f',agent:'codex',project:'xo-swarm',opened:ago(230),last:ago(30),turns:18,working:'Ship the swarm poll-token mint (pairs with #41)'},
  ];
}
export function watcher(){
  return{last_tick:ago(0.2),interval_s:1,sessions_indexed:148,projects:4,files_today:31};
}
export function connections(){
  return[
    {toolkit:'gmail',name:'Gmail',account:'dev@xo.builders',interval_s:900,enabled:true,last_poll:ago(1300),next_poll:null,error:'Connection expired at Composio. Reconnect from Setup.'},
    {toolkit:'googlecalendar',name:'Google Calendar',account:'dev@xo.builders',interval_s:900,enabled:true,last_poll:ago(4),next_poll:ahead(11),error:null},
    {toolkit:'slack',name:'Slack',account:'XO Labs',interval_s:300,enabled:true,last_poll:ago(2),next_poll:ahead(3),error:null},
  ];
}

/* The calendar: events the calendar connection collected, past and
   upcoming, each with the toolkit that produced it. */
export function calendarEvents(){
  const at=(day,h,mi,minutes)=>{const d=new Date();d.setHours(h,mi,0,0);d.setDate(d.getDate()+day);return[d.toISOString(),new Date(d.getTime()+minutes*60000).toISOString()];};
  const ev=(id,title,[starts,ends],detail)=>({key:'connection:googlecalendar:event:'+id,source:'connections',toolkit:'googlecalendar',kind:'calendar.event',
    title,detail,starts,ends,ref:{url:'https://calendar.google.com/event?eid='+id}});
  return[
    ev('pqr','Board prep',at(-3,14,0,90),'Docs · draft the deck'),
    ev('def','Investor update call',at(-1,17,0,30),'Zoom'),
    ev('abc','Design sync with Sagar',at(0,15,0,45),'Google Meet · Sagar Rambade'),
    ev('stu','Review the Work pages',at(0,18,30,30),'with Suraj'),
    ev('ghi','1:1 with Sagar',at(1,10,0,30),'Google Meet'),
    ev('jkl','Sprint review',at(2,11,0,60),'Room 2 · team'),
    ev('mno','Investor office hours',at(5,16,0,60),'Zoom · optional'),
  ];
}

/* Jobs: the scheduler's definitions, state and run history. */
export function jobs(){
  return{
    jobs:[
      {id:'nightly-backup',name:'nightly backup',every_s:86400,enabled:true,next_run:ahead(600),running:false,last:{status:'ok',finished:ago(75),seconds:4.1}},
      {id:'telemetry-rebuild',name:'telemetry rebuild',every_s:3600,enabled:true,next_run:ahead(59),running:true,running_since:ago(1),last:{status:'ok',finished:ago(61),seconds:12.8}},
      {id:'lint-sweep',name:'lint sweep',every_s:21600,enabled:true,next_run:ahead(100),running:false,last:{status:'error',finished:ago(260),seconds:9.4}},
      {id:'docs-snapshot',name:'docs snapshot',every_s:86400,enabled:false,next_run:null,running:false,last:{status:'ok',finished:ago(2900),seconds:2.2}},
    ],
    runs:[
      {job:'telemetry rebuild',status:'running',started:ago(1),seconds:null,exit:null},
      {job:'telemetry rebuild',status:'ok',started:ago(61),seconds:12.8,exit:0},
      {job:'nightly backup',status:'ok',started:ago(75),seconds:4.1,exit:0,note:'212 files to xo-backups'},
      {job:'telemetry rebuild',status:'ok',started:ago(121),seconds:11.9,exit:0},
      {job:'telemetry rebuild',status:'ok',started:ago(181),seconds:12.1,exit:0},
      {job:'lint sweep',status:'error',started:ago(260),seconds:9.4,exit:1,note:'ruff found 3 errors in services/connections/poller.py'},
      {job:'lint sweep',status:'ok',started:ago(620),seconds:8.7,exit:0},
      {job:'nightly backup',status:'ok',started:ago(1515),seconds:4.4,exit:0,note:'209 files to xo-backups'},
    ],
  };
}

/* Project sharing: what the relay knows (the former Sharing page's data). */
export function sharing(){
  return{
    incoming:[{repo:'quirq-ai/xo-docs',from:'ws_a7c1',state:'not_cloned',at:ago(1440)}],
    mine:[
      {project:'xo-swarm',repo:'sharmasuraj0123/xo-swarm',branch:'main',behind:3,checked:ago(2),
        members:[{id:'ws_8f31',owner:true},{id:'ws_c2a9',owner:false}]},
      {project:'xo-space',repo:'sharmasuraj0123/xo-space',branch:'development',behind:0,checked:ago(2),
        members:[{id:'ws_8f31',owner:true},{id:'ws_11de',owner:false},{id:'ws_c2a9',owner:false}]},
    ],
    me:'ws_8f31',
  };
}

/* The live stream: one line per thing that happens, as the logs would
   report it. `source` picks the filter and the label; a line with
   level "error" reads in the error color. */
export const LOG_POOL=[
  {source:'watcher',group:'watcher',text:'tick · 148 sessions indexed · 4 projects',weight:6},
  {source:'claude_code',group:'agents',text:'xo-space · edited space_ui/js/views/work-live.js',weight:5},
  {source:'claude_code',group:'agents',text:'xo-space · Read services/inbox/store.py',weight:4},
  {source:'claude_code',group:'agents',text:'xo-space · todo done · Port the calendar into the kit',weight:2},
  {source:'claude_code',group:'agents',text:'xo-space · Bash · venv/bin/python -m unittest tests.test_space_feed',weight:3},
  {source:'codex',group:'agents',text:'xo-swarm · edited app/api/mint/route.ts',weight:4},
  {source:'codex',group:'agents',text:'xo-swarm · Bash · pnpm test',weight:3},
  {source:'codex',group:'agents',text:'xo-swarm · todo blocked · Deploy swarm #41: waiting on the Clerk key',weight:1},
  {source:'scheduler',group:'jobs',text:'telemetry rebuild · finished ok · 12.8 s',weight:2},
  {source:'scheduler',group:'jobs',text:'telemetry rebuild · started',weight:2},
  {source:'scheduler',group:'jobs',text:'lint sweep · failed · exit 1 · ruff found 3 errors',weight:1,level:'error'},
  {source:'googlecalendar',group:'pollers',text:'polled · 0 new · next in 15 min',weight:2},
  {source:'slack',group:'pollers',text:'polled · 1 new · #eng mention from Sagar Rambade',weight:2},
  {source:'gmail',group:'pollers',text:'poll failed · connection expired at Composio',weight:1,level:'error'},
  {source:'relay',group:'pollers',text:'xo-swarm · fetched origin/main · 3 ahead',weight:1},
  {source:'relay',group:'pollers',text:'xo-space · origin/development · in sync',weight:1},
];

/* Sixty days of history for the charts, seeded so every paint agrees.
   Weekdays are busier than weekends, mornings busier than nights, and each
   project has its own agent and pace. */
export function history(){
  let seed=20260916;
  const rnd=()=>{seed=(seed*1664525+1013904223)%4294967296;return seed/4294967296;};
  const pickOne=list=>list[Math.floor(rnd()*list.length)];
  const KINDS=[['file.edited',26],['todo.completed',9],['todo.status_changed',3],['session.started',6],['workitem.claimed',3],['workitem.closed',3],
    ['workitem.created',2],['issue.opened',3],['issue.closed',2],['gmail.message',6],['slack.mention',4],['googlecalendar.event',3],
    ['job.finished',8],['job.failed',1],['sharing.fetched',3],['peer.sync.applied',1],['agent.note',2]];
  const total=KINDS.reduce((n,[,w])=>n+w,0);
  const kindOf=()=>{let r=rnd()*total;for(const [k,w] of KINDS){r-=w;if(r<=0)return k;}return KINDS[0][0];};
  const AGENT_OF={'xo-space':'claude_code','xo-swarm':'codex','xo-swarm-api':'codex','xo-coworker':'claude_code'};
  const FILES=['services/inbox/store.py','space_ui/js/views/work.js','routers/cowork_agent/bff/inbox.py','tests/test_space_feed.py','app/api/mint/route.ts','docs/work-and-workitems.md','services/connections/poller.py','space_ui/css/work.css'];
  const out=[];
  const now=new Date();
  for(let day=2;day<60;day++){
    const d=new Date(now);d.setDate(now.getDate()-day);
    const weekend=d.getDay()===0||d.getDay()===6;
    const count=Math.round((weekend?3:9)+rnd()*(weekend?4:10));
    for(let i=0;i<count;i++){
      const kind=kindOf(),hour=Math.floor(8+rnd()*12),minute=Math.floor(rnd()*60);
      const ts=new Date(d.getFullYear(),d.getMonth(),d.getDate(),hour,minute,Math.floor(rnd()*60)).toISOString();
      const project=kind.startsWith('gmail')||kind.startsWith('slack')||kind.startsWith('googlecalendar')||kind.startsWith('job.')?null:pickOne(PROJECTS).id;
      const agent=project?AGENT_OF[project]:null;
      const e={key:'hist:'+kind+':'+ts,ts,kind,project,actor:null,detail:'',ref:{},tone:'info',
        source:kind.startsWith('workitem.')||kind.startsWith('session.')||kind.startsWith('todo.')||kind.startsWith('file.')||kind.startsWith('peer.')?'timeline'
          :kind.startsWith('issue.')?'issues':kind.startsWith('job.')?'jobs':kind.startsWith('sharing.')?'sharing':kind==='agent.note'?'posts':'connections'};
      if(kind==='file.edited'){e.title=pickOne(FILES);e.actor={runtime:agent,session:'s'+day};}
      else if(kind==='todo.completed'){e.title=pickOne(['Write the tests','Read the store','Port the component','Fix the lint errors','Update the docs']);e.actor={runtime:agent,session:'s'+day};}
      else if(kind==='todo.status_changed'){e.title=pickOne(['Waiting on a key','Waiting on review','Blocked on the API']);e.detail='blocked';e.tone='attention';e.actor={runtime:agent,session:'s'+day};}
      else if(kind==='session.started'){e.title='Session started in '+project;e.actor={runtime:agent,session:'s'+day};}
      else if(kind.startsWith('workitem.')){e.title=pickOne(['Parser timeout','Secrets rotation','Onboarding copy','Work pages','Attention tests','Mint endpoint']);e.actor=kind==='workitem.created'?null:{runtime:agent,session:'s'+day};e.ref={workitem:'h'+day+i};if(kind==='workitem.closed')e.detail='completed';}
      else if(kind.startsWith('issue.')){e.title='#'+(20+day)+' '+pickOne(['Flaky watcher tick','Sharing drawer polish','Wiki column width','Restart button']);}
      else if(kind==='gmail.message'){e.title=pickOne(['Invoice question','Contract draft','Intro: XO x Acme','Weekly digest']);e.toolkit='gmail';}
      else if(kind==='slack.mention'){e.title='#eng: '+pickOne(['can you look at the 422s?','review when free','deploy went out','standup moved']);e.toolkit='slack';}
      else if(kind==='googlecalendar.event'){e.title=pickOne(['Design sync','Investor call','1:1','Sprint review']);e.toolkit='googlecalendar';}
      else if(kind==='job.finished'){e.title=pickOne(['nightly backup','telemetry rebuild','lint sweep']);e.detail='ok';}
      else if(kind==='job.failed'){e.title=pickOne(['lint sweep','nightly backup']);e.detail='exit 1';e.tone='error';}
      else if(kind==='sharing.fetched'){e.title='commits fetched on '+project;}
      else if(kind==='peer.sync.applied'){e.title='files applied from a peer';}
      else if(kind==='agent.note'){e.title=pickOne(['Two ways to fix this; which?','Done, ready for review','Found a second bug on the way']);e.actor={runtime:agent,session:'s'+day};}
      out.push(e);
    }
  }
  return out.sort((a,b)=>Date.parse(b.ts)-Date.parse(a.ts));
}
