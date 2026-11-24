import logging
import re
import time
from PIL import Image, ImageDraw
from msu2_core import BOTH, LANDSCAPE, Field, Widget, get_font, register

log = logging.getLogger(__name__)

_WEEK = ["一", "二", "三", "四", "五", "六", "日"]
_HEX_COLOR = re.compile(r"^#?[0-9a-fA-F]{6}$")
_BAD_COLORS = set()


def _parse_color(value, default):
    """把 #RRGGBB / RRGGBB 规范成 #RRGGBB；非法值回退默认并提示一次。"""
    text = str(value or "").strip()
    if _HEX_COLOR.match(text):
        return ("#" + text.lstrip("#")).upper()
    if text and text not in _BAD_COLORS:
        _BAD_COLORS.add(text)
        log.warning("时钟颜色值无效: %r，已回退默认 %s", text, default)
    return default


@register
class ClockWidget(Widget):
    name = "clock"
    interval = 1.0
    orientations = BOTH
    priority = 10

    config_schema = [
        Field("time_color", "text", "时间颜色（#RRGGBB）", default="#FFFFFF",
              help="例如 #FFCC00；非法值自动回退默认"),
        Field("date_color", "text", "日期颜色（#RRGGBB）", default="#B0B0B0",
              help="例如 #FFCC00；非法值自动回退默认"),
        Field("week_color", "text", "星期颜色（#RRGGBB）", default="#B0B0B0",
              help="例如 #FFCC00；非法值自动回退默认"),
    ]

    def on_enter(self, ctx):
        ctx.state.pop("_clk_key", None)

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        W, H = ctx.size
        t = time.localtime(ctx.now)
        hh = time.strftime("%H", t)
        mm = time.strftime("%M", t)
        ss = time.strftime("%S", t)
        ymd = time.strftime("%Y-%m-%d", t)
        week = "周" + _WEEK[t.tm_wday]
        cfg = self.get_config(ctx)
        t_col = _parse_color(cfg.get("time_color"), "#FFFFFF")
        d_col = _parse_color(cfg.get("date_color"), "#B0B0B0")
        w_col = _parse_color(cfg.get("week_color"), "#B0B0B0")
        key = (ctx.orientation, W, H, ymd, week, hh, mm, ss,
               t_col, d_col, w_col)
        if ctx.state.get("_clk_key") == key:
            return None
        ctx.state["_clk_key"] = key
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        margin = max(3, W // 32)
        if ctx.orientation == LANDSCAPE:
            f_t = self._fit_time(f"{hh}:{mm}:{ss}", W - 2 * margin,
                                 max(18, min(36, H // 2)))
            f_s = get_font(max(9, min(14, H // 6)), cjk=True)
            cy = H // 2 - max(2, H // 12)
            d.text((W // 2, cy), f"{hh}:{mm}:{ss}",
                   fill=t_col, font=f_t, anchor="mm")
            base_y = H - max(3, H // 20)
            d.text((margin, base_y), ymd, fill=d_col, font=f_s, anchor="ls")
            d.text((W - margin, base_y), week, fill=w_col, font=f_s, anchor="rs")
        else:
            f_big = get_font(max(18, min(56, H // 4)), bold=True, cjk=False)
            f_ss = get_font(max(14, min(30, H // 7)), cjk=False)
            f_s = get_font(max(9, min(15, H // 12)), cjk=True)
            d.text((W // 2, H * 15 // 100), hh, fill=t_col, font=f_big, anchor="mm")
            d.text((W // 2, H * 41 // 100), mm, fill=t_col, font=f_big, anchor="mm")
            d.text((W // 2, H * 5 // 8), ss, fill=t_col, font=f_ss, anchor="mm")
            d.text((W // 2, H * 79 // 100), ymd, fill=d_col, font=f_s, anchor="mm")
            d.text((W // 2, H * 9 // 10), week, fill=w_col, font=f_s, anchor="mm")
        return img

    @staticmethod
    def _fit_time(text, max_w, max_size):
        size = max(16, int(max_size))
        while size > 16:
            f = get_font(size, bold=True, cjk=False)
            try:
                if f.getlength(text) <= max_w:
                    return f
            except Exception:
                return f
            size -= 2
        return get_font(16, bold=True, cjk=False)