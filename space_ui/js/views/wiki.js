/* A compact local starting page. Full guides live in xo-docs. */
const DOCS_ROOT='https://docs.quirq.ai/docs/space';
const GROUPS=[
  {
    title:'Explore your workspace',
    topics:[
      {
        id:'projects',title:'Projects',
        summary:'Explore Overview, List, Graph, Tree, Sharing and Timeline. Inspect project files, activity and committed versions.',
        docs:'/space-walk',view:'projects'
      },
      {
        id:'timeline',title:'Timeline',
        summary:'Explore dated files and project commits, then inspect them in Graph.',
        docs:'/space-walk/timeline',view:'time'
      },
      {
        id:'agents',title:'Agents',
        summary:'Compare agent runs: recorded tokens, tools, models, and session details. Check source coverage and cost limits.',
        docs:'/space-walk/sessions',view:'agents',
        more:[{label:'Replay docs',path:'/space-walk/replay'}]
      }
    ]
  },
  {
    title:'Follow the work',
    topics:[
      {
        id:'inbox',title:'Inbox',
        summary:'Review incoming items, connection activity and scheduled jobs. Mark items seen or done.',
        docs:'/space-walk/inbox',view:'inbox'
      },
      {
        id:'observability',title:'Observability',
        summary:'Understand collected data, storage, watcher activity, and reporting boundaries.',
        docs:'/observability',
        more:[
          {label:'Storage docs',path:'/observability/storage'},
          {label:'Collection docs',path:'/observability/collection'}
        ]
      },
      {
        id:'sharing',title:'Sharing',
        summary:'Inspect repository sharing, incoming changes, and sync status across your Spaces.',
        docs:'/space-walk/sharing',view:'sharing'
      }
    ]
  },
  {
    title:'Configure and contribute',
    topics:[
      {
        id:'setup',title:'Setup & installation',
        summary:'Check roots, runtime health, and credentials. Restart Space and run saved commands with recorded results.',
        docs:'/space-walk/setup',view:'setup/workspace',viewLabel:'Setup',
        more:[{label:'Commands docs',path:'/space-walk/setup#run-saved-commands'},
          {label:'Install docs',path:'/install-space'}]
      },
      {
        id:'connectors',title:'Connectors',
        summary:'Connect services, choose the account enabled here, and control actions and polling.',
        docs:'/space-walk/connectors',view:'setup/connectors'
      },
      {
        id:'contribute',title:'Contribute',
        summary:'Explore the architecture and development workflow, then prepare a focused change.',
        docs:'/contributing'
      }
    ]
  }
];

/* Existing Projects/Quirq hand-offs keep their destinations without keeping
   the old articles. Requests received before mount/show are applied on show. */
const TOPIC_ALIASES={
  overview:'overview',quickstart:'quickstart',
  projects:'projects',timeline:'timeline',agents:'agents',sessions:'agents',inbox:'inbox',
  observability:'observability',sharing:'sharing',setup:'setup',
  connectors:'connectors',contribute:'contribute',
  storage:'observability',installation:'setup','first-run':'quickstart',
  watcher:'observability','xo-data':'observability','quirq-data':'observability',
  flows:'observability',collaboration:'projects',spacewalk:'agents',
  'tab-files':'projects','tab-dashboard':'projects','tab-timeline':'timeline',
  'tab-agents':'agents','tab-sessions':'agents','tab-inbox':'inbox','tab-wiki':'overview',
  'tab-quirq':'observability','tab-setup':'setup','tab-connectors':'connectors'
};
const esc=value=>String(value??'').replace(
  /[&<>"]/g,
  char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char])
);

let root=null;
let go=()=>{};
let visible=false;
let pendingTopic=null;

export default {
  /* The resource link opens Wiki; it does not occupy a primary tab/hotkey. */
  id:'wiki',label:'Wiki',order:7,nav:false,
  async mount(el,ctx){
    root=el;
    go=ctx.switchTo;
    render();
  },
  show(){
    visible=true;
    if(pendingTopic)revealTopic(pendingTopic);
  },
  hide(){visible=false;}
};

function docsLink(path,label,accessibleLabel=label){
  return '<a class="wiki-doc-link" href="'+esc(DOCS_ROOT+path)+'"'
    +' target="_blank" rel="noopener noreferrer"'
    +' aria-label="'+esc(accessibleLabel+' (opens in a new tab)')+'">'
    +esc(label)+' <span aria-hidden="true">↗</span></a>';
}

