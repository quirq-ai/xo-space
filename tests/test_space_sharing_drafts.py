"""Sharing refresh and failed requests retain the current form state.

The real controller runs in a small Node VM; all reads and writes are stubs.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const gate=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return{promise,resolve};};
const notices=[],nodes=new Map();let renders=0,posts=0;
const recipient={value:'',focus(){this.focused=true;},scrollIntoView(){}};
const active={value:true};
const fixtureRoot={classList:{contains:()=>active.value},querySelector:selector=>nodes.get(selector)||null};
nodes.set('#shl-composer input[name="ws"]',recipient);
nodes.set('#shl-composer input[name=ws]',recipient);
const ctx=vm.createContext({
  console,Date,Map,Set,Promise,fixtureRoot,recipient,
  location:{hash:'#/inbox/sharing'},document:{activeElement:null},
  addEventListener:()=>{},INBOX_PAGES:[{id:'sharing',route:'inbox/sharing',parent:'inbox'}],setSectionActions:()=>{},
  toast:text=>notices.push(text),esc:value=>String(value??''),entryFor:()=>null,
  sharingStatusRes:()=>({ok:true}),parked:()=>false,
  failText:()=> 'Fixture failure',shortId:value=>value,
  fetchCatalog:async()=>({ok:true,data:{items:[{id:'alpha'}]}}),
  refreshSharingStatus:async()=>({ok:true}),
  share:async()=>{posts++;return{ok:false};},
});
const source=fs.readFileSync('space_ui/js/views/sharing.js','utf8')
  .replace(/^import\s+[\s\S]*?from\s+['"][^'"]+['"];?\s*/gm,'')
  .replace('export default {','const view = {');
vm.runInContext(source,ctx);
vm.runInContext("root=fixtureRoot;catalog=[{id:'alpha'}];names=new Map([['alpha','Alpha']]);",ctx);
ctx.renderFixture=()=>{renders++;};
vm.runInContext('render=renderFixture;',ctx);
const evaluate=code=>vm.runInContext(code,ctx);
const settle=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
const form=(button,input=recipient)=>({querySelector:selector=>selector==='button[type=submit]'?button:input});
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class SharingDraftTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "-e", PRELUDE + "\n(async()=>{\n" + source +
             "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_show_waits_for_fresh_status_before_composer_can_be_used(self):
        self.probe(r"""
const catalogRead=gate(),statusRead=gate();let status={ok:false};
ctx.fetchCatalog=()=>catalogRead.promise;
ctx.refreshSharingStatus=async()=>{await statusRead.promise;status={ok:true};};
ctx.sharingStatusRes=()=>status;
evaluate('catalogDirty=true;paintSnapshot=()=>{};loadCommits=async()=>{};');
const shown=evaluate('view.show()');
assert.equal(typeof shown?.then,'function','Navigation must await the current status');
let finished=false;shown.then(()=>{finished=true;});
catalogRead.resolve({ok:true,data:{items:[{id:'alpha'}]}});await settle();
assert.equal(finished,false,'Catalog alone cannot complete a stale-status handoff');
statusRead.resolve();await shown;
await evaluate("onClick({target:{closest:()=>({disabled:false,dataset:{act:'composer'}})}});");
await evaluate("onClick({target:{closest:()=>({disabled:false,dataset:{act:'pick',id:'alpha'}})}});");
assert.equal(evaluate('composer.pick'),'alpha');
assert.equal(posts,0,'Opening a share form must never send it');
""")

    def test_failed_share_restores_live_button_after_composer_repaint(self):
        self.probe(r"""
const pending=gate();ctx.share=()=>{posts++;return pending.promise;};
evaluate("composer={pick:'alpha',filter:'alp',ws:'recipient-draft'};");
const original={disabled:false};ctx.currentForm=form(original);
let liveButton=original;
nodes.set('#shl-composer-go',liveButton);
nodes.set('#shl-composer',{set outerHTML(markup){
  liveButton={disabled:markup.includes('id="shl-composer-go" disabled')};
  nodes.set('#shl-composer-go',liveButton);
}});
const request=evaluate("doShare('alpha','recipient-draft',currentForm,true)");
assert.equal(original.disabled,true);
evaluate('paintComposer();');
assert.notEqual(liveButton,original,'Exercise a newly painted submit node');
assert.equal(liveButton.disabled,true,'A repaint cannot enable a duplicate submission');
pending.resolve({ok:false});await request;
assert.equal(liveButton.disabled,false,'The visible button must allow retry after failure');
assert.equal(evaluate('composer.ws'),'recipient-draft');
assert.equal(evaluate('composer.filter'),'alp');
assert.equal(evaluate('composer.pick'),'alpha');
ctx.currentForm=form(liveButton);
await evaluate("doShare('alpha','recipient-draft',currentForm,true)");
assert.equal(posts,2,'The retry reaches the existing sharing operation');
""")

    def test_composer_cannot_submit_a_project_removed_from_the_catalog(self):
        self.probe(r"""
evaluate("composer={pick:'alpha',filter:'alp',ws:'keep-this-recipient'};catalog=[];");
ctx.currentForm=form({disabled:false});
await evaluate("doShare('alpha','keep-this-recipient',currentForm,true)");
assert.equal(posts,0,'Removal discovered after opening the composer blocks its submit');
assert.match(notices.at(-1),/no longer in the project list/);
assert.equal(evaluate('composer.ws'),'keep-this-recipient','A rejected submit retains the recipient');
assert.equal(evaluate('composer.filter'),'alp');
""")


if __name__ == "__main__":
    unittest.main()
