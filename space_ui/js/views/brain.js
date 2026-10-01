/* Brain: the knowledge brain's page (services/brain, /api/brain/*).
   Four panels behind one pill strip:
     Recall       ask a cue in a context; every answer shows its score, the
                  note for that context, its evidence and the path that
                  found it. Guesses (hypotheses) are listed apart, only when
                  exploring. Ticked answers can be marked used together.
     Sources      which projects the brain learns from, and learning them.
     Discoveries  open findings, recurring patterns, analogies, hypotheses.
     Create       a goal becomes ranked designs; nothing is built until a
                  person approves one. Experiences record what came of it.
   A piece opens in a side panel with its notes per source, evidence, links
   and similar pieces. Evidence opens the file in the Data previewer. */
import {apiFetch,failText} from '../core/api.js';
import {esc,rel,pills,toast} from '../core/ui.js';
import {mdToHtml} from '../core/markdown.js';
import {projectPage} from '../core/navigation.js?v=20260930-brain1';

const PANELS=[['recall','Recall'],['sources','Sources'],['discoveries','Discoveries'],['create','Create']];
const RESULTS=[['worked','Worked'],['partial','Partial'],['failed','Failed']];
let root=null,panel='recall',status=null,sources=[],projects=[],poll=null,switchView=null;
let lastRecall=null,chosen=new Set();

const num=(v,d=2)=>Number.isFinite(v)?Number(v).toFixed(d):'';
const pct=v=>Math.round(Math.max(0,Math.min(1,v||0))*100);
const api=(path,opts)=>apiFetch('/api/brain'+path,opts);
const sourceName=id=>sources.find(s=>s.id===id)?.name||id||'';

function sourceOptions(selected=''){
  return '<option value="">All sources</option>'+sources.map(s=>'<option value="'+esc(s.id)+'"'
    +(s.id===selected?' selected':'')+'>'+esc(s.name)+'</option>').join('');
}

function shell(){
  return '<div class="brn">'
    +'<div class="brn-head"><span class="brn-eyebrow">Knowledge brain</span>'
    +'<span class="brn-sum" data-sum></span><span class="brn-spacer"></span>'
    +pills(PANELS,panel,'brn-panel','Brain panels','brn-filter')+'</div>'
    +'<p class="brn-model" data-model></p>'
    +'<section class="brn-panel" data-panel="recall">'
      +'<form class="brn-ask" data-recall>'
        +'<input class="brn-input" name="cue" maxlength="2000" autocomplete="off" '
          +'placeholder="Ask: a question, a word, a need" aria-label="Cue">'
        +'<select class="brn-select" name="context" aria-label="Context" data-context></select>'
        +'<label class="brn-check" data-answer-option hidden><input type="checkbox" name="answer" checked> Write an answer</label>'
        +'<label class="brn-check"><input type="checkbox" name="explore"> Include guesses</label>'
        +'<button class="brn-btn is-primary" type="submit">Recall</button>'
      +'</form>'
      +'<div class="brn-usebar" data-usebar hidden><span data-usecount></span>'
        +'<button class="brn-btn" type="button" data-use>Mark used together</button></div>'
      +'<div data-recall-out><p class="brn-empty">Recall finds pieces by meaning, then follows links to what the cue never mentioned.</p></div>'
    +'</section>'
    +'<section class="brn-panel" data-panel="sources" hidden>'
      +'<div class="brn-row-actions"><button class="brn-btn" type="button" data-learn-all>Learn all again</button></div>'
      +'<div data-sources-out></div></section>'
    +'<section class="brn-panel" data-panel="discoveries" hidden>'
      +'<div class="brn-row-actions"><button class="brn-btn" type="button" data-discover>Run discovery</button></div>'
      +'<div data-discoveries-out></div></section>'
    +'<section class="brn-panel" data-panel="create" hidden>'
      +'<form class="brn-goal" data-goal>'
        +'<textarea class="brn-input" name="goal" rows="2" maxlength="2000" placeholder="A goal: what should be built?" aria-label="Goal"></textarea>'
        +'<select class="brn-select" name="context" aria-label="Context" data-context></select>'
        +'<button class="brn-btn is-primary" type="submit">Propose designs</button>'
      +'</form>'
      +'<p class="brn-hint" data-create-hint></p>'
      +'<div data-goals-out></div>'
      +'<h3 class="brn-h3">Experiences</h3>'
      +'<form class="brn-exp" data-exp>'
        +'<input class="brn-input" name="goal" maxlength="1000" placeholder="What was attempted" aria-label="Experience goal">'
        +'<select class="brn-select" name="result" aria-label="Result">'+RESULTS.map(([k,l])=>'<option value="'+k+'">'+l+'</option>').join('')+'</select>'
        +'<input class="brn-input" name="lessons" maxlength="2000" placeholder="Lesson (optional)" aria-label="Lesson">'
        +'<button class="brn-btn" type="submit">Record</button>'
      +'</form>'
      +'<div data-exp-out></div>'
    +'</section>'
    +'<aside class="brn-drawer" data-drawer hidden aria-label="Piece"></aside>'
  +'</div>';
}

