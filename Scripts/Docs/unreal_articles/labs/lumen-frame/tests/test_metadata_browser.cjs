#!/usr/bin/env node
/*
 * Focused browser metadata read tests for PackageStore.
 *
 * The package is synthetic and served through the injected fetchFn, so these
 * cases do not depend on a built capture. They cover raw metadata, gzip
 * metadata, an HTTP-decoded body at a .json.gz URL, a missing
 * DecompressionStream, corrupt gzip that must not become an empty dataset,
 * and selected-package path resolution for index.files, summary.record and
 * the footer download links. Payload chunks are unchanged and remain covered
 * by test_read_path.cjs.
 */
'use strict';

const assert = require('assert');
const path = require('path');
const zlib = require('zlib');
const locations = require('./test_paths.cjs');
const explorerModule = require(path.join(locations.assets, 'frame-explorer.js'));

// load() reads these browser globals; define them only after requiring the
// module so its DOM init block is not entered in Node.
global.window = { location: { search: '' } };
global.document = { baseURI: 'https://example.test/Unreal/frame-explorer.html' };

const BASE = 'https://example.test/Unreal/assets/lumen-frame/';

const SUMMARY = {
  format: 'unreal-frame-summary',
  version: 1,
  total: 1,
  items: [{
    id: 'resource-1',
    name: 'Fixture.Resource',
    kind: 'texture',
    payloadCount: 0,
    searchText: 'fixture resource',
    record: 'records/resource/resource-1.json'
  }]
};

function summaryFor(recordPath) {
  return Object.assign({}, SUMMARY, {
    items: [Object.assign({}, SUMMARY.items[0], { record: recordPath })]
  });
}

const PASSES = { format: 'unreal-frame-summary', version: 1, total: 0, items: [] };
const COVERAGE = { gaps: [], counts: {}, complete: true, statement: 'synthetic metadata test' };
const SCHEMA = { status: 'joined', resourceSchemas: [], parameterSchemas: [], sourceRefs: {} };
const CAPTURE = { infos: {}, consoleVariables: [], nativeFiles: [] };
const STRUCTURES = { byPointer: {} };
const NATIVE_INPUTS = { items: [], count: 0 };
const RECORD = { id: 'resource-1', payloads: [], synthetic: true };

function json(value) {
  return Buffer.from(JSON.stringify(value), 'utf8');
}

function gzip(value) {
  return zlib.gzipSync(json(value), { mtime: 0 });
}

function resetState() {
  explorerModule.state.index = null;
  explorerModule.state.packageBase = null;
  explorerModule.state.resources = null;
  explorerModule.state.passes = null;
  explorerModule.state.coverage = null;
  explorerModule.state.schema = null;
  explorerModule.state.capture = null;
  explorerModule.state.structures = null;
  explorerModule.state.nativeInputs = null;
  explorerModule.state.resourceById = new Map();
  explorerModule.state.passById = new Map();
  explorerModule.state.recordCache = new Map();
  explorerModule.state.sourceRefMap = new Map();
}

