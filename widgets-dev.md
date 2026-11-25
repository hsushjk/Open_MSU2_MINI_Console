# 插件开发手册

本文面向要给上位机写插件的人。插件是一段 Python 代码：上位机按固定间隔调用它，它返回一张 160×80 的图像，上位机负责编码并通过 USB 串口送到设备。插件不需要关心串口、编码和协议。

本文只讲插件接口。设备协议见固件仓库的 README，上位机的安装与命令行参数见本仓库 README。

## 1. 代码位置

| 路径 | 作用 |
|---|---|
| `widgets/` | 内置插件，随仓库一起分发 |
| `widgets/__init__.py` | 插件加载器（内置 + 外挂），不需要改 |
| `widgets/_util.py` | 文本测量、截断、滚动等公共函数 |
| `msu2_core/widget.py` | `Widget` 基类、`RenderContext`、字体 |
| `msu2_core/registry.py` | 注册表 `WIDGET_REGISTRY` 与 `register` |
| `msu2_core/config_schema.py` | 声明式配置 `Field` 与合并逻辑 |
| `msu2_core/branding.py` | `data_dir()`：程序同目录的数据目录 |
| `example/widgets/hello_widget.py` | 外挂插件示例（含动作、触控、配置） |

外挂插件目录按以下顺序扫描，路径不存在就跳过：

1. 环境变量 `MSU2_WIDGETS_DIR`（可写多个，Windows 用 `;` 分隔，其它平台用 `:`）
2. 打包版（exe）：exe 同目录的 `widgets\`
3. 源码版：程序目录的 `widgets_ext\`
4. 用户目录：`%LOCALAPPDATA%\MSU2_MINI\widgets`，非 Windows 为 `$XDG_DATA_HOME/MSU2_MINI/widgets`（缺省 `~/.local/share`）

`python msu2_mini.py --plugins` 会打印实际扫描到的目录、加载失败的插件，以及全部插件的名称、朝向、优先级和来源（内置/外挂）。

## 2. 最小插件

在 `widgets/` 下新建 `hello.py`：

```python
from PIL import Image, ImageDraw

from msu2_core import BOTH, Widget, get_font, register


