/* Files representations share one native-link control in each local toolbar. */
import {FILE_VIEWS} from './navigation.js?v=20260914-files2';

export function fileViewControls(activeId){
  return '<nav class="file-views" aria-label="File view">'+FILE_VIEWS.map(view=>
    '<a href="#/'+view.route+'" data-file-mode="'+view.id+'"'
    +(view.id===activeId?' aria-current="page"':'')+'>'+view.label+'</a>'
  ).join('')+'</nav>';
}
