'use strict';

// Pure decoder tests.  Run with: node tests/test_codec.cjs
// Expected half and packed-float values below were computed independently with
// Python's struct/format maths, not by the JavaScript decoder under test.

const assert = require('assert');
const c = require(require('path').join(require('./test_paths.cjs').assets, 'frame-codec.js'));

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('ok - ' + name);
  } catch (error) {
    console.error('not ok - ' + name);
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  }
}

test('half-float known bit patterns', () => {
  const cases = [
    [0x3C00, 1], [0xC000, -2], [0x7BFF, 65504], [0x0001, 5.960464477539063e-8],
    [0x3555, 0.333251953125], [0x7C00, Infinity], [0x0000, 0]
  ];
  for (const [bits, expected] of cases) assert.strictEqual(c.halfToNumber(bits), expected);
  assert.ok(Number.isNaN(c.halfToNumber(0x7E00)));
  assert.strictEqual(Object.is(c.halfToNumber(0x8000), -0), true);
});

test('R11G11B10 known independent bit patterns', () => {
  assert.deepStrictEqual(c.r11g11b10ToFloats(0x781E03C0), [1, 1, 1]);
  assert.deepStrictEqual(c.r11g11b10ToFloats(0x80200400), [2, 2, 2]);
  assert.deepStrictEqual(c.r11g11b10ToFloats(0x701C0380), [0.5, 0.5, 0.5]);
  assert.deepStrictEqual(c.r11g11b10ToFloats(0xF7FDFFBF), [65024, 65024, 64512]);
  assert.deepStrictEqual(c.r11g11b10ToFloats(0x00000000), [0, 0, 0]);
  assert.deepStrictEqual(c.r11g11b10ToFloats(0x00000001), [9.5367431640625e-7, 0, 0]);
  assert.strictEqual(c.r11g11b10ToFloats(0x000007C0)[0], Infinity); // R exponent 31, mantissa 0
  assert.ok(Number.isNaN(c.r11g11b10ToFloats(0x000007C1)[0])); // R exponent 31, mantissa 1
});

test('A2B10G10R10 UNORM known word', () => {
  const bytes = Uint8Array.from([0xFF, 0x03, 0x08, 0x90]); // 0x900803FF LE
  const decoded = c.decodeTexel(bytes, 'PF_A2B10G10R10');
  assert.strictEqual(decoded.supported, true);
  assert.strictEqual(decoded.wordHex, '0x900803ff');
  assert.deepStrictEqual(decoded.channels.map((ch) => ch.raw), [1023, 512, 256, 2]);
  assert.strictEqual(decoded.values[0], 1);
  assert.strictEqual(decoded.values[1], 512 / 1023);
  assert.strictEqual(decoded.values[2], 256 / 1023);
  assert.strictEqual(decoded.values[3], 2 / 3);
});

test('UNORM8 BGRA channel swizzle and raw retention', () => {
  const bytes = Uint8Array.from([10, 20, 30, 40]); // memory B,G,R,A
  const decoded = c.decodeTexel(bytes, 'PF_B8G8R8A8');
  assert.deepStrictEqual(decoded.channels.map((ch) => ch.name), ['R', 'G', 'B', 'A']);
  assert.deepStrictEqual(decoded.values.map((v) => Math.round(v * 255)), [30, 20, 10, 40]);
  assert.deepStrictEqual(decoded.raw, [10, 20, 30, 40]);
});

test('float16 RGBA and float32 scalar', () => {
  const half = new Uint8Array(8);
  new DataView(half.buffer).setUint16(0, 0x3C00, true);
  new DataView(half.buffer).setUint16(2, 0x4000, true);
  new DataView(half.buffer).setUint16(4, 0x4200, true);
  new DataView(half.buffer).setUint16(6, 0x3800, true);
  const decoded = c.decodeTexel(half, 'PF_FloatRGBA');
  assert.deepStrictEqual(decoded.values, [1, 2, 3, 0.5]);

  const f32 = new Uint8Array(4);
  new DataView(f32.buffer).setFloat32(0, -12.5, true);
  assert.deepStrictEqual(c.decodeTexel(f32, 'PF_R32_FLOAT').values, [-12.5]);
});

test('integer 32-bit signed and unsigned', () => {
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setInt32(0, -2147483648, true);
  assert.deepStrictEqual(c.decodeTexel(bytes, 'PF_R32_SINT').values, [-2147483648]);
  new DataView(bytes.buffer).setUint32(0, 4294967295, true);
  assert.deepStrictEqual(c.decodeTexel(bytes, 'PF_R32_UINT').values, [4294967295]);
});

