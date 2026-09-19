// Report Scout v1: shared browser/host inference, with no filesystem or DOM access.
// Content is visible only after a read. Teacher/evaluation oracles are separate.
export const TASK_MODEL_VERSION = 'linear-file-ranker-v1';
export const TASK_RUNTIME_VERSION = 'report-scout-runtime-v1';
export const FEATURE_VERSION = 'report-scout-features-v1';
export const FEATURE_NAMES = Object.freeze([
  'bias', 'json', 'csv', 'filename_query_overlap', 'path_query_overlap',
  'measurement_name', 'summary_name', 'raw_name', 'draft_name', 'archive_path',
  'output_path', 'reference_path', 'depth', 'log_size', 'requested_field_name_overlap',
  'backup_name', 'dated_name', 'budget_remaining', 'useful_read_fraction',
]);
export const TASK_LIMITS = Object.freeze({ files: 64, fileBytes: 256000, workspaceBytes: 4000000,
  workspaces: 160, datasetBytes: 24000000, fields: 12, rows: 10000 });
const clone = value => JSON.parse(JSON.stringify(value));
const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const byteLength = text => new TextEncoder().encode(text).length;
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const tokens = text => String(text).toLowerCase().match(/[a-z0-9]+/g) || [];
const unique = items => [...new Set(items)];
const overlap = (a, b) => b.length ? unique(b).filter(token => a.includes(token)).length / unique(b).length : 0;
const comparePath = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const exactKeys = (value, keys, label) => assert(plain(value) && Object.keys(value).every(key => keys.includes(key)), `Unknown ${label} field.`);
function rng(seed) {
  let state = seed >>> 0;
  return () => { state = (state + 0x6D2B79F5) >>> 0; let t = state;
    t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}

export function createTaskPolicy(seed = 17) {
  assert(Number.isSafeInteger(seed), 'Policy seed must be a safe integer.');
  return { schemaVersion: 1, family: TASK_MODEL_VERSION, featureVersion: FEATURE_VERSION,
    seed, weights: Array(FEATURE_NAMES.length).fill(0), trainedEpochs: 0 };
}
export function validateTaskPolicy(policy) {
  exactKeys(policy, ['schemaVersion', 'family', 'featureVersion', 'seed', 'weights', 'trainedEpochs'], 'policy');
  assert(plain(policy) && policy.schemaVersion === 1 && policy.family === TASK_MODEL_VERSION
    && policy.featureVersion === FEATURE_VERSION, 'Unsupported Report Scout policy or feature version.');
  assert(Number.isSafeInteger(policy.seed) && Number.isSafeInteger(policy.trainedEpochs)
    && policy.trainedEpochs >= 0, 'Invalid policy seed or epoch count.');
  assert(Array.isArray(policy.weights) && policy.weights.length === FEATURE_NAMES.length
    && policy.weights.every(value => Number.isFinite(value) && Math.abs(value) <= 1000), 'Invalid Report Scout weights.');
  return policy;
}

function validateTask(raw = {}) {
  assert(plain(raw), 'Task must be an object.');
  const fields = raw.fields ?? ['run', 'success', 'duration'];
  assert(Array.isArray(fields) && fields.length > 0 && fields.length <= TASK_LIMITS.fields
    && fields.every(field => typeof field === 'string' && field.length > 0 && field.length <= 100
      && !/[\u0000-\u001f]/.test(field) && !field.split(/[/.]/).some(part => ['__proto__', 'prototype', 'constructor'].includes(part))), 'Task fields must be 1–12 safe column names, dotted paths, or JSON pointers.');
  assert(new Set(fields).size === fields.length, 'Task fields must be unique.');
  const query = raw.query ?? 'experiment metrics';
  assert(typeof query === 'string' && query.length <= 240, 'Task query must be text up to 240 characters.');
  const maxReads = raw.maxReads ?? 4;
  assert(Number.isInteger(maxReads) && maxReads >= 1 && maxReads <= TASK_LIMITS.files, 'Read budget must be an integer from 1 to 64.');
  return { fields: fields.slice(), query: query.normalize('NFC'), maxReads };
}
export function validateWorkspace(raw) {
  assert(plain(raw), 'Workspace must be an object.');
  assert(typeof raw.id === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/.test(raw.id), 'Workspace needs a safe, stable ID.');
  assert(typeof raw.name === 'string' && raw.name.trim().length > 0 && raw.name.length <= 160, 'Workspace needs a name up to 160 characters.');
  assert(Array.isArray(raw.files) && raw.files.length > 0 && raw.files.length <= TASK_LIMITS.files, 'Workspace must contain 1–64 JSON or CSV files.');
  const seen = new Set(); let total = 0;
  const files = raw.files.map(file => {
    assert(plain(file) && typeof file.path === 'string' && typeof file.content === 'string', 'Each file needs path and text content.');
    const path = file.path.normalize('NFC');
    assert(path.length > 0 && path.length <= 240 && !/[\\:\u0000-\u001f]/.test(path)
      && !path.startsWith('/') && path.split('/').every(part => part && part !== '.' && part !== '..')
      && /\.(json|csv)$/i.test(path), `Unsafe or unsupported workspace path: ${path.slice(0, 80)}`);
    assert(!seen.has(path.toLowerCase()), `Duplicate workspace path: ${path}`); seen.add(path.toLowerCase());
    const size = byteLength(file.content); total += size;
    assert(size <= TASK_LIMITS.fileBytes, `File exceeds 256 KB: ${path}`);
    return { path, content: file.content };
  }).sort((a, b) => comparePath(a.path, b.path));
  assert(total <= TASK_LIMITS.workspaceBytes, 'Workspace exceeds 4 MB.');
  return { id: raw.id, name: raw.name.trim(), files, task: validateTask(raw.task) };
}
export function validateDataset(raw) {
  assert(plain(raw) && typeof raw.id === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/.test(raw.id)
    && typeof raw.name === 'string' && raw.name.length > 0 && raw.name.length <= 160, 'Dataset needs an ID and name.');
  const result = { id: raw.id, name: raw.name }, ids = new Set(), fingerprints = new Set();
  let count = 0, bytes = 0;
  for (const split of ['train', 'validation', 'test']) {
    assert(Array.isArray(raw[split]) && (split !== 'train' || raw[split].length > 0), `Dataset needs a ${split} array; training cannot be empty.`);
    result[split] = raw[split].map(value => {
      const workspace = validateWorkspace(value); count++; bytes += workspace.files.reduce((sum, f) => sum + byteLength(f.content), 0);
      assert(count <= TASK_LIMITS.workspaces && bytes <= TASK_LIMITS.datasetBytes, 'Dataset exceeds workspace or byte limits.');
      assert(!ids.has(workspace.id), `Workspace ID appears more than once: ${workspace.id}`); ids.add(workspace.id);
      // Ignore names, IDs and paths so relabeled copies cannot leak across splits.
      const fingerprint = sha256(canonicalJSON(workspace.files.map(f => f.content).sort(comparePath)));
      assert(!fingerprints.has(fingerprint), 'Duplicate workspace content: split by independent workspaces, not copied rows.'); fingerprints.add(fingerprint);
      return workspace;
    });
  }
  assert(count <= TASK_LIMITS.workspaces && bytes <= TASK_LIMITS.datasetBytes, 'Dataset exceeds workspace or byte limits.');
  return result;
}

function checkJSONValues(value, depth = 0, counter = { n: 0 }) {
  assert(depth <= 32 && ++counter.n <= 50000, 'JSON exceeds nesting or value limits.');
  if (typeof value === 'number') assert(Number.isFinite(value), 'JSON contains a non-finite number.');
  if (value && typeof value === 'object') for (const item of Object.values(value)) checkJSONValues(item, depth + 1, counter);
}
function csvScalar(value) {
  if (value === '') return null;
  if (value === 'true') return true;
  if (value === 'false') return false;
  if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value)) {
    const number = Number(value); assert(Number.isFinite(number), 'CSV contains a non-finite number.'); return number;
  }
  return value;
}
export function parseCSV(text) {
  text = text.replace(/^\uFEFF/, '');
  const rows = []; let row = [], field = '', quoted = false, closed = false;
  const pushField = () => { row.push(field); field = ''; closed = false; };
  const pushRow = () => { pushField(); if (row.some(value => value !== '')) rows.push(row); row = [];
    assert(rows.length <= TASK_LIMITS.rows + 1, 'CSV exceeds the row limit.'); };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else { quoted = false; closed = true; } }
      else field += ch;
    } else if (ch === ',') pushField();
    else if (ch === '\n' || ch === '\r') { pushRow(); if (ch === '\r' && text[i + 1] === '\n') i++; }
    else if (ch === '"') { assert(field.length === 0 && !closed, 'Unexpected CSV quote.'); quoted = true; }
    else { assert(!closed, 'Unexpected content after a closing CSV quote.'); field += ch; }
  }
  assert(!quoted, 'Unclosed CSV quote.');
  if (field.length || row.length || closed) pushRow();
  assert(rows.length > 0, 'CSV is empty.');
  const headers = rows.shift();
  assert(headers.every(h => h.length > 0) && new Set(headers).size === headers.length, 'CSV needs unique, nonempty column names.');
  return rows.map((values, index) => { assert(values.length === headers.length, `CSV row ${index + 2} has the wrong column count.`);
    return Object.fromEntries(headers.map((key, i) => [key, csvScalar(values[i]) ])); });
}
function lookup(row, field) {
  if (Object.hasOwn(row, field)) return row[field];
  const parts = field.startsWith('/') ? field.slice(1).split('/').map(p => p.replaceAll('~1', '/').replaceAll('~0', '~')) : field.split('.');
  let value = row;
  for (const part of parts) { if (!value || typeof value !== 'object' || !Object.hasOwn(value, part)) return undefined; value = value[part]; }
  return value;
}
export function extractFile(file, task) {
  let rows, base = '$', singleObject = false;
  if (/\.csv$/i.test(file.path)) { rows = parseCSV(file.content); base = 'csv'; }
  else {
    const value = JSON.parse(file.content.replace(/^\uFEFF/, '')); checkJSONValues(value);
    if (Array.isArray(value)) rows = value;
    else if (plain(value)) {
      const arrayKey = ['rows', 'records', 'data', 'results'].find(key => Array.isArray(value[key]));
      if (arrayKey) { rows = value[arrayKey]; base = `$.${arrayKey}`; } else { rows = [value]; singleObject = true; }
    } else throw new Error('JSON must contain an object or an array of objects.');
  }
  assert(rows.length <= TASK_LIMITS.rows, 'File exceeds the row limit.');
  const records = [], available = new Set(), contentHash = sha256(file.content);
  rows.forEach((row, index) => {
    if (!plain(row)) return;
    const entries = task.fields.map(field => [field, lookup(row, field)]);
    for (const [field, value] of entries) if (value !== undefined && value !== null) available.add(field);
    if (entries.every(([, value]) => value !== undefined && value !== null)) records.push({
      values: Object.fromEntries(entries.map(([key, value]) => [key, clone(value)])),
      source: { path: file.path, location: base === 'csv' ? `row ${index + 2}` : singleObject ? '$' : `${base}[${index}]`, contentHash },
    });
  });
  return { records, availableFields: [...available] };
}

