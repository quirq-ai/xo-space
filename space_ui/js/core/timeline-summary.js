/* Totals describe the mapped data in the selected lanes and date window.
   Playback and tracing dim dots; they do not change this scope. */
const dateOf=value=>typeof value==='string'&&value?Date.parse(value+'T00:00:00'):NaN;
const count=(n,word)=>n.toLocaleString('en-US')+' '+word+(n===1?'':'s');
const formatDate=value=>new Date(value).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});

export function timelineSummary({mode,lanes,totalProjects,files,history,start,end,filtered=false}){
  if(!lanes.length)return totalProjects
    ?{summary:'No matching projects',context:'Try a different project name.'}
    :{summary:'No projects mapped',context:'Project dates will appear when Git data is available.'};
  const selected=new Set(lanes),withData=new Set();
  let items=0;
  if(mode==='project'){
    for(const id of selected)for(const day of history[id]||[]){
      const stamp=dateOf(day.d);
      if(!Number.isFinite(stamp)||!Number.isFinite(day.n)||day.n<=0)continue;
      withData.add(id);
      if(stamp>=start&&stamp<=end)items+=day.n;
    }
  }else{
    for(const file of files){
      if(!selected.has(file.cat))continue;
      const stamp=dateOf(file.date);if(!Number.isFinite(stamp))continue;
      withData.add(file.cat);
      if(stamp>=start&&stamp<=end)items++;
    }
  }
  const noun=mode==='project'?'commit':'dated file';
  const projects=filtered?selected.size+' of '+count(totalProjects,'mapped project'):count(selected.size,'mapped project');
  const window=Number.isFinite(start)&&Number.isFinite(end)?formatDate(start)+' – '+formatDate(end):'Date range unavailable';
  const missing=selected.size-withData.size,missingKind=mode==='project'?'commit data':'dated files';
  let context=mode==='project'?'Dot size shows commits per day.':'File dates mark their first Git commit.';
  if(!withData.size)context='No '+missingKind+' in these mapped projects.';
  else if(!items)context='No '+(mode==='project'?'commits':'dated files')+' in this date range.';
  if(missing&&withData.size)context+=' '+count(missing,'project')+' without '+missingKind+'.';
  return {summary:count(items,noun)+' · '+projects+' · '+window,context};
}
