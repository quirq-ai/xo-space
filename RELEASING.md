# Releasing XO Space

A release is an annotated SemVer tag on `main` plus a GitHub Release. Pull
requests merge into `main` continuously; a release is cut when the maintainers
decide, after testing and validation on the `development` staging branch. Not
every merge is a release, and a release is not cut right after a merge. The
tag gives that version a name, a changelog and something to quote in a bug
report.

Know what that means for users: installs follow the `main` tip (the installer
clones it and the Setup tab's Update fast-forwards to it), not the latest
tag. A change merged into `main` reaches new installs and updates before it
is part of a named release. Keep that in mind when deciding what merges.

## Version numbers

`vMAJOR.MINOR.PATCH`, first release `v1.0.0`.

- MAJOR: the update needs manual action from the user, or breaks the frontend
  request/response contract.
- MINOR: new features.
- PATCH: fixes only, including hotfixes.

## Cutting a release

1. Decide that `main` is ready: the changes since the last tag have been
   tested on `development` (`git merge --ff-only main` into it, or a merge
   commit if it has diverged) and validated there. Verify `main` itself:
   `venv/bin/python scripts/check_route_parity.py` and the test suite.
2. Tag the `main` commit (annotated):

   ```
   git checkout main
   git pull --ff-only
   git tag -a v1.2.0 -m "v1.2.0"
   ```

3. Push the tag: `git push origin v1.2.0`
4. Publish the release, with notes generated from the merged PR titles:
   `gh release create v1.2.0 --generate-notes`

To see what the next release contains before choosing its number:
`git log --oneline $(git describe --tags --abbrev=0)..main`

## Rules

- **Tag only commits that are on `main`.**
- **A tag is a decision, not a reflex.** It follows testing and validation on
  `development`; several merges may go into one release.
- **Never move or delete a pushed tag.** People, caches and the container
  registry trust it. Fix a bad release by shipping the next PATCH version.
- **Use annotated tags (`-a`).** They record who tagged and when, and
  `git describe` ignores lightweight tags by default.

## Hotfix

1. Branch from `main`, fix, PR to `main`, merge.
2. When validated, tag the PATCH bump on `main` and publish it (steps 2-4
   above).
3. Bring `development` up to date with `main` so staging has the fix.

## Side effects of pushing a tag

Pushing a `v*` tag triggers `.github/workflows/publish-container.yml`, which
builds a multi-arch image and publishes it to ghcr tagged with the version
(`1.2.0` and `1.2`).

## Recommended repository setting

A GitHub tag ruleset for `v*` that restricts who may create tags and blocks
deletion and updates (Settings -> Rules -> Rulesets -> New tag ruleset).

## Not done yet, on purpose

- The process is manual. Automate a step once it has proved repetitive.
- Tags name versions; they do not gate delivery. The updater
  (`services/cowork_agent/self_update.py`) follows the `main` tip, so
  "merged" and "released" are already different moments. If that gap starts
  to matter to users, the installer and the updater should follow release
  tags instead.
- The API's reported version (`server.py`, `version="1.0.0"`) is a fixed
  string and is not derived from the tag.
