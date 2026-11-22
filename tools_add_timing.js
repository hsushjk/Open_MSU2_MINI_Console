// 给上位机加三段计时：编码、写入、合成
const fs = require('fs');
const p = 'C:/Users/hsush/Desktop/MSU2_MINI/host/msu2_mini.py';
const raw = fs.readFileSync(p, 'utf8');
const crlf = raw.indexOf('\r\n') >= 0;
let s = raw.split('\r\n').join('\n');

const oldShow = [
    '    def show_image(self, img):',
    '        frame = self.encoder.encode(',
    '            img, raw_mode=getattr(self.transport, "raw_mode", False))',
    '        if not frame:',
    '            return True',
    '        SEG = 8192',
    '        for i in range(0, len(frame), SEG):',
    '            if not self.transport.write(frame[i:i+SEG]):',
    '                return False',
    '        self.transport.read_all()',
    '        return True'
].join('\n');

const newShow = [
    '    def show_image(self, img):',
    '        t0 = time.perf_counter()',
    '        frame = self.encoder.encode(',
    '            img, raw_mode=getattr(self.transport, "raw_mode", False))',
    '        self.enc_ms = (time.perf_counter() - t0) * 1000.0',
    '        if not frame:',
    '            return True',
    '        seg_write = 8192',
    '        for i in range(0, len(frame), seg_write):',
    '            if not self.transport.write(frame[i:i+seg_write]):',
    '                return False',
    '        self.transport.read_all()',
    '        self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms',
    '        self.frame_bytes = len(frame)',
    '        return True'
].join('\n');

if (s.indexOf(oldShow) < 0) {
    console.log('show_image pattern not found');
    process.exit(1);
}
s = s.split(oldShow).join(newShow);

const oldComp = '    def _composite(self, widget_img, mono):';
const newComp = [
    '    def _composite(self, widget_img, mono):',
    '        t0 = time.perf_counter()',
    '        img = self._composite_impl(widget_img, mono)',
    '        cost = (time.perf_counter() - t0) * 1000.0',
    '        prev = self.metrics.get("comp_ms", cost)',
    '        self.metrics["comp_ms"] = prev * 0.8 + cost * 0.2',
    '        return img',
    '',
    '    def _composite_impl(self, widget_img, mono):'
].join('\n');

if (s.indexOf(oldComp) < 0) {
    console.log('_composite pattern not found');
    process.exit(1);
}
s = s.split(oldComp).join(newComp);

const oldMetrics = '            "render_ms": {k: round(v, 2) for k, v in top},';
const newMetrics = [
    '            "render_ms": {k: round(v, 2) for k, v in top},',
    '            "enc_ms": round(getattr(self.device, "enc_ms", 0.0), 2),',
    '            "tx_ms": round(getattr(self.device, "tx_ms", 0.0), 2),',
    '            "frame_bytes": int(getattr(self.device, "frame_bytes", 0)),',
    '            "composite_ms": round(self.metrics.get("comp_ms", 0.0), 2),'
].join('\n');

if (s.indexOf(oldMetrics) < 0) {
    console.log('metrics pattern not found');
    process.exit(1);
}
s = s.split(oldMetrics).join(newMetrics);

fs.writeFileSync(p, crlf ? s.split('\n').join('\r\n') : s, 'utf8');
console.log('timing added: enc/tx/composite + metrics fields');