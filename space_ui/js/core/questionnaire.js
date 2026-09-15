/* Questionnaire primitive, vanilla port of shadcn's Questionnaire
   (https://ui.shadcn.com/docs/components/base/questionnaire).

   In the React version the styled components in questionnaire.tsx wrap a
   headless primitive that owns the state machine: which item is active,
   required-answer validation, Previous / Skip / Next / Submit, the progress
   label, and focus after navigation. This module is that primitive plus the
   markup, emitting the same data-slot attributes so css/questionnaire.css
   styles it exactly like the shadcn source.

   Contract:
     createQuestionnaire({items, onSubmit, labels}) -> {el, destroy, goTo}

     items: [{
       name: 'role',                 // form field name (required)
       prompt: 'What is your role?', // legend text
       description: '...',           // optional
       required: true,               // Skip hidden, Next validates
       multiple: false,              // checkboxes instead of radios
       choices: [{value, label, description?}],   // fixed choices
       input: {type:'text'|'textarea', placeholder?, label?}, // freeform
     }]
     A choices item may also carry `input` to allow an "other" freeform
     answer alongside the fixed ones.

     onSubmit(answers, formData): answers is {name: string | string[]}
     built from FormData, the same shape as answers.get / getAll.

   Every fieldset stays in the form (inactive ones carry `hidden`), so the
   final FormData holds every answer, which is how shadcn's example reads
   them: `new FormData(event.currentTarget)`. */
import {esc} from './ui.js';

const CHECK_ICON=
  '<svg data-slot="questionnaire-choice-indicator-check" viewBox="0 0 24 24" fill="none" '
  +'stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  +'<path d="M20 6 9 17l-5-5"/></svg>';

const defaultLabels={
  previous:'Previous',skip:'Skip',next:'Next',submit:'Submit',
  required:'This question is required.',
  progress:(index,total)=>'Question '+index+' of '+total,
};