@register
class HelloWidget(Widget):
    """在屏幕中央显示一行文字。"""      # 第一行 docstring 会成为 Web 上的插件描述

    name = "hello"                      # 唯一 ID，不能与其它插件重名
    interval = 1.0                      # 期望的刷新间隔（秒）
    orientations = BOTH                 # BOTH / (LANDSCAPE,) / (PORTRAIT,)
    priority = 100                     # 越小越靠前，影响默认顺序

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(img).text(
            (W // 2, H // 2), "Hello", fill="white",
            font=get_font(24), anchor="mm")
        return img
```

保存后启动上位机：源码运行会在日志里看到 `插件模块加载完成: 成功 N 个 (... hello)`；Web 页面（默认 `http://127.0.0.1:8765`）的插件列表里出现 `hello`，启用后小屏显示。程序运行中新增或修改插件文件，约 2 秒后会自动重载；也可以点 Web 页面的「重载插件」。

## 3. Widget 类属性

| 属性 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | `str` | 必填 | 唯一 ID，也是配置键名 |
| `interval` | `float` | `1.0` | 期望刷新间隔（秒）。实际最快受单帧传输耗时限制，见第 12 节 |
| `orientations` | `tuple` | `BOTH` | 支持的朝向：`BOTH`、`(LANDSCAPE,)`、`(PORTRAIT,)` |
| `priority` | `int` | `50` | 越小越靠前；决定 Web 列表默认顺序和首次生成配置的顺序 |
| `config_schema` | `list[Field]` | `[]` | 声明式配置，见第 8 节 |
| `actions` | `dict[str, str]` | `{}` | Web 上的按钮：`{动作名: 按钮文字}`，见第 9 节 |
| `touch_help` | `str` | `""` | 触控说明，显示在 Web 的插件行 |

插件与内置同名时会覆盖内置实现，日志里打印 `插件 X 被重复注册，后者覆盖前者`。不要依赖这个行为，除非确实要替换内置插件。

## 4. 生命周期

| 方法 | 调用时机 | 常见用途 |
|---|---|---|
| `setup(self, ctx)` | 插件实例创建后 | 读文件、开后台线程、初始化状态 |
| `teardown(self, ctx)` | 程序退出、热重载、改配置重建前 | 停线程、关文件、释放资源 |
| `on_enter(self, ctx)` | 切换到本插件所在页 | 清缓存，强制下一帧重绘 |
| `on_leave(self, ctx)` | 离开本插件所在页 | 暂停计时、保存状态 |
| `render(self, ctx)` | 每 `interval` 秒一次（仅在当前页） | 返回一帧图像，或 `None` 表示不重绘 |
| `get_config(self, ctx)` | 自行调用 | 读取本插件配置（已用默认值补齐） |
| `on_action(self, action, payload, ctx)` | Web 按钮或触控事件 | 返回 `{"ok": bool, "err": str}` |

`render` 必须是同步返回；耗时操作放后台线程。`render` 抛异常会被捕获并记录，渲染循环继续，但每帧抛异常会刷满日志，所以内部要自己兜底。

## 5. RenderContext

| 字段 | 类型 | 说明 |
|---|---|---|
| `ctx.size` | `(W, H)` | 当前朝向下的画布尺寸：横屏 `(160, 80)`，竖屏 `(80, 160)` |
| `ctx.orientation` | `str` | `"landscape"` 或 `"portrait"` |
| `ctx.now` | `float` | 当前时间戳（`time.time()`） |
| `ctx.state` | `dict` | 本插件私有、跨帧保留的状态字典 |
| `ctx.config` | `dict` | 全局配置（只读，不要修改） |
| `ctx.device` | `MiniDevice` | 底层设备对象，插件一般不需要用 |

`ctx.state` 只在当前插件实例内有效：热重载、改配置（会重建插件）、程序退出都会清空。需要跨重启保留的数据按第 11 节落盘。

## 6. 渲染约定

- 画布尺寸用 `ctx.size` 取，不要写死 160×80：竖屏是 80×160。
- 推荐返回 `RGBA` 图像，alpha 会和背景（纯色、图片或 GIF）合成；返回 `RGB` 按不透明处理。
- 返回 `None` 表示"这一帧不用重画"。内容没变时返回 `None` 可以省掉一次完整传输，例如用缓存键判断：

```python
def render(self, ctx):
    cfg = self.get_config(ctx)
    key = (ctx.orientation, cfg["title"], ctx.state.get("msg"))
    if ctx.state.get("_key") == key:
        return None
    ctx.state["_key"] = key
    ...
    return img
```

- 单帧渲染尽量控制在 50 ms 以内。上位机是单线程调度，渲染慢会直接压低帧率。
- 字体用 `get_font(size, bold=False, cjk=True)`（带缓存）。内置 NotoSansMonoCJKsc，中英文都能显示；`probe_fonts()` 列出可用字体，`set_user_font()` 可换用户字体。字号建议 8–40，横屏常用 10–24。
- 长文本用 `widgets/_util.py` 提供的函数：`text_w(font, text)` 测宽、`ellipsis(font, text, max_w)` 截断、`row_text(...)` 单行滚动、`marquee(...)` 横向滚动。
- 时间敏感的动画（秒针、滚动字幕）应基于 `ctx.now` 计算，不要依赖 `interval` 精确。

## 7. 声明式配置

在类里声明 `config_schema`，Web 上该插件就会出现「设置」表单，保存后写入 `widget_config/<插件名>.json`：

```python
from msu2_core import Field

class MyWidget(Widget):
    config_schema = [
        Field("title", "str", "标题", default="你好"),
        Field("count", "int", "条数", default=5, min=1, max=20),
        Field("speed", "float", "速度", default=1.0, min=0.1, max=5.0, step=0.1),
        Field("enable", "bool", "启用", default=True),
        Field("mode", "select", "模式", default="A", options=["A", "B", "C"]),
        Field("items", "list_str", "条目", default=["a", "b"], help="每行一条"),
    ]

    def render(self, ctx):
        cfg = self.get_config(ctx)
        title = cfg["title"]          # 已被类型转换与范围钳制
```

| 参数 | 说明 |
|---|---|
| `key` | 字段名（第一个位置参数） |
| `type` | `str` / `int` / `float` / `bool` / `select` / `list_str` |
| `label` | Web 上显示的名称 |
| `default` | 默认值 |
| `help` | 字段后的说明文字 |
| `options` | 仅 `select`：可选项列表；值不在列表里时回退默认值 |
| `min` / `max` / `step` | 仅 `int` / `float`：范围与步长，读取时按 `min`/`max` 钳制 |

`list_str` 在表单里是多行文本，读取时得到 `list[str]`（最多 200 项）。`bool` 接受 `"1"/"true"/"yes"/"on"` 等写法。

配置保存位置是程序同目录的 `widget_config/<插件名>.json`，每个插件一个文件；总 `config.json` 只保留页面、设备、显示等全局设置。改配置会重建插件实例（`ctx.state` 清空），如果需要保留状态，在 `on_leave` 或 `teardown` 里自行落盘。

## 8. 动作：`actions` 与 `on_action`

`actions` 里的每一条都会在 Web 插件卡片上生成一个按钮：

```python
class TodoWidget(Widget):
    actions = {"add": "新增一条", "clear": "清空"}

    def on_action(self, action, payload, ctx):
        if action == "add":
            ctx.state.setdefault("items", []).append("[ ] 新的条目")
            return {"ok": True, "msg": "已添加"}
        if action == "clear":
            ctx.state["items"] = []
            return {"ok": True}
        return {"ok": False, "err": "未知动作: %s" % action}
```

调用路径：

- Web 按钮：`POST /api/widget_action`，`payload` 为请求 JSON 里的 `payload` 字段（没有则为 `{}`）。
- 硬件触控（当前页、且该手势设为 `passthrough`）：`on_action("touch_single", {}, ctx)`、`on_action("touch_double", {}, ctx)`、`on_action("touch_long", {}, ctx)`。

返回值必须是 dict：

- `{"ok": True}`：请求立刻重绘，并把当前配置落盘（适合"插件自己改了自己的配置"的场景）。
- `{"ok": False, "err": "..."}`：Web 上提示错误。
- 其它字段会原样返回给 Web 端，可用来传提示文本。

`on_action` 抛异常会被捕获，Web 收到 `{"ok": False, "err": "..."}`，日志里有完整堆栈。

## 9. 硬件触控

设备只有一个电容触摸通道，没有坐标。上位机按时间判断手势，相关常量：

| 常量 | 值 | 含义 |
|---|---|---|
| `TOUCH_SAMPLE_INTERVAL` | 0.03 s | 触摸采样间隔 |
| `TOUCH_LONG_PRESS` | 0.8 s | 按住多久算长按 |
| `TOUCH_DOUBLE_WINDOW` | 0.5 s | 两次单击的最大间隔（超过则第一次单击不再等待） |
| `TOUCH_MIN_TAP_GAP` | 0.012 s | 两次点击的最小间隔 |

Web 页面的「触控」区域把每种手势映射到全局动作（翻页、亮度、锁定、截图、播放宏、启动程序等）或 `passthrough`。设为 `passthrough` 时事件才交给插件。插件用 `touch_help` 写一行说明，会显示在 Web 的插件行上。

注意双击判定：如果也配置了单击处理，单击回调会晚 `double_window` 才触发（上位机要先确认不是双击）。

## 10. 数据持久化

| 数据 | 位置 | 说明 |
|---|---|---|
| 帧间状态 | `ctx.state` | 实例内有效，重建即丢 |
| 插件配置 | `widget_config/<插件名>.json` | 由 `config_schema` 自动管理 |
| 自建数据文件 | `data_dir()` | 程序同目录；用插件名前缀 |

```python
from msu2_core.branding import data_dir

def _state_path():
    return data_dir() / "my_plugin_state.json"
```

不要把文件写到 `%LOCALAPPDATA%`、系统临时目录或用户主目录：上位机的约定是"所有数据都在程序同目录"，方便整个文件夹拷走。写文件用"临时文件 + `os.replace`"保证原子性，读取时对损坏的 JSON 做兜底。

## 11. 后台线程与耗时操作

需要网络、磁盘、抓屏的插件，把耗时操作放到后台线程，`render` 只读最新快照：

```python
import threading

class StatsWidget(Widget):
    def setup(self, ctx):
        self._lock = threading.Lock()
        self._latest = None
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        while not self._stop.wait(2.0):
            try:
                data = fetch_something()
            except Exception:
                continue                      # 后台线程异常必须自己吃掉
            with self._lock:
                self._latest = data

    def teardown(self, ctx):
        self._stop.set()
        self._th.join(timeout=1.5)            # 必须等线程退出

    def render(self, ctx):
        with self._lock:
            data = self._latest
        if data is None:
            return None
        ...
```

约束：

- 线程必须是 daemon，并在 `teardown` 里停掉并 `join`，否则热重载会留下旧线程。
- 不要在 `render` 里 sleep、等网络或用阻塞锁。
- 不要跨线程共用不支持并发的对象（例如抓屏库的实例、tkinter 对象）；这类库通常要求"每个线程一份实例"。
- 后台线程里抛出的异常不会中断程序，但线程会静默退出，务必自己 `try/except` 并记录。

## 12. 帧率与 `interval` 的实际含义

`interval` 是"希望多久渲染一次"，不是保证值。上位机在发送一帧后还会按实测传输耗时设置一个最小帧间隔（约传输耗时的 1.15 倍，最多 0.6 秒），避免把设备接收缓冲顶满。所以：

- 小画面（1–3 KB/帧）通常能跑到 20–50 fps；
- 整屏 160×80 RGB565 一帧约 25.6 KB，Windows 下约 200 ms 一帧，约 5 fps；
- 把 `interval` 设成 0.01 不会更快，只会让调度器一直处于"没到时间"的状态。

想让画面更流畅，优先减少每帧的实际变化量：只画变化的内容、用缓存键在内容不变时返回 `None`、少用大面积渐变和抖动图案（它们会让每帧字节数变大）。

## 13. 外挂插件

- 单个文件：`<外挂目录>/my_plugin.py`
- 包：`<外挂目录>/my_plugin/__init__.py`（可以带同目录的辅助模块和数据文件）

两者都能被自动发现；包适合带资源文件或多个模块的插件。外挂插件与内置插件的写法完全一致（同样的 `Widget` 基类、配置、动作）。

依赖声明三种位置任选其一：

- `<插件名>.requirements.txt`（单文件插件的常见做法）
- 外挂目录里的 `requirements.txt`（该目录所有插件共用）
- 包内的 `requirements.txt`

缺失依赖时会先尝试用当前 Python 执行 `pip install`；打包版没有独立 Python 时，直接从 PyPI 下载纯 Python（或与上位机 Python ABI 一致的）wheel 解压到 `_deps` 目录并加入 `sys.path`。纯 Python 依赖最省事，带编译扩展的包需要和上位机的 Python 版本/架构匹配。

新增或修改插件文件后约 2 秒自动重载；也可以在 Web 页面点「重载插件」。重载和改配置都会重建插件实例，`ctx.state` 会清空。

投屏/镜像类插件：插件名包含 `screen`、`cast`、`mirror`、`display`、`view` 时，启动会在 Web 页面弹一次提示；确认后写入 `config.json` 的 `plugin_ack` 列表，之后不再提示。删除该列表项可恢复提示。

## 14. 创建与调试

```bash
python msu2_mini.py --new-widget my_clock   # 生成模板（默认写到 widgets_ext/）
python msu2_mini.py --plugins               # 列出插件、来源目录与加载失败原因
python msu2_mini.py --log-level DEBUG       # 打开调试日志
```

常见问题：

| 现象 | 检查 |
|---|---|
| 插件没出现 | 日志里是否有 `加载插件模块 X 失败`；文件名是否以 `_` 开头（会被跳过）；是否与内置插件重名 |
| Web 里看不到 | 插件列表分「已启用 / 已停用」两栏；看朝向是否需要切换 |
| 屏幕上没变化 | `orientations` 是否包含当前朝向；`pages` 里是否启用；`render` 是否一直返回 `None` |
| 触控没反应 | 对应手势是否设为 `passthrough`；日志里 `touch_*` 是否有异常 |
| 改配置后状态丢了 | 预期行为：改配置会重建插件，需要自行落盘 |
| 配置找不到 | 插件配置在 `widget_config/<插件名>.json`，不在总 `config.json` |

## 15. 内置插件

`widgets/` 目录自带 16 个插件，可以直接阅读源码作为参考：

| 名称 | 说明 |
|---|---|
| `clock` | 时间、日期、星期，颜色可配 |
| `calendar` | 月历 |
| `countdown` | 倒数日 |
| `holiday` | 节假日/纪念日倒数 |
| `anniversary` | 纪念日列表与累计天数 |
| `pcstats` | CPU、内存、负载 |
| `network_detail` | 网络流量与速率 |
| `disk_io` | 磁盘读写速率 |
| `music` | 媒体播放信息（依赖系统接口，缺依赖时降级） |
| `todo` | 待办清单，可读写 Markdown 文件 |
| `crypto` | 汇率/报价（需要网络） |
| `clipboard` | 剪贴板历史（Windows） |
| `app_usage` | 前台应用使用时长（Windows） |
| `keyboard_stats` | 键盘按键统计（需要可选依赖） |
| `mouse_mileage` | 鼠标移动距离与点击统计（需要可选依赖） |
| `gif` | 播放 GIF 动图（作为背景页使用） |

## 16. 已知限制

- 一帧最多 25 600 字节，也就是 160×80 的 RGB565 全屏；超过会被裁掉或需要重发，表现为掉帧。
- 设备没有帧缓冲，像素是边收边画的：一帧发送过程中屏幕会自上而下刷新。大范围的整帧更新能看到这个过程，插件无法规避（差分由上位机统一处理）。
- 触控只有单击、双击、长按三种手势，没有坐标、压力或滑动。
- 插件不能直接改亮度、朝向、待机设置，这些是全局设置。
- 插件运行在上位机进程里，崩溃会带崩整个程序，务必对文件、网络、第三方库的异常做兜底。