/* Data representations share one native-link control in each local toolbar. */
import {DATA_VIEWS} from './navigation.js?v=20260919-work4';

export function dataViewControls(activeId){
  return '<nav class="data-views" aria-label="Data view">'+DATA_VIEWS.map(view=>
    '<a href="#/'+view.route+'" data-data-mode="'+view.id+'"'
    +(view.id===activeId?' aria-current="page"':'')+'>'+view.label+'</a>'
  ).join('')+'</nav>';
}
