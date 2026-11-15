"""Dependency-free PNG reader and image statistics for the reference capture.

Only 8-bit, non-interlaced PNG color types 0/2/4/6 are accepted, which covers
Unreal's ordinary viewport screenshots.  No pixels are modified.
"""
import struct
import zlib


class Png:
    def __init__(self, path):
        self.path = str(path)
        data = open(path, 'rb').read()
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('not a PNG: ' + self.path)
        pos = 8
        self.width = self.height = None
        self.bit_depth = self.color_type = self.interlace = None
        idat = []
        self.chunks = []
        while pos < len(data):
            length = struct.unpack('>I', data[pos:pos + 4])[0]
            kind = data[pos + 4:pos + 8]
            payload = data[pos + 8:pos + 8 + length]
            self.chunks.append(kind.decode('latin1'))
            pos += 12 + length
            if kind == b'IHDR':
                (self.width, self.height, self.bit_depth, self.color_type,
                 _compression, _filter, self.interlace) = struct.unpack('>IIBBBBB', payload)
            elif kind == b'IDAT':
                idat.append(payload)
            elif kind == b'IEND':
                break
        if self.width is None:
            raise ValueError('missing IHDR')
        if self.bit_depth != 8 or self.interlace != 0:
            raise ValueError(f'unsupported PNG bit depth/interlace: {self.bit_depth}/{self.interlace}')
        channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(self.color_type)
        if channels is None:
            raise ValueError(f'unsupported PNG color type {self.color_type}')
        self.channels = channels
        raw = zlib.decompress(b''.join(idat))
        stride = self.width * channels
        rows = []
        prev = bytearray(stride)
        p = 0
        for _y in range(self.height):
            ftype = raw[p]
            p += 1
            line = bytearray(raw[p:p + stride])
            p += stride
            if ftype == 0:
                pass
            elif ftype == 1:  # Sub
                for i in range(channels, stride):
                    line[i] = (line[i] + line[i - channels]) & 0xFF
            elif ftype == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ftype == 3:  # Average
                for i in range(stride):
                    left = line[i - channels] if i >= channels else 0
                    line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
            elif ftype == 4:  # Paeth
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i - channels] if i >= channels else 0
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            else:
                raise ValueError(f'unknown PNG filter {ftype}')
            rows.append(bytes(line))
            prev = line
        self.rows = rows
        self.rgb = [None, None, None]
        if channels >= 3:
            self.rgb = [[row[i::channels] for row in rows] for i in range(3)]
        else:
            self.rgb = [[row[0::channels] for row in rows] for _ in range(3)]

    def pixel(self, x, y):
        ch = self.channels
        row = self.rows[y]
        if ch >= 3:
            i = x * ch
            return row[i], row[i + 1], row[i + 2]
        v = row[x * ch]
        return v, v, v


def _mean(vals):
    return sum(vals) / len(vals)


def _std(vals, mean):
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def region(png, x0, x1, y0, y1):
    rs = gs = bs = n = 0
    for y in range(y0, y1):
        row = png.rows[y]
        for x in range(x0, x1):
            i = x * png.channels
            rs += row[i]
            gs += row[i + 1]
            bs += row[i + 2]
            n += 1
    return [rs / n, gs / n, bs / n]


def stats(png):
    lum = []
    rvals = []
    gvals = []
    bvals = []
    colors = set()
    near_black = near_white = 0
    total = png.width * png.height
    for y in range(png.height):
        row = png.rows[y]
        for x in range(png.width):
            i = x * png.channels
            r, g, b = row[i], row[i + 1], row[i + 2]
            rvals.append(r)
            gvals.append(g)
            bvals.append(b)
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            lum.append(l)
            colors.add((r, g, b))
            if l <= 2:
                near_black += 1
            if l >= 253:
                near_white += 1
    mean = _mean(lum)
    std = _std(lum, mean)
    return {
        'file': png.path,
        'width': png.width,
        'height': png.height,
        'channels': png.channels,
        'mean_rgb': [_mean(rvals), _mean(gvals), _mean(bvals)],
        'mean_luminance': mean,
        'std_luminance': std,
        'min_luminance': min(lum),
        'max_luminance': max(lum),
        'unique_rgb_colors': len(colors),
        'near_black_fraction': near_black / total,
        'near_white_fraction': near_white / total,
        'nonblack_fraction_lum_gt_8': sum(1 for v in lum if v > 8) / total,
        'left_wall_rgb': region(png, 190, 410, 130, 600),
        'right_wall_rgb': region(png, 880, 1090, 130, 600),
        'room_center_rgb': region(png, 500, 780, 220, 610),
        'border_rgb': region(png, 0, 120, 0, 720),
    }


def compare(a, b):
    total = a.width * a.height
    diff_sum = 0.0
    big = 0
    la = []
    lb = []
    for y in range(a.height):
        ra, rb = a.rows[y], b.rows[y]
        for x in range(a.width):
            i = x * a.channels
            j = x * b.channels
            d = (abs(ra[i] - rb[j]) + abs(ra[i + 1] - rb[j + 1]) + abs(ra[i + 2] - rb[j + 2])) / 3.0
            diff_sum += d
            if d > 8:
                big += 1
            la.append(0.2126 * ra[i] + 0.7152 * ra[i + 1] + 0.0722 * ra[i + 2])
            lb.append(0.2126 * rb[j] + 0.7152 * rb[j + 1] + 0.0722 * rb[j + 2])
    ma, mb = _mean(la), _mean(lb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(la, lb))
    va = sum((x - ma) ** 2 for x in la)
    vb = sum((y - mb) ** 2 for y in lb)
    return {
        'mean_absolute_rgb_difference_0_255': diff_sum / total,
        'fraction_pixels_rgb_difference_gt_8': big / total,
        'luminance_pearson_correlation': cov / ((va * vb) ** 0.5) if va and vb else None,
    }