export function createQuestionnaire({items,onSubmit,labels={}}){
  if(!Array.isArray(items)||!items.length)throw new Error('createQuestionnaire: items must be a non-empty array');
  const text={...defaultLabels,...labels};
  const total=items.length;
  let active=0;
  const uid='qn-'+Math.random().toString(36).slice(2,8);

  const form=document.createElement('form');
  form.setAttribute('data-slot','questionnaire');
  form.noValidate=true;
  form.innerHTML=
    '<div data-slot="questionnaire-progress" role="progressbar" aria-label="Questionnaire progress" '
      +'aria-valuemin="1" aria-valuemax="'+total+'"></div>'
    +items.map(renderItem).join('')
    +'<div data-slot="questionnaire-actions">'
      +'<button type="button" class="ui-btn" data-variant="outline" data-size="default" data-slot="questionnaire-previous">'+esc(text.previous)+'</button>'
      +'<button type="button" class="ui-btn" data-variant="outline" data-size="default" data-slot="questionnaire-skip">'+esc(text.skip)+'</button>'
      +'<button type="button" class="ui-btn" data-variant="default" data-size="default" data-slot="questionnaire-next">'+esc(text.next)+'</button>'
      +'<button type="submit" class="ui-btn" data-variant="default" data-size="default" data-slot="questionnaire-submit">'+esc(text.submit)+'</button>'
    +'</div>';

  const progress=form.querySelector('[data-slot="questionnaire-progress"]');
  const fieldsets=[...form.querySelectorAll('[data-slot="questionnaire-item"]')];
  const btn=slot=>form.querySelector('[data-slot="questionnaire-'+slot+'"]');
  const prevBtn=btn('previous'),skipBtn=btn('skip'),nextBtn=btn('next'),submitBtn=btn('submit');

  function renderItem(item,i){
    const id=uid+'-'+i;
    const type=item.multiple?'checkbox':'radio';
    const choices=(item.choices||[]).map((c,ci)=>{
      /* 1..9 keycaps, like the shadcn demo's data-shortcut */
      const shortcut=ci<9?String(ci+1):null;
      return '<label data-slot="questionnaire-choice" data-type="'+type+'"'+(shortcut?' data-shortcut="'+shortcut+'"':'')+'>'
        +'<input data-slot="questionnaire-choice-input" type="'+type+'" name="'+esc(item.name)+'" value="'+esc(c.value)+'">'
        +'<span aria-hidden="true" data-slot="questionnaire-choice-indicator">'
          +'<span data-slot="questionnaire-choice-indicator-dot"></span>'+CHECK_ICON
        +'</span>'
        +'<span data-slot="questionnaire-choice-label">'
          +'<span>'+esc(c.label)+'</span>'
          +(c.description?'<span data-slot="questionnaire-choice-description">'+esc(c.description)+'</span>':'')
        +'</span>'
        +'<span data-slot="questionnaire-choice-shortcut">'+(shortcut||'')+'</span>'
      +'</label>';
    }).join('');
    const input=item.input?
      '<div data-slot="questionnaire-input-wrapper">'
        +(item.input.type==='textarea'
          ?'<textarea data-slot="questionnaire-input" name="'+esc(item.name)+'" '
            +'placeholder="'+esc(item.input.placeholder||'')+'" aria-label="'+esc(item.input.label||item.prompt)+'"></textarea>'
          :'<input data-slot="questionnaire-input" type="'+esc(item.input.type||'text')+'" name="'+esc(item.name)+'" '
            +'placeholder="'+esc(item.input.placeholder||'')+'" aria-label="'+esc(item.input.label||item.prompt)+'">')
      +'</div>':'';
    return '<fieldset data-slot="questionnaire-item" data-name="'+esc(item.name)+'" tabindex="-1" hidden '
      +'aria-describedby="'+id+'-error">'
      +'<legend data-slot="questionnaire-title">'+esc(item.prompt)+'</legend>'
      +(item.description?'<p data-slot="questionnaire-description">'+esc(item.description)+'</p>':'')
      +'<div data-slot="questionnaire-choices">'+choices+input+'</div>'
      +'<div data-slot="questionnaire-error" id="'+id+'-error" aria-live="polite"></div>'
    +'</fieldset>';
  }

  /* --- state --- */
  function answered(i){
    const fs=fieldsets[i];
    if([...fs.querySelectorAll('input[type=radio],input[type=checkbox]')].some(c=>c.checked))return true;
    const free=fs.querySelector('[data-slot="questionnaire-input"]');
    return !!(free&&free.value.trim());
  }
  function setError(i,msg){
    const fs=fieldsets[i];
    fs.querySelector('[data-slot="questionnaire-error"]').textContent=msg||'';
    for(const c of fs.querySelectorAll('[data-slot="questionnaire-choice"]')){
      if(msg)c.setAttribute('data-invalid','');else c.removeAttribute('data-invalid');
    }
    const free=fs.querySelector('[data-slot="questionnaire-input"]');
    if(free)free.setAttribute('aria-invalid',msg?'true':'false');
  }
  function validate(i){
    if(items[i].required&&!answered(i)){
      setError(i,text.required);
      /* failed validation focuses an available answer control */
      const first=fieldsets[i].querySelector('input,textarea');
      first?.focus();
      return false;
    }
    setError(i,'');
    return true;
  }
  function syncChecked(fs){
    for(const c of fs.querySelectorAll('[data-slot="questionnaire-choice"]')){
      const inp=c.querySelector('input');
      if(inp.checked)c.setAttribute('data-checked','');else c.removeAttribute('data-checked');
      if(inp.disabled)c.setAttribute('data-disabled','');else c.removeAttribute('data-disabled');
    }
  }
  function render({focus=true}={}){
    fieldsets.forEach((fs,i)=>{fs.hidden=i!==active;});
    progress.textContent=text.progress(active+1,total);
    progress.setAttribute('aria-valuenow',String(active+1));
    progress.setAttribute('aria-valuetext',progress.textContent);
    const last=active===total-1;
    prevBtn.disabled=active===0;
    skipBtn.hidden=!!items[active].required;
    nextBtn.hidden=last;
    submitBtn.hidden=!last;
    syncChecked(fieldsets[active]);
    /* successful navigation focuses the newly active item */
    if(focus)fieldsets[active].focus({preventScroll:false});
  }
  function goTo(i,{focus=true}={}){
    if(i<0||i>=total)return;
    active=i;
    render({focus});
  }
  function next({skip=false}={}){
    if(!skip&&!validate(active))return;
    setError(active,'');
    if(active<total-1)goTo(active+1);
  }
  function prev(){setError(active,'');if(active>0)goTo(active-1);}

  /* --- events --- */
  prevBtn.addEventListener('click',prev);
  skipBtn.addEventListener('click',()=>next({skip:true}));
  nextBtn.addEventListener('click',()=>next());
  form.addEventListener('change',e=>{
    const fs=e.target.closest('[data-slot="questionnaire-item"]');
    if(fs){syncChecked(fs);if(answered(active))setError(active,'');}
  });
  form.addEventListener('input',e=>{
    if(e.target.matches('[data-slot="questionnaire-input"]')&&answered(active))setError(active,'');
  });
  form.addEventListener('keydown',e=>{
    /* Enter in a text input advances instead of submitting the whole form;
       digits 1..9 pick the matching choice when focus is not in a text field. */
    const inText=e.target.matches('input[type=text],input[type=email],input[type=number],input[type=url],textarea');
    if(e.key==='Enter'&&inText&&!e.target.matches('textarea')){
      e.preventDefault();
      if(active===total-1)form.requestSubmit();else next();
      return;
    }
    if(!inText&&/^[1-9]$/.test(e.key)&&!e.metaKey&&!e.ctrlKey&&!e.altKey){
      const choice=fieldsets[active].querySelector('[data-slot="questionnaire-choice"][data-shortcut="'+e.key+'"] input');
      if(choice){
        e.preventDefault();e.stopPropagation(); /* keep the digit from switching Space tabs */
        choice.checked=choice.type==='checkbox'?!choice.checked:true;
        choice.dispatchEvent(new Event('change',{bubbles:true}));
        choice.focus();
      }
    }
  });
  form.addEventListener('submit',e=>{
    e.preventDefault();
    for(let i=0;i<total;i++){
      if(items[i].required&&!answered(i)){goTo(i);validate(i);return;}
    }
    const fd=new FormData(form);
    const answers={};
    for(const item of items){
      const all=fd.getAll(item.name).filter(v=>String(v).trim()!=='');
      answers[item.name]=item.multiple?all:(all[0]??null);
    }
    onSubmit?.(answers,fd);
  });

  render({focus:false});
  return {
    el:form,
    goTo,
    reset(){form.reset();fieldsets.forEach((_,i)=>setError(i,''));goTo(0,{focus:false});},
    destroy(){form.remove();},
  };
}