test('unknown and ambiguous formats stay raw with a gap', () => {
  const bytes = Uint8Array.from([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]);
  const unknown = c.decodeTexel(bytes, 'PF_SOMETHING_NEW');
  assert.strictEqual(unknown.supported, false);
  assert.deepStrictEqual(unknown.raw, Array.from(bytes));
  assert.ok(unknown.gap && unknown.gap.length > 0);
  const ambiguous = c.decodeTexel(bytes, 'PF_R16G16B16A16_UINT');
  assert.strictEqual(ambiguous.supported, false);
  assert.ok(/ambiguous/i.test(ambiguous.gap));
  assert.deepStrictEqual(ambiguous.raw, Array.from(bytes));
});

test('cross-chunk range assembly and typed read', () => {
  const refs = [
    { sha: 'a'.repeat(64), offset: 0, length: 5 },
    { sha: 'b'.repeat(64), offset: 5, length: 5 }
  ];
  const first = Uint8Array.from([0xAA, 0xAA, 0xAA, 0xAA, 0x00]);
  const second = Uint8Array.from([0x3C, 0xBB, 0xBB, 0xBB, 0xBB]);
  const bytes = c.assembleRange(refs, [first, second], 4, 2, 10);
  assert.deepStrictEqual(Array.from(bytes), [0x00, 0x3C]);
  assert.strictEqual(c.halfToNumber(c.readUintLE(bytes, 0, 2)), 1);
  assert.deepStrictEqual(c.chunkRange(refs, 4, 2, 10).map((p) => [p.sha[0], p.chunkOffset, p.length]), [['a', 4, 1], ['b', 0, 1]]);
});

test('negative, overflow and uncovered ranges are rejected', () => {
  const refs = [{ sha: 'a'.repeat(64), offset: 0, length: 4 }];
  assert.throws(() => c.chunkRange(refs, -1, 1, 4), RangeError);
  assert.throws(() => c.chunkRange(refs, 0, 5, 4), RangeError);
  assert.throws(() => c.chunkRange(refs, 4, 1, 4), RangeError);
  assert.throws(() => c.assembleRange(refs, [Uint8Array.from([1, 2, 3, 4])], 5, 1, 4), RangeError);
  assert.throws(() => c.readUintLE(Uint8Array.from([1]), -1, 1), RangeError);
  assert.throws(() => c.readUintLE(Uint8Array.from([1]), 0, 2), RangeError);
  assert.throws(() => c.readBitsLE(Uint8Array.from([1]), 0, 0), RangeError);
  assert.throws(() => c.readBitsLE(Uint8Array.from([1]), 0, 65), RangeError);
});

test('schema field extraction uses known types and bit offsets', () => {
  const bytes = Uint8Array.from([0xB2, 0x01, 0x00, 0x00, 0x00, 0x00, 0x80, 0x3F]);
  const packed = c.extractField(bytes, 0, { type: 'uint16', bitOffset: 2, bitWidth: 4 });
  assert.strictEqual(packed.supported, true);
  assert.deepStrictEqual(packed.values, [12]);
  const normalised = c.extractField(bytes, 0, { type: 'uint8', bitWidth: 8, decode: 'unorm' });
  assert.ok(Math.abs(normalised.values[0] - 0xB2 / 255) < 1e-12);
  const f32 = c.extractField(bytes, 4, { type: 'float32' });
  assert.strictEqual(f32.values[0], 1);
  assert.throws(() => c.extractField(bytes, -1, { type: 'uint8' }), RangeError);
  assert.throws(() => c.extractField(bytes, 8, { type: 'uint8' }), RangeError);
  assert.throws(() => c.extractField(bytes, 0, { type: 'uint8', count: 100000 }), RangeError);
});

test('unsafe schema decode expression is never evaluated', () => {
  const bytes = Uint8Array.from([1, 2, 3, 4]);
  const result = c.extractField(bytes, 0, { type: 'uint32', decode: "__import__('os').system('echo bad')" });
  assert.strictEqual(result.supported, true);
  assert.strictEqual(result.decodeIgnored, true);
  assert.deepStrictEqual(result.values, [0x04030201]);
  assert.strictEqual(result.decodeApplied, null);
});