/* ── status and sources ─────────────────────────────────────────────── */
async function loadStatus(){
  const [st,src]=await Promise.all([api('/status'),api('/sources')]);
  if(!st.ok){root.querySelector('[data-sum]').textContent=failText(st);return;}
  status=st.data;
  if(src.ok){sources=src.data.sources;projects=src.data.projects;}
  const c=status.counts;
  root.querySelector('[data-sum]').textContent=c.pieces+' pieces · '+c.shared_pieces+' shared · '
    +c.links+' links · '+c.patterns+' patterns · '+c.open_findings+' open findings';
  const m=status.model;
  root.querySelector('[data-model]').innerHTML=m.error
    ?'Model <b>'+esc(m.name)+'</b> did not load: '+esc(m.error)+'. Running on statistics only.'
    :m.reason?'Model <b>'+esc(m.name)+'</b>'+(m.agent&&m.name==='agent'?' ('+esc(m.agent)+')':'')+' connected'+(m.embed?' with embeddings':'')+'. Background learning '+(status.loop.enabled?'on':'off')+'.'
    :'No model connected: learning and recall run on statistics; designs need <code>BRAIN_MODEL</code>. Background learning '+(status.loop.enabled?'on':'off')+'.';
  for(const sel of root.querySelectorAll('[data-context]')){const v=sel.value;sel.innerHTML=sourceOptions(v);}
  const hint=root.querySelector('[data-create-hint]');
  hint.textContent=m.reason?'':'Designing needs a model. Set BRAIN_MODEL=agent (or your own module) in .env and restart Space.';
  root.querySelector('[data-goal] button').disabled=!m.reason;
  root.querySelector('[data-answer-option]').hidden=!m.reason;
  const busy=(status.learning||[]).length||(status.building||[]).length;
  if(busy&&!poll)poll=setInterval(()=>{refreshPanel();},2500);
  if(!busy&&poll){clearInterval(poll);poll=null;}
}

