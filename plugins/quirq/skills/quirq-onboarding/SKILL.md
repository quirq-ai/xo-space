---
name: quirq-onboarding
description: First-run setup for the XO Space plugin. Use right after the plugin is installed, or when the user asks to set up XO Space in ChatGPT or Codex.
---

# Set up XO Space

Goal: the user ends with a running local Space and knows where to find it.

1. Call `space_status`.
   - Connected: go to step 3.
   - Not reachable: continue with step 2.
2. Follow the `quirq` skill's workflow: run discovery, then start an installed
   Space, or offer the one-time install when it is not installed. Install or
   start only after the user agrees. Then call `space_status` again.
3. Call `space_open` with `section` `projects/overview` so the user sees their
   Space in the conversation.
4. Tell the user, briefly:
   - **XO Space** in the sidebar opens Space at any time; it can also open
     beside a conversation.
   - Typing **@** in the composer lets them mention a Space project, the inbox
     or active agent sessions.
   - The plugin's settings choose the page Space opens on and whether the open
     page is shared with the chat.
   - Always include the link `<base_url>/space/` too: a terminal such as Codex
     CLI cannot display Space, and the browser shows the same Space.
   - If you started Space in step 2: it keeps running while this task is open;
     if XO Space later says it is unreachable, ask "start XO Space" to start it
     again.

Keep it short. Do not show tool names to the user.
