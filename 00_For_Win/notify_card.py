"""右下角通知卡片：**只有点右上角 ✕ 才会消失**（不自动消失）。

由 `winenv._notify_windows_dialog()` 以 **pythonw** 另起一个进程调用：

    pythonw.exe notify_card.py

为什么是窗口而不是 Toast：Toast 横幅会被 Windows 按时收走（2026-09-21 实测，
即便写了 `scenario="urgent"` + `duration="long"`），用户要的是"看得见、点掉才走"。
为什么用 pythonw 而不是 powershell：pythonw 是窗口子系统程序，**不会有控制台窗口**。

所有内容都从环境变量取（不拼命令行，中文/引号/换行都不会坏）：
    WB_DLG_TITLE   标题（调用方已截断）
    WB_DLG_BODY    正文（调用方已截断）
    WB_DLG_SLOT    屏幕上已存在的卡片张数（0 最靠下，往上叠）
    WB_DLG_MARK    标记文件：启动时写入自己的 PID，关闭时删除
    WB_DLG_DEBUG   可选：把几何信息追加到这个文件（排错用）
"""

from __future__ import annotations

import ctypes
import json
import os
import pathlib
import time
import tkinter as tk

MARK = os.environ.get("WB_DLG_MARK", "")
DEBUG = os.environ.get("WB_DLG_DEBUG", "")
TITLE = os.environ.get("WB_DLG_TITLE", "")
BODY = os.environ.get("WB_DLG_BODY", "")

BG = "#2b2b2b"
FG = "#ebebeb"
ACCENT = "#0078d4"
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_BODY = ("Microsoft YaHei UI", 9)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _work_area() -> tuple[int, int, int, int]:
    """返回**排除任务栏**后的工作区 (left, top, right, bottom)；取不到退回全屏。"""
    try:
        rect = _RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                                      ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    try:
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    finally:
        root.destroy()


def _slot() -> int:
    try:
        return max(0, int(os.environ.get("WB_DLG_SLOT", "0") or 0))
    except ValueError:
        return 0


def _write_pid() -> None:
    """★ 用自己的 PID 覆盖标记：父进程拿到的 Popen.pid 未必是本进程。"""
    if not MARK:
        return
    try:
        pathlib.Path(MARK).write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    root = tk.Tk()
    root.overrideredirect(True)      # 无边框 → 像一张通知卡片
    root.attributes("-topmost", True)  # 置顶
    root.configure(bg=BG)

    pad = 16
    wrap = 348

    tk.Frame(root, bg=ACCENT, height=3).pack(fill="x")

    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=pad, pady=(10, 0))
    tk.Label(head, text=TITLE, fg="#ffffff", bg=BG, font=FONT_TITLE,
             anchor="w").pack(side="left", fill="x", expand=True)
    close = tk.Label(head, text="✕", fg="#dcdcdc", bg=BG,
                     font=("Segoe UI", 10, "bold"), cursor="hand2")
    close.pack(side="right")
    close.bind("<Button-1>", lambda _e: root.destroy())
    close.bind("<Enter>", lambda _e: close.configure(fg="#ffffff"))
    close.bind("<Leave>", lambda _e: close.configure(fg="#dcdcdc"))

    tk.Label(root, text=BODY, fg=FG, bg=BG, justify="left", anchor="nw",
             wraplength=wrap, font=FONT_BODY).pack(fill="x", padx=pad, pady=(6, 14))

    root.update_idletasks()
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    left, top, right, bottom = _work_area()
    margin = 12
    x = right - width - margin
    y = bottom - height - margin - _slot() * (height + 10)
    if y < top + margin:
        y = top + margin
    root.geometry("+%d+%d" % (x, y))

    if DEBUG:
        try:
            with open(DEBUG, "a", encoding="utf-8") as f:
                f.write("work_area=({},{},{},{}) size={}x{} at=({},{}) slot={} pid={}\n".format(
                    left, top, right, bottom, width, height, x, y, _slot(), os.getpid()))
        except OSError:
            pass

    _write_pid()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    finally:
        if MARK:
            try:
                pathlib.Path(MARK).unlink()
            except OSError:
                pass
