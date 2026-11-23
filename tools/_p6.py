import io

P = r"C:\Users\hsush\Desktop\happy\Open_MSU2_MINI_Console\msu2_mini.py"
s = io.open(P, encoding="utf-8").read()
n = 0

def sub(old, new, cnt=1):
    global s, n
    assert old in s, "MISSING: " + old[:70]
    s = s.replace(old, new, cnt)
    n += 1

# ---------- 1. 提示文案与动作名 ----------
sub("""            self._run_with_notify(_act_mute_toggle, "静音状态已切换", "静音切换失败")""",
    """            threading.Thread(target=_act_mute_toggle, daemon=True).start()""")
sub("""            self._run_with_notify(_act_send_macro, "已发送键盘宏", "键盘宏发送失败",
                                  macro)""",
    """            self._run_with_notify(_act_send_macro, "已发送键盘宏", "键盘宏发送失败",
                                  macro)""")
sub("""            self._run_with_notify(_act_launch_program, "已启动程序", "启动程序失败",
                                  program)""",
    """            self._run_with_notify(_act_launch_program, "已启动可执行文件", "启动可执行文件失败",
                                  program)""")
sub("""            log.info("触摸触发关闭程序")
            self.notify("正在关闭程序…", 2.0)""",
    """            log.info("触摸触发关闭上位机")
            self.notify("上位机正在被关闭…", 2.0)""")

# 亮度提示统一说“亮度”
sub('''        elif action == "dim_up":
            # 提高亮度 = 减少压暗量
            self.set_dim(max(0, self._dim - 15))
            self.notify("伪亮度 %d%%" % (100 - self._dim), 1.0)
        elif action == "dim_down":
            self.set_dim(min(90, self._dim + 15))
            self.notify("伪亮度 %d%%" % (100 - self._dim), 1.0)''',
    '''        elif action == "dim_up":
            # 提高亮度 = 减少压暗量
            self.set_dim(max(0, self._dim - 15))
            self.notify("亮度 %d%%" % (100 - self._dim), 1.0)
        elif action == "dim_down":
            self.set_dim(min(90, self._dim + 15))
            self.notify("亮度 %d%%" % (100 - self._dim), 1.0)''')

# ---------- 2. 下拉：分组名与标签 ----------
sub('''  ["passthrough", "未定义（透传给插件）", "通用"],
  ["next_page", "下一页", "小屏"],
  ["prev_page", "上一页", "小屏"],
  ["flip", "上下翻转", "小屏"],
  ["toggle_orientation", "横竖屏切换", "小屏"],
  ["lock", "锁定/解锁当前页", "小屏"],
  ["dim_up", "伪亮度 +15%（屏幕更亮）", "小屏"],
  ["dim_down", "伪亮度 -15%（屏幕更暗）", "小屏"],
  ["screenshot", "截屏（宿主机）", "电脑"],
  ["mute_toggle", "静音 / 解除（宿主机）", "电脑"],
  ["macro", "自定义键盘宏（宿主机）", "电脑"],
  ["launch", "启动自定义程序（宿主机）", "电脑"],
  ["shutdown", "关闭程序（宿主机）", "电脑"],''',
    '''  ["passthrough", "未定义（透传给插件）", "通用"],
  ["next_page", "下一页", "小屏"],
  ["prev_page", "上一页", "小屏"],
  ["flip", "上下翻转", "小屏"],
  ["toggle_orientation", "横竖屏切换", "小屏"],
  ["lock", "锁定/解锁当前页", "小屏"],
  ["dim_up", "亮度 +15%", "小屏"],
  ["dim_down", "亮度 -15%", "小屏"],
  ["screenshot", "截屏", "宿主机"],
  ["mute_toggle", "静音 / 解除", "宿主机"],
  ["macro", "自定义键盘宏", "宿主机"],
  ["launch", "启动可执行文件", "宿主机"],
  ["shutdown", "关闭上位机", "宿主机"],''')

# ---------- 3. 存档协议：page → flags ----------
sub('''    def devcfg_info(self, timeout=0.5):''',
    '''    def devcfg_info(self, timeout=0.5):''')
