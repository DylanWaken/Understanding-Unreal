/*
 * frame-codec.js - dependency-free numeric decoders for the Unreal frame explorer.
 *
 * This file is deliberately usable from both the browser and Node so the decoder can
 * be tested without UI automation.  It only decodes layouts that are named explicitly
 * by capture metadata.  Unknown formats are reported as unsupported; they are never
 * guessed or replaced with zeroes.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.FrameCodec = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var SAFE_DECODE_TAGS = Object.freeze([
    'none', 'raw', 'uint', 'sint', 'unorm', 'float', 'half', 'float16',
    'float32', 'r11g11b10', 'a2b10g10r10', 'a2b10g10r10_unorm'
  ]);

  function normFormat(format) {
    if (format === null || format === undefined) return '';
    var s = String(format).trim().toUpperCase();
    if (s.indexOf('PF_') === 0) s = s.slice(3);
    return s;
  }

  var FORMAT_ALIASES = Object.freeze({
    FLOATRGB: 'R11G11B10_FLOAT',
    FLOATR11G11B10: 'R11G11B10_FLOAT',
    R11G11B10: 'R11G11B10_FLOAT',
    R16G16B16A16_UNORM: 'R16G16B16A16_UINT'
  });

  function canonicalFormat(format) {
    var name = normFormat(format);
    return FORMAT_ALIASES[name] || name;
  }

  function formatsEquivalent(left, right) {
    var a = canonicalFormat(left);
    var b = canonicalFormat(right);
    return !!a && a === b;
  }

  function formatList(value) {
    if (value === null || value === undefined) return [];
    var list = Array.isArray(value) ? value : [value];
    return list.map(function (item) { return String(item); }).filter(function (item) { return item.length > 0; });
  }

  /**
   * Decide whether a source-schema field may be extracted from bytes stored in
   * `runtimeFormat`.  A field with appliesToFormats is active only for one of
   * those formats.  A field without appliesToFormats belongs to the schema's
   * declared format, so a different runtime format leaves it inactive too.
   * This is deliberately independent of storage.formatAlternatives: an
   * alternative is not evidence that the captured payload actually used that
   * permutation.
   */
  function fieldFormatGate(field, runtimeFormat, declaredFormat) {
    if (!field || typeof field !== 'object') {
      return { active: false, reason: 'Field metadata is missing.' };
    }
    var applies = formatList(field.appliesToFormats);
    var allowed = applies.length ? applies : formatList(declaredFormat);
    if (!allowed.length) return { active: true, reason: null, appliesToFormats: [] };
    if (!runtimeFormat) {
      return {
        active: false,
        appliesToFormats: allowed,
        reason: 'This field belongs to ' + allowed.join(', ')
          + ' and no runtime stored format is recorded for the captured payload; the offset is displayed but not extracted.'
      };
    }
    for (var i = 0; i < allowed.length; i++) {
      if (formatsEquivalent(runtimeFormat, allowed[i])) {
        return { active: true, reason: null, appliesToFormats: applies };
      }
    }
    return {
      active: false,
      appliesToFormats: applies,
      reason: (applies.length
        ? 'This field applies only to ' + applies.join(', ')
        : 'This field belongs to the schema declared format ' + allowed.join(', '))
        + '; the captured payload is stored as ' + runtimeFormat
        + '. The source offset is displayed for reference only and is not extracted.'
    };
  }

  function formatVariantFor(storage, runtimeFormat) {
    if (!storage || typeof storage !== 'object' || !runtimeFormat) return null;
    var variants = Array.isArray(storage.formatVariants) ? storage.formatVariants : [];
    for (var i = 0; i < variants.length; i++) {
      var variant = variants[i];
      if (variant && typeof variant === 'object' && formatsEquivalent(runtimeFormat, variant.format)) {
        return variant;
      }
    }
    return null;
  }

  /**
   * Convert a storage.formatVariants entry into displayable/extractable field
   * records.  The variant is only used when its format matches the runtime
   * stored format; the caller decides that with formatVariantFor().
   */
  function formatVariantFields(variant) {
    if (!variant || typeof variant !== 'object') return [];
    var list = Array.isArray(variant.components) ? variant.components : [];
    var defaultRefs = variant.sourceRefs || [];
    return list.map(function (component) {
      var field = {};
      if (component && typeof component === 'object') {
        Object.keys(component).forEach(function (key) { field[key] = component[key]; });
      }
      field.appliesToFormats = [String(variant.format)];
      field.variantFormat = String(variant.format);
      field.sourceRefs = field.sourceRefs || defaultRefs;
      return field;
    });
  }

  function channel(name, type, bits, bitOffset, byteOffset) {
    return {
      name: name,
      type: type,
      bits: bits,
      bitOffset: bitOffset,
      byteOffset: byteOffset === undefined ? null : byteOffset
    };
  }

  function rgba8(swizzle) {
    // swizzle lists the memory byte index for R, G, B, A.
    return [
      channel('R', 'unorm', 8, swizzle[0] * 8),
      channel('G', 'unorm', 8, swizzle[1] * 8),
      channel('B', 'unorm', 8, swizzle[2] * 8),
      channel('A', 'unorm', 8, swizzle[3] * 8)
    ];
  }

  var FORMATS = {
    // 8-bit UNORM
    R8: { bytes: 1, layout: 'UNORM8', channels: [channel('R', 'unorm', 8, 0)] },
    G8: { bytes: 1, layout: 'UNORM8', channels: [channel('G', 'unorm', 8, 0)] },
    A8: { bytes: 1, layout: 'UNORM8', channels: [channel('A', 'unorm', 8, 0)] },
    L8: { bytes: 1, layout: 'UNORM8', channels: [channel('L', 'unorm', 8, 0)] },
    R8G8: { bytes: 2, layout: 'UNORM8', channels: [channel('R', 'unorm', 8, 0), channel('G', 'unorm', 8, 8)] },
    R8G8B8A8: { bytes: 4, layout: 'UNORM8', channels: rgba8([0, 1, 2, 3]) },
    B8G8R8A8: { bytes: 4, layout: 'UNORM8', channels: rgba8([2, 1, 0, 3]) },

    // 16-bit UNORM
    G16: { bytes: 2, layout: 'UNORM16', channels: [channel('R', 'unorm', 16, 0)] },
    G16R16: { bytes: 4, layout: 'UNORM16', channels: [channel('R', 'unorm', 16, 0), channel('G', 'unorm', 16, 16)] },
    A16B16G16R16: {
      bytes: 8, layout: 'UNORM16',
      channels: [channel('R', 'unorm', 16, 0), channel('G', 'unorm', 16, 16), channel('B', 'unorm', 16, 32), channel('A', 'unorm', 16, 48)]
    },
    R16G16B16A16_UNORM: {
      bytes: 8, layout: 'UNORM16',
      channels: [channel('R', 'unorm', 16, 0), channel('G', 'unorm', 16, 16), channel('B', 'unorm', 16, 32), channel('A', 'unorm', 16, 48)]
    },

    // 16-bit floats
    R16F: { bytes: 2, layout: 'FLOAT16', channels: [channel('R', 'float16', 16, 0, 0)] },
    R16F_FILTER: { bytes: 2, layout: 'FLOAT16', channels: [channel('R', 'float16', 16, 0, 0)] },
    G16R16F: {
      bytes: 4, layout: 'FLOAT16',
      channels: [channel('R', 'float16', 16, 0, 0), channel('G', 'float16', 16, 16, 2)]
    },
    G16R16F_FILTER: {
      bytes: 4, layout: 'FLOAT16',
      channels: [channel('R', 'float16', 16, 0, 0), channel('G', 'float16', 16, 16, 2)]
    },
    FLOATRGBA: {
      bytes: 8, layout: 'FLOAT16',
      channels: [channel('R', 'float16', 16, 0, 0), channel('G', 'float16', 16, 16, 2),
        channel('B', 'float16', 16, 32, 4), channel('A', 'float16', 16, 48, 6)]
    },
    R16G16B16A16_FLOAT: {
      bytes: 8, layout: 'FLOAT16',
      channels: [channel('R', 'float16', 16, 0, 0), channel('G', 'float16', 16, 16, 2),
        channel('B', 'float16', 16, 32, 4), channel('A', 'float16', 16, 48, 6)]
    },

    // 32-bit floats
    R32_FLOAT: { bytes: 4, layout: 'FLOAT32', channels: [channel('R', 'float32', 32, 0, 0)] },
    G32R32F: {
      bytes: 8, layout: 'FLOAT32',
      channels: [channel('R', 'float32', 32, 0, 0), channel('G', 'float32', 32, 32, 4)]
    },
    A32B32G32R32F: {
      bytes: 16, layout: 'FLOAT32',
      channels: [channel('R', 'float32', 32, 0, 0), channel('G', 'float32', 32, 32, 4),
        channel('B', 'float32', 32, 64, 8), channel('A', 'float32', 32, 96, 12)]
    },
    R32G32B32A32_FLOAT: {
      bytes: 16, layout: 'FLOAT32',
      channels: [channel('R', 'float32', 32, 0, 0), channel('G', 'float32', 32, 32, 4),
        channel('B', 'float32', 32, 64, 8), channel('A', 'float32', 32, 96, 12)]
    },

    // Integer 8/16/32
    R8_UINT: { bytes: 1, layout: 'UINT8', channels: [channel('R', 'uint', 8, 0)] },
    R8_SINT: { bytes: 1, layout: 'SINT8', channels: [channel('R', 'sint', 8, 0)] },
    R8G8_UINT: {
      bytes: 2, layout: 'UINT8',
      channels: [channel('R', 'uint', 8, 0), channel('G', 'uint', 8, 8)]
    },
    R8G8B8A8_UINT: {
      bytes: 4, layout: 'UINT8',
      channels: [channel('R', 'uint', 8, 0), channel('G', 'uint', 8, 8),
        channel('B', 'uint', 8, 16), channel('A', 'uint', 8, 24)]
    },
    R16_UINT: { bytes: 2, layout: 'UINT16', channels: [channel('R', 'uint', 16, 0)] },
    R16_SINT: { bytes: 2, layout: 'SINT16', channels: [channel('R', 'sint', 16, 0)] },
    R16G16_UINT: {
      bytes: 4, layout: 'UINT16',
      channels: [channel('R', 'uint', 16, 0), channel('G', 'uint', 16, 16)]
    },
    R32_UINT: { bytes: 4, layout: 'UINT32', channels: [channel('R', 'uint', 32, 0)] },
    R32_SINT: { bytes: 4, layout: 'SINT32', channels: [channel('R', 'sint', 32, 0)] },
    R32G32_UINT: {
      bytes: 8, layout: 'UINT32',
      channels: [channel('R', 'uint', 32, 0), channel('G', 'uint', 32, 32)]
    },
    R32G32B32A32_UINT: {
      bytes: 16, layout: 'UINT32',
      channels: [channel('R', 'uint', 32, 0), channel('G', 'uint', 32, 32),
        channel('B', 'uint', 32, 64), channel('A', 'uint', 32, 96)]
    },
    R32G32B32A32_SINT: {
      bytes: 16, layout: 'SINT32',
      channels: [channel('R', 'sint', 32, 0), channel('G', 'sint', 32, 32),
        channel('B', 'sint', 32, 64), channel('A', 'sint', 32, 96)]
    },
    R32G32B32_UINT: {
      bytes: 12, layout: 'UINT32',
      channels: [channel('R', 'uint', 32, 0), channel('G', 'uint', 32, 32), channel('B', 'uint', 32, 64)]
    },

    // Packed formats
    FLOATR11G11B10: { bytes: 4, layout: 'R11G11B10_FLOAT', method: 'r11g11b10', channels: [] },
    R11G11B10_FLOAT: { bytes: 4, layout: 'R11G11B10_FLOAT', method: 'r11g11b10', channels: [] },
    R11G11B10: { bytes: 4, layout: 'R11G11B10_FLOAT', method: 'r11g11b10', channels: [] },
    FLOATRGB: { bytes: 4, layout: 'R11G11B10_FLOAT', method: 'r11g11b10', channels: [] },
    A2B10G10R10: { bytes: 4, layout: 'A2B10G10R10_UNORM', method: 'a2b10g10r10', channels: [] },
    A2B10G10R10_UNORM: { bytes: 4, layout: 'A2B10G10R10_UNORM', method: 'a2b10g10r10', channels: [] },
    A2B10G10R10_UINT: { bytes: 4, layout: 'A2B10G10R10_UINT', method: 'a2b10g10r10_uint', channels: [] }
  };

  // A few UE enum names are ambiguous in DumpGPU's JSON because GPixelFormats[].Name
  // does not preserve the UNORM/UINT distinction.  Do not guess them.
  var AMBIGUOUS = {
    R16G16B16A16_UINT: 'DumpGPU reports PF_R16G16B16A16_UINT for both the UNORM and UINT UE enum entries; numeric interpretation is ambiguous without source metadata.'
  };

  function describeFormat(format) {
    var name = normFormat(format);
    var entry = FORMATS[name];
    if (!entry) {
      return {
        supported: false,
        format: format === null || format === undefined ? null : String(format),
        normalized: name,
        layout: null,
        bytesPerTexel: null,
        channels: [],
        method: null,
        gap: AMBIGUOUS[name] || ('No explicitly identified decoder layout for pixel format "' + (name || '(missing)') + '". Raw bytes and native metadata are retained.')
      };
    }
    return {
      supported: true,
      format: format === null || format === undefined ? null : String(format),
      normalized: name,
      layout: entry.layout,
      bytesPerTexel: entry.bytes,
      channels: entry.channels.slice(),
      method: entry.method || 'channels',
      gap: null
    };
  }

  /**
   * Names of every component actually stored by a format, in memory order.
   * Packed formats expose their explicit bitfield channels; single-channel
   * formats expose one name (rendered as a scalar channel by the UI).
   */
  function channelNames(format) {
    var info = describeFormat(format);
    if (!info.supported) return [];
    if (info.method === 'r11g11b10') return ['R', 'G', 'B'];
    if (info.method === 'a2b10g10r10' || info.method === 'a2b10g10r10_uint') {
      return ['R', 'G', 'B', 'A'];
    }
    return info.channels.map(function (ch) { return ch.name; });
  }

  function asUint8Array(value) {
    if (value instanceof Uint8Array) return value;
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    if (Array.isArray(value)) return Uint8Array.from(value);
    throw new TypeError('Expected a byte array, ArrayBuffer or typed array.');
  }

  function requireRange(bytes, offset, length, label) {
    if (!Number.isInteger(offset) || offset < 0) {
      throw new RangeError((label || 'offset') + ' must be a non-negative integer; got ' + offset);
    }
    if (!Number.isInteger(length) || length < 0) {
      throw new RangeError((label || 'length') + ' must be a non-negative integer; got ' + length);
    }
    if (offset + length > bytes.length) {
      throw new RangeError((label || 'range') + ' exceeds payload: ' + offset + '+' + length + ' > ' + bytes.length);
    }
  }

  function readUintLE(bytes, offset, length) {
    bytes = asUint8Array(bytes);
    requireRange(bytes, offset, length, 'readUintLE');
    var value = 0n;
    for (var i = 0; i < length; i++) {
      value |= BigInt(bytes[offset + i]) << BigInt(8 * i);
    }
    return value;
  }

  function readBitsLE(bytes, bitOffset, bitWidth) {
    if (!Number.isInteger(bitOffset) || bitOffset < 0) {
      throw new RangeError('bitOffset must be a non-negative integer; got ' + bitOffset);
    }
    if (!Number.isInteger(bitWidth) || bitWidth <= 0 || bitWidth > 64) {
      throw new RangeError('bitWidth must be an integer in 1..64; got ' + bitWidth);
    }
    var startByte = Math.floor(bitOffset / 8);
    var shift = bitOffset % 8;
    var byteLength = Math.ceil((shift + bitWidth) / 8);
    var word = readUintLE(bytes, startByte, byteLength);
    var mask = (1n << BigInt(bitWidth)) - 1n;
    return (word >> BigInt(shift)) & mask;
  }

  function numberFromBigInt(value) {
    if (value <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(value);
    return value.toString();
  }

  function signExtend(value, bits) {
    var sign = 1n << BigInt(bits - 1);
    var mask = (1n << BigInt(bits)) - 1n;
    value &= mask;
    return (value & sign) ? value - (1n << BigInt(bits)) : value;
  }

  function halfToNumber(bits) {
    bits = Number(bits) & 0xffff;
    var sign = (bits & 0x8000) ? -1 : 1;
    var exp = (bits >> 10) & 0x1f;
    var mant = bits & 0x3ff;
    if (exp === 0) {
      if (mant === 0) return sign === 1 ? 0 : -0;
      return sign * Math.pow(2, -14) * (mant / 1024);
    }
    if (exp === 31) {
      return mant === 0 ? sign * Infinity : NaN;
    }
    return sign * Math.pow(2, exp - 15) * (1 + mant / 1024);
  }

  function float11ToNumber(bits) {
    bits = Number(bits) & 0x7ff;
    var exp = (bits >> 6) & 0x1f;
    var mant = bits & 0x3f;
    if (exp === 0) {
      return mant === 0 ? 0 : Math.pow(2, -14) * (mant / 64);
    }
    if (exp === 31) {
      return mant === 0 ? Infinity : NaN;
    }
    return Math.pow(2, exp - 15) * (1 + mant / 64);
  }

  function float10ToNumber(bits) {
    bits = Number(bits) & 0x3ff;
    var exp = (bits >> 5) & 0x1f;
    var mant = bits & 0x1f;
    if (exp === 0) {
      return mant === 0 ? 0 : Math.pow(2, -14) * (mant / 32);
    }
    if (exp === 31) {
      return mant === 0 ? Infinity : NaN;
    }
    return Math.pow(2, exp - 15) * (1 + mant / 32);
  }

  function r11g11b10ToFloats(word) {
    word = Number(word) >>> 0;
    var r = word & 0x7ff;
    var g = (word >>> 11) & 0x7ff;
    var b = (word >>> 22) & 0x3ff;
    return [float11ToNumber(r), float11ToNumber(g), float10ToNumber(b)];
  }

  function a2b10g10r10ToFloats(word) {
    word = Number(word) >>> 0;
    return [
      (word & 0x3ff) / 1023,
      ((word >>> 10) & 0x3ff) / 1023,
      ((word >>> 20) & 0x3ff) / 1023,
      ((word >>> 30) & 0x3) / 3
    ];
  }

  function decodeFloat32(bytes, offset) {
    var view = new DataView(bytes.buffer, bytes.byteOffset + offset, 4);
    return view.getFloat32(0, true);
  }

  function decodeChannel(bytes, ch, precomputedWord) {
    var raw;
    var value;
    if (ch.type === 'float16') {
      raw = Number(readUintLE(bytes, ch.byteOffset, 2));
      value = halfToNumber(raw);
      return { name: ch.name, type: ch.type, bits: ch.bits, raw: raw, rawHex: '0x' + raw.toString(16).padStart(4, '0'), value: value };
    }
    if (ch.type === 'float32') {
      raw = dataViewUint32(bytes, ch.byteOffset);
      value = decodeFloat32(bytes, ch.byteOffset);
      return { name: ch.name, type: ch.type, bits: ch.bits, raw: raw, rawHex: '0x' + raw.toString(16).padStart(8, '0'), value: value };
    }
    raw = readBitsLE(bytes, ch.bitOffset, ch.bits);
    if (ch.type === 'unorm') {
      value = Number(raw) / (Math.pow(2, ch.bits) - 1);
    } else if (ch.type === 'sint') {
      value = numberFromBigInt(signExtend(raw, ch.bits));
    } else if (ch.type === 'uint') {
      value = numberFromBigInt(raw);
    } else {
      throw new Error('Unknown channel type ' + ch.type);
    }
    return {
      name: ch.name,
      type: ch.type,
      bits: ch.bits,
      raw: numberFromBigInt(raw),
      rawHex: '0x' + raw.toString(16).padStart(Math.ceil(ch.bits / 4), '0'),
      value: value
    };
  }

  function dataViewUint32(bytes, offset) {
    var view = new DataView(bytes.buffer, bytes.byteOffset + offset, 4);
    return view.getUint32(0, true);
  }

  function decodeTexel(input, format) {
    var bytes = asUint8Array(input);
    var info = describeFormat(format);
    var result = {
      supported: false,
      format: info.format,
      normalized: info.normalized,
      layout: info.layout,
      bytesPerTexel: info.bytesPerTexel,
      channels: [],
      values: [],
      raw: Array.prototype.slice.call(bytes),
      gap: info.gap
    };
    if (!info.supported) return result;

    if (bytes.length < info.bytesPerTexel) {
      result.gap = 'Texel requires ' + info.bytesPerTexel + ' bytes but only ' + bytes.length + ' are available.';
      return result;
    }

    result.supported = true;
    if (info.method === 'r11g11b10') {
      var word = dataViewUint32(bytes, 0);
      var floats = r11g11b10ToFloats(word);
      result.channels = [
        { name: 'R', type: 'float11', bits: 11, raw: word & 0x7ff, value: floats[0] },
        { name: 'G', type: 'float11', bits: 11, raw: (word >>> 11) & 0x7ff, value: floats[1] },
        { name: 'B', type: 'float10', bits: 10, raw: (word >>> 22) & 0x3ff, value: floats[2] }
      ];
      result.values = floats.slice();
      result.word = word;
      result.wordHex = '0x' + word.toString(16).padStart(8, '0');
      return result;
    }
    if (info.method === 'a2b10g10r10') {
      var aword = dataViewUint32(bytes, 0);
      var av = a2b10g10r10ToFloats(aword);
      result.channels = [
        { name: 'R', type: 'unorm10', bits: 10, raw: aword & 0x3ff, value: av[0] },
        { name: 'G', type: 'unorm10', bits: 10, raw: (aword >>> 10) & 0x3ff, value: av[1] },
        { name: 'B', type: 'unorm10', bits: 10, raw: (aword >>> 20) & 0x3ff, value: av[2] },
        { name: 'A', type: 'unorm2', bits: 2, raw: (aword >>> 30) & 0x3, value: av[3] }
      ];
      result.values = av.slice();
      result.word = aword;
      result.wordHex = '0x' + aword.toString(16).padStart(8, '0');
      return result;
    }
    if (info.method === 'a2b10g10r10_uint') {
      var iword = dataViewUint32(bytes, 0);
      result.channels = [
        { name: 'R', type: 'uint10', bits: 10, raw: iword & 0x3ff, value: iword & 0x3ff },
        { name: 'G', type: 'uint10', bits: 10, raw: (iword >>> 10) & 0x3ff, value: (iword >>> 10) & 0x3ff },
        { name: 'B', type: 'uint10', bits: 10, raw: (iword >>> 20) & 0x3ff, value: (iword >>> 20) & 0x3ff },
        { name: 'A', type: 'uint2', bits: 2, raw: (iword >>> 30) & 0x3, value: (iword >>> 30) & 0x3 }
      ];
      result.values = result.channels.map(function (c) { return c.value; });
      result.word = iword;
      result.wordHex = '0x' + iword.toString(16).padStart(8, '0');
      return result;
    }

    result.channels = info.channels.map(function (ch) { return decodeChannel(bytes, ch); });
    result.values = result.channels.map(function (c) { return c.value; });
    return result;
  }

  function chunkRange(refs, offset, length, payloadLength) {
    if (!Array.isArray(refs) || refs.length === 0) {
      throw new RangeError('Payload has no chunk references.');
    }
    if (!Number.isInteger(offset) || offset < 0) throw new RangeError('offset must be a non-negative integer.');
    if (!Number.isInteger(length) || length < 0) throw new RangeError('length must be a non-negative integer.');
    if (payloadLength !== undefined && payloadLength !== null) {
      if (!Number.isInteger(payloadLength) || payloadLength < 0) throw new RangeError('payloadLength must be a non-negative integer.');
      if (offset + length > payloadLength) throw new RangeError('Requested range ' + offset + '+' + length + ' exceeds payload length ' + payloadLength + '.');
    }
    var end = offset + length;
    var out = [];
    for (var i = 0; i < refs.length; i++) {
      var ref = refs[i];
      if (!Number.isInteger(ref.offset) || !Number.isInteger(ref.length) || ref.offset < 0 || ref.length <= 0) {
        throw new RangeError('Invalid chunk reference at index ' + i + '.');
      }
      var refEnd = ref.offset + ref.length;
      var from = Math.max(offset, ref.offset);
      var to = Math.min(end, refEnd);
      if (to > from) {
        out.push({
          index: i,
          sha: ref.sha,
          chunkOffset: from - ref.offset,
          length: to - from,
          destOffset: from - offset
        });
      }
    }
    if (out.length === 0 && length !== 0) {
      throw new RangeError('No chunks cover range ' + offset + '+' + length + '.');
    }
    var covered = out.reduce(function (sum, r) { return sum + r.length; }, 0);
    if (covered !== length) {
      throw new RangeError('Chunk references cover ' + covered + ' of ' + length + ' requested bytes.');
    }
    return out;
  }

  function assembleRange(refs, chunkDatas, offset, length, payloadLength) {
    var pieces = chunkRange(refs, offset, length, payloadLength);
    var out = new Uint8Array(length);
    for (var i = 0; i < pieces.length; i++) {
      var piece = pieces[i];
      var data = asUint8Array(chunkDatas[i]);
      requireRange(data, piece.chunkOffset, piece.length, 'chunk ' + piece.sha);
      out.set(data.subarray(piece.chunkOffset, piece.chunkOffset + piece.length), piece.destOffset);
    }
    return out;
  }

  var TYPE_INFO = {
    uint8: { type: 'uint', bits: 8 },
    uint16: { type: 'uint', bits: 16 },
    uint32: { type: 'uint', bits: 32 },
    uint64: { type: 'uint', bits: 64 },
    int8: { type: 'sint', bits: 8 },
    int16: { type: 'sint', bits: 16 },
    int32: { type: 'sint', bits: 32 },
    sint: { type: 'sint', bits: 32 },
    uint: { type: 'uint', bits: 32 },
    unorm8: { type: 'unorm', bits: 8 },
    unorm16: { type: 'unorm', bits: 16 },
    unorm: { type: 'unorm', bits: 32 },
    float16: { type: 'float16', bits: 16 },
    half: { type: 'float16', bits: 16 },
    float32: { type: 'float32', bits: 32 },
    float: { type: 'float32', bits: 32 },
    bool: { type: 'uint', bits: 32 }
  };

  function normalizeFieldType(type) {
    if (!type) return null;
    var s = String(type).trim().toLowerCase().replace(/^ubmt_/, '').replace(/^pf_/, '');
    if (TYPE_INFO[s]) return { key: s, type: TYPE_INFO[s].type, bits: TYPE_INFO[s].bits };
    var m = /^(unorm|uint|int|float)(\d+)$/.exec(s);
    if (m) {
      var n = Number(m[2]);
      var kind = m[1];
      if (kind === 'float') {
        if (n === 16) return { key: s, type: 'float16', bits: 16 };
        if (n === 32) return { key: s, type: 'float32', bits: 32 };
        if ([2, 3, 4].indexOf(n) !== -1) return { key: s, type: 'vector-float', bits: n * 32, vectorLength: n };
        if ([10, 11, 14, 15, 30].indexOf(n) !== -1) return { key: s, type: 'floatN', bits: n };
        return null;
      }
      if (kind === 'unorm') return { key: s, type: 'unorm', bits: n };
      if (kind === 'int') return { key: s, type: 'sint', bits: n };
      // uint2/uint4 are ambiguous: a 2/4-bit scalar or a vector of uint32.
      // extractField resolves the vector case from the explicit bitWidth.
      if (n === 2 || n === 4) return { key: s, type: 'uint-vector-or-bits', bits: n, vectorLength: n };
      return { key: s, type: 'uint', bits: n };
    }
    return null;
  }

  function float32FromBits(bits) {
    var word = Number(bits) >>> 0;
    var buf = new ArrayBuffer(4);
    new DataView(buf).setUint32(0, word, true);
    return new DataView(buf).getFloat32(0, true);
  }

  /**
   * Extract a known numeric field from an explicitly offset byte range.
   * This function never evaluates a schema-provided string as code.
   */
  function extractField(bytes, byteOffset, spec) {
    bytes = asUint8Array(bytes);
    spec = spec || {};
    var info = normalizeFieldType(spec.type);
    if (!info) {
      return {
        supported: false,
        reason: 'Unsupported field type "' + (spec.type || '(missing)') + '"; raw bytes are retained and no value is fabricated.',
        values: [], rawValues: [], decode: spec.decode || null
      };
    }
    if (!Number.isInteger(byteOffset) || byteOffset < 0) {
      throw new RangeError('byteOffset must be a non-negative integer.');
    }
    var vectorLength = info.vectorLength || 0;
    var bitWidth = spec.bitWidth === undefined || spec.bitWidth === null ? info.bits : Number(spec.bitWidth);
    var bitOffset = spec.bitOffset === undefined || spec.bitOffset === null ? 0 : Number(spec.bitOffset);
    var count = spec.count === undefined || spec.count === null ? 1 : Number(spec.count);
    // uint2/uint4 are scalar N-bit fields when the explicit width is N, and
    // vectors of uint32 when the explicit width is N*32.
    if (info.type === 'uint-vector-or-bits') {
      if (bitWidth === vectorLength * 32) info = { type: 'vector-uint', bits: vectorLength * 32, vectorLength: vectorLength };
      else info = { type: 'uint', bits: vectorLength, vectorLength: 0 };
      vectorLength = info.vectorLength || 0;
    }
    if (vectorLength) {
      if (bitWidth !== vectorLength * 32) {
        throw new RangeError('Vector field "' + spec.type + '" requires bitWidth ' + (vectorLength * 32) + '.');
      }
      if (bitOffset % 8 !== 0) throw new RangeError('Vector fields must be byte-aligned.');
      if (!Number.isInteger(count) || count < 1 || count > 65536) throw new RangeError('count must be an integer in 1..65536.');
    } else {
      if (!Number.isInteger(bitWidth) || bitWidth <= 0 || bitWidth > 64) {
        throw new RangeError('bitWidth must be an integer in 1..64 for scalar fields.');
      }
      if (!Number.isInteger(bitOffset) || bitOffset < 0) throw new RangeError('bitOffset must be a non-negative integer.');
      if (!Number.isInteger(count) || count < 1 || count > 65536) throw new RangeError('count must be an integer in 1..65536.');
    }
    var decodeText = spec.decode === undefined || spec.decode === null ? null : String(spec.decode);
    var decodeTag = decodeText ? decodeText.trim().toLowerCase() : null;
    var decodeIgnored = !!(decodeTag && SAFE_DECODE_TAGS.indexOf(decodeTag) === -1);
    if (decodeIgnored) decodeTag = null;

    var strideBits = bitWidth;
    var totalBits = bitOffset + strideBits * count;
    var totalBytes = Math.ceil(totalBits / 8);
    requireRange(bytes, byteOffset, totalBytes, 'field extraction');
    var values = [];
    var rawValues = [];
    var decodedExtra = [];
    var decodeApplied = null;

    if (vectorLength) {
      var vectorValues = [];
      for (var vi = 0; vi < count; vi++) {
        var vbase = byteOffset + Math.floor((bitOffset + vi * strideBits) / 8);
        var vector = [];
        for (var vc = 0; vc < vectorLength; vc++) {
          var word = new DataView(bytes.buffer, bytes.byteOffset + vbase + vc * 4, 4);
          if (info.type === 'vector-float') {
            vector.push(word.getFloat32(0, true));
          } else {
            vector.push(word.getUint32(0, true));
          }
          rawValues.push(word.getUint32(0, true));
        }
        vectorValues.push(vector);
        if (count === 1) values = vector;
        else values.push(vector);
      }
      return {
        supported: true,
        type: spec.type,
        normalizedType: info.type,
        vectorLength: vectorLength,
        bitWidth: bitWidth,
        bitOffset: bitOffset,
        count: count,
        byteOffset: byteOffset,
        byteLength: totalBytes,
        values: values,
        vectorValues: vectorValues,
        rawValues: rawValues,
        decodedExtra: [],
        decode: decodeText,
        decodeIgnored: decodeIgnored,
        decodeApplied: null
      };
    }

    for (var i = 0; i < count; i++) {
      var bitPos = bitOffset + i * strideBits;
      var raw = readBitsLE(bytes, byteOffset * 8 + bitPos, bitWidth);
      rawValues.push(numberFromBigInt(raw));
      var value;
      if (decodeTag === 'unorm') {
        value = Number(raw) / (Math.pow(2, bitWidth) - 1);
        decodeApplied = 'stored unorm code / (2^bits - 1); source affine equation and consumer normalization are not applied';
      } else if (decodeTag === 'float' || decodeTag === 'float32') {
        if (bitWidth !== 32 || bitPos % 8 !== 0) throw new RangeError('float32 decode requires byte-aligned 32-bit fields.');
        value = new DataView(bytes.buffer, bytes.byteOffset + byteOffset + Math.floor(bitPos / 8), 4).getFloat32(0, true);
        decodeApplied = 'IEEE-754 binary32 little-endian';
      } else if (decodeTag === 'half' || decodeTag === 'float16') {
        if (bitWidth !== 16 || bitPos % 8 !== 0) throw new RangeError('float16 decode requires byte-aligned 16-bit fields.');
        value = halfToNumber(raw);
        decodeApplied = 'IEEE-754 binary16 little-endian';
      } else if (decodeTag === 'r11g11b10') {
        if (bitWidth !== 32) throw new RangeError('R11G11B10 decode requires a 32-bit packed field.');
        var packedRgb = r11g11b10ToFloats(Number(raw));
        value = packedRgb;
        decodedExtra.push({ kind: 'r11g11b10', channels: ['R', 'G', 'B'], values: packedRgb });
        decodeApplied = 'R11G11B10 unsigned-float channels';
      } else if (decodeTag === 'a2b10g10r10' || decodeTag === 'a2b10g10r10_unorm') {
        if (bitWidth !== 32) throw new RangeError('A2B10G10R10 decode requires a 32-bit packed field.');
        var packedRgba = a2b10g10r10ToFloats(Number(raw));
        value = packedRgba;
        decodedExtra.push({ kind: 'a2b10g10r10', channels: ['R', 'G', 'B', 'A'], values: packedRgba });
        decodeApplied = 'A2B10G10R10 UNORM channels';
      } else if (info.type === 'float16') {
        if (bitWidth !== 16 || bitPos % 8 !== 0) throw new RangeError('float16 extraction requires byte-aligned 16-bit fields.');
        value = halfToNumber(raw);
      } else if (info.type === 'float32') {
        if (bitWidth !== 32 || bitPos % 8 !== 0) throw new RangeError('float32 extraction requires byte-aligned 32-bit fields.');
        value = new DataView(bytes.buffer, bytes.byteOffset + byteOffset + Math.floor(bitPos / 8), 4).getFloat32(0, true);
      } else if (info.type === 'floatN') {
        var width = bitWidth;
        if (width === 11) value = float11ToNumber(Number(raw));
        else if (width === 10) value = float10ToNumber(Number(raw));
        else if (width === 14 || width === 15) value = halfToNumber((Number(raw) << 1) & 0xfffe);
        else if (width === 30) value = float32FromBits(Number((raw << 2n) & 0xffffffffn));
        else throw new RangeError('No float' + width + ' numeric decoder.');
      } else if (info.type === 'unorm') {
        value = Number(raw) / (Math.pow(2, bitWidth) - 1);
        if (!decodeApplied) {
          decodeApplied = 'stored unorm code / (2^bits - 1); source affine equation and consumer normalization are not applied';
        }
      } else if (info.type === 'sint') {
        value = numberFromBigInt(signExtend(raw, bitWidth));
      } else {
        value = numberFromBigInt(raw);
      }
      values.push(value);
    }
    return {
      supported: true,
      type: spec.type,
      normalizedType: info.type,
      bitWidth: bitWidth,
      bitOffset: bitOffset,
      count: count,
      byteOffset: byteOffset,
      byteLength: totalBytes,
      values: values,
      rawValues: rawValues,
      decodedExtra: decodedExtra,
      decode: decodeText,
      decodeIgnored: decodeIgnored,
      decodeApplied: decodeApplied
    };
  }

  /**
   * Exact texel addressing for a sidecar subresource.  The sidecar's
   * RowPitchBytes and ZSliceStrideBytes are authoritative; width*bytesPerTexel
   * is never substituted for a recorded row pitch.  Bounds are the selected
   * subresource's own ExtentX/Y and ZSliceCount, not the parent allocation.
   */
  function texelOffset(layout, x, y, z, bytesPerTexel, payloadLength) {
    if (!layout || typeof layout !== 'object') {
      return { ok: false, error: 'No SubresourceLayouts record for this payload; texel addressing is not assumed.' };
    }
    var width = Number(layout.extentX !== undefined ? layout.extentX : layout.ExtentX);
    var height = Number(layout.extentY !== undefined ? layout.extentY : layout.ExtentY);
    var depth = Math.max(1, Number((layout.zSliceCount !== undefined ? layout.zSliceCount : layout.ZSliceCount) || 1));
    var rowPitch = Number(layout.rowPitchBytes !== undefined ? layout.rowPitchBytes : layout.RowPitchBytes);
    var zStride = Number(layout.zSliceStrideBytes !== undefined ? layout.zSliceStrideBytes : layout.ZSliceStrideBytes);
    var bpt = Number(bytesPerTexel || layout.bytesPerTexel || 0);
    var xx = Number(x), yy = Number(y), zz = Number(z || 0);
    if (![width, height, depth, rowPitch, zStride, bpt].every(function (n) { return Number.isFinite(n) && n >= 0; })) {
      return { ok: false, error: 'Subresource layout has a non-numeric extent, pitch or stride.' };
    }
    if (!Number.isInteger(xx) || !Number.isInteger(yy) || !Number.isInteger(zz)) {
      return { ok: false, error: 'x, y and z must be integers.' };
    }
    if (xx < 0 || xx >= width) return { ok: false, error: 'x ' + xx + ' is outside the selected subresource 0..' + (width - 1) + '.' };
    if (yy < 0 || yy >= height) return { ok: false, error: 'y ' + yy + ' is outside the selected subresource 0..' + (height - 1) + '.' };
    if (zz < 0 || zz >= depth) return { ok: false, error: 'z ' + zz + ' is outside the selected subresource 0..' + (depth - 1) + '.' };
    var offset = zz * zStride + yy * rowPitch + xx * bpt;
    if (!Number.isSafeInteger(offset) || offset < 0 || offset + bpt > Number(payloadLength)) {
      return { ok: false, error: 'Computed byte range ' + offset + '..' + (offset + bpt) + ' is outside the captured payload (' + payloadLength + ' B).' };
    }
    return {
      ok: true, offset: offset, bytesPerTexel: bpt,
      width: width, height: height, depth: depth,
      rowPitch: rowPitch, zSliceStride: zStride, z: zz,
      rowBytes: width * bpt, rowPaddingBytes: rowPitch - width * bpt
    };
  }

  function layoutFormat(layout) {
    if (!layout || typeof layout !== 'object') return null;
    return layout.storedFormat || layout.StoredFormat || null;
  }

  /**
   * Decide whether a schema's packed storage layout is proven by machine-readable
   * metadata.  Descriptive prose and formatAlternatives are not proof.
   */
  function schemaLayoutProof(storage, runtimeFormat, fields) {
    var declared = storage && typeof storage === 'object' ? storage.format : storage;
    var text = declared === null || declared === undefined ? null : String(declared);
    var match = text ? /PF_[A-Za-z0-9_]+/.exec(text) : null;
    var explicit = match ? match[0] : null;
    var variants = storage && typeof storage === 'object' && Array.isArray(storage.formatVariants)
      ? storage.formatVariants : [];
    var variant = null;
    for (var i = 0; i < variants.length; i++) {
      if (variants[i] && runtimeFormat && formatsEquivalent(runtimeFormat, variants[i].format)) {
        variant = variants[i];
        break;
      }
    }
    var primary = !!explicit && !!runtimeFormat && formatsEquivalent(runtimeFormat, explicit);
    var explicitFieldGates = (fields || []).filter(function (field) {
      return field && Array.isArray(field.appliesToFormats) && field.appliesToFormats.length;
    });
    return {
      declaredFormatText: text,
      explicitFormat: explicit,
      machineReadable: !!explicit,
      proof: explicit ? 'explicit-pf' : (text ? 'descriptive-only' : 'none'),
      primaryFieldsExtractable: primary,
      activeVariant: variant ? variant.format : null,
      extractionEligible: primary || !!variant,
      explicitFieldGates: explicitFieldGates.length,
      reason: primary ? null : (explicit
        ? 'The captured stored format ' + runtimeFormat + ' does not match the schema layout ' + explicit + '.'
        : 'Schema storage.format is descriptive prose with no machine-readable PF_* tag' + (text ? ' (' + text + ')' : '') + '; field offsets are displayed but not applied. A descriptive format or formatAlternative never establishes a packed layout.')
    };
  }

  function hexDump(bytes, startOffset, bytesPerLine) {
    bytes = asUint8Array(bytes);
    startOffset = startOffset || 0;
    bytesPerLine = bytesPerLine || 16;
    var lines = [];
    for (var i = 0; i < bytes.length; i += bytesPerLine) {
      var slice = bytes.subarray(i, Math.min(i + bytesPerLine, bytes.length));
      var hex = [];
      var ascii = '';
      for (var j = 0; j < bytesPerLine; j++) {
        if (j < slice.length) {
          hex.push(slice[j].toString(16).padStart(2, '0'));
          ascii += slice[j] >= 32 && slice[j] < 127 ? String.fromCharCode(slice[j]) : '.';
        } else {
          hex.push('  ');
          ascii += ' ';
        }
      }
      lines.push({
        address: startOffset + i,
        hex: hex.slice(0, 8).join(' ') + '  ' + hex.slice(8).join(' '),
        ascii: ascii
      });
    }
    return lines;
  }

  return Object.freeze({
    version: '1.0.0',
    SAFE_DECODE_TAGS: SAFE_DECODE_TAGS,
    FORMATS: FORMATS,
    normFormat: normFormat,
    canonicalFormat: canonicalFormat,
    formatsEquivalent: formatsEquivalent,
    fieldFormatGate: fieldFormatGate,
    formatVariantFor: formatVariantFor,
    formatVariantFields: formatVariantFields,
    describeFormat: describeFormat,
    channelNames: channelNames,
    decodeTexel: decodeTexel,
    halfToNumber: halfToNumber,
    r11g11b10ToFloats: r11g11b10ToFloats,
    a2b10g10r10ToFloats: a2b10g10r10ToFloats,
    float11ToNumber: float11ToNumber,
    float10ToNumber: float10ToNumber,
    readUintLE: readUintLE,
    readBitsLE: readBitsLE,
    chunkRange: chunkRange,
    assembleRange: assembleRange,
    extractField: extractField,
    normalizeFieldType: normalizeFieldType,
    texelOffset: texelOffset,
    layoutFormat: layoutFormat,
    schemaLayoutProof: schemaLayoutProof,
    hexDump: hexDump
  });
});