function openView(view,label,visibleLabel='Open view'){
  return '<button type="button" class="wiki-open-view" data-open-tab="'+esc(view)+'"'
    +' aria-label="Open '+esc(label)+' view">'+esc(visibleLabel)
    +' <span aria-hidden="true">→</span></button>';
}

function topicCard(topic){
  return '<article class="wiki-topic" id="wiki-'+topic.id+'"'
    +' data-wiki-topic="'+topic.id+'" tabindex="-1" aria-labelledby="wiki-title-'+topic.id+'">'
    +'<h3 id="wiki-title-'+topic.id+'">'+esc(topic.title)+'</h3>'
    +'<p>'+esc(topic.summary)+'</p>'
    +(topic.more?'<div class="wiki-more">'+topic.more.map(link=>
      docsLink(link.path,link.label)).join('')+'</div>':'')
    +'<div class="wiki-topic-actions">'
      +docsLink(topic.docs,'Open docs','Open '+topic.title+' docs')
      +(topic.view?openView(topic.view,topic.viewLabel||topic.title):'')
    +'</div>'
    +'</article>';
}

function render(){
  root.innerHTML='<div class="wiki-shell">'
    +'<main class="wiki-main" aria-labelledby="wiki-title">'
      +'<div class="wiki-content">'
        +'<header class="wiki-welcome" id="wiki-overview" tabindex="-1">'
          +'<div><div class="wiki-kicker">Space Wiki</div>'
            +'<h1 id="wiki-title">Welcome to Space</h1>'
            +'<p>Start with your workspace, follow the work, and find the full guide when you need it.</p></div>'
          +docsLink('','Open docs','Open the full Space documentation')
        +'</header>'
        +'<section class="wiki-quickstart" id="wiki-quickstart" tabindex="-1" aria-labelledby="wiki-quickstart-title">'
          +'<h2 id="wiki-quickstart-title">Your first three steps</h2>'
          +'<ol>'
            +'<li><span class="wiki-step-number" aria-hidden="true">01</span><div>'
              +'<h3>Check your setup</h3><p>Confirm the projects root and your runtime.</p>'
              +openView('setup/workspace','Setup','Open Setup')+'</div></li>'
            +'<li><span class="wiki-step-number" aria-hidden="true">02</span><div>'
              +'<h3>Bring a project</h3><p>Create or add a project inside that root.</p>'
              +docsLink('/first-space','Open docs','Open the first project guide')+'</div></li>'
            +'<li><span class="wiki-step-number" aria-hidden="true">03</span><div>'
              +'<h3>Explore your work</h3><p>Browse files and todos, then try another lens.</p>'
              +openView('projects','Projects','Open Projects')+'</div></li>'
          +'</ol>'
        +'</section>'
        +GROUPS.map((group,index)=>
          '<section class="wiki-group" aria-labelledby="wiki-group-'+index+'">'
            +'<h2 id="wiki-group-'+index+'">'+esc(group.title)+'</h2>'
            +'<div class="wiki-topics">'+group.topics.map(topicCard).join('')+'</div>'
          +'</section>').join('')
        +'<footer class="wiki-footer">This overview is available locally. Full guides open in a new tab and need an internet connection.</footer>'
      +'</div>'
    +'</main>'
  +'</div>';
  root.addEventListener('click',event=>{
    const button=event.target.closest('[data-open-tab]');
    if(button&&root.contains(button))go(button.dataset.openTab);
  });
}

function revealTopic(id){
  const target=root.querySelector('#wiki-'+id);
  const main=root.querySelector('.wiki-main');
  if(!target||!main)return;
  pendingTopic=null;
  root.querySelectorAll('.is-highlighted').forEach(el=>el.classList.remove('is-highlighted'));
  target.classList.add('is-highlighted');
  /* Scroll only the Wiki pane; scrollIntoView can move the clipped app stage. */
  main.scrollTop+=target.getBoundingClientRect().top-main.getBoundingClientRect().top-20;
  target.focus({preventScroll:true});
}

addEventListener('space:wiki-page',event=>{
  const id=String(event.detail||'');
  if(!Object.hasOwn(TOPIC_ALIASES,id))return;
  pendingTopic=TOPIC_ALIASES[id];
  if(root&&visible)revealTopic(pendingTopic);
});