function renderSources(){
  const out=root.querySelector('[data-sources-out]');
  const bySource=new Map(sources.map(s=>[s.id,s]));
  const rows=projects.map(p=>{
    const s=p.source_id?bySource.get(p.source_id):null;
    const st=s?.stats||{};
    const state=s?(s.learning?'learning…':s.status==='error'?'error':s.status):'not learned';
    return '<tr><td class="brn-name">'+esc(p.display_name)+'</td>'
      +'<td><span class="brn-chip'+(s?.status==='error'?' is-bad':s?' is-good':'')+'" title="'+esc(s?.error||'')+'">'+esc(state)+'</span></td>'
      +'<td class="brn-numcell">'+(s?(st.files??0):'')+'</td><td class="brn-numcell">'+(s?(st.chunks??0):'')+'</td>'
      +'<td class="brn-numcell">'+(s?(st.pieces??0):'')+'</td><td class="brn-numcell">'+(s?(st.shared??0):'')+'</td>'
      +'<td class="brn-when">'+(s?.learned_at?esc(rel(s.learned_at)):'')+'</td><td class="brn-actions">'
      +(s?'<button class="brn-btn" data-relearn="'+esc(s.id)+'"'+(s.learning?' disabled':'')+'>Learn again</button>'
         +'<button class="brn-btn is-danger" data-forget="'+esc(s.id)+'"'+(s.learning?' disabled':'')+'>Forget</button>'
       :p.pid?'<button class="brn-btn" data-add="'+esc(p.project)+'">Learn</button>':'<span class="brn-muted">no project id yet</span>')
      +'</td></tr>';
  }).join('');
  out.innerHTML=projects.length?'<table class="brn-table"><thead><tr><th>Project</th><th>State</th><th>Files</th>'
    +'<th>Chunks</th><th>Pieces</th><th>Shared</th><th>Learned</th><th></th></tr></thead><tbody>'+rows+'</tbody></table>'
    :'<p class="brn-empty">No projects in this workspace yet.</p>';
}

async function addSource(project){
  const r=await api('/sources',{method:'POST',body:{project}});
  if(!r.ok){toast(failText(r));return;}
  await learn(r.data.id);
}
async function learn(sourceId,force=false){
  const r=await api('/learn',{method:'POST',body:sourceId?{source_id:sourceId,force}:{force}});
  if(!r.ok){toast(failText(r));return;}
  toast(r.data.started.length?'Learning '+r.data.started.map(sourceName).join(', '):'Already learning');
  await loadStatus();renderSources();
}
async function forget(sourceId){
  if(!confirm('Forget '+sourceName(sourceId)+'? Pieces only it supported are removed; shared pieces keep the other notes.'))return;
  const r=await api('/sources/'+encodeURIComponent(sourceId),{method:'DELETE'});
  if(!r.ok){toast(failText(r));return;}
  toast('Forgot '+sourceName(sourceId));await loadStatus();renderSources();
}

/* ── recall ─────────────────────────────────────────────────────────── */
function evidenceList(items){
  if(!items?.length)return'';
  return '<ul class="brn-evidence">'+items.map(e=>'<li><button type="button" class="brn-link" data-open-project="'
    +esc(e.source||'')+'" data-open-path="'+esc(e.path)+'">'+esc((e.source||e.source_id)+'/'+e.path+':'+e.start_line+'-'+e.end_line)
    +'</button>'+(e.quote?' <span class="brn-quote">'+esc(e.quote)+'</span>':'')+'</li>').join('')+'</ul>';
}

function resultCard(r,guess){
  const p=r.piece,sourcesUsed=r.notes||[];
  const note=r.note?.note?'<p class="brn-note"><b>'+esc(sourceName(r.note.source_id))+'</b>: '+esc(r.note.note)+'</p>'
    :sourcesUsed.slice(0,2).map(n=>n.note?'<p class="brn-note"><b>'+esc(n.source)+'</b>: '+esc(n.note)+'</p>':'').join('');
  return '<li class="brn-card'+(guess?' is-guess':'')+'">'
    +'<div class="brn-card-head">'
      +(guess?'':'<input type="checkbox" class="brn-tick" data-tick="'+p.id+'"'+(chosen.has(p.id)?' checked':'')+' aria-label="Choose '+esc(p.name)+'">')
      +'<button type="button" class="brn-piece" data-piece="'+p.id+'">'+esc(p.name)+'</button>'
      +(guess?'<span class="brn-chip is-guess">guess</span>':'')
      +(sourcesUsed.length>1?'<span class="brn-chip">shared · '+sourcesUsed.length+' sources</span>':'')
      +(p.novel?'<span class="brn-chip is-accent">novel</span>':'')
      +'<span class="brn-spacer"></span><span class="brn-meter" title="score '+num(r.score,3)+'"><span style="width:'+pct(r.score)+'%"></span></span>'
      +'<span class="brn-num">'+num(r.score,2)+'</span></div>'
    +(p.description?'<p class="brn-desc">'+esc(p.description)+'</p>':'')+note
    +'<p class="brn-path">'+esc(r.explanation)+'</p>'+evidenceList(r.evidence)+'</li>';
}