sub('''                                "reverse": buf[8],
                                "page": buf[9],''',
    '''                                "reverse": buf[8],
                                "flags": buf[9],''')
sub('''    def devcfg_write(self, dim, orient, reverse, page, timeout=1.5):
        """把配置写进设备 NOR；成功返回 True。擦除+编程约 20 ms，别和发帧抢串口。"""''',
    '''    def devcfg_write(self, dim, orient, reverse, flags=0, timeout=1.5):
        """把配置写进设备 NOR；成功返回 True。擦除+编程约 20 ms，别和发帧抢串口。"""''')
sub('''                self._ser.write(MiniProtocol.devcfg_write(dim, orient,
                                                          reverse, page))''',
    '''                self._ser.write(MiniProtocol.devcfg_write(dim, orient,
                                                          reverse, flags))''')
sub('''    def devcfg_write(self, dim, orient, reverse, page):
        return self.transport.devcfg_write(dim, orient, reverse, page)''',
    '''    def devcfg_write(self, dim, orient, reverse, flags=0):
        return self.transport.devcfg_write(dim, orient, reverse, flags)''')

# ---------- 4. 连上设备：以上位机为准，把值写进设备 ----------
old = s[s.index("    def _devcfg_boot_sync(self):"):s.index("    def _flush_devcfg(self):")]
new = '''    def _devcfg_boot_sync(self):
        """连上设备后先读一眼它记的东西（只是日志），然后以上位机为准把值写进去。

        设备存档的意义是“不接上位机时它也懂自己该什么亮度/朝向”，
        一旦连上上位机，显示相关的一切听上位机的。
        """
        try:
            info = self.device.devcfg_info()
        except Exception:
            info = None
        if not info:
            self._devcfg_dirty = True
            return
        log.info("设备存档: NOR=%s 有效=%s 亮度=%d%% 朝向=%s 翻转=%s 调试=%s",
                 info["flash_id"], "是" if info["valid"] else "否",
                 100 - info["dim"], "竖屏" if info["orient"] else "横屏",
                 "开" if info["reverse"] else "关",
                 "开" if (info.get("flags") or 0) & 1 else "关")
        stored = (100 - info["dim"],
                  1 if info["orient"] else 0,
                  1 if info["reverse"] else 0)
        mine = (100 - self._dim,
                1 if self.orientation == PORTRAIT else 0,
                1 if self.reverse else 0)
        if not info["valid"] or stored != mine:
            self._devcfg_dirty = True     # 让主循环把上位机的值盖上去

'''
s = s.replace(old, new, 1)
n += 1

# ---------- 5. 写存档：不带页，日志统一说“亮度” ----------
old = s[s.index("    def _flush_devcfg(self):"):s.index("    def _save_flush(self):")] \
    if "    def _save_flush(self):" in s else None
sub('''        page = self._current if self._widgets else 0
        ok = False
        try:
            ok = self.device.devcfg_write(
                _coerce_int(self._dim, 0, 0, 90),
                1 if self.orientation == PORTRAIT else 0,
                1 if self.reverse else 0,
                page & 0xFF)''',
    '''        ok = False
        try:
            ok = self.device.devcfg_write(
                _coerce_int(self._dim, 0, 0, 90),
                1 if self.orientation == PORTRAIT else 0,
                1 if self.reverse else 0,
                0)''')
sub('''        if ok:
            log.info("设备存档已更新: 亮度 %d%% 朝向 %s 页 %s",
                     self._dim, self.orientation,
                     self._widgets[page].name if self._widgets else "-")
        else:''',
    '''        if ok:
            log.info("设备存档已更新: 亮度 %d%% 朝向 %s 翻转 %s",
                     100 - self._dim, self.orientation,
                     "开" if self.reverse else "关")
        else:''')

# ---------- 6. 换页不再触发存档写入（设备不存页） ----------
sub('''        if self.config.get("current_page") != name:
            self.config["current_page"] = name
            self._save_config()
            self._devcfg_dirty = True''',
    '''        if self.config.get("current_page") != name:
            self.config["current_page"] = name
            self._save_config()''')

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("patched:", n)