function makeServer(files) {
  const calls = [];
  async function fetchFn(url) {
    const pathname = new URL(url).pathname.replace(/^\/Unreal\/assets\/lumen-frame\//, '');
    calls.push(pathname);
    const body = files[pathname];
    if (body === undefined) return new Response('missing ' + pathname, { status: 404 });
    if (body instanceof Error) throw body;
    return new Response(body, { status: 200 });
  }
  return { fetchFn: fetchFn, calls: calls, files: files };
}

function makeIndex(files, schemaPath) {
  return {
    format: 'unreal-frame-explorer-package',
    version: 1,
    adapter: 'synthetic',
    generatedUtc: '2026-01-01T00:00:00+00:00',
    files: files,
    schema: { status: 'joined', path: schemaPath },
    chunks: [],
    counts: { resources: 1, passes: 0, payloadFiles: 0, gapCount: 0 }
  };
}

function makeStore(server, overrides) {
  return new explorerModule.PackageStore(Object.assign({
    baseUrl: BASE,
    fetchFn: server.fetchFn
  }, overrides || {}));
}

function assertPackageLoaded(store, expectedRecord) {
  assert.strictEqual(explorerModule.state.index.adapter, 'synthetic');
  assert.strictEqual(explorerModule.state.resources.items.length, 1);
  assert.strictEqual(explorerModule.state.resources.items[0].record, expectedRecord);
  assert.strictEqual(explorerModule.state.resources.items[0].name, 'Fixture.Resource');
  assert.deepStrictEqual(explorerModule.state.passes.items, []);
  assert.strictEqual(explorerModule.state.coverage.complete, true);
  assert.deepStrictEqual(explorerModule.state.schema.resourceSchemas, []);
  assert.strictEqual(explorerModule.state.capture.infos && Object.keys(explorerModule.state.capture.infos).length, 0);
  assert.deepStrictEqual(explorerModule.state.structures.byPointer, {});
  assert.deepStrictEqual(explorerModule.state.nativeInputs.items, []);
  assert.strictEqual(store.baseUrl, BASE);
}

async function testRawMetadataAndFooterResolution() {
  resetState();
  const files = {
    'index.json': json(makeIndex({
      index: 'index.json',
      resources: 'meta/resources.json',
      passes: 'meta/passes.json',
      coverage: 'meta/coverage.json',
      schema: 'meta/schema.json',
      structures: 'meta/structures.json',
      capture: 'meta/capture.json',
      inventory: 'meta/inventory.json',
      nativeInputs: 'meta/native-inputs.json',
      chunks: 'chunks/',
      records: 'meta/records/',
      native: 'native/'
    }, 'meta/schema.json')),
    'meta/resources.json': json(summaryFor('meta/records/resource/resource-1.json')),
    'meta/passes.json': json(PASSES),
    'meta/coverage.json': json(COVERAGE),
    'meta/schema.json': json(SCHEMA),
    'meta/structures.json': json(STRUCTURES),
    'meta/capture.json': json(CAPTURE),
    'meta/native-inputs.json': json(NATIVE_INPUTS),
    'meta/records/resource/resource-1.json': json(RECORD)
  };
  const server = makeServer(files);
  const store = makeStore(server);
  await store.load();

  assertPackageLoaded(store, 'meta/records/resource/resource-1.json');
  assert(server.calls.includes('meta/resources.json'), 'index.files resources path must be fetched');
  assert(server.calls.includes('meta/schema.json'), 'index.schema.path must be fetched');
  assert(!server.calls.includes('resources.json'), 'logical resources.json must not be fetched when index.files names another path');

  const record = await store.record(explorerModule.state.resources.items[0].record);
  assert.strictEqual(record.id, 'resource-1');
  const recordFetches = server.calls.filter(function (item) { return item === 'meta/records/resource/resource-1.json'; }).length;
  assert.strictEqual(recordFetches, 1, 'summary.record actual path is fetched once');
  await store.record(explorerModule.state.resources.items[0].record);
  assert.strictEqual(server.calls.filter(function (item) { return item === 'meta/records/resource/resource-1.json'; }).length, 1,
    'record cache uses the resolved actual path');

  assert.strictEqual(explorerModule.packageUrl('index.json'), BASE + 'index.json');
  assert.strictEqual(explorerModule.packageUrl('inventory.json'), BASE + 'meta/inventory.json');
  assert.strictEqual(explorerModule.packageUrl('coverage.json'), BASE + 'meta/coverage.json');
  assert.strictEqual(explorerModule.packageUrl('capture.json'), BASE + 'meta/capture.json');
  assert.strictEqual(explorerModule.packageUrl('native/Base/ResourceDescs.json'), BASE + 'native/Base/ResourceDescs.json',
    'native downloads stay selected-package relative');
  assert.strictEqual(explorerModule.packageUrl('chunks/' + 'a'.repeat(64) + '.gz'), BASE + 'chunks/' + 'a'.repeat(64) + '.gz',
    'chunk downloads stay selected-package relative');
  console.log('ok - raw index.files paths, summary.record and footer/native path resolution');
}

async function testGzipMetadata() {
  resetState();
  const files = {
    'index.json': json(makeIndex({
      index: 'index.json',
      resources: 'generated/resources.json.gz',
      passes: 'generated/passes.json.gz',
      coverage: 'generated/coverage.json.gz',
      schema: 'generated/schema.json.gz',
      structures: 'generated/structures.json.gz',
      capture: 'generated/capture.json.gz',
      inventory: 'generated/inventory.json.gz',
      nativeInputs: 'generated/native-inputs.json.gz',
      chunks: 'chunks/',
      records: 'records/',
      native: 'native/'
    }, 'generated/schema.json.gz')),
    'generated/resources.json.gz': gzip(summaryFor('records/resource/resource-1.json.gz')),
    'generated/passes.json.gz': gzip(PASSES),
    'generated/coverage.json.gz': gzip(COVERAGE),
    'generated/schema.json.gz': gzip(SCHEMA),
    'generated/structures.json.gz': gzip(STRUCTURES),
    'generated/capture.json.gz': gzip(CAPTURE),
    'generated/native-inputs.json.gz': gzip(NATIVE_INPUTS),
    'records/resource/resource-1.json.gz': gzip(RECORD)
  };
  const server = makeServer(files);
  const store = makeStore(server);
  await store.load();

  assertPackageLoaded(store, 'records/resource/resource-1.json.gz');
  assert(server.calls.includes('generated/resources.json.gz'));
  assert(server.calls.includes('generated/schema.json.gz'));
  const record = await store.record('records/resource/resource-1.json.gz');
  assert.strictEqual(record.synthetic, true);
  assert.strictEqual(explorerModule.packageUrl('inventory.json'), BASE + 'generated/inventory.json.gz',
    'footer logical name follows index.files to the compressed actual path');
  console.log('ok - gzip metadata detected by body magic and inflated exactly once');
}

async function testAlreadyHttpDecodedBody() {
  resetState();
  // The server/browser already removed Content-Encoding gzip, so a .json.gz
  // URL returns plain JSON. No DecompressionStream is available: the reader
  // must parse the decoded body rather than inflating it again.
  const files = {
    'index.json': json(makeIndex({
      index: 'index.json',
      resources: 'generated/resources.json.gz',
      passes: 'generated/passes.json.gz',
      coverage: 'generated/coverage.json.gz',
      schema: 'generated/schema.json.gz',
      structures: 'generated/structures.json.gz',
      capture: 'generated/capture.json.gz',
      inventory: 'generated/inventory.json.gz',
      nativeInputs: 'generated/native-inputs.json.gz',
      chunks: 'chunks/',
      records: 'records/',
      native: 'native/'
    }, 'generated/schema.json.gz')),
    'generated/resources.json.gz': json(summaryFor('records/resource/resource-1.json.gz')),
    'generated/passes.json.gz': json(PASSES),
    'generated/coverage.json.gz': json(COVERAGE),
    'generated/schema.json.gz': json(SCHEMA),
    'generated/structures.json.gz': json(STRUCTURES),
    'generated/capture.json.gz': json(CAPTURE),
    'generated/native-inputs.json.gz': json(NATIVE_INPUTS),
    'records/resource/resource-1.json.gz': json(RECORD)
  };
  const server = makeServer(files);
  const store = makeStore(server, { DecompressionStream: null, gzipCtor: null });
  await store.load();

  assertPackageLoaded(store, 'records/resource/resource-1.json.gz');
  const record = await store.record('records/resource/resource-1.json.gz');
  assert.strictEqual(record.id, 'resource-1');
  console.log('ok - already HTTP-decoded body at a .json.gz path parses without DecompressionStream');
}

async function testMissingDecompressionStream() {
  resetState();
  const index = makeIndex({
    index: 'index.json',
    resources: 'generated/resources.json.gz',
    passes: 'generated/passes.json.gz',
    coverage: 'generated/coverage.json.gz',
    schema: 'generated/schema.json.gz',
    structures: 'generated/structures.json.gz',
    capture: 'generated/capture.json.gz',
    inventory: 'generated/inventory.json.gz',
    nativeInputs: 'generated/native-inputs.json.gz'
  }, 'generated/schema.json.gz');
  const files = {
    'index.json': json(index),
    'generated/resources.json.gz': gzip(summaryFor('records/resource/resource-1.json.gz')),
    'generated/passes.json.gz': json(PASSES),
    'generated/coverage.json.gz': json(COVERAGE),
    'generated/schema.json.gz': json(SCHEMA),
    'generated/structures.json.gz': json(STRUCTURES),
    'generated/capture.json.gz': json(CAPTURE),
    'generated/native-inputs.json.gz': json(NATIVE_INPUTS)
  };
  const server = makeServer(files);
  const store = makeStore(server, { DecompressionStream: null, gzipCtor: null });
  explorerModule.state.index = index;
  await assert.rejects(function () { return store.fetchJson('resources.json'); },
    /no DecompressionStream/);
  await assert.rejects(function () { return store.load(); },
    /no DecompressionStream/);
  assert.strictEqual(explorerModule.state.resources, null, 'missing decompressor must not create an empty resources dataset');
  console.log('ok - missing DecompressionStream rejects compressed metadata instead of substituting data');
}

async function testCorruptGzipIsAnError() {
  resetState();
  const corrupt = Buffer.from([0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0xde, 0xad, 0xbe, 0xef]);
  const index = makeIndex({
    index: 'index.json',
    resources: 'generated/resources.json.gz',
    passes: 'generated/passes.json.gz',
    coverage: 'generated/coverage.json.gz',
    schema: 'generated/schema.json.gz',
    structures: 'generated/structures.json.gz',
    capture: 'generated/capture.json.gz',
    inventory: 'generated/inventory.json.gz',
    nativeInputs: 'generated/native-inputs.json.gz'
  }, 'generated/schema.json.gz');
  const files = {
    'index.json': json(index),
    'generated/resources.json.gz': corrupt,
    'generated/passes.json.gz': json(PASSES),
    'generated/coverage.json.gz': json(COVERAGE),
    'generated/schema.json.gz': json(SCHEMA),
    'generated/structures.json.gz': json(STRUCTURES),
    'generated/capture.json.gz': json(CAPTURE),
    'generated/native-inputs.json.gz': json(NATIVE_INPUTS)
  };
  const server = makeServer(files);
  const store = makeStore(server);
  explorerModule.state.index = index;
  await assert.rejects(function () { return store.fetchJson('resources.json'); }, /Could not decompress metadata/);
  await assert.rejects(function () { return store.tryFetchJson('resources.json', { items: [], total: 0 }); },
    /Could not decompress metadata/, 'corruption must not take the absent-file fallback');
  await assert.rejects(function () { return store.load(); }, /Could not decompress metadata/);
  assert.strictEqual(explorerModule.state.resources, null, 'corrupt gzip must not be hidden as an empty resources dataset');

  // A .gz path whose decoded body is not JSON must also fail loudly.
  const invalidFiles = Object.assign({}, files);
  invalidFiles['generated/resources.json.gz'] = Buffer.from('not json', 'utf8');
  const invalidServer = makeServer(invalidFiles);
  const invalidStore = makeStore(invalidServer);
  explorerModule.state.index = index;
  await assert.rejects(function () { return invalidStore.fetchJson('resources.json'); }, /Invalid JSON/);
  console.log('ok - corrupt gzip and invalid JSON surface as errors, never as empty useful datasets');
}

async function testMissingDeclaredMetadata() {
  for (const entry of [
    { files: { resources: 'resources.json.gz' }, schema: {}, logical: 'resources.json' },
    { files: { resources: 'resources.json' }, schema: {}, logical: 'resources.json' },
    { files: {}, schema: { path: 'schema.json.gz' }, logical: 'schema.json' },
    { files: [{ logical: 'coverage', path: 'coverage.json.gz' }], schema: {}, logical: 'coverage.json' }
  ]) {
    resetState();
    const store = makeStore(makeServer({}));
    explorerModule.state.index = entry;
    await assert.rejects(() => store.tryFetchJson(entry.logical, { silentlyEmpty: true }), /HTTP 404/,
      'a file promised by the manifest must not fall back to an empty dataset');
  }
  resetState();
  explorerModule.state.index = { files: {}, schema: {} };
  const store = makeStore(makeServer({}));
  assert.deepStrictEqual(await store.tryFetchJson('native-inputs.json', { items: [] }), { items: [] },
    'a legacy package can omit an undeclared optional metadata file');
  console.log('ok - missing declared metadata fails; legacy undeclared optional metadata can fall back');
}

async function main() {
  await testRawMetadataAndFooterResolution();
  await testGzipMetadata();
  await testAlreadyHttpDecodedBody();
  await testMissingDecompressionStream();
  await testCorruptGzipIsAnError();
  await testMissingDeclaredMetadata();
  console.log('metadata browser tests passed');
}

main().catch(function (error) {
  console.error('not ok - ' + (error && error.stack ? error.stack : error));
  process.exit(1);
});