test('real-schema packed field types', () => {
  // GBufferA: R10 normal x=0 at 1023, then y=512, z=0, 2-bit payload=2.
  const word = (1023 | (512 << 10) | (0 << 20) | (2 << 30)) >>> 0;
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setUint32(0, word, true);
  const nx = c.extractField(bytes, 0, { type: 'unorm10', bitOffset: 0, bitWidth: 10 });
  assert.strictEqual(nx.values[0], 1);
  const ny = c.extractField(bytes, 0, { type: 'unorm10', bitOffset: 10, bitWidth: 10 });
  assert.strictEqual(ny.values[0], 512 / 1023);
  const payload = c.extractField(bytes, 0, { type: 'uint2', bitOffset: 30, bitWidth: 2 });
  assert.deepStrictEqual(payload.values, [2]);

  // R11 channel types as explicitly named by the source schema.
  const packed = new Uint8Array(4);
  new DataView(packed.buffer).setUint32(0, 0x781e03c0, true);
  assert.deepStrictEqual(c.extractField(packed, 0, { type: 'float11', bitOffset: 0, bitWidth: 11 }).values, [1]);
  assert.deepStrictEqual(c.extractField(packed, 0, { type: 'float11', bitOffset: 11, bitWidth: 11 }).values, [1]);
  assert.deepStrictEqual(c.extractField(packed, 0, { type: 'float10', bitOffset: 22, bitWidth: 10 }).values, [1]);

  // float4 with an explicit 128-bit width is a 4-float vector, not 128 bits.
  const vec = new Uint8Array(16);
  [1, 2, 3, 4].forEach((v, i) => new DataView(vec.buffer).setFloat32(i * 4, v, true));
  const vecResult = c.extractField(vec, 0, { type: 'float4', bitWidth: 128 });
  assert.deepStrictEqual(vecResult.values, [1, 2, 3, 4]);

  // float14/float30 are bit reconstructions, not generic truncations.
  const half = new Uint8Array(2);
  const halfWord = 0x3C00;
  new DataView(half.buffer).setUint16(0, (halfWord & 0x7FFE) >> 1, true);
  assert.strictEqual(c.extractField(half, 0, { type: 'float14', bitOffset: 0, bitWidth: 14 }).values[0], 1);
  const f30 = new Uint8Array(4);
  new DataView(f30.buffer).setFloat32(0, 2.5, true);
  assert.strictEqual(c.extractField(f30, 0, { type: 'float30', bitOffset: 2, bitWidth: 30 }).values[0], 2.5);

  // uint2 with width 64 is a uint32 vector (card page data), not a 2-bit scalar.
  const uvec = new Uint8Array(8);
  new DataView(uvec.buffer).setUint32(0, 7, true);
  new DataView(uvec.buffer).setUint32(4, 9, true);
  assert.deepStrictEqual(c.extractField(uvec, 0, { type: 'uint2', bitWidth: 64 }).values, [7, 9]);
});

test('format descriptions are explicit and unknown formats have no byte-size guess', () => {
  assert.strictEqual(c.describeFormat('PF_R11G11B10_FLOAT').bytesPerTexel, 4);
  assert.strictEqual(c.describeFormat('PF_FloatRGBA').bytesPerTexel, 8);
  assert.strictEqual(c.describeFormat('PF_Unknown').supported, false);
  assert.strictEqual(c.describeFormat('PF_Unknown').bytesPerTexel, null);
});

test('channel names list every stored component, including packed and scalar layouts', () => {
  assert.deepStrictEqual(c.channelNames('PF_A2B10G10R10'), ['R', 'G', 'B', 'A']);
  assert.deepStrictEqual(c.channelNames('PF_A2B10G10R10_UINT'), ['R', 'G', 'B', 'A']);
  assert.deepStrictEqual(c.channelNames('PF_FloatRGBA'), ['R', 'G', 'B', 'A']);
  assert.deepStrictEqual(c.channelNames('PF_R11G11B10_FLOAT'), ['R', 'G', 'B']);
  assert.deepStrictEqual(c.channelNames('PF_R32_FLOAT'), ['R']);
  assert.deepStrictEqual(c.channelNames('PF_Unknown'), []);
});

test('actual GBufferA/B texels from binary-20261112-133826', () => {
  // GBufferA payload-00548 sha256 cbfd73f1...; texel (640,300) at byte 1538560
  // is 0x5ff001ff.  This capture stores the native packed A2B10G10R10 layout,
  // NOT an RGBA8 export; the capture team's gbuffer-a-normal.png agrees.
  const gbufferA = Uint8Array.from([0xff, 0x01, 0xf0, 0x5f]);
  const decodedA = c.decodeTexel(gbufferA, 'PF_A2B10G10R10');
  assert.strictEqual(decodedA.wordHex, '0x5ff001ff');
  assert.deepStrictEqual(decodedA.channels.map((ch) => ch.raw), [511, 0, 511, 1]);
  assert.strictEqual(decodedA.values[0], 511 / 1023);
  assert.strictEqual(decodedA.values[1], 0);
  assert.strictEqual(decodedA.values[2], 511 / 1023);
  assert.strictEqual(decodedA.values[3], 1 / 3);

  // GBufferB payload-00551 sha256 6ea88063... at the same texel: memory BGRA
  // [178,127,0,161] -> shader (R,G,B,A) = (0,127,178,161).
  const gbufferB = Uint8Array.from([178, 127, 0, 161]);
  const decodedB = c.decodeTexel(gbufferB, 'PF_B8G8R8A8');
  assert.deepStrictEqual(decodedB.values.map((v) => Math.round(v * 255)), [0, 127, 178, 161]);
});

