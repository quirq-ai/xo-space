/* Shared page markup. Text and attributes are escaped; actions is trusted
   markup assembled by the calling view, never an API-provided HTML string. */
import {esc} from './ui.js';

export function pageHeader({title,description='',titleId='',descriptionId='',level=1,actions='',className=''}){
  const heading=Number.isInteger(level)&&level>=1&&level<=6?'h'+level:'h1';
  return '<header class="space-page-header'+(className?' '+esc(className):'')+'">'
    +'<div class="space-page-heading"><'+heading+(titleId?' id="'+esc(titleId)+'"':'')+' tabindex="-1">'+esc(title)+'</'+heading+'>'
    +(description||descriptionId?'<p class="space-page-description"'+(descriptionId?' id="'+esc(descriptionId)+'"':'')+'>'+esc(description)+'</p>':'')
    +'</div><div class="space-page-actions">'+actions+'</div></header>';
}
