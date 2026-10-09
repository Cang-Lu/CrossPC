"""图形界面: 设置几台电脑的相对位置。

为什么需要一个界面: 这个工具的核心配置就是"Deiban 的屏幕在 Windows 的哪一边、
上边缘对齐了没有"。纯文本坐标既难写也难验证 —— 差 100 像素就会出现"鼠标顶到
屏幕边过不去"这种很难查的问题, 所以画布上直接拖, 松手自动吸附成无缝拼接。

界面只写配置文件, 不安装钩子、不接管键鼠, 所以在 server 上随时可以放心打开。
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

from . import __version__
from .backend import BackendError, get_backend
from .config import Config, ClientEntry, SizeCache, default_config_path
from .hotkey import HotkeyError, make_hotkeys
from .layout import Rect, relative_direction
from .net import local_ipv4_addresses
from .util import Log

# 画布配色
COLOR_BG = "#1e2430"
COLOR_SERVER = "#2d4a6b"
COLOR_CLIENT = "#2f5d4a"
COLOR_CLIENT_SEL = "#3f7f65"
COLOR_EDGE = "#8fb8e0"
COLOR_TEXT = "#e8eef6"
COLOR_GRID = "#2a3240"
COLOR_HINT = "#8b98a8"

#: 拖动时的吸附容差(虚拟像素)
SNAP_TOLERANCE = 30
#: "只对齐坐标不贴边"时, 两个矩形在另一个方向上允许的最大间距
SNAP_ALIGN_GAP = 200


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return min(a1, b1) - max(a0, b0)


def _gap(a0: int, a1: int, b0: int, b1: int) -> int:
    """两段区间之间的距离(重叠时为 0)。"""
    if _overlap(a0, a1, b0, b1) > 0:
        return 0
    return min(abs(a0 - b1), abs(b0 - a1))


class LayoutCanvas(tk.Canvas):
    """画虚拟桌面, 拖动 client 方块设置相对位置。"""

    def __init__(self, master, app: "GuiApp", **kw):
        super().__init__(master, background=COLOR_BG, highlightthickness=0, **kw)
        self.app = app
        self.scale = 0.1
        self.offset = (0, 0)
        self._drag: Optional[str] = None
        self._drag_start = (0, 0)
        self._drag_rect0 = Rect()
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ------------------------------------------------------------ 坐标换算
    def _compute_transform(self) -> None:
        rects = self.app.all_rects()
        if not rects:
            return
        bounds = rects[0]
        for r in rects[1:]:
            bounds = bounds.union(r)
        w = max(self.winfo_width(), 100)
        h = max(self.winfo_height(), 100)
        margin = 40
        sx = (w - 2 * margin) / max(bounds.w, 1)
        sy = (h - 2 * margin) / max(bounds.h, 1)
        self.scale = max(min(sx, sy), 0.005)
        # 让内容居中: 计算偏移
        content_w = bounds.w * self.scale
        content_h = bounds.h * self.scale
        self.offset = (int((w - content_w) / 2 - bounds.x * self.scale),
                       int((h - content_h) / 2 - bounds.y * self.scale))
        self._bounds = bounds

    def to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        return (x * self.scale + self.offset[0], y * self.scale + self.offset[1])

    def to_virtual(self, cx: float, cy: float) -> Tuple[int, int]:
        return (int((cx - self.offset[0]) / self.scale),
                int((cy - self.offset[1]) / self.scale))

    # ------------------------------------------------------------ 绘制
    def redraw(self) -> None:
        self.delete("all")
        self._compute_transform()
        self._draw_grid()
        for name, rect, is_server, selected in self.app.machines_for_canvas():
            self._draw_machine(name, rect, is_server, selected)
        self._draw_hint()

    def _draw_grid(self) -> None:
        bounds = getattr(self, "_bounds", None)
        if bounds is None:
            return
        step = 500
        x = (bounds.x // step) * step
        while x <= bounds.right:
            cx, _ = self.to_canvas(x, bounds.y)
            self.create_line(cx, 0, cx, self.winfo_height(), fill=COLOR_GRID)
            x += step
        y = (bounds.y // step) * step
        while y <= bounds.bottom:
            _, cy = self.to_canvas(bounds.x, y)
            self.create_line(0, cy, self.winfo_width(), cy, fill=COLOR_GRID)
            y += step

    def _draw_machine(self, name: str, rect: Rect, is_server: bool,
                      selected: bool) -> None:
        x0, y0 = self.to_canvas(rect.x, rect.y)
        x1, y1 = self.to_canvas(rect.right, rect.bottom)
        fill = (COLOR_SERVER if is_server else
                (COLOR_CLIENT_SEL if selected else COLOR_CLIENT))
        self.create_rectangle(x0, y0, x1, y1, fill=fill, outline=COLOR_EDGE,
                              width=2 if selected else 1)
        tag = "本机 (server)" if is_server else name
        self.create_text((x0 + x1) / 2, (y0 + y1) / 2 - 12, text=tag,
                         fill=COLOR_TEXT, font=("Segoe UI", 11, "bold"))
        self.create_text((x0 + x1) / 2, (y0 + y1) / 2 + 10,
                         text="%d x %d" % (rect.w, rect.h),
                         fill=COLOR_TEXT, font=("Segoe UI", 9))
        self.create_text((x0 + x1) / 2, (y0 + y1) / 2 + 28,
                         text="(%d, %d)" % (rect.x, rect.y),
                         fill=COLOR_HINT, font=("Segoe UI", 8))

    def _draw_hint(self) -> None:
        self.create_text(12, 10, anchor="nw", fill=COLOR_HINT,
                         font=("Segoe UI", 9), text=(
                             "拖动方块设置相对位置; 松手会自动吸附成无缝拼接。\n"
                             "鼠标从本机方块推到相邻方块的那条边, 就会跑到那台电脑。"))

    # ------------------------------------------------------------ 拖动
    def _hit(self, cx: float, cy: float) -> Optional[str]:
        for name, rect, is_server, _ in reversed(self.app.machines_for_canvas()):
            if is_server:
                continue
            x0, y0 = self.to_canvas(rect.x, rect.y)
            x1, y1 = self.to_canvas(rect.right, rect.bottom)
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return name
        return None

    def _on_press(self, event) -> None:
        name = self._hit(event.x, event.y)
        if name is None:
            self.app.select(None)
            return
        self.app.select(name)
        self._drag = name
        self._drag_start = self.to_virtual(event.x, event.y)
        entry = self.app.cfg.client_by_name(name)
        self._drag_rect0 = entry.rect if entry and entry.rect else Rect()

    def _on_motion(self, event) -> None:
        if self._drag is None:
            return
        vx, vy = self.to_virtual(event.x, event.y)
        dx = vx - self._drag_start[0]
        dy = vy - self._drag_start[1]
        self.app.move_client(self._drag,
                             self._drag_rect0.x + dx, self._drag_rect0.y + dy,
                             snap=False)
        self.redraw()

    def _on_release(self, event) -> None:
        if self._drag is None:
            return
        name = self._drag
        self._drag = None
        self.app.snap_client(name)
        self.redraw()


class GuiApp:
    def __init__(self, root: tk.Tk, cfg: Config, log: Log):
        self.root = root
        self.cfg = cfg
        self.log = log
        self.selected: Optional[str] = None
        self.sizes = SizeCache(SizeCache.default_path(cfg.path))
        self.server_rect = Rect(0, 0, 1920, 1080)
        self.monitors: List[Rect] = []
        self.ips: List[str] = local_ipv4_addresses()
        self._probe()

        root.title("CrossPC %s —— 设置电脑的相对位置" % __version__)
        root.geometry("1080x680")
        root.minsize(860, 520)
        try:
            root.tk.call("tk", "scaling", 1.2)
        except Exception:
            pass

        self._build()
        self.refresh_list()
        self.canvas.redraw()
        self._update_status()

    # ------------------------------------------------------------ 探测
    def _probe(self) -> None:
        try:
            backend = get_backend(self.log.debug)
            backend.prepare()
            self.server_rect = backend.desktop_rect()
            self.monitors = backend.monitors()
            self.cfg.server_screen = (self.server_rect.w, self.server_rect.h)
            self.probe_error = ""
        except Exception as exc:
            self.probe_error = str(exc)
            if self.cfg.server_screen:
                self.server_rect = Rect(0, 0, *self.cfg.server_screen)
            self.log.warn("探测本机分辨率失败: %s" % exc)

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        left = ttk.Frame(self.root, padding=8)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self.root, padding=(0, 8, 8, 0))
        right.pack(side="right", fill="both", expand=True)

        ttk.Label(left, text="机器", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.listbox = tk.Listbox(left, height=8, width=26, exportselection=False)
        self.listbox.pack(fill="x", pady=(2, 4))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="添加 client", command=self.add_client).pack(
            side="left", expand=True, fill="x")
        ttk.Button(btns, text="删除", command=self.del_client).pack(
            side="left", expand=True, fill="x")

        self.form = ttk.LabelFrame(left, text="属性", padding=6)
        self.form.pack(fill="x", pady=8)
        self.vars: Dict[str, tk.Variable] = {}
        self.entries: Dict[str, ttk.Entry] = {}
        rows = [
            ("name", "名称"),
            ("host", "对方 IP(仅 client)"),
            ("w", "屏幕宽(px)"),
            ("h", "屏幕高(px)"),
            ("x", "左边位置 x"),
            ("y", "上边位置 y"),
        ]
        for i, (key, label) in enumerate(rows):
            ttk.Label(self.form, text=label).grid(row=i, column=0, sticky="w",
                                                  pady=1)
            var = tk.StringVar()
            ent = ttk.Entry(self.form, textvariable=var, width=16)
            ent.grid(row=i, column=1, sticky="ew", pady=1)
            ent.bind("<Return>", lambda e: self.apply_form())
            self.vars[key] = var
            self.entries[key] = ent
        self.form.columnconfigure(1, weight=1)
        ttk.Button(self.form, text="应用修改", command=self.apply_form).grid(
            row=len(rows), column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # 全局设置
        g = ttk.LabelFrame(left, text="全局", padding=6)
        g.pack(fill="x")
        for i, (key, label) in enumerate([("port", "端口"), ("token", "口令"),
                                          ("name", "本机名"),
                                          ("panic", "紧急热键"),
                                          ("lock", "锁定热键")]):
            ttk.Label(g, text=label).grid(row=i, column=0, sticky="w")
            var = tk.StringVar()
            ttk.Entry(g, textvariable=var, width=16).grid(row=i, column=1,
                                                          sticky="ew")
            self.vars["g_" + key] = var
        self.vars["clip"] = tk.BooleanVar()
        ttk.Checkbutton(g, text="同步剪辑板", variable=self.vars["clip"]).grid(
            row=5, column=0, columnspan=2, sticky="w")
        self.vars["clip_img"] = tk.BooleanVar()
        ttk.Checkbutton(g, text="同步图片(截图)",
                        variable=self.vars["clip_img"]).grid(
            row=6, column=0, columnspan=2, sticky="w")
        g.columnconfigure(1, weight=1)

        self.canvas = LayoutCanvas(right, self)
        self.canvas.pack(fill="both", expand=True)

        bottom = ttk.Frame(right)
        bottom.pack(fill="x", pady=(6, 0))
        self.status = ttk.Label(bottom, text="", justify="left", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="重新探测本机分辨率",
                   command=self.reprobe).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="保存配置", command=self.save).pack(side="right")

        self._load_globals()

    # ------------------------------------------------------------ 数据映射
    def all_rects(self) -> List[Rect]:
        out = [self.server_rect]
        for c in self.cfg.enabled_clients():
            r = self._rect_of(c)
            if r:
                out.append(r)
        return out

    def _rect_of(self, entry: ClientEntry) -> Optional[Rect]:
        if entry.rect:
            return entry.rect
        size = self.sizes.get(entry.name)
        if size:
            return Rect(0, 0, size[0], size[1])
        return None

    def machines_for_canvas(self):
        out = [("本机", self.server_rect, True, False)]
        for c in self.cfg.enabled_clients():
            r = self._rect_of(c)
            if r is None:
                # 还没连接过、也没有尺寸: 给个占位方块, 用户先摆位置
                r = Rect(0, 0, 1920, 1080)
                c.rect = r
            out.append((c.name, r, False, c.name == self.selected))
        return out

    def move_client(self, name: str, x: int, y: int, snap: bool = True) -> None:
        entry = self.cfg.client_by_name(name)
        if entry is None:
            return
        r = self._rect_of(entry) or Rect(0, 0, 1920, 1080)
        entry.rect = Rect(int(x), int(y), r.w, r.h)
        if snap:
            self.snap_client(name)
        self._sync_form()
        self._update_status()

    def snap_client(self, name: str) -> None:
        """松手后吸附。

        x 和 y 两个轴**独立**求解: 一个轴负责"无缝贴边"(两个矩形共边),
        另一个轴负责"对齐"(上边缘对齐/居中)。之前把两类候选混在一个评分里,
        结果"本来就对齐"的那个轴永远拿 0 分, 贴边反而永远轮不上 —— 拖到离
        边缘 12 像素的地方松手, 会留下一道缝, 鼠标就过不去了。
        """
        entry = self.cfg.client_by_name(name)
        if entry is None or entry.rect is None:
            return
        r = entry.rect
        layout = self.cfg.build_layout(self.server_rect, self.sizes.sizes)
        others = [m.rect for m in layout.machines if m.name != name]
        bx, dx = self._best_axis(r, others, "x")
        by, dy = self._best_axis(r, others, "y")
        entry.rect = Rect(
            bx if (bx is not None and dx is not None and dx <= SNAP_TOLERANCE)
            else r.x,
            by if (by is not None and dy is not None and dy <= SNAP_TOLERANCE)
            else r.y,
            r.w, r.h)

    @staticmethod
    def _best_axis(r: Rect, others: List[Rect], axis: str):
        """在 axis 方向上给出最佳候选坐标与偏差量。"""
        if axis == "x":
            along = (r.x, r.right)
            perp = (r.y, r.bottom)
            size = r.w
        else:
            along = (r.y, r.bottom)
            perp = (r.x, r.right)
            size = r.h
        best_val: Optional[int] = None
        best_d: Optional[int] = None
        for o in others:
            o_along = (o.x, o.right) if axis == "x" else (o.y, o.bottom)
            o_perp = (o.y, o.bottom) if axis == "x" else (o.x, o.right)
            cands: List[int] = []
            if _overlap(*perp, *o_perp) > 0:
                # 无缝贴边: 本边在另一个方向上要有重叠, 否则贴上去很怪
                cands.append(o_along[0] - size)
                cands.append(o_along[1])
            if _gap(*perp, *o_perp) <= SNAP_ALIGN_GAP:
                # 只对齐坐标(不贴边), 两个矩形在另一个方向上别离太远
                mid = (o_along[0] + o_along[1]) // 2
                cands.append(o_along[0])
                cands.append(o_along[1] - size)
                cands.append(mid - size // 2)
            for cand in cands:
                d = abs(along[0] - cand)
                if best_d is None or d < best_d:
                    best_val, best_d = cand, d
        return best_val, best_d

    # ------------------------------------------------------------ 列表/表单
    def refresh_list(self) -> None:
        self.listbox.delete(0, "end")
        self.listbox.insert("end", "本机 (server) %dx%d"
                            % (self.server_rect.w, self.server_rect.h))
        for c in self.cfg.enabled_clients():
            marker = " *" if c.name == self.selected else ""
            self.listbox.insert("end", "%s%s" % (c.name, marker))
        self._sync_form()

    def select(self, name: Optional[str]) -> None:
        self.selected = name
        self.refresh_list()
        if name:
            idx = [c.name for c in self.cfg.enabled_clients()].index(name) + 1
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(idx)
        self.canvas.redraw()

    def _on_select(self, event=None) -> None:
        sel = self.listbox.curselection()
        if not sel or sel[0] == 0:
            self.selected = None
        else:
            clients = self.cfg.enabled_clients()
            idx = sel[0] - 1
            self.selected = clients[idx].name if idx < len(clients) else None
        self._sync_form()
        self.canvas.redraw()

    def _sync_form(self) -> None:
        if self.selected is None:
            for key in ("name", "host", "w", "h", "x", "y"):
                self.vars[key].set("")
            for key, ent in self.entries.items():
                ent.state(["disabled"])
            return
        for ent in self.entries.values():
            ent.state(["!disabled"])
        entry = self.cfg.client_by_name(self.selected)
        if entry is None:
            return
        r = self._rect_of(entry) or Rect(0, 0, 1920, 1080)
        self.vars["name"].set(entry.name)
        self.vars["host"].set(entry.host)
        self.vars["w"].set(str(r.w))
        self.vars["h"].set(str(r.h))
        self.vars["x"].set(str(r.x))
        self.vars["y"].set(str(r.y))

    def _load_globals(self) -> None:
        self.vars["g_port"].set(str(self.cfg.port))
        self.vars["g_token"].set(self.cfg.token)
        self.vars["g_name"].set(self.cfg.name)
        self.vars["g_panic"].set(self.cfg.hotkey_panic)
        self.vars["g_lock"].set(self.cfg.hotkey_lock)
        self.vars["clip"].set(self.cfg.clipboard_enabled)
        self.vars["clip_img"].set(self.cfg.clipboard_images)

    # ------------------------------------------------------------ 动作
    def add_client(self) -> None:
        base = "client%d" % (len(self.cfg.clients) + 1)
        name = base
        n = 1
        while self.cfg.client_by_name(name):
            n += 1
            name = "%s%d" % (base, n)
        size = (1920, 1080)
        right = max((r.right for r in self.all_rects()), default=1920)
        entry = ClientEntry(name=name, host="", rect=Rect(right, 0, *size))
        self.cfg.clients.append(entry)
        self.selected = name
        self.refresh_list()
        self.snap_client(name)
        self.canvas.redraw()
        self._update_status()

    def del_client(self) -> None:
        if not self.selected:
            messagebox.showinfo("提示", "先选中要删除的 client")
            return
        if not messagebox.askyesno("确认", "删除 client「%s」?" % self.selected):
            return
        self.cfg.clients = [c for c in self.cfg.clients if c.name != self.selected]
        self.selected = None
        self.refresh_list()
        self.canvas.redraw()

    def apply_form(self) -> None:
        if not self.selected:
            return
        entry = self.cfg.client_by_name(self.selected)
        if entry is None:
            return
        try:
            w = int(self.vars["w"].get() or 1920)
            h = int(self.vars["h"].get() or 1080)
            x = int(self.vars["x"].get() or 0)
            y = int(self.vars["y"].get() or 0)
        except ValueError:
            messagebox.showerror("输入有误", "宽/高/坐标必须是整数")
            return
        old = entry.name
        new_name = (self.vars["name"].get() or old).strip()
        if new_name != old:
            if self.cfg.client_by_name(new_name):
                messagebox.showerror("名字重复", "已经有一个叫「%s」的机器了" % new_name)
                return
            if self.sizes.get(old):
                self.sizes.set(new_name, *self.sizes.get(old))
            self.selected = new_name
        entry.name = new_name
        entry.host = self.vars["host"].get().strip()
        entry.rect = Rect(x, y, max(w, 100), max(h, 100))
        self.refresh_list()
        self.canvas.redraw()
        self._update_status()

    def reprobe(self) -> None:
        self._probe()
        self.refresh_list()
        self.canvas.redraw()
        self._update_status()

    def save(self) -> None:
        try:
            self.cfg.port = int(self.vars["g_port"].get() or self.cfg.port)
        except ValueError:
            messagebox.showerror("输入有误", "端口必须是整数")
            return
        self.cfg.token = self.vars["g_token"].get()
        self.cfg.name = (self.vars["g_name"].get() or self.cfg.name).strip()
        self.cfg.hotkey_panic = self.vars["g_panic"].get().strip()
        self.cfg.hotkey_lock = self.vars["g_lock"].get().strip()
        self.cfg.clipboard_enabled = bool(self.vars["clip"].get())
        self.cfg.clipboard_images = bool(self.vars["clip_img"].get())
        try:
            make_hotkeys(self.cfg.hotkey_panic, self.cfg.hotkey_lock)
        except HotkeyError as exc:
            messagebox.showerror("热键无效", str(exc))
            return
        # 注意: 这里不删掉"没有 rect"的 client —— 那是留给"首次连接自动登记"的
        path = self.cfg.save()
        self.status.configure(text="已保存: %s" % path)
        self._update_status("已保存到 %s" % path)
        messagebox.showinfo("已保存", "配置已写入:\n%s" % path)

    def _update_status(self, extra: str = "") -> None:
        parts = []
        if self.probe_error:
            parts.append("本机分辨率探测失败: %s" % self.probe_error)
        else:
            parts.append("本机(server) %dx%d, %d 个显示器"
                         % (self.server_rect.w, self.server_rect.h,
                            max(len(self.monitors), 1)))
        if self.ips:
            parts.append("局域网地址: %s" % ", ".join(self.ips))
        host = self.ips[0] if self.ips else "<本机IP>"
        parts.append("Debian 上运行: python3 -m crosspc client --host %s%s"
                     % (host, "" if self.cfg.port == 39987
                        else " --port %d" % self.cfg.port))
        if extra:
            parts.append(extra)
        self.status.configure(text="\n".join(parts))


def run_gui(args) -> int:
    from .cli import load_config, make_log
    cfg = load_config(args)
    log = make_log(cfg, args)
    if not cfg.path:
        cfg.path = default_config_path()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("打不开图形界面: %s" % exc, file=sys.stderr)
        if sys.platform.startswith("linux"):
            print("Debian 上需要装: sudo apt install python3-tk", file=sys.stderr)
        print("也可以直接手写配置文件: %s" % cfg.path, file=sys.stderr)
        return 1
    app = GuiApp(root, cfg, log)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    del app
    return 0