/* The model's answer, its [n] references turned into links to the cited
   lines. mdToHtml escapes before it formats, so the only markup added here
   is the reference buttons, built from the server's evidence list. */
function answerCard(a){
  if(a.error)return '<p class="brn-error">Could not write an answer ('+esc(a.model)+'): '+esc(a.error)+'</p>';
  const byN=new Map((a.evidence||[]).map(e=>[e.n,e]));
  const ref=e=>'<button type="button" class="brn-cite" data-open-project="'+esc(e.source)+'" data-open-path="'+esc(e.path)
    +'" title="'+esc(e.source+'/'+e.path+':'+e.start_line+'-'+e.end_line)+'">'+e.n+'</button>';
  const html=mdToHtml(a.text).replace(/\[(\d{1,3})\]/g,(m,n)=>byN.has(Number(n))?ref(byN.get(Number(n))):m);
  const cited=a.citations||[];
  return '<section class="brn-answer"><div class="brn-answer-head"><span class="brn-eyebrow">Answer</span>'
    +'<span class="brn-chip">'+esc(a.model)+'</span><span class="brn-muted">from what the brain recalled</span></div>'
    +'<div class="brn-md">'+html+'</div>'
    +(a.missing?'<p class="brn-missing"><b>Not covered by what the brain knows:</b> '+esc(a.missing)+'</p>':'')
    +(cited.length?'<ol class="brn-cites">'+cited.map(e=>'<li value="'+e.n+'"><button type="button" class="brn-link" data-open-project="'
      +esc(e.source)+'" data-open-path="'+esc(e.path)+'">'+esc(e.source+'/'+e.path+':'+e.start_line+'-'+e.end_line)+'</button></li>').join('')+'</ol>':'')
    +'</section>';
}

function renderRecall(){
  const out=root.querySelector('[data-recall-out]');
  const r=lastRecall;
  if(!r)return;
  const ctx=r.context_detected?'<p class="brn-muted brn-ctx">Context: <b>'+esc(r.context_name)+'</b>, named in your question'
    +(r.about_source?'; answering about the project as a whole':'')+'.</p>':'';
  const gap=r.gap?'<p class="brn-gap">Nothing known matches this well (best match '+num(r.best_match)+'). It is recorded as missing knowledge.</p>':'';
  const answer=r.answer?answerCard(r.answer):'';
  const facts=r.results.length?'<ol class="brn-results">'+r.results.map(x=>resultCard(x,false)).join('')+'</ol>':'';
  const guesses=r.hypotheses.length?'<h3 class="brn-h3">Guesses <span class="brn-muted">reached through hypothesis links, not evidence</span></h3>'
    +'<ol class="brn-results">'+r.hypotheses.map(x=>resultCard(x,true)).join('')+'</ol>':'';
  const found=facts||guesses?'<details class="brn-found"'+(r.answer&&!r.answer.error?'':' open')+'><summary>How the brain found this: '
    +r.results.length+' pieces'+(r.hypotheses.length?', '+r.hypotheses.length+' guesses':'')+'</summary>'+facts+guesses+'</details>':'';
  out.innerHTML=ctx+gap+answer+found||'<p class="brn-empty">No answers.</p>';
  updateUsebar();
}

function updateUsebar(){
  const bar=root.querySelector('[data-usebar]');
  bar.hidden=chosen.size<2;
  root.querySelector('[data-usecount]').textContent=chosen.size+' chosen';
}

