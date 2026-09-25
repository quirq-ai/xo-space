# Report Scout parity fixtures

`failure-scout.fly.json` is the actual Failure Scout export from the browser
training lab (`solar-claude/public/fly/examples/failure-scout.fly.json`).
`workspace.json` is its first held-out synthetic workspace from
`public/fly/examples/space-failures.dataset.json`; it contains twelve files,
including the three failure records used in the narrated training tutorial.
No real credentials or user workspace data are included.

`browser-golden.json` records the browser core's complete trace and report for
that export and workspace. It was produced by calling `runTask(workspace,
artifact.policy)` from the exact shared core, without host-side changes.
The host test compares every decision feature, score, probability, chosen path,
extracted value, content hash and citation. Only the workspace name and ID are
host-specific. No evaluation oracle runs in the host.

The installed copy of the training app's `src/fly/tasks/core.js` lives at
`services/cowork_agent/adapters/fly/node/core.mjs`. It is byte-for-byte identical;
`node/provenance.json` pins its SHA-256 and contract versions. Update that copy,
checksum and golden fixture together when deliberately upgrading the contract.
