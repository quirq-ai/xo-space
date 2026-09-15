/* Questionnaire test page (#/questionnaire). Hidden from the tab bar; exists
   to exercise the shadcn Questionnaire port in core/questionnaire.js against
   Space's chrome. Mirrors the demo on ui.shadcn.com: single choice, multiple
   choice, freeform, and a skippable question. */
import {createQuestionnaire} from '../core/questionnaire.js';
import {toast} from '../core/ui.js';

const items=[
  {
    name:'role',
    prompt:'What best describes your role?',
    description:'This helps us tailor the workspace defaults.',
    required:true,
    choices:[
      {value:'engineer',label:'Engineer',description:'You write and ship code.'},
      {value:'designer',label:'Designer',description:'You own the look and flow.'},
      {value:'founder',label:'Founder / operator'},
      {value:'other',label:'Something else'},
    ],
  },
  {
    name:'tools',
    prompt:'Which tools do you use daily?',
    description:'Pick all that apply.',
    required:true,
    multiple:true,
    choices:[
      {value:'linear',label:'Linear'},
      {value:'github',label:'GitHub'},
      {value:'slack',label:'Slack'},
      {value:'notion',label:'Notion'},
      {value:'gmail',label:'Gmail'},
    ],
  },
  {
    name:'team_size',
    prompt:'How large is your team?',
    required:false,
    choices:[
      {value:'solo',label:'Just me'},
      {value:'2-5',label:'2 to 5'},
      {value:'6-20',label:'6 to 20'},
      {value:'20+',label:'More than 20'},
    ],
  },
  {
    name:'goal',
    prompt:'What do you want an agent to take off your plate first?',
    description:'One sentence is plenty.',
    required:true,
    input:{type:'textarea',placeholder:'e.g. triage the inbox every morning'},
  },
];

let root=null;

export default {
  id:'questionnaire',
  label:'Questionnaire',
  order:99,nav:false,
  async mount(el){
    root=el;
    root.innerHTML=
      '<div class="qn-page">'
        +'<h1>Questionnaire</h1>'
        +'<p>shadcn/ui questionnaire, ported to Space CSS. Digits 1 to 9 pick a choice; Enter advances.</p>'
        +'<div class="qn-card"></div>'
        +'<pre class="qn-result" aria-live="polite"></pre>'
      +'</div>';
    const result=root.querySelector('.qn-result');
    const q=createQuestionnaire({
      items,
      onSubmit(answers){
        result.textContent=JSON.stringify(answers,null,2);
        toast('Questionnaire submitted');
      },
    });
    root.querySelector('.qn-card').appendChild(q.el);
  },
};