async function doRecall(form){
  const cue=form.cue.value.trim();
  if(!cue)return;
  const writes=!!(status?.model?.reason&&form.answer.checked);
  const body={cue,explore:form.explore.checked,answer:writes};
  if(form.context.value)body.context_source=form.context.value;
  const btn=form.querySelector('button[type=submit]');btn.disabled=true;
  root.querySelector('[data-recall-out]').innerHTML='<p class="brn-empty brn-thinking">'+(writes
    ?'Recalling, then '+esc(status.model.name)+' writes an answer from what was found. This can take a minute.'
    :'Recalling…')+'</p>';
  const r=await api('/recall',{method:'POST',body});
  btn.disabled=false;
  if(!r.ok){root.querySelector('[data-recall-out]').innerHTML='<p class="brn-error">'+esc(failText(r))+'</p>';return;}
  lastRecall=r.data;chosen=new Set();renderRecall();
}

async function useTogether(){
  const r=await api('/use',{method:'POST',body:{piece_ids:[...chosen]}});
  if(!r.ok){toast(failText(r));return;}
  toast('Linked '+r.data.pairs+' pairs');chosen=new Set();renderRecall();
}

/* ── piece drawer ───────────────────────────────────────────────────── */
function linkRow(l){
  const arrow=l.direction==='out'?esc(l.relation)+' →':'← '+esc(l.relation);
  return '<li><span class="brn-rel">'+arrow+'</span> <button type="button" class="brn-link" data-piece="'+l.other.id+'">'
    +esc(l.other.name)+'</button> <span class="brn-muted">'+esc(l.kind)+' · '+esc(l.signal)+' · '+num(l.weight)+'</span>'
    +(l.explanation?'<div class="brn-muted">'+esc(l.explanation)+'</div>':'')+'</li>';
}

async function openPiece(id){
  const drawer=root.querySelector('[data-drawer]');
  drawer.hidden=false;drawer.innerHTML='<p class="brn-empty">Loading…</p>';
  const [r,sim]=await Promise.all([api('/pieces/'+id),api('/pieces/'+id+'/similar?limit=6')]);
  if(!r.ok){drawer.innerHTML='<p class="brn-error">'+esc(failText(r))+'</p>';return;}
  const p=r.data;
  drawer.innerHTML='<div class="brn-drawer-head"><h3>'+esc(p.name)+'</h3><button type="button" class="brn-btn" data-close>Close</button></div>'
    +'<p class="brn-muted">level '+p.level+(p.keywords.length?' · '+esc(p.keywords.join(', ')):'')+(p.score?' · experience '+num(p.score,1):'')+'</p>'
    +(p.description?'<p class="brn-desc">'+esc(p.description)+'</p>':'')
    +'<h4>How each source uses it</h4><ul class="brn-notes">'+p.notes.map(n=>'<li><b>'+esc(n.source)+'</b> <span class="brn-muted">'
      +n.mentions+' mentions · strength '+num(n.strength)+(n.origin==='experience'?' · from a build':'')+'</span><div>'+esc(n.note)+'</div></li>').join('')+'</ul>'
    +'<h4>Evidence</h4>'+(evidenceList(p.evidence)||'<p class="brn-muted">none</p>')
    +'<h4>Links</h4>'+(p.links.length?'<ul class="brn-links">'+p.links.map(linkRow).join('')+'</ul>':'<p class="brn-muted">none</p>')
    +(p.hypotheses.length?'<h4>Guesses</h4><ul class="brn-links is-guess">'+p.hypotheses.map(linkRow).join('')+'</ul>':'')
    +(p.patterns.length?'<h4>Patterns</h4><p>'+p.patterns.map(x=>esc(x.name)).join(' · ')+'</p>':'')
    +(sim.ok&&sim.data.length?'<h4>Similar</h4><p>'+sim.data.map(s=>'<button type="button" class="brn-link" data-piece="'+s.id+'">'
      +esc(s.name)+'</button> <span class="brn-muted">'+num(s.similarity)+'</span>').join(' · ')+'</p>':'');
}

