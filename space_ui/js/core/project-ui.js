/* Small, shared controls for project cards. Clipboard actions never toggle a
   card, and tooltips live above its scrolling content rather than clipping. */
import {esc,toast} from './ui.js';

const paths={
  copy:'<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  share:'<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m9 10 6-4M9 14l6 4"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  activity:'<path d="M2 12h4l3-8 6 16 3-8h4"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  external:'<path d="M14 3h7v7M21 3 10 14M10 3H3v18h18v-7"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"/>',
  issue:'<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 3v1"/>',
};
const github='<path d="M12 .75a11.25 11.25 0 0 0-3.56 21.92c.56.1.77-.24.77-.54v-2.09c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.65-1.25-1.65-1.02-.7.08-.69.08-.69 1.13.08 1.72 1.16 1.72 1.16 1 1.72 2.64 1.22 3.28.93.1-.73.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.57 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .95-.3 3.09 1.16a10.76 10.76 0 0 1 5.62 0c2.15-1.45 3.09-1.16 3.09-1.16.61 1.55.23 2.7.11 2.98.72.79 1.16 1.79 1.16 3.02 0 4.33-2.63 5.29-5.14 5.57.4.35.76 1.04.76 2.1v3.07c0 .3.2.65.78.54A11.25 11.25 0 0 0 12 .75Z"/>';
export const icon=name=>'<svg class="project-icon" data-icon="'+name+'" viewBox="0 0 24 24" aria-hidden="true" focusable="false" '+(name==='github'?'fill="currentColor"':'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"')+'>'+(name==='github'?github:paths[name]||paths.copy)+'</svg>';
export function copyButton(label,value,attrs=''){
  return '<button type="button" class="project-icon-button" data-copy-value="'+esc(value)+'" data-copy-label="'+esc(label)+'" aria-label="Copy '+esc(label)+'" data-tip="Copy '+esc(label)+'" '+attrs+'><span class="project-copy-glyph">'+icon('copy')+icon('check')+'</span></button>';
}

let nextTooltip=0;
export function bindProjectUi(root){
  const tooltip=document.createElement('div');tooltip.id='project-tooltip-'+(++nextTooltip);
  tooltip.className='project-tooltip';tooltip.setAttribute('role','tooltip');tooltip.hidden=true;document.body.appendChild(tooltip);
  const live=document.createElement('span');live.className='project-copy-live';live.setAttribute('role','status');live.setAttribute('aria-live','polite');root.appendChild(live);
  let target=null;
  const pending=new WeakSet(),timers=new WeakMap();
  const observeTip=new MutationObserver(()=>{if(target)show(target);});
  new MutationObserver(()=>{if(target&&(!target.isConnected||!target.getClientRects().length))hide();})
    .observe(root,{childList:true,subtree:true});
  function hide(){
    observeTip.disconnect();
    if(target?.getAttribute('aria-describedby')===tooltip.id)target.removeAttribute('aria-describedby');
    target=null;tooltip.hidden=true;
  }
  function show(button){
    if(!button||!root.contains(button)||!button.dataset.tip||!button.getClientRects().length){hide();return;}
    hide();target=button;tooltip.textContent=button.dataset.tip;tooltip.hidden=false;
    button.setAttribute('aria-describedby',tooltip.id);
    const box=button.getBoundingClientRect(),size=tooltip.getBoundingClientRect();
    tooltip.style.left=Math.max(8,Math.min(innerWidth-size.width-8,box.left+(box.width-size.width)/2))+'px';
    tooltip.style.top=(box.top>=size.height+12?box.top-size.height-7:box.bottom+7)+'px';
    observeTip.observe(button,{attributes:true,attributeFilter:['data-tip']});
  }
  root.addEventListener('pointerover',event=>{const button=event.target.closest('[data-tip]');if(button!==target)show(button);});
  root.addEventListener('pointerout',event=>{if(target&&!target.contains(event.relatedTarget))hide();});
  root.addEventListener('focusin',event=>show(event.target.closest('[data-tip]')));
  root.addEventListener('focusout',hide);
  root.addEventListener('keydown',event=>{if(event.key==='Escape')hide();});
  addEventListener('scroll',hide,true);addEventListener('resize',hide);addEventListener('space:view',hide);
  root.addEventListener('click',async event=>{
    const button=event.target.closest('[data-copy-value]');
    if(!button||!root.contains(button)||pending.has(button))return;
    event.preventDefault();pending.add(button);clearTimeout(timers.get(button));
    button.dataset.copyState='busy';button.setAttribute('aria-busy','true');
    const value=button.dataset.copyValue,label=button.dataset.copyLabel||'value',original='Copy '+label;
    const unchanged=()=>button.isConnected&&button.dataset.copyValue===value&&(button.dataset.copyLabel||'value')===label;
    live.textContent='';
    try{
      await navigator.clipboard.writeText(value);
      if(!unchanged())return;
      button.dataset.copyState='done';button.dataset.tip='Copied';live.textContent=label+' copied.';
      timers.set(button,setTimeout(()=>{if(!unchanged())return;delete button.dataset.copyState;button.dataset.tip=original;if(target===button)show(button);},1800));
    }catch{
      if(!unchanged())return;
      button.dataset.copyState='error';button.dataset.tip='Could not copy. Select the text to copy it.';
      live.textContent='Could not copy '+label+'. Select the text to copy it.';toast(live.textContent);
    }finally{
      pending.delete(button);button.removeAttribute('aria-busy');
      if(button.isConnected&&(target===button||document.activeElement===button))show(button);
    }
  });
}
