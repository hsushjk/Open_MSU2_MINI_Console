// 上位机：RLE 压缩段（op 0x0D）
const fs = require('fs');
const p = 'C:/Users/hsush/Desktop/MSU2_MINI/host/msu2_mini.py';
const raw = fs.readFileSync(p, 'utf8');
const crlf = raw.indexOf('\r\n') >= 0;
let s = raw.split('\r\n').join('\n');

if (s.indexOf('lcd_rle_segment') >= 0) {
    console.log('already patched');
    process.exit(0);
}

// 1) numpy 导入与编码器
const anchorImp = 'HEARTBEAT_INTERVAL = 2.0';
const addImp = [
    'try:',
    '    import numpy as _np',
    'except Exception:  # pragma: no cover - 没装 numpy 时退化为原样发送',
    '    _np = None',
    '',
    '',
    'def _rle_payload(chunk):',
    '    """大端 RGB565 字节流做行程编码，返回 [像素数, 高, 低] 三元组序列。"""',
    '    n = len(chunk) // 2',
    '    if n <= 0 or _np is None:',
    '        return b""',
    '    arr = _np.frombuffer(chunk[:n * 2], dtype=">u2")',
    '    if n > 1:',
    '        cut = _np.flatnonzero(arr[1:] != arr[:-1]) + 1',
    '        starts = _np.concatenate((_np.zeros(1, dtype=_np.int64), cut))',
    '        lens = _np.diff(_np.concatenate((starts, _np.array([n], dtype=_np.int64))))',
    '    else:',
    '        starts = _np.zeros(1, dtype=_np.int64)',
    '        lens = _np.array([n], dtype=_np.int64)',
    '    counts = []',
    '    vals = []',
    '    for st, ln in zip(starts.tolist(), lens.tolist()):',
    '        v = int(arr[st])',
    '        while ln > 255:',
    '            counts.append(255)',
    '            vals.append(v)',
    '            ln -= 255',
    '        counts.append(ln)',
    '        vals.append(v)',
    '    if not counts:',
    '        return b""',
    '    cnt = _np.array(counts, dtype=_np.uint8).tobytes()',
    '    val = _np.array(vals, dtype=">u2").tobytes()',
    '    out = bytearray(len(counts) * 3)',
    '    out[0::3] = cnt',
    '    out[1::3] = val[0::2]',
    '    out[2::3] = val[1::2]',
    '    return bytes(out)',
    '',
    '',
    'def lcd_segment_payload(chunk):',
    '    """选压缩或原样：压缩后更小就走 0x0D，否则按 0x0C 原样发。"""',
    '    rle = _rle_payload(chunk)',
    '    if rle and len(rle) < len(chunk):',
    '        return MiniProtocol.lcd_rle_segment(rle)',
    '    return MiniProtocol.lcd_raw_segment(chunk)',
    '',
    '',
    'HEARTBEAT_INTERVAL = 2.0'
].join('\n');

if (s.indexOf(anchorImp) < 0) { console.log('import anchor missing'); process.exit(1); }
s = s.split(anchorImp).join(addImp);

// 2) 协议方法
const anchorRaw = '    @staticmethod\n    def lcd_raw_end():';
const addMethod = [
    '    @staticmethod',
    '    def lcd_rle_segment(payload):',
    '        n = len(payload)',
    '        return (MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW, 0x0D,',
    '                                    (n >> 8) & 0xFF, n & 0xFF, 0x01)',
    '                + bytes(payload))',
    '',
    '    @staticmethod',
    '    def lcd_raw_end():'
].join('\n');

if (s.indexOf(anchorRaw) < 0) { console.log('protocol anchor missing'); process.exit(1); }
s = s.split(anchorRaw).join(addMethod);

// 3) 分块与整帧都走压缩
const oldTiles = [
    '        out = bytearray()',
    '        for tx, ty in tiles:',
    '            out += MiniProtocol.lcd_set_xy(tx, ty)',
    '            out += MiniProtocol.lcd_set_size(tx + T, ty + T)',
    '            out += MiniProtocol.lcd_enter_direct_mode()',
    '            for r in range(ty, ty + T):',
    '                off = (r * SCREEN_W + tx) * 2',
    '                out += MiniProtocol.lcd_raw_segment(raw[off:off + T * 2])',
    '            out += MiniProtocol.lcd_raw_end()',
    '        return bytes(out)'
].join('\n');

const newTiles = [
    '        out = bytearray()',
    '        for tx, ty in tiles:',
    '            out += MiniProtocol.lcd_set_xy(tx, ty)',
    '            out += MiniProtocol.lcd_set_size(tx + T, ty + T)',
    '            out += MiniProtocol.lcd_enter_direct_mode()',
    '            pre = bytearray()',
    '            for r in range(ty, ty + T):',
    '                off = (r * SCREEN_W + tx) * 2',
    '                pre += raw[off:off + T * 2]',
    '            out += lcd_segment_payload(bytes(pre))',
    '            out += MiniProtocol.lcd_raw_end()',
    '        return bytes(out)'
].join('\n');

if (s.indexOf(oldTiles) < 0) { console.log('tiles anchor missing'); process.exit(1); }
s = s.split(oldTiles).join(newTiles);

const oldFull = [
    '    def _encode_full(self, raw):',
    '        out = bytearray()',
    '        for si in range(0, len(raw), 256):',
    '            out += MiniProtocol.lcd_raw_segment(raw[si:si + 256])',
    '        out += MiniProtocol.lcd_raw_end()',
    '        return bytes(out)'
].join('\n');

const newFull = [
    '    def _encode_full(self, raw):',
    '        out = bytearray()',
    '        for si in range(0, len(raw), 512):',
    '            out += lcd_segment_payload(raw[si:si + 512])',
    '        out += MiniProtocol.lcd_raw_end()',
    '        return bytes(out)'
].join('\n');

if (s.indexOf(oldFull) < 0) { console.log('full anchor missing'); process.exit(1); }
s = s.split(oldFull).join(newFull);

fs.writeFileSync(p, crlf ? s.split('\n').join('\r\n') : s, 'utf8');
console.log('host: RLE encoder + 0x0D segments');