test('actual Lumen.PageTable packed entry from binary-20261112-133826', () => {
  // resource-0097 / payload-00065 sha256 49c369ed...; entry 10 at byte 80 is
  // word0 0x66008020, word1 10.  Field definitions come from the joined
  // resource.lumen-page-table schema, never from evaluating its decode strings.
  const entry = Uint8Array.from([0x20, 0x80, 0x00, 0x66, 0x0a, 0x00, 0x00, 0x00]);
  assert.deepStrictEqual(c.extractField(entry, 0, { type: 'uint12', bitOffset: 0, bitWidth: 12 }).values, [32]);
  assert.deepStrictEqual(c.extractField(entry, 0, { type: 'uint12', bitOffset: 12, bitWidth: 12 }).values, [8]);
  assert.deepStrictEqual(c.extractField(entry, 0, { type: 'uint4', bitOffset: 24, bitWidth: 4 }).values, [6]);
  assert.deepStrictEqual(c.extractField(entry, 0, { type: 'uint4', bitOffset: 28, bitWidth: 4 }).values, [6]);
  assert.deepStrictEqual(c.extractField(entry, 4, { type: 'uint32' }).values, [10]);
});

test('format gates select the runtime layout and never an alternative', () => {
  assert.strictEqual(c.formatsEquivalent('PF_FloatRGB', 'PF_R11G11B10_FLOAT'), true);
  assert.strictEqual(c.formatsEquivalent('PF_FloatRGBA', 'PF_R11G11B10_FLOAT'), false);

  const gated = { name: 'IrradianceR', type: 'float11', bitOffset: 0, appliesToFormats: ['PF_FloatR11G11B10'] };
  assert.strictEqual(c.fieldFormatGate(gated, 'PF_FloatR11G11B10').active, true);
  const wrong = c.fieldFormatGate(gated, 'PF_FloatRGBA');
  assert.strictEqual(wrong.active, false);
  assert.ok(/applies only to PF_FloatR11G11B10/.test(wrong.reason));
  assert.strictEqual(c.fieldFormatGate({ name: 'NormalX', type: 'unorm10' }, 'PF_A2B10G10R10').active, true);
  // A field without appliesToFormats belongs to the schema's declared format,
  // not to whatever alternative happened to be captured.
  const primaryOnly = c.fieldFormatGate({ name: 'NormalX', type: 'unorm10' }, 'PF_FloatRGBA', 'PF_A2B10G10R10');
  assert.strictEqual(primaryOnly.active, false);
  assert.ok(/schema declared format PF_A2B10G10R10/.test(primaryOnly.reason));

  const storage = {
    format: 'PF_FloatR11G11B10',
    formatAlternatives: ['PF_FloatRGBA', 'PF_A32B32G32R32F'],
    formatVariants: [
      { format: 'PF_FloatRGBA', strideBytes: 8, sourceRefs: ['ref.variant'],
        components: [{ name: 'IrradianceR', type: 'float16', byteOffset: 0 },
          { name: 'IrradianceG', type: 'float16', byteOffset: 2 }] },
      { format: 'PF_A32B32G32R32F', strideBytes: 16, components: [{ name: 'IrradianceR', type: 'float32', byteOffset: 0 }] }
    ]
  };
  assert.strictEqual(c.formatVariantFor(storage, 'PF_FloatR11G11B10'), null);
  const active = c.formatVariantFor(storage, 'PF_FloatRGBA');
  assert.strictEqual(active.format, 'PF_FloatRGBA');
  const fields = c.formatVariantFields(active);
  assert.deepStrictEqual(fields.map((field) => [field.name, field.type, field.byteOffset]), [['IrradianceR', 'float16', 0], ['IrradianceG', 'float16', 2]]);
  assert.deepStrictEqual(fields[0].appliesToFormats, ['PF_FloatRGBA']);
  assert.deepStrictEqual(fields[0].sourceRefs, ['ref.variant']);
  // The alternative string alone must not select a variant.
  assert.strictEqual(c.formatVariantFor({ format: 'PF_FloatR11G11B10', formatAlternatives: ['PF_FloatRGBA'] }, 'PF_FloatRGBA'), null);
});

