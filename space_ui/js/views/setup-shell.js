/* Setup forms stay mounted while the shared navigation changes sections. */
import {pageHeader} from '../core/page-layout.js?v=20260914-unified1';

export function renderSetupShell(root){
  root.innerHTML=`<div class="setup-page space-page">
    <div class="setup-layout">
      <div class="setup-content">
        <section id="setup-search-results" aria-label="Setup search results" hidden></section>
        <section class="setup-panel" id="setup-panel-workspace" aria-labelledby="setup-workspace-title">
          ${pageHeader({title:'Workspace',titleId:'setup-workspace-title',description:'Choose your project folder and where Space keeps its settings.',actions:'<button class="setup-refresh space-button" id="setup-refresh" type="button">Refresh status</button>'})}
          <div class="setup-alert" id="setup-alert" role="status"><div><b>Checking settings…</b></div></div>
          <div class="setup-form-error" id="setup-restart-error" role="alert" hidden></div>
          <p class="setup-step-position">Step 1 of 3</p>
          <section class="setup-card setup-identity" id="setup-identity" aria-label="Workspace identity"></section>
          <section class="setup-card setup-roots">
            <div class="setup-card-head"><h3>Folders</h3><i id="roots-badge">Checking</i></div>
            <form id="roots-form" novalidate>
              <div class="setup-root-fields">
                <div><label for="xo-root-input">Projects folder</label><input id="xo-root-input" type="text" autocomplete="off" spellcheck="false" placeholder="/Users/you/xo-projects">
                  <small>Contains your project folders. Changing this does not move them.</small><code id="xo-root-applied"></code></div>
                <div><label for="quirq-root-input">Space data folder</label><input id="quirq-root-input" type="text" autocomplete="off" spellcheck="false" placeholder="/Users/you/.quirq">
                  <small>Settings, credentials and activity data. Default: ~/.quirq/</small><code id="quirq-root-applied"></code></div>
              </div>
              <div class="setup-form-error" id="roots-error" role="alert" hidden></div>
              <div class="setup-root-apply" id="roots-apply" hidden>
                <div><b>Apply folder changes</b><p>Restart to use the saved folders. For an installer-managed container, run this command to update its mounts.</p></div>
                <pre id="roots-command"></pre>
              </div>
              <div class="setup-actions"><button class="setup-primary space-button is-primary" id="roots-save" type="submit">Save folders</button><button class="setup-secondary space-button" id="roots-copy" type="button" hidden>Copy installer command</button></div>
            </form>
          </section>
          <details class="setup-details"><summary>Folder and connection details</summary><section class="setup-overview" id="setup-overview" aria-label="Installation paths"></section></details>
          <footer class="setup-step-footer"><button class="setup-secondary space-button" type="button" data-setup-go="intelligence">Next: Intelligence layer →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-intelligence" aria-labelledby="setup-intelligence-title" hidden>
          ${pageHeader({title:'Intelligence layer',titleId:'setup-intelligence-title',description:'Connect an agent and choose the activity Space collects.'})}
          <p class="setup-step-position">Step 2 of 3</p>
          <section class="setup-card setup-runtime" aria-labelledby="setup-agent-title">
            <div class="setup-card-head"><h3 id="setup-agent-title">Connect an agent</h3><button class="setup-secondary space-button" type="button" data-setup-go="secrets">Manage secrets</button></div>
            <form id="runtime-form" novalidate>
              <div class="setup-agent-label"><label for="runtime-agent">Agent for new chats</label><i id="setup-applied-badge">Checking</i></div><select id="runtime-agent" name="agent_name" required><option>Loading…</option></select>
              <div class="setup-form-error" id="runtime-error" role="alert" hidden></div>
              <div class="setup-actions"><button class="setup-primary space-button is-primary" id="runtime-save" type="submit">Save agent</button></div>
            </form>
            <div class="setup-sources" id="setup-sources"><div class="setup-empty">Checking agents…</div></div>
          </section>
          <section class="setup-card setup-runtime" aria-labelledby="setup-activity-title">
            <div class="setup-card-head"><h3 id="setup-activity-title">Activity collection</h3></div>
            <form id="activity-form" novalidate>
              <div class="setup-check-row"><label class="setup-switch" for="runtime-watcher"><input id="runtime-watcher" type="checkbox"><span></span></label><div><b>Update activity automatically</b><small>Keep sessions and project history up to date.</small></div></div>
              <label for="runtime-source-mode">Activity sources</label><select id="runtime-source-mode"><option value="all">All agents</option><option value="active">Chat agent only</option></select>
              <small>Scheduled commands also need automatic activity updates enabled.</small>
              <details class="setup-inline-details"><summary>Advanced</summary><label for="runtime-interval">Check every</label><div class="setup-number"><input id="runtime-interval" type="number" min=".25" max="60" step=".25" inputmode="decimal"><span>seconds</span></div><small>0.25–60 seconds. Default: 1.</small></details>
              <div class="setup-form-error" id="activity-error" role="alert" hidden></div>
              <div class="setup-actions"><button class="setup-primary space-button is-primary" id="activity-save" type="submit">Save activity settings</button></div>
            </form>
          </section>
          <div class="setup-usage-reporting" id="usage-reporting" hidden></div>
          <footer class="setup-step-footer"><button class="setup-secondary space-button" type="button" data-setup-go="projects">Next: Projects →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-projects" aria-labelledby="setup-projects-page-title" hidden>
          ${pageHeader({title:'Projects',titleId:'setup-projects-page-title',description:'Clone repositories into this Space or remove local projects.'})}
          <p class="setup-step-position">Step 3 of 3</p>
          <section class="setup-card setup-projects" id="setup-projects" aria-label="Projects"></section>
          <footer class="setup-step-footer"><button class="setup-primary space-button is-primary" id="setup-open-projects" type="button">Open Projects →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-connectors" aria-labelledby="setup-connectors-title" hidden>
          <div id="setup-connectors-placeholder">${pageHeader({title:'Connectors',titleId:'setup-connectors-loading-title',description:'Tools and apps for this workspace.'})}</div>
          <div id="setup-connectors"></div>
        </section>

        <section class="setup-panel" id="setup-panel-secrets" aria-labelledby="setup-secrets-title" hidden>
          ${pageHeader({title:'Secrets',titleId:'setup-secrets-title',description:'Environment values used by Space and its agents.'})}
          <section class="setup-card setup-credentials">
            <div class="setup-card-head"><h3>Saved secrets <span id="secret-count">—</span></h3><button class="setup-secondary space-button" id="secret-add" type="button">Add secret</button></div>
            <div id="secret-list"><div class="setup-empty">Loading credentials…</div></div>
            <div class="setup-form-error" id="secret-error" role="alert" hidden></div>
            <form id="secret-form" novalidate hidden>
              <h4 id="secret-form-title">Add secret</h4>
              <label for="secret-key">Key</label><input id="secret-key" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="ANTHROPIC_API_KEY" required>
              <label for="secret-value">Value</label><div class="setup-value-wrap"><input id="secret-value" type="password" autocomplete="new-password" spellcheck="false" placeholder="Paste a value" required><button id="secret-toggle" type="button" aria-label="Show value">Show</button></div>
              <small>Saved values stay hidden. Restart to apply changes.</small>
              <div class="setup-actions"><button class="setup-primary space-button is-primary" id="secret-save" type="submit">Save secret</button><button class="setup-secondary space-button" id="secret-cancel" type="button">Cancel</button></div>
            </form>
          </section>
        </section>

        <section class="setup-panel" id="setup-panel-commands" aria-labelledby="setup-commands-title" hidden>
          ${pageHeader({title:'Commands',titleId:'setup-commands-title',description:'Run commands and view their results.'})}
          <div class="setup-commands-intro">
            <p>Click <b>Run</b> to execute on this Space’s machine. Open the command’s <b>Inbox</b> for results and recent output. Intervals run automatically while the watcher and scheduler are enabled.</p>
            <p>Logs: <code>~/.quirq/scheduler/logs/&lt;command-id&gt;.log</code><br>Run history: <code>~/.quirq/scheduler/runs/&lt;command-id&gt;.jsonl</code><br>These are the default paths. Inbox shows the exact log path for your Space.</p></div>
          <section class="setup-card setup-commands" id="setup-commands" aria-label="Commands"></section>
        </section>

        <section class="setup-panel" id="setup-panel-server" aria-labelledby="setup-server-title" hidden>
          ${pageHeader({title:'Server',titleId:'setup-server-title',description:'Apply saved changes and keep Space up to date.'})}
          <section class="setup-card setup-maintenance">
            <div class="setup-card-head"><h3>Restart</h3><button class="setup-restart" id="setup-restart" data-restart type="button" disabled>Restart server</button></div>
            <div class="setup-server-body"><p class="setup-restart-hint" id="setup-restart-hint" role="status"></p><button class="setup-restart" id="runtime-restart" data-restart type="button" hidden>Apply &amp; restart</button></div>
          </section>
          <section class="setup-card setup-version">
            <div class="setup-card-head"><h3>Updates</h3><i id="update-badge">Not checked</i></div>
            <div class="setup-version-body"><div class="setup-version-state" id="update-state"><p>Check for a newer version of Space.</p></div><div class="setup-actions"><button class="setup-secondary space-button" id="update-check" type="button">Check for updates</button><button class="setup-primary space-button is-primary" id="update-apply" type="button" hidden>Update now</button><button class="setup-restart" id="update-restart" data-restart type="button" hidden>Restart server</button></div></div>
          </section>
          <button class="setup-secondary space-button" id="setup-quirq" type="button">Technical details →</button>
        </section>
      </div>
    </div>
  </div>`;
}
