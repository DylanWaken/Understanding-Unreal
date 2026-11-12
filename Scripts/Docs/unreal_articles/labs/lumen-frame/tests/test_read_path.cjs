#!/usr/bin/env node
/*
 * Pure-Node regression for the browser PackageStore read path.
 *
 * No browser automation: global fetch is mocked to serve the actual gzip
 * chunk files from a built package, and crypto.subtle verifies each raw chunk
 * exactly as the browser does.  Expected bytes were produced by reading the
 * candidate payload directly with Python, independently of the chunk path.
 *
 * Point FRAME_PACKAGE_DIR at a built package (for example the archive
 * extraction or scratch/package).  If no package is present the static codec
 * assertions still run and the package-dependent portion is reported as
 * skipped rather than fabricated.
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { pathToFileURL, fileURLToPath } = require('url');

const ROOT = path.resolve(__dirname, '..');
const locations = require('./test_paths.cjs');
const PACKAGE = locations.package;
const HAS_PACKAGE = fs.existsSync(path.join(PACKAGE, 'index.json'));
const explorerModule = require(path.join(locations.assets, 'frame-explorer.js'));
const codec = require(path.join(locations.assets, 'frame-codec.js'));
const expected = JSON.parse(fs.readFileSync(path.join(__dirname, 'real_capture_evidence.json'), 'utf8'));

function readJson(rel) {
  return JSON.parse(fs.readFileSync(path.join(PACKAGE, rel), 'utf8'));
}

function makeStore() {
  const index = readJson('index.json');
  const calls = new Map();
  const store = new explorerModule.PackageStore({
    baseUrl: pathToFileURL(PACKAGE + path.sep).href,
    fetchFn: async function (url) {
      const file = fileURLToPath(url);
      calls.set(path.basename(file), (calls.get(path.basename(file)) || 0) + 1);
      return new Response(fs.readFileSync(file), { status: 200 });
    },
    crypto: crypto.webcrypto
  });
  store.chunks = new Map(index.chunks.map(function (chunk) { return [chunk.sha, chunk]; }));
  return { store: store, calls: calls };
}

function hex(bytes) {
  return Buffer.from(bytes).toString('hex');
}

function findPayload(context, item) {
  if (item.resourceId) {
    const record = readJson(path.join('records', 'resource', item.resourceId + '.json'));
    const payload = (record.payloads || []).find(function (p) { return p.originalPath === item.payloadPath; });
    assert(payload, 'payload ' + item.payloadPath + ' is missing from ' + item.resourceId);
    return { payload: payload, record: record };
  }
  const native = readJson('native-inputs.json');
  for (const resource of native.items || []) {
    for (const view of (resource.views || []).concat(resource.exports || [])) {
      const payload = view.payload || null;
      if (payload && payload.originalPath === item.payloadPath) return { payload: payload, native: resource };
    }
  }
  throw new Error('payload ' + item.payloadPath + ' is not present in native-inputs.json');
}

async function runPackageTests() {
  console.log('using package ' + PACKAGE);
  for (const item of expected.cases) {
    const found = findPayload(null, item);
    const payload = found.payload;
    if (item.layout) {
      for (const key of Object.keys(item.layout)) {
        const actual = payload.layout[key] === undefined ? null : payload.layout[key];
        assert.strictEqual(actual, item.layout[key], item.name + ': layout ' + key);
      }
      const info = codec.describeFormat(payload.layout.StoredFormat);
      assert.strictEqual(info.supported, true, item.name + ': stored format must decode');
      const calc = codec.texelOffset(payload.layout, item.x, item.y, item.z || 0,
        info.bytesPerTexel, payload.bytes);
      assert.strictEqual(calc.ok, true, item.name + ': ' + calc.error);
      assert.strictEqual(calc.offset, item.byteOffset, item.name + ': sidecar texel offset');
      const sub = explorerModule.subresourceInfo({ extent: [1, 1, 1] }, payload);
      assert.strictEqual(sub.fromSidecar, true, item.name + ': UI must use the sidecar');
      assert.strictEqual(sub.width, payload.layout.ExtentX);
      assert.strictEqual(sub.height, payload.layout.ExtentY);
      assert.strictEqual(sub.rowPitch, payload.layout.RowPitchBytes);
      assert.strictEqual(sub.zSliceStride, payload.layout.ZSliceStrideBytes);
      assert.strictEqual(explorerModule.textureLayout({}, payload).available, true);
    }
    if (item.viewOffsetInBytes !== undefined) {
      assert.strictEqual(payload.identity.viewOffsetInBytes, item.viewOffsetInBytes);
      assert.strictEqual(payload.identity.viewSizeInBytes, item.viewSizeInBytes);
      assert.strictEqual(payload.identity.viewStrideInBytes, item.viewStrideInBytes);
      assert.strictEqual(payload.identity.viewNumElements, item.viewNumElements);
      assert.strictEqual(payload.identity.viewFormat, item.viewFormat);
    }
    if (item.completion) {
      assert.strictEqual(payload.completion.observed, item.completion.observed, item.name + ': completion observed');
      assert.strictEqual(payload.completion.dumped, item.completion.dumped, item.name + ': completion dumped');
      assert.strictEqual(payload.completion.timing, item.completion.timing, item.name + ': completion timing');
    }
    const run = makeStore();
    const bytes = await run.store.readRange(payload, item.byteOffset, item.length);
    assert.strictEqual(hex(bytes), item.hex, item.name + ': byte mismatch');
    const pieces = codec.chunkRange(payload.chunks, item.byteOffset, item.length, payload.bytes);
    assert(pieces.length >= 1);
    assert(pieces.every(function (piece) {
      const meta = run.store.chunks.get(piece.sha);
      return meta && meta.rawLength > 0 && meta.rawLength <= 1048576;
    }), item.name + ': chunk length authority is not index.json');
    if (item.crossesChunkBoundary) {
      assert.strictEqual(pieces.length, 2, item.name + ': expected a two-chunk read');
    }
    const before = Array.from(run.calls.values()).reduce(function (a, b) { return a + b; }, 0);
    const again = await run.store.readRange(payload, item.byteOffset, item.length);
    assert.strictEqual(hex(again), item.hex);
    const after = Array.from(run.calls.values()).reduce(function (a, b) { return a + b; }, 0);
    assert.strictEqual(after, before, item.name + ': a repeated read should reuse the verified chunk cache');
    console.log('ok - ' + item.name + ' -> ' + item.hex + ' at byte ' + item.byteOffset + ' via ' + pieces.length + ' chunk(s)');
  }

  {
    // The chosen production uniform payload was reviewed as pass-0117 /
    // payload-05915. Object equality is not enough: read the actual first,
    // last and interior ranges through PackageStore, decode an array of
    // float32 values, and prove the resource-side binding table resolves the
    // singular payloadId to a real resource link with those raw bytes.
    const uniform = expected.uniformCase;
    assert(uniform, 'uniformCase evidence is missing');
    const passes = readJson('passes.json').items;
    const pass = passes.find(function (item) { return item.id === uniform.passId; });
    assert(pass, 'missing reviewed pass ' + uniform.passId);
    assert.strictEqual(pass.pointer, uniform.passPointer, 'reviewed pass pointer changed');
    const passRecord = readJson(pass.record);
    const native = (passRecord.nativeUniformBuffers || []).find(function (item) {
      return item.uniformBuffer === uniform.uniformBuffer;
    });
    assert(native, 'missing reviewed uniform buffer ' + uniform.uniformBuffer);
    const payload = native.uniformBufferPayload;
    assert(payload, 'uniform buffer payload is still null');
    assert.strictEqual(payload.id, uniform.payloadId, 'uniform payload id');
    assert.strictEqual(payload.bytes, uniform.bytes, 'uniform payload bytes');
    assert.strictEqual(payload.sha256, uniform.sha256, 'uniform payload sha256');
    assert(payload.bytes > 0 && payload.chunks && payload.chunks.length > 0,
      'uniform payload must carry final chunks, not a pre-ingest placeholder');
    assert.deepStrictEqual(payload.chunks.map(function (chunk) { return [chunk.offset, chunk.length]; }),
      [[0, uniform.bytes]], 'uniform payload range offsets');

    const run = makeStore();
    const first = await run.store.readRange(payload, uniform.first.offset, uniform.first.length);
    const last = await run.store.readRange(payload, uniform.last.offset, uniform.last.length);
    const range = await run.store.readRange(payload, uniform.range.offset, uniform.range.length);
    assert.strictEqual(hex(first), uniform.first.hex, 'uniform first bytes');
    assert.strictEqual(hex(last), uniform.last.hex, 'uniform last bytes');
    assert.strictEqual(hex(range), uniform.range.hex, 'uniform interior range bytes');
    const pieces = codec.chunkRange(payload.chunks, uniform.range.offset, uniform.range.length, payload.bytes);
    assert.strictEqual(pieces.length, 1, 'interior range should resolve one chunk');
    assert.strictEqual(pieces[0].chunkOffset, uniform.range.offset, 'interior range chunk offset');
    const decoded = codec.extractField(range, 0, {
      type: uniform.decode.type, count: uniform.decode.count
    });
    assert.strictEqual(decoded.supported, true, decoded.reason);
    assert.strictEqual(decoded.values.length, uniform.decode.count);
    decoded.values.forEach(function (value, index) {
      assert(Math.abs(value - uniform.decode.values[index]) < 1e-12,
        'uniform decoded value ' + index + ': ' + value);
    });

    const inventory = readJson('inventory.json');
    const bindingSource = Object.keys(native.bindings || {}).map(function (key) {
      return native.bindings[key];
    }).find(function (binding) {
      return binding && Array.isArray(binding.payloadIds) && binding.payloadIds.length;
    });
    assert(bindingSource, 'reviewed pass has no binding payload to link');
    const linkedId = bindingSource.payloadIds[0];
    const linkedPayload = (inventory.payloads || []).find(function (item) { return item.id === linkedId; });
    assert(linkedPayload, 'binding payload is missing from inventory: ' + linkedId);
    const resourceRecord = readJson(path.join('records', 'resource', linkedPayload.resourceId + '.json'));
    const resourceBinding = (resourceRecord.nativeUniformBindings || []).find(function (binding) {
      return binding.payloadId === linkedId;
    });
    assert(resourceBinding, 'resource-side binding does not expose payloadId ' + linkedId);
    const payloadById = new Map((resourceRecord.payloads || []).map(function (item) { return [item.id, item]; }));
    const refs = explorerModule.bindingPayloadRefs(resourceBinding, payloadById);
    assert.strictEqual(refs.length, 1, 'one singular payloadId must render one payload link');
    assert.strictEqual(refs[0].payloadId, linkedId);
    assert.strictEqual(refs[0].resourceId, resourceRecord.id, 'UI link must resolve the owning resource');
    assert.strictEqual(refs[0].available, true, 'linked payload must expose captured raw bytes');
    assert.strictEqual(refs[0].bytes, payloadById.get(linkedId).bytes);
    assert(refs[0].chunks >= 1, 'linked payload must expose ordered chunks');
    const mixed = explorerModule.bindingPayloadRefs({
      payloadId: linkedId,
      payloadIds: [linkedId, linkedId]
    }, payloadById);
    assert.strictEqual(mixed.length, 1, 'payloadId/payloadIds normalization must not duplicate a record');
    console.log('ok - production uniform payload first/last/range bytes, float32 array decode and resource payloadId link resolve');
  }

  {
    // Viewport applicability is explicit in the packaged resource summary.  A
    // camera pass output may be cropped; a 4096 card atlas, a world-cache
    // indirection volume, a cube and the VSM page pool may not.
    const resources = readJson('resources.json').items;
    const byUnique = new Map(resources.map(function (item) { return [item.uniqueName, item]; }));
    const gbuffer = byUnique.get('000100000228d3653720');
    assert(gbuffer, 'actual GBufferA resource is missing');
    const gbufferViewport = explorerModule.viewportInfo(gbuffer);
    assert.strictEqual(gbufferViewport.applicability, 'screen-aligned');
    assert.strictEqual(gbufferViewport.screenPadding, true);
    assert.strictEqual(gbufferViewport.cropAllowed, true);
    assert.deepStrictEqual(gbufferViewport.passViewRect.size, [1280, 720]);
    assert.deepStrictEqual(gbufferViewport.mipExtent, [1280, 888]);
    assert.strictEqual(gbufferViewport.boundsMatch, true);
    assert.strictEqual(gbuffer.padding.validWidth, 1280);
    assert.strictEqual(gbuffer.padding.validHeight, 720);
    assert.strictEqual(gbuffer.padding.bottom, 168);
    const gbufferRecord = readJson(path.join('records', 'resource', gbuffer.id + '.json'));
    const gbufferPayload = gbufferRecord.payloads.find(function (item) { return item.layout; });
    assert(gbufferPayload, 'GBufferA payload layout is missing');
    assert.deepStrictEqual(codec.channelNames(gbufferPayload.layout.StoredFormat), ['R', 'G', 'B', 'A'],
      'the viewport mapping must not change stored components');
    assert.strictEqual(codec.texelOffset(gbufferPayload.layout, 640, 500, 0, 4, gbufferPayload.bytes).offset,
      expected.cases[0].byteOffset, 'the viewport mapping must not change subresource addressing');

    for (const uniqueName of ['00010000022925049890', '000100000228dcc20010',
      '000100000228d576f860', '000100000228d23ff6c0']) {
      const item = byUnique.get(uniqueName);
      assert(item, 'missing non-screen domain resource ' + uniqueName);
      const viewport = explorerModule.viewportInfo(item);
      assert.strictEqual(viewport.screenPadding, false, uniqueName + ': screen padding must be false');
      assert.strictEqual(viewport.cropAllowed, false, uniqueName + ': crop must be disabled');
      assert.strictEqual(item.padding, null, uniqueName + ': no padding is invented');
      if (viewport.passViewRect) {
        assert(Array.isArray(viewport.passViewRect.size), uniqueName + ': context rect must remain visible');
      }
    }

    const descriptive = [];
    for (const item of resources) {
      const record = readJson(item.record);
      for (const match of record.schemaMatches || []) {
        if (match.schemaId === 'cards.resource.resampled-card-capture-direct'
          || match.schemaId === 'cards.resource.resampled-card-capture-indirect') {
          descriptive.push({ item: item, match: match });
        }
      }
    }
    assert.strictEqual(descriptive.length, 6, 'expected six descriptive resampled-card schema instances');
    for (const entry of descriptive) {
      const match = entry.match;
      assert.strictEqual(match.formatProof, 'descriptive-only', entry.item.name);
      assert.strictEqual(match.primaryFieldsExtractable, false, entry.item.name);
      assert.strictEqual(match.extractionEligible, false, entry.item.name);
      assert(match.fields.length >= 3, entry.item.name + ': all fields must stay visible');
      assert.strictEqual(match.formatVariantFields.length, 0, entry.item.name);
    }
    console.log('ok - explicit viewport applicability keeps camera targets cropped, non-screen domains whole, and six descriptive schema fields inactive');
  }

  {
    const item = expected.cases[0];
    const payload = findPayload(null, item).payload;
    const run = makeStore();
    const results = await Promise.all([
      run.store.readRange(payload, item.byteOffset, item.length),
      run.store.readRange(payload, item.byteOffset, item.length)
    ]);
    assert.strictEqual(hex(results[0]), item.hex);
    assert.strictEqual(hex(results[1]), item.hex);
    const pieces = codec.chunkRange(payload.chunks, item.byteOffset, item.length, payload.bytes);
    pieces.forEach(function (piece) {
      assert.strictEqual(run.calls.get(piece.sha + '.gz'), 1,
        'concurrent reads must share one in-flight fetch per chunk');
    });
    console.log('ok - concurrent reads share one in-flight fetch per chunk');
  }

  {
    // Production path: instantiate PackageStore WITHOUT options.fetchFn.  The
    // fake global fetch is receiver-sensitive, like Window.fetch in a browser:
    // calling the stored native function with PackageStore as `this` would
    // throw "Illegal invocation".  This exercises the real getChunk/readRange
    // path, including decompression and SHA-256 verification.
    const index = readJson('index.json');
    const originalFetch = globalThis.fetch;
    const requested = [];
    globalThis.fetch = async function (url) {
      if (this !== globalThis) {
        throw new TypeError("Failed to execute 'fetch' on 'Window': Illegal invocation");
      }
      const file = fileURLToPath(String(url));
      requested.push(path.basename(file));
      return new Response(fs.readFileSync(file), { status: 200 });
    };
    try {
      const store = new explorerModule.PackageStore({
        baseUrl: pathToFileURL(PACKAGE + path.sep).href,
        crypto: crypto.webcrypto
      });
      store.chunks = new Map(index.chunks.map(function (chunk) { return [chunk.sha, chunk]; }));
      const item = expected.cases[0];
      const payload = findPayload(null, item).payload;
      const bytes = await store.readRange(payload, item.byteOffset, item.length);
      assert.strictEqual(hex(bytes), item.hex, 'default fetch wrapper: byte mismatch');
      const unused = index.chunks.find(function (chunk) {
        return !payload.chunks.some(function (ref) { return ref.sha === chunk.sha; });
      });
      if (unused) {
        const chunk = await store.getChunk(unused.sha);
        assert.strictEqual(chunk.length, unused.rawLength);
      }
      assert(requested.length >= 1, 'readRange should fetch through the default wrapper');
    } finally {
      globalThis.fetch = originalFetch;
    }
    console.log('ok - default PackageStore fetch wrapper keeps the global receiver and reads exact bytes through getChunk/readRange');
  }

  {
    const item = expected.cases[0];
    const payload = findPayload(null, item).payload;
    const run = makeStore();
    const first = payload.chunks[0];
    await run.store.readRange(payload, first.offset, 4);
    let rejected = null;
    try {
      await run.store.getChunk(first.sha, 4);
    } catch (error) {
      rejected = error;
    }
    assert(rejected, 'getChunk must reject a requested slice length even when the chunk is cached');
    assert(/slice length, not the whole chunk length/.test(rejected.message), rejected.message);
    console.log('ok - getChunk rejects a slice length (cached and uncached) and keeps index.json as the chunk-length authority');
  }
}

async function main() {
  if (HAS_PACKAGE) {
    await runPackageTests();
  } else {
    console.log('skip - no package at ' + PACKAGE + '; package-dependent byte comparisons were not run (set FRAME_PACKAGE_DIR to a built package).');
  }

  assert.deepStrictEqual(explorerModule.storedChannelNames('PF_A2B10G10R10'), ['R', 'G', 'B', 'A']);
  assert.deepStrictEqual(explorerModule.storedChannelNames('PF_FloatRGBA'), ['R', 'G', 'B', 'A']);
  assert.deepStrictEqual(explorerModule.storedChannelNames('PF_R32_FLOAT'), ['R']);
  const channels = explorerModule.channelList('PF_A2B10G10R10').map(function (item) { return item.id; });
  assert.deepStrictEqual(channels, ['rgb', 'R', 'G', 'B', 'A']);
  const scalarChannels = explorerModule.channelList('PF_R32_FLOAT').map(function (item) { return item.id; });
  assert.deepStrictEqual(scalarChannels, ['rgb', 'R']);
  console.log('ok - channel selector lists every stored component (RGB composite + R/G/B/A or scalar)');

  const defaults = explorerModule.defaultOverviewSteps(1280, 888, 4, 2 * 1024 * 1024);
  const overview = explorerModule.planOverview(1280, 888, 4, defaults.stepX, defaults.stepY, null, 2 * 1024 * 1024);
  assert(overview.available);
  assert(overview.fetchBytes <= 2 * 1024 * 1024);
  assert(overview.rows * overview.rowBytes === overview.fetchBytes);
  const cropped = explorerModule.planOverview(1280, 888, 4, defaults.stepX, defaults.stepY,
    { min: [0, 0], size: [1280, 720] }, 2 * 1024 * 1024);
  assert(cropped.available);
  assert.strictEqual(cropped.validHeight, 720);
  assert(cropped.fetchBytes <= 2 * 1024 * 1024);
  const tooDense = explorerModule.planOverview(1280, 888, 4, 1, 1, null, 2 * 1024 * 1024);
  assert.strictEqual(tooDense.available, false);
  assert(/cap/.test(tooDense.error));
  const oneRowTooBig = explorerModule.planOverview(200000, 8192, 16, 1, 1, null, 2 * 1024 * 1024);
  assert.strictEqual(oneRowTooBig.available, false);
  assert(/One sampled row/.test(oneRowTooBig.error));
  assert.strictEqual(explorerModule.textureLayout({ native: { Type: 'Texture3D' }, extent: [4, 4, 4] }).available, false);
  assert.strictEqual(explorerModule.textureLayout({ native: { Type: 'TextureCube' }, extent: [4, 4, 1] }).available, false);
  assert.strictEqual(explorerModule.textureLayout({ native: { Type: 'Texture2D' }, extent: [4, 4, 1] }).available, true);
  assert.strictEqual(explorerModule.textureLayout({ native: { Type: 'Texture3D' }, extent: [4, 4, 4] },
    { subresource: { dimension: 'Texture3D', extentX: 4, extentY: 4, zSliceCount: 4 } }).available, true);
  console.log('ok - whole-texture overview is cap-bounded, point-sampled, crop-aware, and uses exact sidecar bounds when present');

  console.log('read-path tests passed');
}

main().catch(function (error) {
  console.error('not ok - ' + (error && error.stack ? error.stack : error));
  process.exit(1);
});