test('sidecar texel addressing uses recorded row pitch, z stride and subresource bounds', () => {
  const volume = {
    dimension: 'Texture3D', extentX: 2, extentY: 2, depth: 2, zSliceCount: 2,
    rowPitchBytes: 8, zSliceStrideBytes: 16, storedFormat: 'PF_R32_FLOAT',
    firstArraySlice: 0, numArraySlices: 0
  };
  assert.deepStrictEqual(c.texelOffset(volume, 0, 0, 0, 4, 32), {
    ok: true, offset: 0, bytesPerTexel: 4, width: 2, height: 2, depth: 2,
    rowPitch: 8, zSliceStride: 16, z: 0, rowBytes: 8, rowPaddingBytes: 0
  });
  assert.strictEqual(c.texelOffset(volume, 1, 1, 1, 4, 32).offset, 16 + 8 + 4);
  const outOfZ = c.texelOffset(volume, 0, 0, 2, 4, 32);
  assert.strictEqual(outOfZ.ok, false);
  assert.ok(/z 2 is outside/.test(outOfZ.error));
  const outOfX = c.texelOffset(volume, 2, 0, 0, 4, 32);
  assert.strictEqual(outOfX.ok, false);
  assert.ok(/x 2 is outside/.test(outOfX.error));
  const tooShort = c.texelOffset(volume, 1, 1, 1, 4, 31);
  assert.strictEqual(tooShort.ok, false);
  assert.ok(/outside the captured payload/.test(tooShort.error));

  const face = {
    dimension: 'TextureCube', extentX: 1, extentY: 1, depth: 1, zSliceCount: 1,
    rowPitchBytes: 4, zSliceStrideBytes: 4, storedFormat: 'PF_R32_FLOAT',
    firstArraySlice: 3, numArraySlices: 1, cubeFace: 3
  };
  assert.strictEqual(c.texelOffset(face, 0, 0, 0, 4, 4).offset, 0);
  assert.strictEqual(face.cubeFace, face.firstArraySlice);

  const arrayMip = {
    dimension: 'Texture2DArray', extentX: 1, extentY: 1, depth: 1, zSliceCount: 1,
    rowPitchBytes: 4, zSliceStrideBytes: 4, storedFormat: 'PF_R32_FLOAT',
    firstArraySlice: 1, numArraySlices: 1
  };
  assert.strictEqual(c.texelOffset(arrayMip, 0, 0, 0, 4, 4).offset, 0);
  assert.strictEqual(c.texelOffset(null, 0, 0, 0, 4, 4).ok, false);
});

test('descriptive storage prose never proves a packed schema layout', () => {
  const prose = {
    kind: 'texture2d',
    format: 'Lumen::GetDirectLightingAtlasFormat() = GetLightingDataFormat (R11G11B10 in this capture)',
    strideBytes: 4,
    formatAlternatives: ['PF_FloatRGBA / PF_A32B32G32R32F when r.Lumen.LightingDataFormat selects them']
  };
  const proof = c.schemaLayoutProof(prose, 'PF_FloatR11G11B10', []);
  assert.strictEqual(proof.machineReadable, false);
  assert.strictEqual(proof.proof, 'descriptive-only');
  assert.strictEqual(proof.primaryFieldsExtractable, false);
  assert.strictEqual(proof.extractionEligible, false);
  assert.ok(/never establishes a packed layout/.test(proof.reason));

  const explicit = c.schemaLayoutProof({ format: 'PF_FloatR11G11B10' }, 'PF_FloatR11G11B10', []);
  assert.strictEqual(explicit.machineReadable, true);
  assert.strictEqual(explicit.primaryFieldsExtractable, true);
  assert.strictEqual(explicit.extractionEligible, true);

  const wrong = c.schemaLayoutProof({ format: 'PF_A2B10G10R10' }, 'PF_FloatRGBA', []);
  assert.strictEqual(wrong.extractionEligible, false);
  const variant = c.schemaLayoutProof({
    format: 'PF_FloatR11G11B10',
    formatVariants: [{ format: 'PF_FloatRGBA', components: [{ name: 'x', type: 'float16', byteOffset: 0 }] }]
  }, 'PF_FloatRGBA', []);
  assert.strictEqual(variant.activeVariant, 'PF_FloatRGBA');
  assert.strictEqual(variant.primaryFieldsExtractable, false);
  assert.strictEqual(variant.extractionEligible, true);
});

if (!process.exitCode) console.log('\n' + passed + ' codec tests passed.');