/* ── discoveries ────────────────────────────────────────────────────── */
async function renderDiscoveries(){
  const out=root.querySelector('[data-discoveries-out]');
  const [f,p,a,h]=await Promise.all([api('/findings?status=open'),api('/patterns'),api('/analogies'),api('/hypotheses?limit=50')]);
  if(!f.ok){out.innerHTML='<p class="brn-error">'+esc(failText(f))+'</p>';return;}
  const findings=f.data.length?'<ul class="brn-findings">'+f.data.map(x=>'<li><span class="brn-chip">'+esc(x.kind)+'</span> '
    +'<span class="brn-ftitle">'+esc(x.title)+'</span>'+(x.count>1?' <span class="brn-muted">×'+x.count+'</span>':'')
    +'<span class="brn-spacer"></span><span class="brn-when">'+esc(rel(x.updated_at))+'</span>'
    +'<button type="button" class="brn-btn" data-done="'+x.id+'">Done</button>'
    +(x.body?'<div class="brn-muted">'+esc(x.body)+'</div>':'')+'</li>').join('')+'</ul>':'<p class="brn-empty">Nothing waiting.</p>';
  const patterns=p.ok&&p.data.length?p.data.map(x=>'<div class="brn-card"><div class="brn-card-head"><b>'+esc(x.name)+'</b>'
    +'<span class="brn-chip">'+x.support+' sources</span>'+(x.score?'<span class="brn-chip">experience '+num(x.score,1)+'</span>':'')+'</div>'
    +(x.description?'<p class="brn-desc">'+esc(x.description)+'</p>':'')
    +Object.entries(x.members).map(([src,ms])=>'<p class="brn-note"><b>'+esc(src)+'</b>: '
      +ms.map(m=>'<button type="button" class="brn-link" data-piece="'+m.id+'">'+esc(m.name)+'</button>').join(', ')+'</p>').join('')+'</div>').join('')
    :'<p class="brn-empty">No recurring patterns yet: they need two or more sources.</p>';
  const analogies=a.ok&&a.data.length?a.data.map(x=>'<div class="brn-card"><div class="brn-card-head"><b>'+esc(x.to.name)+'</b>'
    +'<span class="brn-muted">could learn from</span><b>'+esc(x.from.name)+'</b><span class="brn-chip">'+esc(x.pattern.name)+'</span>'
    +'<span class="brn-chip">'+esc(x.explained_by)+'</span></div><p class="brn-desc">'+esc(x.explanation)+'</p>'
    +(x.suggestions.length?'<p class="brn-note">Suggests: '+x.suggestions.map(s=>'<button type="button" class="brn-link" data-piece="'
      +s.id+'">'+esc(s.name)+'</button>').join(', ')+'</p>':'')+'</div>').join('')
    :'<p class="brn-empty">No analogies yet.</p>';
  const hyps=h.ok&&h.data.length?'<ul class="brn-links is-guess">'+h.data.map(x=>'<li><span class="brn-chip is-guess">guess</span> '
    +'<button type="button" class="brn-link" data-piece="'+x.a.id+'">'+esc(x.a.name)+'</button> <span class="brn-rel">'+esc(x.relation)+'</span> '
    +'<button type="button" class="brn-link" data-piece="'+x.b.id+'">'+esc(x.b.name)+'</button> <span class="brn-muted">'+num(x.weight)
    +' · '+esc(x.explanation)+'</span></li>').join('')+'</ul>':'<p class="brn-empty">No hypotheses.</p>';
  out.innerHTML='<h3 class="brn-h3">Findings</h3>'+findings+'<h3 class="brn-h3">Recurring patterns</h3>'+patterns
    +'<h3 class="brn-h3">Analogies</h3>'+analogies+'<h3 class="brn-h3">Hypotheses <span class="brn-muted">predicted links, never used as facts</span></h3>'+hyps;
}

async function runDiscovery(btn){
  btn.disabled=true;const r=await api('/discover',{method:'POST'});btn.disabled=false;
  if(!r.ok){toast(failText(r));return;}
  toast(r.data.patterns+' patterns · '+r.data.hypotheses_written+' hypotheses · '+r.data.analogies+' analogies');
  await loadStatus();await renderDiscoveries();
}

