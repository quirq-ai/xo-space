---
name: xo-deploy
description: Prepare an XO project for deployment to XO Launchpad. Use when asked to make a project deployable, to "deploy to Launchpad", or when XO's Deploy panel starts a "Deploy to Launchpad" thread. You prepare the Dockerfile, the port and launchpad.json; the Space's deploy script does the deploy.
---

# Preparing a project for XO Launchpad

XO Space deploys a project from GitHub: Launchpad adds a build workflow to the
project's repo, GitHub Actions builds the Dockerfile on every push to the
deploy branch, and Launchpad rolls out each new image. The Space's deploy
script does all of that when the owner presses **Deploy**. It holds the keys,
creates the repo and the app, pushes and follows the build.

**Your part is to prepare the project and nothing more.**

The flow's one definition is `services/launchpad/flow.json` in the XO Space
install. A thread started from the Deploy panel names its path, the project's
check result and the check command in XO's note. Read them first.

## What the project needs

1. **A Dockerfile at the project root** that builds the app for linux/amd64,
   with the repo root as the build context.
   - Start the app with its own production start command (not a dev server),
     listening on `0.0.0.0:<port>`. Reading `$PORT` is fine if the Dockerfile
     sets `ENV PORT=<port>`.
   - Keep the image small: a slim base, and a multi-stage build when there is
     a build step.
   - Put no secrets in the image or the repo.
   - Add a `.dockerignore` when the context would otherwise carry `.git`,
     `node_modules`, `.env` files or build output.
2. **`launchpad.json` at the project root**, committed. It is a pointer, never
   a secret. You write only these fields:
   ```json
   {"schema": 1, "name": "my-app", "port": 3000, "health_path": "/health",
    "env": [{"name": "DATABASE_URL", "sensitive": true}, {"name": "LOG_LEVEL", "sensitive": false}]}
   ```
   - `name`: 1-25 lowercase letters, digits or hyphens.
   - `port`: the port the container listens on. `EXPOSE` it in the Dockerfile too.
   - `health_path`: a path that answers 2xx/3xx once the app is up, or `null`
     for a port check.
   - `env`: the variable names the app reads, each with `sensitive`. **Never
     values.** The owner types values in the deploy form, and they go straight
     to Launchpad.
   - `storage` (only when the app keeps files that must outlive a restart, such as a
     SQLite database or uploads): `{"path": "/data", "size_gb": 2}`, 1-20 GB. Point the
     app's data at that path. A disk is chosen when the app is created and can only grow.
   - `size`: leave it out unless the app clearly needs more than the default
     (`xo.medium`: 1 CPU, 4 GB). The owner picks it in the deploy form, and it is
     fixed once the app exists.
   - If the file already has `app_id`, `slug`, `url`, `workflow` or `repo`, the project is
     linked to an app. Keep those fields exactly as they are.
3. **A git repository with the work committed.** If the project is not one
   yet, `git init` it with a `.gitignore` that keeps out dependencies, build
   output and `.env` files, then commit. Only commits deploy.

## Steps

1. Read the check in XO's note, or run it yourself (below), and its reasons.
2. Work out how the app really starts. Read its code, its `package.json` /
   `requirements.txt` / `Procfile`, and its README.
3. Write the Dockerfile, `.dockerignore` and `launchpad.json`. If Docker is
   available and allowed, build and run the image once and hit the port and
   health path.
4. Run the check until it says `ready` (or `linked`). It only reads files:
   `python3 <xo-space>/services/launchpad/launchpad.py check --project .`
5. Commit the files on your branch.
6. Tell the owner:
   - what you added, the port and the health path;
   - which environment variables they must fill in;
   - that they merge your branch (or ask you to), then press **Deploy**.

   The deploy runs from the project folder's branch, not from your worktree.

## Never

- Call `xo-launchpad`, the Launchpad API or the Space's deploy endpoints.
  Never push, create GitHub repos, or change git remotes, origin especially.
- Write `app_id`, `slug`, `url`, `workflow` or `repo` into `launchpad.json`.
- Put a secret value anywhere in the repo. Ask the owner to enter it in the
  deploy form.
- Rewrite the deploy branch's history.
