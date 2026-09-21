/* Setup layout only. Section IDs and labels are shared with navigation. */
import {SETUP_STEPS,SETUP_MANAGE} from '../core/setup-sections.js?v=20260922-work4';

export function renderSetupShell(root){
  root.innerHTML=`<div class="setup-page">
    <header class="setup-hero">
      <h1>Setup</h1>
      <button class="setup-refresh" id="setup-refresh" type="button">Refresh status</button>
    </header>
    <div class="setup-alert" id="setup-alert" role="status"><div><b>Checking settings…</b></div></div>
    <div class="setup-form-error" id="setup-restart-error" role="alert" hidden></div>
    <div class="setup-layout">
      <nav class="setup-nav" id="setup-nav" aria-label="Setup sections">
        <p>Set up</p>
        ${SETUP_STEPS.map(({id,route,number:n,label})=>`
          <a href="#/${route}" data-setup-step data-setup-go="${id}" aria-controls="setup-panel-${id}"${id==='workspace'?' aria-current="step"':''}>
            <span class="setup-nav-icon">${n}</span><span><b>${label}</b><small id="setup-step-${id}">Checking…</small></span>
          </a>`).join('')}
        <p>Manage</p>
        ${SETUP_MANAGE.map(({id,route,label,description})=>`<a href="#/${route}" data-setup-go="${id}" aria-controls="setup-panel-${id}"><span class="setup-nav-icon" aria-hidden="true">›</span><span><b>${label}</b><small${id==='secrets'?' id="setup-step-secrets"':''}>${description}</small></span></a>`).join('')}
      </nav>
      <div class="setup-content">
        <section id="setup-search-results" aria-label="Setup search results" hidden></section>
        <section class="setup-panel" id="setup-panel-workspace" aria-labelledby="setup-workspace-title">
          <header class="setup-section-head"><h2 id="setup-workspace-title" tabindex="-1">Workspace</h2><p>Customize your workspace and choose where its projects and settings live.</p></header>
          <section class="setup-card setup-branding" id="setup-branding" aria-label="Workspace branding"></section>
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
              <div class="setup-actions"><button class="setup-primary" id="roots-save" type="submit">Save folders</button><button class="setup-secondary" id="roots-copy" type="button" hidden>Copy installer command</button></div>
            </form>
          </section>
          <details class="setup-details"><summary>Folder and connection details</summary><section class="setup-overview" id="setup-overview" aria-label="Installation paths"></section></details>
          <footer class="setup-step-footer"><button class="setup-secondary" type="button" data-setup-go="intelligence">Next: Intelligence layer →</button></footer>
        </section>

        <section class="setup-panel" id="setup-panel-intelligence" aria-labelledby="setup-intelligence-title" hidden>
          <header class="setup-section-head"><h2 id="setup-intelligence-title" tabindex="-1">Intelligence layer</h2><p>Connect an agent and choose the activity Space collects.</p></header>
          <section class="setup-card setup-runtime" aria-labelledby="setup-agent-title">
            <div class="setup-card-head"><h3 id="setup-agent-title">Connect an agent</h3><button class="setup-secondary" type="button" data-setup-go="secrets">Manage secrets</button></div>
            <form id="runtime-form" novalidate>
              <div class="setup-agent-label"><label for="runtime-agent">Agent for new chats</label><i id="setup-applied-badge">Checking</i></div><select id="runtime-agent" name="agent_name" required><option>Loading…</option></select>
              <div class="setup-form-error" id="runtime-error" role="alert" hidden></div>
              <div class="setup-actions"><button class="setup-primary" id="runtime-save" type="submit">Save agent</button></div>
            </form>
            <div class="setup-sources" id="setup-sources"><div class="setup-empty">Checking agents…</div></div>
          </section>
          <section class="setup-card setup-runtime" aria-labelledby="setup-activity-title">
            <div class="setup-card-head"><h3 id="setup-activity-title">Activity collection</h3></div>
            <form id="activity-form" novalidate>
              <div class="setup-check-row"><label class="setup-switch" for="runtime-watcher"><input id="runtime-watcher" type="checkbox"><span></span></label><div><b>Update activity automatically</b><small>Keep sessions and project history up to date.</small></div></div>
              <label for="runtime-source-mode">Activity sources</label><select id="runtime-source-mode"><option value="all">All agents</option><option value="active">Chat agent only</option></select>
              <small>Scheduled jobs also need automatic activity updates enabled.</small>
              <details class="setup-inline-details"><summary>Advanced</summary><label for="runtime-interval">Check every</label><div class="setup-number"><input id="runtime-interval" type="number" min=".25" max="60" step=".25" inputmode="decimal"><span>seconds</span></div><small>0.25–60 seconds. Default: 1.</small></details>
              <div class="setup-form-error" id="activity-error" role="alert" hidden></div>
              <div class="setup-actions"><button class="setup-primary" id="activity-save" type="submit">Save activity settings</button></div>
            </form>
          </section>
          <div class="setup-usage-reporting" id="usage-reporting" hidden></div>
          <footer class="setup-step-footer"><a class="setup-secondary" href="#/projects/manage">Manage projects →</a></footer>
        </section>

        <section class="setup-panel" id="setup-panel-connections" aria-labelledby="setup-connections-title" hidden>
          <div id="setup-connections"></div>
        </section>

        <section class="setup-panel" id="setup-panel-secrets" aria-labelledby="setup-secrets-title" hidden>
          <header class="setup-section-head"><h2 id="setup-secrets-title" tabindex="-1">Secrets</h2><p>Environment values used by Space and its agents.</p></header>
          <section class="setup-card setup-credentials">
            <div class="setup-card-head"><h3>Saved secrets <span id="secret-count">—</span></h3><button class="setup-secondary" id="secret-add" type="button">Add secret</button></div>
            <div id="secret-list"><div class="setup-empty">Loading credentials…</div></div>
            <div class="setup-form-error" id="secret-error" role="alert" hidden></div>
            <form id="secret-form" novalidate hidden>
              <h4 id="secret-form-title">Add secret</h4>
              <label for="secret-key">Key</label><input id="secret-key" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="ANTHROPIC_API_KEY" required>
              <label for="secret-value">Value</label><div class="setup-value-wrap"><input id="secret-value" type="password" autocomplete="new-password" spellcheck="false" placeholder="Paste a value" required><button id="secret-toggle" type="button" aria-label="Show value">Show</button></div>
              <small>Saved values stay hidden. Restart to apply changes.</small>
              <div class="setup-actions"><button class="setup-primary" id="secret-save" type="submit">Save secret</button><button class="setup-secondary" id="secret-cancel" type="button">Cancel</button></div>
            </form>
          </section>
        </section>

        <section class="setup-panel" id="setup-panel-commands" aria-labelledby="setup-commands-title" hidden>
          <header class="setup-section-head setup-commands-intro"><h2 id="setup-commands-title" tabindex="-1">Jobs</h2>
            <p>A job is a saved command that runs on this Space’s machine. <b>Repeating</b> jobs run again and again on the schedule you choose. <b>One time</b> jobs run once on the day and time you pick, or whenever you click <b>Run now</b>. <b>Results</b> shows each run’s output.</p>
            <p>Logs: <code>~/.quirq/logs/scheduler/&lt;job-id&gt;.log</code><br>Run history: <code>~/.quirq/scheduler/runs/&lt;job-id&gt;.jsonl</code><br>These are the default paths. Results shows the exact log path for your Space.</p></header>
          <div class="setup-commands" id="setup-commands"></div>
        </section>

        <section class="setup-panel" id="setup-panel-server" aria-labelledby="setup-server-title" hidden>
          <header class="setup-section-head"><h2 id="setup-server-title" tabindex="-1">Server</h2><p>Apply saved changes and keep Space up to date.</p></header>
          <section class="setup-card setup-maintenance">
            <div class="setup-card-head"><h3>Restart</h3><button class="setup-restart" id="setup-restart" data-restart type="button" disabled>Restart server</button></div>
            <div class="setup-server-body"><p class="setup-restart-hint" id="setup-restart-hint" role="status"></p><button class="setup-restart" id="runtime-restart" data-restart type="button" hidden>Apply &amp; restart</button></div>
          </section>
          <section class="setup-card setup-version">
            <div class="setup-card-head"><h3>Updates</h3><i id="update-badge">Not checked</i></div>
            <div class="setup-version-body"><div class="setup-version-state" id="update-state"><p>Check for a newer version of Space.</p></div><div class="setup-actions"><button class="setup-secondary" id="update-check" type="button">Check for updates</button><button class="setup-primary" id="update-apply" type="button" hidden>Update now</button><button class="setup-restart" id="update-restart" data-restart type="button" hidden>Restart server</button></div></div>
          </section>
          <button class="setup-secondary" id="setup-quirq" type="button">Technical details →</button>
        </section>
      </div>
    </div>
  </div>`;
}
