/* Independent, bounded reads for composite views. The deadline resolves even
   when a provider ignores cancellation, and aborts the actual fetch as well. */
import {apiFetch} from './api.js';

export async function readSnapshot(path,{timeoutMs=12000}={}){
  const controller=new AbortController();
  const deadline=Number.isFinite(timeoutMs)&&timeoutMs>0?timeoutMs:12000;
  let timer;
  try{
    return await Promise.race([
      apiFetch(path,{signal:controller.signal}),
      new Promise(resolve=>{timer=setTimeout(()=>{
        controller.abort();resolve({ok:false,data:null,error:'Request timed out'});
      },deadline);}),
    ]);
  }catch{return {ok:false,data:null,error:'Request failed'};}
  finally{clearTimeout(timer);}
}