/* ── create ─────────────────────────────────────────────────────────── */
function designCard(d){
  const s=d.scores||{},plan=d.plan||{},b=d.build||{};
  const reuse=(plan.reuses||[]).map(r=>'<li>'+(r.piece_id?'<button type="button" class="brn-link" data-piece="'+r.piece_id+'">'+esc(r.name)+'</button>'
    :'<b>'+esc(r.name)+'</b> <span class="brn-chip">pattern</span>')+(r.guess?' <span class="brn-chip is-guess">guess</span>':'')
    +' <span class="brn-muted">'+r.sources+' sources</span>'+(r.how?' · '+esc(r.how):'')+'</li>').join('');
  const list=(title,items)=>items?.length?'<h4>'+title+'</h4><ul>'+items.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'';
  const actions=d.status==='proposed'?'<button type="button" class="brn-btn is-primary" data-approve="'+d.id+'">Approve</button>'
    :d.status==='approved'?'<button type="button" class="brn-btn is-primary" data-build="'+d.id+'"'+(d.building?' disabled':'')+'>Build</button>':'';
  const built=b.started_at?'<p class="brn-note">Build: <b>'+esc(d.status)+'</b>'+(b.project?' in <code>'+esc(b.project)+'</code>':'')
    +(b.outcome?' · '+esc(b.outcome):'')+(b.attempts?.length?' · '+b.attempts.length+' test runs':'')+(b.error?' · '+esc(b.error):'')+'</p>':'';
  return '<div class="brn-card brn-design is-'+esc(d.status)+'"><div class="brn-card-head"><span class="brn-rank">#'+d.rank+'</span><b>'+esc(d.title)+'</b>'
    +'<span class="brn-chip">'+esc(d.status)+'</span><span class="brn-spacer"></span>'
    +'<span class="brn-meter" title="total '+num(s.total,3)+'"><span style="width:'+pct(s.total)+'%"></span></span><span class="brn-num">'+num(s.total)+'</span></div>'
    +'<p class="brn-scores">reuse '+num(s.reuse)+' · experience '+num(s.experience)+' · risk '+num(s.risk)+' · fit '+num(s.fit)+'</p>'
    +'<p class="brn-desc">'+esc(d.summary)+'</p>'
    +(reuse?'<h4>Reuses</h4><ul>'+reuse+'</ul>':'')+list('New',plan.new_parts)+list('Risks',plan.risks)+list('Steps',plan.steps)
    +(plan.test_command?'<h4>Test command <span class="brn-muted">runs in the new project after the build</span></h4><code class="brn-code">'
      +esc(plan.test_command.join(' '))+'</code>':'')
    +built+(actions?'<div class="brn-row-actions">'+actions+'</div>':'')+'</div>';
}

async function renderCreate(){
  const [g,e]=await Promise.all([api('/goals?limit=20'),api('/experiences?limit=30')]);
  root.querySelector('[data-goals-out]').innerHTML=g.ok?(g.data.length?g.data.map(goal=>'<div class="brn-goalcard"><h3 class="brn-h3">'
    +esc(goal.goal)+' <span class="brn-muted">'+esc(goal.status)+' · '+esc(rel(goal.created_at))+'</span></h3>'
    +goal.designs.map(designCard).join('')+'</div>').join(''):'<p class="brn-empty">No goals yet.</p>')
    :'<p class="brn-error">'+esc(failText(g))+'</p>';
  root.querySelector('[data-exp-out]').innerHTML=e.ok&&e.data.length?'<ul class="brn-findings">'+e.data.map(x=>'<li><span class="brn-chip is-'
    +esc(x.result)+'">'+esc(x.result)+'</span> '+esc(x.goal)+(x.lessons?' <span class="brn-muted">· '+esc(x.lessons)+'</span>':'')
    +'<span class="brn-spacer"></span><span class="brn-when">'+esc(rel(x.created_at))+'</span></li>').join('')+'</ul>'
    :'<p class="brn-empty">No experiences recorded.</p>';
}

