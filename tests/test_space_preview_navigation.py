"""Exercise the preview's Projects navigation and atlas reload handoff."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PreviewNavigationTests(unittest.TestCase):
    def test_preview_continuity_and_one_shot_reload_restore(self) -> None:
        script = r"""
          import fs from 'node:fs';
          import vm from 'node:vm';
          import assert from 'node:assert/strict';

          // Run the real preview implementation in a fresh browser-like
          // context per page. Stub only its imports, DOM and HTTP boundary.
          const source=fs.readFileSync(process.argv[1],'utf8')
            .replace(/^import .*;\n/gm,'').replace('export function initPreview','function initPreview');
          const key='space.previewReload';
          const history=[
            {hash:'newhash',short_hash:'newhash',date:'2026-09-14',subject:'new',path:'readme.md'},
            {hash:'oldhash',short_hash:'oldhash',date:'2026-09-13',subject:'old',path:'old-name.md'}
          ];
          const payload=content=>({ok:true,data:{name:'readme.md',kind:'markdown',
            content,size_bytes:12,modified_at:'2026-09-14',truncated:false}});
          const settle=async()=>{for(let i=0;i<25;i++)await Promise.resolve();};
          function storage(map=new Map()){
            return {map,getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};
          }
          function element(id){
            const classes=new Set(),listeners=new Map();
            return {id,style:{},hidden:false,value:'',textContent:'',_html:'',
              offsetWidth:610,offsetHeight:520,
              get innerHTML(){return this._html;},set innerHTML(s){this._html=s;if(id==='preview-version')this.value='';},
              classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),
                toggle:(x,on)=>on?classes.add(x):classes.delete(x)},
              addEventListener(name,fn){listeners.set(name,fn);},removeEventListener(){},
              fire(name,event={}){listeners.get(name)?.(event);},
              getBoundingClientRect(){return {left:455,top:110};},
              insertAdjacentHTML(_where,html){this._html+=html;},setPointerCapture(){}
            };
          }
          function page({hash='#/projects',store=storage(),respond,items=history}={}){
            const ids=['preview','preview-body','preview-version','preview-name','preview-path',
              'preview-meta','preview-source','preview-close','header'];
            const els=Object.fromEntries(ids.map(id=>[id,element(id)]));
            els.preview.querySelector=selector=>els[selector.replace(/^#/,'')];
            const listeners=new Map(),calls=[];
            const apiFetch=async url=>{
              calls.push(url);
              const custom=respond?.(url);
              if(custom!==undefined)return await custom;
              if(url.includes('/file-history'))return {ok:true,data:{is_repo:true,items}};
              return payload(url.includes('commit=')?'historic body':'working body');
            };
            const env={location:{hash},sessionStorage:store,innerWidth:1280,innerHeight:900,
              document:{getElementById:id=>els[id]},API_BASE:'',apiFetch,
              mdToHtml:text=>'<rendered>'+text+'</rendered>',console,
              addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn);}};
            vm.createContext(env);vm.runInContext(source,env);env.initPreview();
            const emit=(name,detail)=>{for(const fn of listeners.get(name)||[])fn({detail});};
            return {els,calls,store,env,emit,
              open:()=>emit('space:preview-file',{project:'demo',path:'readme.md',name:'Read me'}),
              click:id=>els.preview.fire('click',{target:{closest:s=>s==='#'+id?els[id]:null}}),
              switchTo(id,tab){env.location.hash='#/'+id;emit('space:view',{id,tab});},
              save(){emit('space:before-atlas-reload');},
              isOpen:()=>els.preview.classList.contains('is-open')};
          }

          const p=page();p.open();await settle();
          assert.equal(p.isOpen(),true);
          assert.match(p.els['preview-body'].innerHTML,/<rendered>working body/);
          const firstBody=p.els['preview-body'].innerHTML;
          for(const lens of ['dashboard','projects','graph','tree','sharing']){
            p.switchTo(lens,'projects');
            assert.equal(p.isOpen(),true,lens);
            assert.equal(p.els['preview-body'].innerHTML,firstBody,lens);
          }
          assert.equal(p.calls.length,2,'ordinary lens navigation never refetches the file');
          p.els['preview-version'].value='1';p.els['preview-version'].fire('change');await settle();
          p.click('preview-source');
          assert.match(p.els['preview-body'].innerHTML,/<pre[^>]*>historic body/);
          p.switchTo('dashboard','projects');p.save();
          const record=JSON.parse(p.store.getItem(key));
          assert.equal(record.version,'oldhash');
          assert.equal(record.source,true);
          assert.equal(record.file.path,'readme.md');
          assert.equal(JSON.stringify(record).includes('body'),false,'no file content is stored');
          assert.deepEqual(Object.keys(record).sort(),['file','geometry','route','source','version']);
          assert.deepEqual(record.geometry,{left:455,top:110,width:610,height:520});

          // History order changes across the reload: restore the commit,
          // not the old dropdown index, including its historical filename.
          const snapshot=p.store.getItem(key);
          const restored=page({hash:'#/dashboard',store:p.store,items:[history[1],history[0]]});
          await settle();
          assert.equal(p.store.getItem(key),null,'consume before asynchronous reads');
          assert.equal(restored.isOpen(),true);
          assert.equal(restored.els['preview-version'].value,'0');
          assert.equal(restored.els['preview-source'].textContent,'Rendered');
          assert.match(restored.els['preview-body'].innerHTML,/<pre[^>]*>historic body/);
          assert.ok(restored.calls.some(url=>url.includes('commit=oldhash&commit_path=old-name.md')));
          assert.equal(restored.els.preview.style.left,'455px');
          assert.equal(restored.els.preview.style.top,'110px');
          assert.equal(restored.els.preview.style.width,'610px');
          assert.equal(restored.els.preview.style.height,'520px');
          assert.equal(page({hash:'#/dashboard',store:p.store}).isOpen(),false,'one-shot restore');

          restored.switchTo('time','time');
          assert.equal(restored.isOpen(),false);
          assert.equal(restored.els['preview-body'].innerHTML,'');
          restored.save();assert.equal(restored.store.getItem(key),null);
          for(const hash of ['#/time','#/sessions','#/inbox','#/projects']){
            const store=storage(new Map([[key,snapshot]]));
            const other=page({hash,store});await settle();
            assert.equal(other.isOpen(),false,'snapshot is only valid for its destination');
            assert.equal(other.calls.length,0);
            assert.equal(store.getItem(key),null,'discard on another route');
            assert.equal(page({hash:'#/dashboard',store}).isOpen(),false);
          }

          // A second reload while the first file/history request is in
          // flight must carry the intended historical version forward.
          let resolveFile;
          const delayedFile=new Promise(resolve=>resolveFile=resolve);
          const slow=page({hash:'#/dashboard',store:storage(new Map([[key,snapshot]])),
            respond:url=>url.includes('/file?')?delayedFile:undefined});
          slow.switchTo('graph','projects');slow.save();
          assert.equal(JSON.parse(slow.store.getItem(key)).version,'oldhash');
          slow.switchTo('sessions','sessions');resolveFile(payload('late old file'));await settle();
          assert.equal(slow.isOpen(),false);
          assert.equal(slow.els['preview-body'].innerHTML,'');
          assert.equal(slow.store.getItem(key),null,'closing cancels any reload handoff');

          // An in-flight restored version cannot overwrite a newer file.
          let resolveVersion;
          const delayedVersion=new Promise(resolve=>resolveVersion=resolve);
          const racing=page({hash:'#/dashboard',store:storage(new Map([[key,snapshot]])),
            respond:url=>url.includes('commit=')?delayedVersion:undefined});
          await settle();
          racing.emit('space:preview-file',{project:'other',path:'new.md'});await settle();
          resolveVersion(payload('stale version'));await settle();
          assert.equal(racing.els['preview-path'].textContent,'other/new.md');
          assert.match(racing.els['preview-body'].innerHTML,/<rendered>working body/);

          const missingVersion=page({hash:'#/dashboard',store:storage(new Map([[key,snapshot]])),items:[]});
          await settle();
          assert.equal(missingVersion.isOpen(),true);
          assert.equal(missingVersion.els['preview-version'].hidden,true);
          assert.match(missingVersion.els['preview-body'].innerHTML,/<pre[^>]*>working body/);

          for(const bad of ['invalid json','null',JSON.stringify({route:'#/dashboard',file:{path:7}})]){
            const broken=page({hash:'#/dashboard',store:storage(new Map([[key,bad]]))});
            await settle();assert.equal(broken.isOpen(),false);assert.equal(broken.calls.length,0);
          }
          const unavailable={getItem(){throw Error('blocked');},setItem(){throw Error('blocked');},removeItem(){throw Error('blocked');}};
          const blocked=page({store:unavailable});blocked.open();await settle();blocked.save();
          assert.equal(blocked.isOpen(),true);
          blocked.switchTo('inbox','inbox');assert.equal(blocked.isOpen(),false);
          console.log(JSON.stringify({passed:true}));
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, "--",
             str(ROOT / "space_ui" / "js" / "core" / "preview.js")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"passed": True})


if __name__ == "__main__":
    unittest.main()