function candidateFeatures(run, file) {
  const path = file.path.toLowerCase(), name = path.split('/').at(-1), nameTokens = tokens(name);
  const queryTokens = tokens(run.workspace.task.query), size = byteLength(file.content);
  const reads = run.inspected.length, useful = run.inspected.filter(item => item.matchedRows > 0).length;
  return [1, Number(path.endsWith('.json')), Number(path.endsWith('.csv')),
    overlap(nameTokens, queryTokens), overlap(tokens(path), queryTokens),
    Number(/measurements?|telemetry|observations?|samples?|readings?/.test(name)),
    Number(/summary|aggregate|report|metrics/.test(name)), Number(/raw|shard|part|data/.test(name)),
    Number(/draft|notes?|template|example/.test(name)), Number(/(?:^|\/)(?:archive|old|history|backup|deprecated)(?:\/|$)/.test(path)),
    Number(/(?:^|\/)(?:outputs?|exports?|results?|runs?|measurements?|telemetry)(?:\/|$)/.test(path)),
    Number(/(?:^|\/)(?:docs?|reference|fixtures?|templates?|samples?)(?:\/|$)/.test(path)),
    Math.min(1, (path.split('/').length - 1) / 5), Math.log1p(size) / Math.log1p(TASK_LIMITS.fileBytes),
    overlap(nameTokens, tokens(run.workspace.task.fields.join(' '))), Number(/backup|copy|old|legacy/.test(name)),
    Number(/20\d{2}|\d{8}/.test(name)), (run.workspace.task.maxReads - reads) / run.workspace.task.maxReads,
    reads ? useful / reads : 0];
}
const dot = (weights, features) => weights.reduce((sum, weight, i) => sum + weight * features[i], 0);
function probabilities(scores) { const maximum = Math.max(...scores), exps = scores.map(score => Math.exp(score - maximum)), total = exps.reduce((a, b) => a + b, 0); return exps.map(e => e / total); }
export function taskCandidates(run) {
  if (run.done) return [];
  const seen = new Set(run.inspected.map(entry => entry.path));
  const candidates = run.workspace.files.filter(file => !seen.has(file.path)).map(file => {
    const features = candidateFeatures(run, file); return { id: file.path, path: file.path, features, score: dot(run.policy.weights, features) };
  });
  const distribution = probabilities(candidates.map(c => c.score));
  return candidates.map((candidate, index) => ({ ...candidate, probability: distribution[index] }));
}
function buildReport(run) {
  const scannedAll = run.inspected.length === run.workspace.files.length;
  const hasErrors = run.inspected.some(item => item.error), complete = scannedAll && !hasErrors;
  const available = new Set(run.inspected.flatMap(item => item.availableFields));
  return { schemaVersion: 1, taskFamily: 'report-scout-v1', workspaceId: run.workspace.id,
    title: `${run.workspace.name} · extracted records`, status: complete ? 'complete' : 'partial',
    reason: complete ? 'Every eligible file was inspected.' : scannedAll && hasErrors ? 'Every file was attempted, but some files could not be parsed.'
      : run.done ? 'Read budget reached; uninspected files may contain more records.' : 'Inspection is in progress.',
    scope: { eligibleFiles: run.workspace.files.length, inspectedFiles: run.inspected.length,
      inspectedPaths: run.inspected.map(item => item.path), unreadPaths: run.workspace.files.filter(f => !run.inspected.some(s => s.path === f.path)).map(f => f.path) },
    fields: run.workspace.task.fields.slice(), missingFields: run.workspace.task.fields.filter(field => !available.has(field)),
    records: clone(run.records), errors: run.inspected.filter(item => item.error).map(item => ({ path: item.path, message: item.error })),
    note: 'Records contain all requested fields. Values are copied from cited files; no unseen content or generated facts are included.' };
}
export function createTaskRun(workspace, policy = createTaskPolicy(), { controller = 'learned', seed = 17 } = {}) {
  validateTaskPolicy(policy);
  assert(['learned', 'filename', 'random'].includes(controller) && Number.isSafeInteger(seed), 'Invalid task controller or seed.');
  const run = { workspace: validateWorkspace(workspace), policy: clone(policy), controller, seed,
    randomState: seed >>> 0, inspected: [], records: [], trace: [], done: false, report: null };
  run.report = buildReport(run); return run;
}
function selectCandidate(run, candidates) {
  if (run.controller === 'random') { const random = rng(run.randomState); const i = Math.floor(random() * candidates.length); run.randomState = (run.randomState + 0x6D2B79F5) >>> 0; return candidates[i]; }
  const score = candidate => run.controller === 'filename' ? candidate.features[3] : candidate.score;
  return candidates.reduce((best, candidate) => score(candidate) > score(best) ? candidate : best);
}
export function stepTaskRun(run, candidateId) {
  if (run.done) return run;
  const candidates = taskCandidates(run);
  const candidate = candidateId === undefined ? selectCandidate(run, candidates) : candidates.find(c => c.id === candidateId);
  assert(candidate, 'Choose an eligible, unread file candidate.');
  const file = run.workspace.files.find(f => f.path === candidate.path);
  const inspected = { path: file.path, matchedRows: 0, availableFields: [], error: null };
  try { const extraction = extractFile(file, run.workspace.task); inspected.matchedRows = extraction.records.length;
    inspected.availableFields = extraction.availableFields; run.records.push(...extraction.records); }
  catch (error) { inspected.error = String(error.message).slice(0, 240); }
  run.inspected.push(inspected);
  run.trace.push({ step: run.inspected.length, action: 'inspect_file', candidateId: candidate.id, path: candidate.path,
    features: candidate.features.slice(), score: candidate.score, probability: candidate.probability,
    matchedRows: inspected.matchedRows, error: inspected.error });
  run.done = run.inspected.length >= Math.min(run.workspace.task.maxReads, run.workspace.files.length);
  run.report = buildReport(run); return run;
}
function relevantPaths(workspace) {
  return workspace.files.filter(file => { try { return extractFile(file, workspace.task).records.length > 0; } catch { return false; } }).map(file => file.path);
}
export function summarizeTaskRun(run) {
  // This oracle is for evaluation only, never policy features or report generation.
  const relevant = relevantPaths(run.workspace), found = run.inspected.filter(file => file.matchedRows > 0).length;
  return { coverage: relevant.length ? found / relevant.length : 1, precision: run.inspected.length ? found / run.inspected.length : 0,
    reads: run.inspected.length, complete: run.report.status === 'complete', allRelevantFound: found === relevant.length,
    relevantFiles: relevant.length, foundFiles: found, extractedRecords: run.records.length, errors: run.report.errors.length };
}
export function runTask(workspace, policy = createTaskPolicy(), options = {}) {
  const run = createTaskRun(workspace, policy, options);
  while (!run.done) stepTaskRun(run);
  return { workspaceId: run.workspace.id, name: run.workspace.name, controller: run.controller,
    summary: summarizeTaskRun(run), trace: run.trace, report: run.report };
}
export function summarizeTaskRuns(runs) {
  const mean = fn => runs.length ? runs.reduce((sum, run) => sum + fn(run.summary), 0) / runs.length : 0;
  return { count: runs.length, coverage: mean(s => s.coverage), precision: mean(s => s.precision),
    meanReads: mean(s => s.reads), completeRate: mean(s => Number(s.complete)),
    allRelevantFoundRate: mean(s => Number(s.allRelevantFound)), meanErrors: mean(s => s.errors) };
}
export function evaluateTaskPolicy(policy, workspaces, options = {}) {
  validateTaskPolicy(policy); assert(Array.isArray(workspaces) && workspaces.length > 0, 'Evaluation requires workspaces.');
  const runs = workspaces.map((workspace, index) => runTask(workspace, policy, { ...options, seed: (options.seed ?? 17) + index }));
  return { summary: summarizeTaskRuns(runs), runs };
}
export function collectDemonstrations(workspace) {
  const run = createTaskRun(workspace), examples = [];
  const relevant = new Set(relevantPaths(run.workspace));
  while (!run.done) {
    const candidates = taskCandidates(run), chosen = candidates.findIndex(candidate => relevant.has(candidate.path));
    if (chosen < 0) break;
    examples.push({ features: candidates.map(c => c.features.slice()), chosen, workspaceId: run.workspace.id, source: 'teacher' });
    stepTaskRun(run, candidates[chosen].id);
  }
  return examples;
}
function validateExamples(examples) {
  assert(Array.isArray(examples) && examples.length > 0 && examples.length <= 20000, 'Training requires 1–20,000 labeled decisions.');
  for (const example of examples) assert(plain(example) && Array.isArray(example.features) && example.features.length > 0
    && example.features.length <= TASK_LIMITS.files && Number.isInteger(example.chosen) && example.chosen >= 0
    && example.chosen < example.features.length && example.features.every(row => Array.isArray(row)
      && row.length === FEATURE_NAMES.length && row.every(value => Number.isFinite(value) && Math.abs(value) <= 10)), 'Invalid demonstration features or chosen candidate.');
}
export function evaluateDemonstrations(policy, examples) {
  validateTaskPolicy(policy); validateExamples(examples); let loss = 0, correct = 0;
  for (const example of examples) {
    const scores = example.features.map(f => dot(policy.weights, f)), probs = probabilities(scores);
    loss -= Math.log(Math.max(1e-15, probs[example.chosen]));
    if (scores.indexOf(Math.max(...scores)) === example.chosen) correct++;
  }
  return { loss: loss / examples.length, accuracy: correct / examples.length, count: examples.length };
}
export function trainEpoch(policy, examples, { learningRate = .08, seed = 17 } = {}) {
  validateTaskPolicy(policy); validateExamples(examples);
  assert(Number.isFinite(learningRate) && learningRate > 0 && learningRate <= 1 && Number.isSafeInteger(seed), 'Invalid training rate or seed.');
  const random = rng(seed), order = examples.map((_, i) => i);
  for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
  for (const index of order) {
    const example = examples[index], probs = probabilities(example.features.map(f => dot(policy.weights, f)));
    for (let feature = 0; feature < FEATURE_NAMES.length; feature++) {
      const expected = example.features.reduce((sum, row, i) => sum + probs[i] * row[feature], 0);
      policy.weights[feature] += learningRate * (example.features[example.chosen][feature] - expected);
    }
  }
  policy.trainedEpochs++; validateTaskPolicy(policy);
  return evaluateDemonstrations(policy, examples);
}