async function propose(form){
  const goal=form.goal.value.trim();if(!goal)return;
  const body={goal};if(form.context.value)body.context_source=form.context.value;
  const btn=form.querySelector('button[type=submit]');btn.disabled=true;btn.textContent='Thinking…';
  const r=await api('/goals',{method:'POST',body});
  btn.disabled=false;btn.textContent='Propose designs';
  if(!r.ok){toast(failText(r));return;}
  form.goal.value='';await renderCreate();
}

async function designAction(kind,id,btn){
  btn.disabled=true;
  const r=await api('/designs/'+id+'/'+kind,{method:'POST'});
  if(!r.ok){btn.disabled=false;toast(failText(r));return;}
  toast(kind==='approve'?'Approved':'Building in a new project');
  await loadStatus();await renderCreate();
}

async function recordExperience(form){
  const goal=form.goal.value.trim();if(!goal)return;
  const r=await api('/experiences',{method:'POST',body:{goal,result:form.result.value,lessons:form.lessons.value.trim()}});
  if(!r.ok){toast(failText(r));return;}
  form.reset();toast('Experience recorded');await renderCreate();
}

/* ── wiring ─────────────────────────────────────────────────────────── */
function showPanel(name){
  panel=name;
  for(const b of root.querySelectorAll('[data-brn-panel]')){
    const on=b.dataset.brnPanel===name;b.classList.toggle('is-on',on);b.setAttribute('aria-pressed',String(on));
  }
  for(const s of root.querySelectorAll('[data-panel]'))s.hidden=s.dataset.panel!==name;
  return refreshPanel();
}

async function refreshPanel(){
  await loadStatus();
  if(panel==='sources')renderSources();
  else if(panel==='discoveries')await renderDiscoveries();
  else if(panel==='create')await renderCreate();
}

function bind(){
  root.addEventListener('submit',e=>{
    const f=e.target;e.preventDefault();
    if(f.matches('[data-recall]'))doRecall(f);
    else if(f.matches('[data-goal]'))propose(f);
    else if(f.matches('[data-exp]'))recordExperience(f);
  });
  root.addEventListener('change',e=>{
    const t=e.target.closest('[data-tick]');if(!t)return;
    const id=Number(t.dataset.tick);t.checked?chosen.add(id):chosen.delete(id);updateUsebar();
  });
  root.addEventListener('click',e=>{
    const t=e.target.closest('button');if(!t||!root.contains(t))return;
    if(t.dataset.brnPanel)showPanel(t.dataset.brnPanel);
    else if(t.dataset.piece)openPiece(Number(t.dataset.piece));
    else if(t.hasAttribute('data-close'))root.querySelector('[data-drawer]').hidden=true;
    else if(t.dataset.openPath){
      if(!t.dataset.openProject)return;
      switchView?.('projects/data/list');
      dispatchEvent(new CustomEvent('space:preview-file',{detail:{project:t.dataset.openProject,path:t.dataset.openPath}}));
    }
    else if(t.hasAttribute('data-use'))useTogether();
    else if(t.dataset.add)addSource(t.dataset.add);
    else if(t.dataset.relearn)learn(t.dataset.relearn,true);
    else if(t.dataset.forget)forget(t.dataset.forget);
    else if(t.hasAttribute('data-learn-all'))learn(null);
    else if(t.hasAttribute('data-discover'))runDiscovery(t);
    else if(t.dataset.done)api('/findings/'+t.dataset.done,{method:'PATCH',body:{status:'done'}}).then(()=>refreshPanel());
    else if(t.dataset.approve)designAction('approve',t.dataset.approve,t);
    else if(t.dataset.build)designAction('build',t.dataset.build,t);
  });
}

export default {
  ...projectPage('brain'),section:'brain',toolbar:null,
  async mount(el,{switchTo}){
    root=el;switchView=switchTo;
    root.innerHTML=shell();bind();
    await showPanel(panel);
  },
  show(){if(root)refreshPanel();},
  hide(){if(poll){clearInterval(poll);poll=null;}},
  refresh(){return refreshPanel();},
};
