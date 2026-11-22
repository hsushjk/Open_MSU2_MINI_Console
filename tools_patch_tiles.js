// 上位机：raw 模式改成 16x16 分块增量，并去掉发送路径上的固定延时
const fs = require('fs');
const p = 'C:/Users/hsush/Desktop/MSU2_MINI/host/msu2_mini.py';
let s = fs.readFileSync(p, 'utf8');
const crlf = s.indexOf('\r\n') >= 0;
if (crlf) s = s.split('\r\n').join('\n');
const before = s.length;

function mustReplace(oldText, newText, what) {
    if (!s.includes(oldText)) {
        console.error('未找到:' + what);
        process.exit(1);
    }
    s = s.replace(oldText, newText);
}

mustReplace(
`        self._last_raw = None`,
`        self._last_raw = None
        self._last_tile_raw = None`,
'__init__ 里的 _last_raw');

mustReplace(
`    def reset(self):
        self._last_raw = None`,
`    def reset(self):
        self._last_raw = None
        self._last_tile_raw = None`,
'reset');

mustReplace(
`        if raw_mode:
            for si in range(0, len(raw), 256):
                out += MiniProtocol.lcd_raw_segment(raw[si:si + 256])
            out += MiniProtocol.lcd_raw_end()
            return bytes(out)`,
`        if raw_mode:
            return bytes(out + self._encode_tiles(raw))`,
'raw 分支');

mustReplace(
`            out += MiniProtocol.lcd_segment_end_full()
        return bytes(out)`,
`            out += MiniProtocol.lcd_segment_end_full()
        return bytes(out)

    # ---- v2 分块增量 -------------------------------------------------
    #
    # 160x80 切成 16x16 的块，共 10x5 = 50 块，每块 512 字节。
    # 与上一帧逐块比较，只发变化的块；每块自带窗口，独立成一次流。
    # 变化块超过 32 个时退回整帧传输，避免重复的窗口开销反而更慢。
    TILE = 16
    TILE_FULL_THRESHOLD = 32

    def _encode_tiles(self, raw):
        T = self.TILE
        prev = self._last_tile_raw
        raw = bytes(raw)

        if prev is None or len(prev) != len(raw):
            self._last_tile_raw = raw
            return self._encode_full(raw)

        tiles = []
        for ty in range(0, SCREEN_H, T):
            for tx in range(0, SCREEN_W, T):
                for r in range(ty, min(ty + T, SCREEN_H)):
                    off = (r * SCREEN_W + tx) * 2
                    if raw[off:off + T * 2] != prev[off:off + T * 2]:
                        tiles.append((tx, ty))
                        break

        self._last_tile_raw = raw
        if not tiles:
            return b""
        if len(tiles) > self.TILE_FULL_THRESHOLD:
            return self._encode_full(raw)

        out = bytearray()
        for tx, ty in tiles:
            out += MiniProtocol.lcd_set_xy(tx, ty)
            out += MiniProtocol.lcd_set_size(tx + T, ty + T)
            out += MiniProtocol.lcd_enter_direct_mode()
            for r in range(ty, ty + T):
                off = (r * SCREEN_W + tx) * 2
                out += MiniProtocol.lcd_raw_segment(raw[off:off + T * 2])
            out += MiniProtocol.lcd_raw_end()
        return bytes(out)

    def _encode_full(self, raw):
        out = bytearray()
        for si in range(0, len(raw), 256):
            out += MiniProtocol.lcd_raw_segment(raw[si:si + 256])
        out += MiniProtocol.lcd_raw_end()
        return bytes(out)`,
'encode 尾部');

mustReplace(
`        SEG = 2048
        for i in range(0, len(frame), SEG):
            if not self.transport.write(frame[i:i+SEG]):
                return False
            time.sleep(0.003)
        time.sleep(0.02)
        self.transport.read_all()
        return True`,
`        SEG = 8192
        for i in range(0, len(frame), SEG):
            if not self.transport.write(frame[i:i+SEG]):
                return False
        self.transport.read_all()
        return True`,
'show_image');

if (crlf) s = s.split('\n').join('\r\n');
fs.writeFileSync(p, s, 'utf8');
console.log('patched: ' + before + ' -> ' + s.length);