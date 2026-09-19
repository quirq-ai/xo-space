// Installed host bridge. The portable artifact is data, never imported code.
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import {
  validateArtifact, createTaskRun, taskCandidates, stepTaskRun,
  taskPolicyHash, TASK_RUNTIME_VERSION, TASK_LIMITS,
} from './core.mjs';

const MAX_MESSAGE_BYTES = 24000000;
if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('Fly requires Node.js 20 or newer.');
const coreSha256 = createHash('sha256').update(fs.readFileSync(new URL('./core.mjs', import.meta.url))).digest('hex');
const provenance = JSON.parse(fs.readFileSync(new URL('./provenance.json', import.meta.url), 'utf8'));
if (coreSha256 !== provenance.sourceSha256) throw new Error('Installed Fly core does not match its pinned checksum.');

function workspaceFromMetadata(metadata, task) {
  if (!metadata || !Array.isArray(metadata.files)) throw new Error('Workspace metadata is required.');
  let total = 0;
  const files = metadata.files.map(file => {
    if (!Number.isSafeInteger(file.bytes) || file.bytes < 0 || file.bytes > TASK_LIMITS.fileBytes)
      throw new Error('Invalid metadata byte length.');
    total += file.bytes;
    if (total > TASK_LIMITS.workspaceBytes) throw new Error('Workspace exceeds 4 MB.');
    // Only byte length is an unopened-file feature. ASCII padding has exactly
    // that UTF-8 size, and contains none of the unopened file's content.
    return { path: file.path, content: 'x'.repeat(file.bytes) };
  });
  return { id: metadata.id, name: metadata.name, files, task };
}

function snapshot(run) {
  return { inspected: run.inspected, records: run.records, trace: run.trace,
    done: run.done, report: run.report };
}

function next(run) {
  const candidates = taskCandidates(run);
  // Exact core rule: sorted relative paths, first maximum wins a tie.
  const candidate = candidates.length
    ? candidates.reduce((best, item) => item.score > best.score ? item : best) : null;
  return { state: snapshot(run), candidate };
}

function handle(request) {
  if (!request || !['validate', 'start', 'step'].includes(request.op)) throw new Error('Unknown installed bridge operation.');
  const artifact = validateArtifact(request.artifact);
  const task = request.task ?? artifact.task;
  if (!task || Object.keys(task).some(key => !['query', 'fields', 'maxReads'].includes(key))) throw new Error('Unknown task field.');
  if (request.op === 'validate') {
    const normalized = createTaskRun({ id: 'task-validation', name: 'Task validation', files: [{ path: 'empty.json', content: '{}' }], task }, artifact.policy).workspace.task;
    return { artifact, policyHash: taskPolicyHash(artifact.policy), runtime: TASK_RUNTIME_VERSION, coreSha256, task: normalized };
  }
  const run = createTaskRun(workspaceFromMetadata(request.workspace, task), artifact.policy);
  if (request.op === 'start') return { ...next(run), task: run.workspace.task, coreSha256 };
  const state = request.state;
  if (!state || !Array.isArray(state.inspected) || !Array.isArray(state.trace) || !Array.isArray(state.records)
    || state.inspected.length >= Math.min(run.workspace.files.length, run.workspace.task.maxReads)
    || state.trace.length !== state.inspected.length || state.done) throw new Error('Invalid host run state.');
  Object.assign(run, state);
  const selected = next(run).candidate;
  const read = request.read;
  if (!read || read.path !== selected?.path) throw new Error('Host read does not match the learned decision.');
  const file = run.workspace.files.find(item => item.path === selected.path);
  if (typeof read.error === 'string') {
    // Padding is intentionally unparsable. The core records the failed read,
    // then the host's precise IO error replaces the synthetic parse error.
    file.content = file.content.length ? '"' + 'x'.repeat(file.content.length - 1) : '';
    stepTaskRun(run, selected.path);
    const error = read.error.slice(0, 240);
    run.inspected.at(-1).error = error;
    run.trace.at(-1).error = error;
    // Host IO failures must remain explicit even if a future compatible
    // parser starts accepting empty input. Do not assume a parse-error entry.
    run.report.errors = run.inspected.filter(item => item.error).map(item => ({ path: item.path, message: item.error }));
    run.report.status = 'partial';
    if (run.inspected.length === run.workspace.files.length)
      run.report.reason = 'Every file was attempted, but some files could not be parsed.';
  } else {
    if (typeof read.content !== 'string' || Buffer.byteLength(read.content, 'utf8') !== Buffer.byteLength(file.content, 'utf8'))
      throw new Error('Selected file byte length changed after discovery.');
    file.content = read.content;
    stepTaskRun(run, selected.path);
  }
  return next(run);
}

let bytes = 0, chunks = [];
for await (const chunk of process.stdin) {
  bytes += chunk.length;
  if (bytes > MAX_MESSAGE_BYTES) throw new Error('Host bridge request exceeds 24 MB.');
  chunks.push(chunk);
}
const envelope = JSON.parse(Buffer.concat(chunks).toString('utf8'));
let result;
try { result = { ok: true, value: handle(envelope.request) }; }
catch (error) { result = { ok: false, error: String(error.message).slice(0, 500) }; }
let output = JSON.stringify(result);
if (Buffer.byteLength(output) > MAX_MESSAGE_BYTES) output = JSON.stringify({ ok: false, error: 'Extracted report exceeds the 24 MB host result limit.' });
// The command executor journals stdout. Keep extracted values out of that
// journal: Python owns this precreated 0600 file in a private temp directory.
const fd = fs.openSync(envelope.outputPath, fs.constants.O_WRONLY | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW);
try {
  const stat = fs.fstatSync(fd);
  if (!stat.isFile() || stat.nlink !== 1 || (stat.mode & 0o077)) throw new Error('Unsafe bridge output file.');
  fs.writeFileSync(fd, output, 'utf8');
} finally { fs.closeSync(fd); }
process.stdout.write('Fly bridge completed.\n');