// Canonical JSON and SHA-256 make portable files self-contained and independently
// verifiable in a future host, without Node crypto or asynchronous browser APIs.
export function canonicalJSON(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (plain(value)) return `{${Object.keys(value).sort(comparePath).map(key => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`).join(',')}}`;
  assert(value === null || ['string', 'boolean', 'number'].includes(typeof value), 'Artifact contains a non-JSON value.');
  if (typeof value === 'number') assert(Number.isFinite(value), 'Artifact contains a non-finite number.');
  return JSON.stringify(value);
}
export function sha256(text) {
  const bytes = new TextEncoder().encode(text), words = [], length = bytes.length * 8;
  for (let i = 0; i < bytes.length; i++) words[i >> 2] = (words[i >> 2] || 0) | bytes[i] << (24 - (i % 4) * 8);
  words[length >> 5] = (words[length >> 5] || 0) | 0x80 << (24 - length % 32);
  const end = (((length + 64) >> 9) << 4) + 15; words[end - 1] = Math.floor(length / 4294967296); words[end] = length >>> 0;
  const constants = [], initial = [];
  for (let n = 2; constants.length < 64; n++) {
    let prime = true; for (let d = 2; d * d <= n; d++) if (n % d === 0) { prime = false; break; }
    if (prime) { if (initial.length < 8) initial.push((Math.sqrt(n) % 1 * 4294967296) | 0); constants.push((Math.cbrt(n) % 1 * 4294967296) | 0); }
  }
  const hash = initial.slice(), rotate = (x, n) => x >>> n | x << (32 - n);
  for (let offset = 0; offset < words.length; offset += 16) {
    const w = Array(64).fill(0); for (let i = 0; i < 16; i++) w[i] = words[offset + i] | 0;
    for (let i = 16; i < 64; i++) { const a = w[i - 15], b = w[i - 2];
      w[i] = ((rotate(a, 7) ^ rotate(a, 18) ^ a >>> 3) + w[i - 16] + (rotate(b, 17) ^ rotate(b, 19) ^ b >>> 10) + w[i - 7]) | 0; }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let i = 0; i < 64; i++) {
      const first = (h + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) + (e & f ^ ~e & g) + constants[i] + w[i]) | 0;
      const second = ((rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + (a & b ^ a & c ^ b & c)) | 0;
      h = g; g = f; f = e; e = (d + first) | 0; d = c; c = b; b = a; a = (first + second) | 0;
    }
    [a, b, c, d, e, f, g, h].forEach((value, i) => { hash[i] = (hash[i] + value) | 0; });
  }
  return hash.map(value => (value >>> 0).toString(16).padStart(8, '0')).join('');
}
export const taskPolicyHash = policy => { validateTaskPolicy(policy); return sha256(canonicalJSON(policy)); };
export const datasetDigest = dataset => sha256(canonicalJSON(dataset));

export function createArtifact({ id, name, revision = 1, policy, training = {}, evaluation = {}, task = {} }) {
  validateTaskPolicy(policy);
  const select = (record, keys) => Object.fromEntries(keys.filter(key => record[key] !== undefined).map(key => [key, clone(record[key])]));
  const trainingRecord = select(training, ['epochs', 'examples', 'humanExamples', 'seed', 'learningRate', 'datasetId', 'datasetDigest',
    'splitDigest', 'trainingWorkspaceIds', 'startedAt', 'completedAt', 'policyHash', 'source', 'trainerVersion', 'codeVersion', 'lineage', 'unknownPriorTraining']);
  const evaluationRecord = select(evaluation, ['datasetDigest', 'splitDigest', 'testWorkspaceIds', 'policyHash', 'completedAt']);
  for (const controller of ['learned', 'filename', 'random']) if (evaluation[controller]?.summary) {
    evaluationRecord[controller] = { summary: select(evaluation[controller].summary,
      ['count', 'coverage', 'precision', 'meanReads', 'completeRate', 'allRelevantFoundRate', 'meanErrors']) };
  }
  if (evaluation.summary) evaluationRecord.summary = select(evaluation.summary,
    ['count', 'coverage', 'precision', 'meanReads', 'completeRate', 'allRelevantFoundRate', 'meanErrors']);
  const artifact = { format: 'xo-fly', schemaVersion: 1,
    fly: { id, name, revision }, taskFamily: 'report-scout-v1', policy: clone(policy),
    contracts: { runtime: TASK_RUNTIME_VERSION, observations: 'file-metadata-v1', actions: 'inspect-file-v1',
      features: FEATURE_VERSION, featureNames: [...FEATURE_NAMES], task: 'extract-fields-v1',
      skills: { list: 'relative-file-list-v1', json: 'json-records-v1', csv: 'csv-records-v1', report: 'cited-record-report-v1' },
      inference: 'greedy-score; ties use sorted relative path; no weight updates' },
    task: validateTask(task), requestedCapabilities: ['workspace.list', 'workspace.read-json-csv', 'report.produce'],
    training: trainingRecord, evaluation: evaluationRecord,
    scope: 'A learned file-choice policy. Parsing, extraction and report generation require the preinstalled compatible host skills. No language model, runtime code, or credentials are included.' };
  artifact.integrity = { algorithm: 'SHA-256', digest: sha256(canonicalJSON(artifact)) };
  return validateArtifact(artifact);
}
export function validateArtifact(raw) {
  exactKeys(raw, ['format', 'schemaVersion', 'fly', 'taskFamily', 'policy', 'contracts', 'task', 'requestedCapabilities',
    'training', 'evaluation', 'scope', 'integrity'], 'artifact');
  assert(plain(raw) && raw.format === 'xo-fly' && raw.schemaVersion === 1 && raw.taskFamily === 'report-scout-v1', 'Unsupported portable Fly artifact.');
  exactKeys(raw.fly, ['id', 'name', 'revision'], 'fly identity');
  assert(plain(raw.fly) && typeof raw.fly.id === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/.test(raw.fly.id)
    && typeof raw.fly.name === 'string' && raw.fly.name.trim().length > 0 && raw.fly.name.length <= 80
    && Number.isInteger(raw.fly.revision) && raw.fly.revision > 0, 'Invalid portable Fly identity.');
  validateTaskPolicy(raw.policy); validateTask(raw.task);
  exactKeys(raw.task, ['query', 'fields', 'maxReads'], 'task');
  exactKeys(raw.contracts, ['runtime', 'observations', 'actions', 'features', 'featureNames', 'task', 'skills', 'inference'], 'runtime contract');
  assert(raw.contracts?.runtime === TASK_RUNTIME_VERSION && raw.contracts?.observations === 'file-metadata-v1'
    && raw.contracts?.actions === 'inspect-file-v1' && raw.contracts?.features === FEATURE_VERSION
    && raw.contracts?.task === 'extract-fields-v1'
    && canonicalJSON(raw.contracts.featureNames) === canonicalJSON(FEATURE_NAMES), 'Incompatible task runtime contract.');
  assert(canonicalJSON(raw.contracts.skills) === canonicalJSON({ list: 'relative-file-list-v1', json: 'json-records-v1',
    csv: 'csv-records-v1', report: 'cited-record-report-v1' }), 'Incompatible or unknown host skill.');
  assert(raw.contracts.inference === 'greedy-score; ties use sorted relative path; no weight updates', 'Unsupported inference semantics.');
  assert(canonicalJSON(raw.requestedCapabilities) === canonicalJSON(['workspace.list', 'workspace.read-json-csv', 'report.produce']), 'Unsupported requested capability.');
  assert(plain(raw.training) && plain(raw.evaluation), 'Training and evaluation provenance must be JSON objects.');
  exactKeys(raw.training, ['epochs', 'examples', 'humanExamples', 'seed', 'learningRate', 'datasetId', 'datasetDigest', 'splitDigest',
    'trainingWorkspaceIds', 'startedAt', 'completedAt', 'policyHash', 'source', 'trainerVersion', 'codeVersion', 'lineage', 'unknownPriorTraining'], 'training provenance');
  exactKeys(raw.evaluation, ['datasetDigest', 'splitDigest', 'testWorkspaceIds', 'policyHash', 'completedAt', 'learned', 'filename', 'random', 'summary'], 'evaluation provenance');
  for (const [key, value] of Object.entries(raw.training)) {
    if (['epochs', 'examples', 'humanExamples'].includes(key)) assert(Number.isSafeInteger(value) && value >= 0, `Invalid training ${key}.`);
    else if (key === 'seed') assert(Number.isSafeInteger(value), 'Invalid training seed.');
    else if (key === 'learningRate') assert(Number.isFinite(value) && value > 0 && value <= 1, 'Invalid training rate.');
    else if (key === 'trainingWorkspaceIds') assert(Array.isArray(value) && value.length <= TASK_LIMITS.workspaces && value.every(id => typeof id === 'string' && id.length <= 100), 'Invalid training workspace IDs.');
    else if (key === 'unknownPriorTraining') assert(typeof value === 'boolean', 'Invalid prior training flag.');
    else if (key === 'lineage') {
      assert(Array.isArray(value) && value.length <= 100, 'Training lineage exceeds 100 jobs.');
      for (const entry of value) {
        exactKeys(entry, ['datasetDigest', 'policyHash', 'epochs', 'seed', 'learningRate', 'workspaceDigests'], 'training lineage');
        assert(typeof entry.datasetDigest === 'string' && /^[a-f0-9]{64}$/.test(entry.datasetDigest)
          && typeof entry.policyHash === 'string' && /^[a-f0-9]{64}$/.test(entry.policyHash), 'Invalid lineage digest.');
        assert(Number.isSafeInteger(entry.epochs) && entry.epochs > 0 && Number.isSafeInteger(entry.seed)
          && Number.isFinite(entry.learningRate) && entry.learningRate > 0 && entry.learningRate <= 1, 'Invalid lineage training configuration.');
        assert(Array.isArray(entry.workspaceDigests) && entry.workspaceDigests.length > 0 && entry.workspaceDigests.length <= TASK_LIMITS.workspaces
          && entry.workspaceDigests.every(digest => typeof digest === 'string' && /^[a-f0-9]{64}$/.test(digest)), 'Invalid lineage workspace digests.');
      }
    }
    else assert(typeof value === 'string' && value.length <= 200, `Invalid training ${key}.`);
  }
  const validateSummary = summary => {
    exactKeys(summary, ['count', 'coverage', 'precision', 'meanReads', 'completeRate', 'allRelevantFoundRate', 'meanErrors'], 'evaluation summary');
    for (const [key, value] of Object.entries(summary)) assert(Number.isFinite(value) && value >= 0
      && (!['coverage', 'precision', 'completeRate', 'allRelevantFoundRate'].includes(key) || value <= 1), `Invalid evaluation ${key}.`);
  };
  for (const [key, value] of Object.entries(raw.evaluation)) {
    if (['learned', 'filename', 'random'].includes(key)) { exactKeys(value, ['summary'], 'evaluation condition'); validateSummary(value.summary); }
    else if (key === 'summary') validateSummary(value);
    else if (key === 'testWorkspaceIds') assert(Array.isArray(value) && value.length <= TASK_LIMITS.workspaces && value.every(id => typeof id === 'string' && id.length <= 100), 'Invalid test workspace IDs.');
    else assert(typeof value === 'string' && value.length <= 200, `Invalid evaluation ${key}.`);
  }
  for (const record of [raw.training, raw.evaluation]) if (record.policyHash !== undefined) assert(record.policyHash === taskPolicyHash(raw.policy), 'Provenance belongs to a different policy.');
  assert(typeof raw.scope === 'string' && raw.scope.length <= 500, 'Invalid artifact scope description.');
  exactKeys(raw.integrity, ['algorithm', 'digest'], 'integrity');
  const encoded = canonicalJSON(raw); assert(byteLength(encoded) <= 2000000, 'Portable artifact exceeds 2 MB.');
  assert(raw.integrity?.algorithm === 'SHA-256' && /^[a-f0-9]{64}$/.test(raw.integrity.digest), 'Missing artifact SHA-256 digest.');
  const payload = { ...raw }; delete payload.integrity;
  assert(sha256(canonicalJSON(payload)) === raw.integrity.digest, 'Artifact digest mismatch: data was changed after export.');
  return clone(raw);
}
