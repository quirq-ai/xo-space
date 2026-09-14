/* Project management owns one persistent controller. Navigation and catalog
   refreshes leave clone, access-review and inline sharing drafts mounted. */
import {projectPage} from '../core/navigation.js?v=20260914-manage1';
import {openProjectActivity} from '../core/project-actions.js?v=20260914-details1';
import {mountProjectManagement} from './project-management.js?v=20260914-details1';

let root=null,manager=null,active=false,scrollTop=0;
function refresh(){
  // The controller invalidates older reads and queues a fresh snapshot.
  // The registry already deduplicates repeated manual Refresh clicks.
  return manager?manager.refresh():Promise.resolve();
}

export default {
  ...projectPage('project-manage'),section:'project-manage',toolbar:null,
  mount(el,{switchTo}){
    root=el;
    root.innerHTML='<div class="project-manage"><div id="manage-projects"></div></div>';
    manager=mountProjectManagement(root.querySelector('#manage-projects'),{
      onChange:detail=>dispatchEvent(new CustomEvent(detail?.action==='access'?'space:project-access-changed':'space:projects-changed',{detail})),
      onViewActivity:id=>openProjectActivity(switchTo,id),
    });
    addEventListener('space:add-project',()=>{
      if(active&&location.hash==='#/projects/manage'&&root.classList.contains('is-active'))manager.openAdd();
    });
  },
  show(){
    active=true;root.scrollTop=scrollTop;
    // The Add form needs the mounted controls, not a completed catalog read.
    refresh().catch(error=>console.error('Project management refresh failed:',error));
  },
  hide(){scrollTop=root.scrollTop;active=false;},
  refresh,
  hasDraft:()=>manager?.hasDraft()||false,
};
