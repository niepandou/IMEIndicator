# -*- coding: utf-8 -*-
"""
IME Indicator v7 —— 中英文输入状态提示条

设计原则：**只回答一个问题 —— 现在这个窗口里，能不能直接打出中文？**

    ┌──────────────────────────┐
    │ ┃  中   中文输入          │
    │ ┃       半角              │
    └──────────────────────────┘

做法：轮询前台焦点控件的 IMM 转换状态（ImmGetContext / ImmGetDefaultIMEWnd
+ WM_IME_CONTROL）。鼠标点语言栏、点托盘输入法图标、按 Alt+Shift、按 Shift、
在系统设置里改 —— 全部会反映到同一份状态上，所以不需要猜快捷键。

  中文 = 输入法打开 且 转换模式含 IME_CMODE_NATIVE(0x1)
  全角 = 转换模式含 IME_CMODE_FULLSHAPE(0x8)

**为什么不再显示输入法名字**：同一种语言（zh-CN）下装着多个中文输入法时，
Windows 不向外部进程暴露"当前是哪一个" —— HKL 相同、注册表 InputMethodOverride
不跟手、TSF 的 GetActiveProfile 只反映调用线程、输入法自己的辅助窗口大多不可见。
本项目 v1~v6 为此写了近千行探测代码，实测仍无法稳定区分（微信输入法、微软拼音
完全没有可用信号）。既然绝大多数人只用一种中文输入法，v7 直接砍掉这层：

    中文 / 英文 两种状态，永远准确。

纯 Python 标准库（tkinter + ctypes + winreg），无任何第三方依赖。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import traceback
import winreg

import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk

VERSION = "8.3"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(__file__)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CRASH_LOG = os.path.join(APP_DIR, "crash.log")
EVENT_LOG = os.path.join(APP_DIR, "events.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "IMEIndicator"
MAX_LOG_BYTES = 2 * 1024 * 1024     # events.log 超过就截断，避免长期开日志跑爆磁盘
_pid = os.getpid()


# ==========================================================================
# 配置
# ==========================================================================
DEFAULT_CONFIG = {
    "duration": 1.6,                  # 提示条停留秒数（非常驻模式）
    # 游戏里的停留秒数。为什么单独一个：只要有一块可见窗口压在无边框全屏游戏上，
    # DWM 就会取消游戏的"独立翻转"、改成合成（实测 72→55 FPS），所以在游戏里
    # 停留越短，掉帧持续的时间越短。设成和 duration 一样就等于不缩短。
    "game_duration": 1.0,
    "always_on": False,               # True = 常驻不自动隐藏
    "margin_bottom": 38,              # 距屏幕（工作区）底部距离
    "opacity": 0.85,                  # 整体不透明度
    "transparent_bg": False,          # True = 无面板，只有文字浮在屏幕上
    "follow_mouse_monitor": True,     # 显示在鼠标所在显示器底部
    "show_on_startup": True,          # 启动时先提示一次
    "show_on_mode_change": True,      # 中/英文切换时提示
    "show_on_fullwidth_change": True, # 全角/半角切换时提示
    "show_on_caps_change": False,     # 大写锁定时提示（默认关，避免打字时多弹）
    # Windows 是"每个窗口各自记住输入法"的。切窗口时状态确实会变，但那不是用户
    # 主动切换。默认只在**同一个窗口内**变化才提示，否则切到浏览器/记事本时
    # 提示条会一直乱弹。想全部都提示就改成 True。
    "show_on_window_change": False,
    "show_fullwidth": True,           # 是否显示"全角/半角"
    # 检测间隔（毫秒）。60ms 意味着最坏 60ms 内必被发现，实测切换→弹出 <100ms。
    # 觉得费电可以调大到 100/200；打游戏时按全屏策略自动暂停。
    "poll_ms": 60,
    "hook_hotkeys": True,             # 监听 Alt+Shift 等切换键，命中后立即加速轮询
    # 学到的"这个输入法会不会自己上报全角位"，键是 HKL（如 "08040804"）。
    # 微软拼音不上报（全角时转换模式仍是 0x401），搜狗上报（0x409）。
    # 运行时会自动学，存在这里，下次启动不用重学。
    "ime_caps": {},
    # 全屏/无边框游戏时的策略（全屏判定 = 通知状态 + 窗口几何，见 detect_fullscreen）：
    #   pause  = 完全静默：停止采样、卸掉键盘钩子、收起提示条（默认，零干扰零卡顿）
    #   reduce = 降频到 0.6s 仍提示（无边框/窗口化游戏用；钩子同样卸掉）
    #   normal = 与普通窗口一样
    "fullscreen_policy": "pause",
}

# v6 及更早版本的配置项，v7 已全部废弃。检测到就备份旧文件并重写一份干净的。
LEGACY_KEYS = (
    "identity_override", "identity_manual_first", "identity_vendor_hint",
    "identity_fingerprints", "name_overrides", "preferred_tips",
    "show_on_ime_change", "width", "height",
)


def load_config():
    """读取配置：只认 DEFAULT_CONFIG 里有的键，旧键一律丢弃"""
    cfg = dict(DEFAULT_CONFIG)
    raw = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    for k in DEFAULT_CONFIG:
        if k in raw:
            cfg[k] = raw[k]
    if any(k in raw for k in LEGACY_KEYS):
        # 留着旧文件会让人以为 v7 还支持这些开关，备份后重写
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".v6.bak")
        except Exception:
            pass
        save_config(cfg)
    return cfg


def save_config(cfg):
    """先写临时文件再原子替换，避免写一半断电/崩溃把 config.json 截断"""
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}, f,
                      ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def set_autostart(enable):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_WRITE) as k:
            if enable:
                py = sys.executable.replace("python.exe", "pythonw.exe")
                if not os.path.exists(py):
                    py = sys.executable
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ,
                                  '"%s" "%s"' % (py, SCRIPT))
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except OSError:
                    pass
        return True
    except Exception:
        return False


def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            return bool(winreg.QueryValueEx(k, RUN_NAME)[0])
    except OSError:
        return False


# ==========================================================================
# Win32
# ==========================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_IME_CONTROL = 0x0283
# ⚠️ GET 是 0x0001。曾把 GETCONVERSIONMODE 写成 0x0002 —— 那是 **SET**，于是每次
# 轮询都在把前台程序改成英文，同时把"设置成功"当成读数（恒 0），永远测不出中文。
IMC_GETCONVERSIONMODE = 0x0001
IMC_GETOPENSTATUS = 0x0005
IME_CMODE_NATIVE = 0x0001
IME_CMODE_FULLSHAPE = 0x0008
SMTO_ABORTIFHUNG = 0x0002

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION = 0x00C00000
WS_POPUP = 0x80000000
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
MONITOR_DEFAULTTONEAREST = 2

WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_SHIFT, VK_CONTROL, VK_MENU, VK_SPACE, VK_LWIN = 0x10, 0x11, 0x12, 0x20, 0x5B
VK_CAPITAL = 0x14
# 低层键盘钩子上报的是左右区分的扩展键码（0xA0/0xA1…），只用 0x10 判断永远不命中
SHIFT_KEYS = (0x10, 0xA0, 0xA1)
CTRL_KEYS = (0x11, 0xA2, 0xA3)
MENU_KEYS = (0x12, 0xA4, 0xA5)

MF_BYPOSITION, MF_CHECKED, MF_SEPARATOR = 0x400, 0x8, 0x800

LPVOID = ctypes.c_void_p
_BOOL = ctypes.c_int

user32.GetForegroundWindow.restype = LPVOID
user32.GetParent.restype = LPVOID
user32.IsWindow.restype = _BOOL
user32.IsWindow.argtypes = [LPVOID]
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
user32.GetWindowThreadProcessId.argtypes = [LPVOID, ctypes.POINTER(ctypes.c_ulong)]
user32.GetKeyboardLayout.restype = LPVOID
user32.GetKeyboardLayout.argtypes = [ctypes.c_ulong]
user32.GetClassNameW.argtypes = [LPVOID, wt.LPWSTR, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [LPVOID, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [LPVOID, ctypes.c_int, ctypes.c_ssize_t]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.SendMessageTimeoutW.argtypes = [LPVOID, ctypes.c_uint, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_uint,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.SetWindowsHookExW.restype = LPVOID
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, LPVOID,
                                     ctypes.c_ulong]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [LPVOID, ctypes.c_int, ctypes.c_size_t,
                                  ctypes.c_size_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [LPVOID, ctypes.c_uint, ctypes.c_size_t,
                                  ctypes.c_ssize_t]

imm32.ImmGetContext.restype = LPVOID
imm32.ImmGetContext.argtypes = [LPVOID]
imm32.ImmReleaseContext.restype = _BOOL
imm32.ImmReleaseContext.argtypes = [LPVOID, LPVOID]
imm32.ImmGetOpenStatus.restype = _BOOL
imm32.ImmGetOpenStatus.argtypes = [LPVOID]
imm32.ImmGetConversionStatus.restype = _BOOL
imm32.ImmGetConversionStatus.argtypes = [LPVOID,
                                         ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.c_ulong)]
imm32.ImmGetDefaultIMEWnd.restype = LPVOID
imm32.ImmGetDefaultIMEWnd.argtypes = [LPVOID]

kernel32.GetModuleHandleW.restype = LPVOID
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

user32.GetWindowRect.argtypes = [LPVOID, ctypes.POINTER(wt.RECT)]
user32.MonitorFromWindow.restype = LPVOID
user32.MonitorFromWindow.argtypes = [LPVOID, ctypes.c_ulong]
user32.GetMonitorInfoW.restype = _BOOL
user32.GetMonitorInfoW.argtypes = [LPVOID, ctypes.c_void_p]
user32.IsZoomed.argtypes = [LPVOID]
user32.IsWindowVisible.argtypes = [LPVOID]
user32.PostThreadMessageW.restype = _BOOL
user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                      ctypes.c_size_t, ctypes.c_size_t]
kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
kernel32.GetCurrentThreadId.argtypes = []


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    try:
        user32.GetClassNameW(hwnd, buf, 256)
    except Exception:
        return ""
    return buf.value


def window_pid(hwnd):
    pid = ctypes.c_ulong(0)
    try:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    except Exception:
        return 0
    return pid.value


# --------------------------------------------------------------------------
# 哪些窗口不能当"输入状态来源"
#
# 点一下任务栏/开始菜单/桌面，前台会瞬间变成壳窗口，它自己的输入状态是恒英文，
# 拿去比较就会误报"切到英文了"。这类窗口一律沿用上一次真正应用窗口的结果。
# --------------------------------------------------------------------------
IGNORED_WINDOW_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Shell_FloatingTaskbarWnd",
    "NotifyIconOverflowWindow", "TrayNotifyWnd", "ReBarWindow32",
    "Shell_InputSwitchTopLevelWindow", "InputSwitchControl",
    "IconWindowImmersive", "XamlExplorerHostIslandWindow",
    "ApplicationManager_ImmersiveShellWindow", "Shell_CharmWindow",
    "ImmersiveLauncher", "MultitaskingViewFrame", "ForegroundStaging",
    "TaskListThumbnailWnd", "TaskSwitcherWnd", "AltTabWindow",
    "MainTaskbarInputPaneWindow", "Progman", "WorkerW",
}
SHELL_PROCESSES = {
    "textinputhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "searchapp.exe", "searchhost.exe",
    "lockapp.exe", "logonui.exe", "inputapp.exe",
}
_proc_name_cache = {}


def process_name(pid):
    if pid in _proc_name_cache:
        return _proc_name_cache[pid]
    name = ""
    try:
        kernel32.OpenProcess.restype = LPVOID
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, _BOOL, ctypes.c_ulong]
        kernel32.QueryFullProcessImageNameW.restype = _BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            LPVOID, ctypes.c_ulong, wt.LPWSTR, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.restype = _BOOL
        kernel32.CloseHandle.argtypes = [LPVOID]
        h = kernel32.OpenProcess(0x1000, 0, pid)   # QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = ctypes.c_ulong(260)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                       ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
            finally:
                kernel32.CloseHandle(h)
    except Exception:
        pass
    _proc_name_cache[pid] = name
    return name


def is_noise_window(hwnd):
    """桌面/任务栏/开始菜单，以及本程序自己的窗口 —— 都不能当输入状态来源"""
    if not hwnd:
        return True
    try:
        if window_pid(hwnd) == _pid:
            return True
    except Exception:
        pass
    try:
        cls = window_class(hwnd)
    except Exception:
        return False
    if cls in IGNORED_WINDOW_CLASSES:
        return True
    if cls == "Windows.UI.Core.CoreWindow":
        return process_name(window_pid(hwnd)) in SHELL_PROCESSES
    return False


_last_fg = {"hwnd": None}


def foreground_window():
    """-> 用户真正在用的前台窗口（噪声窗口沿用上一次的值）"""
    h = user32.GetForegroundWindow()
    if h and not is_noise_window(h):
        _last_fg["hwnd"] = h
    elif not user32.IsWindow(_last_fg["hwnd"] or 0):
        # 上一次那个窗口已经关了，回到当前前台（哪怕它是壳窗口，至少是活的）
        _last_fg["hwnd"] = user32.GetForegroundWindow()
    return _last_fg["hwnd"]


# --------------------------------------------------------------------------
# 输入语言（键盘布局）
#
# 这一层和"输入法状态"是两件不同的事，但都很可靠：
#   语言   = GetKeyboardLayout(窗口线程) & 0xFFFF   → 0804=中文 0409=英语
#   状态   = IMM 的中文/英文切换                     → 中文输入法内按 Shift 切英文
#
# 两者组合出三种真实情况，必须能一眼分开：
#   中文布局 + 中文状态  = 打得出中文
#   中文布局 + 英文状态  = 还是那个中文输入法，只是按 Shift 关了（打英文）
#   英语布局 + 英文状态  = 整个语言都切到英语键盘了
# 前两种的 HKL 一模一样，只看 IMM 是分不出来的 —— 这正是之前 UI 看起来"完全一样"的原因。
# --------------------------------------------------------------------------
_LANG_SHORT = {
    0x04: "中文", 0x09: "英语", 0x11: "日语", 0x12: "韩语",
    0x07: "德语", 0x0C: "法语", 0x0A: "西班牙语", 0x10: "意大利语",
    0x19: "俄语", 0x16: "葡萄牙语", 0x22: "乌克兰语", 0x1F: "波兰语",
}


def foreground_langid(hwnd):
    """前台窗口线程的键盘布局语言 ID（低 16 位），取不到返回 0"""
    if not hwnd:
        return 0
    try:
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(ctypes.c_ulong(tid or 0))
        return int(hkl or 0) & 0xFFFF
    except Exception:
        return 0


def is_chinese_layout(langid):
    return (langid & 0x3FF) == 0x04       # primary language id 0x04 = 中文


def lang_name(langid):
    if not langid:
        return ""
    return _LANG_SHORT.get(langid & 0x3FF, "其他语言")


# ==========================================================================
# 读输入状态（本项目的全部检测逻辑，就这几行）
# ==========================================================================
class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", LPVOID), ("hwndFocus", LPVOID),
                ("hwndCapture", LPVOID), ("hwndMenuOwner", LPVOID),
                ("hwndMoveSize", LPVOID), ("hwndCaret", LPVOID),
                ("rcCaret", wt.RECT)]


def focused_control(hwnd):
    """真正拥有键盘焦点的**控件**，而不是顶层窗口。

    IMM 的输入上下文按线程存在、跟着焦点控件走。只拿顶层窗口去查，在很多
    程序里读到的是线程默认值（恒英文）。成熟的开源输入法指示器（InputTip、
    IME_Indicator）都是先 GetGUIThreadInfo 取 hwndFocus 再查 IMM。
    """
    if not hwnd:
        return hwnd
    try:
        user32.GetGUIThreadInfo.restype = _BOOL
        user32.GetGUIThreadInfo.argtypes = [wt.DWORD,
                                            ctypes.POINTER(GUITHREADINFO)]
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        if not tid:
            return hwnd
        g = GUITHREADINFO()
        g.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(g)):
            return g.hwndFocus or g.hwndActive or hwnd
    except Exception:
        pass
    return hwnd


IME_TIMEOUT_MS = 60        # 单条 WM_IME_CONTROL 超时；游戏窗口实测 ≤18ms
IME_RETRY_MS = 12          # 焦点控件读不到时换顶层窗口重试的上限


def ime_window(hwnd):
    if not hwnd:
        return None
    try:
        return imm32.ImmGetDefaultIMEWnd(hwnd) or None
    except Exception:
        return None


def _send_ime(hime, code, timeout_ms):
    """问 IME 一个 WM_IME_CONTROL 子命令 -> (成功?, 值)

    **只认 SendMessageTimeout 的返回值**：英文状态的转换模式本来就是 0，
    拿返回值判断成功会永远读成英文（ImeModePersistence 的 ime_interop.cpp
    里也专门写了这一条）。
    """
    res = ctypes.c_size_t(0)
    try:
        ok = user32.SendMessageTimeoutW(hime, WM_IME_CONTROL, code, None,
                                        SMTO_ABORTIFHUNG,
                                        max(1, int(timeout_ms)),
                                        ctypes.byref(res))
    except Exception:
        return False, 0
    return bool(ok), int(res.value) & 0xFFFFFFFF


def ime_query(hwnd, timeout_ms=IME_TIMEOUT_MS):
    """问一次该窗口的 IME -> (open_status, conv_mode)，没取到的项是 None"""
    hime = ime_window(hwnd)
    if not hime:
        return None, None
    ok_open, opened = _send_ime(hime, IMC_GETOPENSTATUS, timeout_ms)
    ok_conv, conv = _send_ime(hime, IMC_GETCONVERSIONMODE, timeout_ms)
    return (bool(opened) if ok_open else None), (conv if ok_conv else None)


class Reading:
    """一次采样的完整结果。

    cn 为 None 表示"这次没读到"，**不是**英文。上层拿到 None 时必须保持
    上一次的状态，绝不能当成一次变化 —— 这是启动/游戏中乱刷的根源。
    """
    __slots__ = ("hwnd", "langid", "hkl", "cn", "full", "caps", "ok",
                 "source", "ms", "conv", "open_status")

    def __init__(self, hwnd=None, langid=0, hkl=0, cn=None, full=None,
                 caps=False, ok=False, source="none", ms=0.0,
                 conv=None, open_status=None):
        self.hwnd = hwnd
        self.langid = langid
        self.hkl = hkl
        self.cn = cn
        self.full = full
        self.caps = caps
        self.ok = ok
        self.source = source
        self.ms = ms
        self.conv = conv              # IME 原始转换模式（诊断用）
        self.open_status = open_status

    def get(self, k, default=None):
        return getattr(self, k, default)

    def __repr__(self):
        return ("Reading(cn=%r full=%r langid=0x%04X src=%s %.2fms)"
                % (self.cn, self.full, self.langid, self.source, self.ms))


def read_state(hwnd=None, timeout_ms=IME_TIMEOUT_MS):
    """采一次前台输入状态 -> Reading"""
    t0 = time.perf_counter()

    def cost():
        return (time.perf_counter() - t0) * 1000

    if hwnd is None:
        hwnd = foreground_window()
    if not hwnd:
        return Reading(source="no-window", ms=cost())

    try:
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = int(user32.GetKeyboardLayout(ctypes.c_ulong(tid or 0)) or 0)
    except Exception:
        hkl = 0
    langid = hkl & 0xFFFF

    # ① 语言层就够用的情形：英语/日语等键盘上根本没有"中文开关"。
    #    直接判定，一次跨进程调用都不发 —— 游戏里就是靠这条做到零开销。
    if langid and not is_chinese_layout(langid):
        return Reading(hwnd, langid, hkl, False, None, False, True,
                       "layout", cost())

    # ② 中文布局：问 IME。先问焦点控件（IME 状态挂在它所在线程上）。
    focus = focused_control(hwnd)
    target = focus or hwnd
    open_status, conv = ime_query(target, timeout_ms)
    if conv is None and target != hwnd:
        # 焦点控件常常在另一个线程（浏览器渲染进程等），换顶层再试一次。
        # 超时收紧，避免慢目标把一次采样拖成两倍。
        open_status, conv = ime_query(hwnd, min(timeout_ms, IME_RETRY_MS))
    if conv is None:
        return Reading(hwnd, langid, hkl, None, None, False, False,
                       "timeout", cost())

    native = bool(conv & IME_CMODE_NATIVE)
    # 微软拼音：中文 open=1 conv=0x401；英文 open=1 conv=0 → 必须两个一起看
    cn = (bool(open_status) and native) if open_status is not None else native
    return Reading(hwnd, langid, hkl, cn, bool(conv & IME_CMODE_FULLSHAPE),
                   False, True, "ime", cost(),
                   conv=conv, open_status=open_status)


def read_ime_state(hwnd=None, retry=True):
    """旧接口（--once / --debug）：-> (cn, full, ok)"""
    r = read_state(hwnd)
    return r.cn, r.full, r.ok


def caps_lock_on():
    return bool(user32.GetKeyState(VK_CAPITAL) & 0x0001)


# --------------------------------------------------------------------------
# 全屏 / 游戏检测
#
# SHQueryUserNotificationState 只有**独占** D3D 全屏才稳定返回 3；无边框全屏
# 一般返回 2（QUNS_BUSY）。而 2 会被 NVIDIA 覆盖层、某些 Electron 窗口误报
# （PowerToys 刚为此把这两种状态拆成两个开关），所以 2 只能当"疑似"，还要
# 有几何证据。判据：
#   * 3/4（独占全屏 / 演示模式）        → 全屏
#   * 窗口矩形贴满所在显示器且没有标题栏 → 全屏（无边框全屏）
#
# 几何判定的 DPI 坑：本进程不是 per-monitor DPI aware 时 GetWindowRect 和
# GetMonitorInfo 都落在同一套虚拟化坐标里，直接比是安全的；而
# DwmGetWindowAttribute(EXTENDED_FRAME_BOUNDS) 返回的是物理像素（本机实测
# 2560×1440 vs 2048×1152），混用会永远判不出来 —— 所以这里只用前两个。
# --------------------------------------------------------------------------
QUNS_NOT_PRESENT, QUNS_BUSY, QUNS_D3D_FULLSCREEN = 1, 2, 3
QUNS_PRESENTATION, QUNS_ACCEPTS, QUNS_QUIET, QUNS_APP = 4, 5, 6, 7


def query_notification_state():
    """1=未使用 2=忙碌(全屏/演示) 3=独占全屏 4=演示模式 5=正常 6=静默 7=应用"""
    try:
        shell32.SHQueryUserNotificationState.restype = ctypes.c_long
        shell32.SHQueryUserNotificationState.argtypes = [
            ctypes.POINTER(ctypes.c_int)]
        st = ctypes.c_int(0)
        if shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0:
            return st.value
    except Exception:
        pass
    return QUNS_ACCEPTS


def monitor_rect(hwnd):
    """窗口所在显示器的**整块**矩形（不是工作区）"""
    try:
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def window_rect(hwnd):
    try:
        r = wt.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def covers_monitor(hwnd, tol=2, ratio=0.98):
    """窗口是否贴满所在显示器（无边框全屏的几何特征）"""
    if not hwnd or is_noise_window(hwnd):
        return False
    try:
        if not user32.IsWindowVisible(hwnd):
            return False
        style = int(user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
        ex = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    except Exception:
        style, ex = 0, 0
    if ex & WS_EX_TOOLWINDOW:
        return False
    # 有标题栏 = 普通最大化窗口；无边框全屏一定没有标题栏
    if (style & WS_CAPTION) == WS_CAPTION:
        return False
    wr, mr = window_rect(hwnd), monitor_rect(hwnd)
    if not wr or not mr:
        return False
    mw, mh = mr[2] - mr[0], mr[3] - mr[1]
    if mw <= 0 or mh <= 0:
        return False
    if abs(wr[0] - mr[0]) > tol or abs(wr[1] - mr[1]) > tol:
        return False
    ix = min(wr[2], mr[2]) - max(wr[0], mr[0])
    iy = min(wr[3], mr[3]) - max(wr[1], mr[1])
    if ix <= 0 or iy <= 0:
        return False
    return ix * iy >= ratio * mw * mh


def detect_fullscreen(hwnd=None):
    """-> (是否全屏, 原因字符串)；原因会写进日志，方便排查误判"""
    hwnd = hwnd or foreground_window()
    quns = query_notification_state()
    if quns in (QUNS_D3D_FULLSCREEN, QUNS_PRESENTATION):
        return True, "quns%d" % quns
    if covers_monitor(hwnd):
        return True, "geometry"
    if quns == QUNS_BUSY and covers_monitor(hwnd, tol=48, ratio=0.90):
        # 通知状态说忙、几何也基本贴满（放宽到 90%），才算
        return True, "quns2+geometry"
    return False, "no"


# ==========================================================================
# 崩溃处理 / 单实例
# ==========================================================================
def log_crash(where, text):
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n%s\n\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), where, text))
    except Exception:
        pass


def _crash_dialog(headline, detail):
    try:
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW.argtypes = [LPVOID, wt.LPCWSTR, wt.LPCWSTR,
                                       ctypes.c_uint]
        user32.MessageBoxW(None, "%s\n\n详情见 crash.log：\n%s"
                           % (headline, detail[:400]), "IME Indicator", 0x10)
    except Exception:
        pass


def install_crash_handler():
    """双击 .bat 时用的是 pythonw（没有控制台），未捕获异常会让进程无声无息地
    死掉。接管后至少留一份 crash.log，别让"闪退"变成无头案。"""
    def hook(etype, e, tb):
        text = "".join(traceback.format_exception(etype, e, tb))
        log_crash("未捕获异常", text)
        _crash_dialog("程序出错了：%s" % etype.__name__, "%s" % e)
        sys.__excepthook__(etype, e, tb)

    sys.excepthook = hook

    def tk_hook(exc, val, tb):
        log_crash("界面回调异常",
                  "".join(traceback.format_exception(exc, val, tb)))

    tk.Tk.report_callback_exception = tk_hook


TRAY_GUID = "{8F2A5C31-6D14-4A3E-9E77-1C0D5B7A2E41}"


def already_running():
    """同名互斥体挡第二个实例。

    托盘图标按 GUID 注册，第二个实例要么注册失败要么顶掉前一个，失败的那一侧
    既没图标也没窗口，退出入口就没了，只能去任务管理器杀进程。
    """
    try:
        kernel32.CreateMutexW.restype = LPVOID
        kernel32.CreateMutexW.argtypes = [LPVOID, _BOOL, wt.LPCWSTR]
        # 句柄故意不关闭：进程活着它就有效，进程退出系统自动回收
        h = kernel32.CreateMutexW(None, False,
                                  "IMEIndicator-%s" % TRAY_GUID.strip("{}"))
        return bool(h) and ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def notify_already_running():
    try:
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW.argtypes = [LPVOID, wt.LPCWSTR, wt.LPCWSTR,
                                       ctypes.c_uint]
        user32.MessageBoxW(None, "输入法提示条已经在运行了。\n"
                           "右键托盘图标可打开设置或退出。",
                           "IME Indicator", 0x40)
    except Exception:
        pass


# ==========================================================================
# 显示位置
# ==========================================================================
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", ctypes.c_ulong)]


def work_area_of_cursor():
    pt = wt.POINT()
    user32.GetCursorPos.restype = _BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetCursorPos(ctypes.byref(pt))
    user32.MonitorFromPoint.restype = LPVOID
    user32.MonitorFromPoint.argtypes = [wt.POINT, ctypes.c_ulong]
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    user32.GetMonitorInfoW.restype = _BOOL
    user32.GetMonitorInfoW.argtypes = [LPVOID, ctypes.c_void_p]
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    user32.GetSystemMetrics.restype = ctypes.c_int
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ==========================================================================
# 提示条
# ==========================================================================
FONT_FAMILY = "Microsoft YaHei UI"


class Indicator:
    BG = "#101419"
    BORDER = "#2A313D"
    TEXT = "#F2F5FA"
    DIM = "#7C8698"
    SOFT = "#AEB8CA"
    BLUE = "#3D7EFF"        # 中文输入
    BLUE_DIM = "#26364F"
    GREEN = "#17A673"       # 英语键盘
    GREEN_DIM = "#1C3A32"
    AMBER = "#F2A33C"       # 中文输入法内切到英文 / 全角 / 大写锁定
    AMBER_DIM = "#8A5F28"
    UNK = "#5A6274"
    UNK_DIM = "#2A303B"
    SHADOW = "#05070A"

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.config(bg=self.BG)
        self.w, self.h = 248, 96
        self.root.geometry("%dx%d+0+0" % (self.w, self.h))
        self.cv = tk.Canvas(self.root, width=self.w, height=self.h, bg=self.BG,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        self.f_big = tkfont.Font(family=FONT_FAMILY, size=30, weight="bold")
        self.f_l1 = tkfont.Font(family=FONT_FAMILY, size=13, weight="bold")
        self.f_l2 = tkfont.Font(family=FONT_FAMILY, size=10)

        self.panel = self.cv.create_rectangle(0, 0, self.w, self.h,
                                              fill=self.BG, outline="")
        self.bar = self.cv.create_rectangle(0, 0, 7, self.h,
                                            fill=self.BLUE_DIM, outline="")
        self.frame = self.cv.create_rectangle(1, 1, self.w - 2, self.h - 2,
                                              outline=self.BORDER, width=1)
        self.sep = self.cv.create_line(0, 0, 0, 0, fill=self.BORDER)
        self.t_big_s = self.cv.create_text(27, 50, anchor="w", text="",
                                           fill=self.SHADOW, font=self.f_big)
        self.t_big = self.cv.create_text(26, 49, anchor="w", text="",
                                         fill=self.BLUE, font=self.f_big)
        self.t_l1 = self.cv.create_text(0, 36, anchor="w", text="",
                                        fill=self.TEXT, font=self.f_l1)
        self.t_l2 = self.cv.create_text(0, 63, anchor="w", text="",
                                        fill=self.DIM, font=self.f_l2)
        # 大字用固定宽度槽位：否则"中/EN"宽度不同，切换时右侧两行文字会左右跳
        self.big_slot = max(self.f_big.measure(s)
                            for s in ("中", "EN", "?"))

        self.hide_job = None
        self.fade_job = None
        self.always_on = bool(cfg.get("always_on"))
        # Tk 的规则：master 被 withdraw 时，它的所有子窗口（含 Toplevel）会一起
        # 消失。提示条平时就是 withdraw 状态，所以设置窗口会被直接"吞掉"——表现
        # 为"点了设置没反应"。打开子窗口前把主窗口设为"已映射但全透明"。
        self.pinned = False
        self.visible = False
        self._geo = None          # 上一次的窗口几何，未变化就不再 SetWindowPos
        self._alpha = None        # 上一次设置的 alpha
        self._fade = True         # 游戏里关掉淡出
        self._in_game = False     # 当前是否在全屏游戏里（决定用 game_duration）
        self._fixed_w = self._fixed_width()
        self.resize(self._fixed_w)
        self.root.update_idletasks()
        self._set_click_through()
        self.apply_transparent_bg()
        self.root.withdraw()

    # ---------- 窗口状态 ----------
    def pin(self, on):
        """有 Toplevel（设置窗口）时，主窗口必须保持"已映射"，否则子窗口不可见"""
        self.pinned = bool(on)
        try:
            if self.pinned:
                if self.root.state() == "withdrawn":
                    self.root.deiconify()
                if not self.visible:
                    self.root.attributes("-alpha", 0.0)
            elif not self.visible and self.root.state() != "withdrawn":
                self.root.withdraw()
        except Exception:
            pass

    def _hwnd(self):
        hwnd = self.root.winfo_id()
        return user32.GetParent(hwnd) or hwnd

    def _set_click_through(self):
        """鼠标穿透 + 不抢焦点 + 不进任务栏。

        窗口一旦能被点到，点击本身就把焦点抢走、前台窗口随之变化，输入法
        "看起来"就切换了一次 —— 这正是提示条自己乱弹的原因之一。
        """
        try:
            hwnd = self._hwnd()
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                     style | WS_EX_TRANSPARENT | WS_EX_LAYERED
                                     | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def apply_transparent_bg(self):
        on = bool(self.cfg.get("transparent_bg"))
        try:
            self.root.attributes("-transparentcolor", self.BG if on else "")
        except Exception:
            on = False
        for it in (self.panel, self.frame, self.bar):
            self.cv.itemconfig(it, state="hidden" if on else "normal")
        if not on:
            self.cv.itemconfig(self.frame, outline=self.BORDER)

    def resize(self, w):
        w = max(200, min(560, int(w)))
        if w == self.w:
            return
        self.w = w
        self.cv.config(width=w, height=self.h)
        self.root.geometry("%dx%d" % (w, self.h))
        self.cv.coords(self.panel, 0, 0, w, self.h)
        self.cv.coords(self.frame, 1, 1, w - 2, self.h - 2)

    def _fixed_width(self):
        """一次算好所有状态里最宽的那个，之后永不再改窗口宽度。

        每次 show 都 resize 会重建 DWM 的重定向表面，在游戏里就是一次卡顿。
        """
        x2 = 26 + self.big_slot + 20
        best = 248
        for l1 in ("中文输入", "英文 · 输入法内", "英文 · 英语键盘", "状态未知"):
            for l2 in ("● 全角", "半角", "角标未知", "无全角",
                       "● 全角 · 大写锁定", "无全角 · 大写锁定", ""):
                tw = max(self.f_l1.measure(l1),
                         self.f_l2.measure(l2) if l2 else 0)
                best = max(best, int(x2 + tw + 24))
        return best

    def set_fade(self, on):
        """游戏里关掉淡出：每一帧 alpha 变化都是一次 DWM 更新"""
        self._fade = bool(on)

    def _apply_alpha(self):
        want = float(self.cfg.get("opacity", 0.85))
        if self._alpha is None or abs(self._alpha - want) > 1e-3:
            try:
                self.root.attributes("-alpha", want)
            except Exception:
                pass
            self._alpha = want

    def place(self):
        w, h = self.w, self.h
        if self.cfg.get("follow_mouse_monitor", True):
            left, top, right, bottom = work_area_of_cursor()
        else:
            user32.GetSystemMetrics.restype = ctypes.c_int
            left, top = 0, 0
            right = user32.GetSystemMetrics(0)
            bottom = user32.GetSystemMetrics(1)
        x = left + (right - left - w) // 2
        y = bottom - h - int(self.cfg.get("margin_bottom", 38))
        # 位置没变就别再 SetWindowPos：移动一次就是一次 DWM 重排
        if self._geo == (x, y, w, h):
            return
        self._geo = (x, y, w, h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ---------- 显示 ----------
    def show(self, cn, full, caps=False, hl=(), langid=0):
        """cn: True=中文 False=英文 None=未读到
        full: True=全角 False=半角 None=未知
        langid: 前台窗口的输入语言（HKL 低 16 位）

        三种真实状态必须能一眼分开：
          中文布局 + 中文 → 蓝「中」中文输入        （打得出中文）
          中文布局 + 英文 → 琥珀「EN」英文·输入法内  （还是那个输入法，按 Shift 关了）
          英语布局 + 英文 → 绿「EN」英文输入        （整个语言切到英语键盘了）
        """
        hl = set(hl or ())
        zh = is_chinese_layout(langid)

        if cn is True:
            big, l1, color, dimc = "中", "中文输入", self.BLUE, self.BLUE_DIM
        elif cn is False and zh:
            big, l1, color, dimc = ("EN", "英文 · 输入法内",
                                    self.AMBER, self.AMBER_DIM)
        elif cn is False:
            big, l1, color, dimc = ("EN", "英文 · 英语键盘",
                                    self.GREEN, self.GREEN_DIM)
        else:
            big, l1, color, dimc = "?", "状态未知", self.UNK, self.UNK_DIM

        if not self.cfg.get("show_fullwidth", True):
            l2 = ""
        elif langid and not zh:
            # 英语/其他语言的键盘没有全角概念，读到的"半角"只是 IMM 的默认值，
            # 显示出来会让人以为那里真有个角标开关
            l2 = "无全角"
        elif full is True:
            l2 = "● 全角"
        elif full is False:
            l2 = "半角"
        else:
            l2 = "角标未知"
        if caps:
            l2 = (l2 + " · 大写锁定") if l2 else "大写锁定"

        x2 = 26 + self.big_slot + 20
        tw = max(self.f_l1.measure(l1), self.f_l2.measure(l2) if l2 else 0)
        # 宽度在启动时就算成固定值，show 里不再改窗口大小
        self.resize(self._fixed_w)

        self.cv.coords(self.t_big_s, 27, 50)
        self.cv.coords(self.t_big, 26, 49)
        self.cv.coords(self.t_l1, x2, 36)
        self.cv.coords(self.t_l2, x2, 63)
        self.cv.coords(self.sep, x2 - 11, 26, x2 - 11, 72)

        live = bool(hl)
        accent = self.AMBER if ("full" in hl) else color
        self.cv.itemconfig(self.t_big, text=big, fill=color)
        self.cv.itemconfig(self.t_big_s, text=big)
        self.cv.itemconfig(self.t_l1, text=l1,
                           fill=self.TEXT if live else self.SOFT)
        self.cv.itemconfig(self.t_l2, text=l2,
                           fill=self.AMBER if (full is True or caps) else self.DIM)
        self.cv.itemconfig(self.bar, fill=accent if live else dimc)
        self.cv.itemconfig(self.frame, outline=accent if live else self.BORDER,
                           width=2 if live else 1)

        self.place()
        self.visible = True
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
        except Exception:
            pass
        if self.hide_job:
            try:
                self.root.after_cancel(self.hide_job)
            except Exception:
                pass
            self.hide_job = None
        # 淡出是自递归的 after 链；不取消的话，快速连续切换时上一次的淡出会把
        # 刚弹出的新提示接着关掉（"闪一下就没了"）
        if self.fade_job:
            try:
                self.root.after_cancel(self.fade_job)
            except Exception:
                pass
            self.fade_job = None
        self._apply_alpha()
        self.reschedule_hide()

    def reschedule_hide(self):
        if self.hide_job:
            try:
                self.root.after_cancel(self.hide_job)
            except Exception:
                pass
            self.hide_job = None
        if not self.always_on:
            dur = float(self.cfg.get("duration", 1.6))
            if getattr(self, "_in_game", False):
                # 游戏里少停留一会儿：这段时间正是 DWM 在合成、帧数被拖低的时段
                dur = min(dur, float(self.cfg.get("game_duration", 1.0)))
            self.hide_job = self.root.after(int(dur * 1000), self.fade_out)

    def hide_now(self):
        """立刻收起：不走淡出、不写 alpha，直接 withdraw。

        游戏模式下这就是唯一的收场方式 —— 淡出每一步都在改 alpha，
        每一步都是一次 DWM 更新。
        """
        for attr in ("hide_job", "fade_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        self.visible = False
        self._alpha = 0.0
        if not self.pinned:
            try:
                self.root.withdraw()
            except Exception:
                pass
        else:
            try:
                self.root.attributes("-alpha", 0.0)
            except Exception:
                pass

    def fade_out(self):
        self.hide_job = None
        if self.always_on:
            return
        if not getattr(self, "_fade", True):
            self.hide_now()
            return
        try:
            self._fade(float(self.cfg.get("opacity", 0.85)))
        except Exception:
            pass

    def _fade(self, a):
        a -= 0.12
        self.fade_job = None
        if a <= 0.02:
            try:
                self.root.attributes("-alpha", 0.0)
            except Exception:
                pass
            self._alpha = 0.0
            self.visible = False
            if not self.pinned:
                self.root.withdraw()
            return
        try:
            self.root.attributes("-alpha", a)
        except Exception:
            pass
        self.fade_job = self.root.after(26, lambda: self._fade(a))


# ==========================================================================
# 托盘图标（纯 ctypes）
# ==========================================================================
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __str__(self):
        d4 = "".join("%02x" % b for b in self.Data4[:2]) + "-" + \
             "".join("%02x" % b for b in self.Data4[2:])
        return "{%08X-%04X-%04X-%s}" % (self.Data1, self.Data2, self.Data3,
                                        d4.upper())


def guid_from_str(s):
    s = s.strip("{}")
    a, b, c, d, e = s.split("-")
    g = GUID()
    g.Data1 = int(a, 16)
    g.Data2 = int(b, 16)
    g.Data3 = int(c, 16)
    h = d + e
    for i in range(8):
        g.Data4[i] = int(h[i * 2:i * 2 + 2], 16)
    return g


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_ulong),
                ("yHotspot", ctypes.c_ulong), ("hbmMask", wt.HBITMAP),
                ("hbmColor", wt.HBITMAP)]


DT_CENTER, DT_VCENTER, DT_SINGLELINE = 0x0001, 0x0004, 0x0020
WM_TRAY = 0x0400 + 100
WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0205, 0x0203
WM_DESTROY = 0x0002
ID_SHOW, ID_ALWAYS, ID_TRANSP = 1, 2, 3
ID_FULLW, ID_AUTO, ID_LOG = 4, 5, 6
ID_SETTINGS, ID_EXIT = 7, 8


def make_icon(text="中", bg=(0x3C, 0x8D, 0xFF), fg=0x00FFFFFF, size=32):
    """自绘托盘图标：圆角纯色底 + 白色字符。

    不用 LoadIconW 的系统预置图标，是因为它返回的句柄在 64 位下若不声明
    restype 会被截断成 32 位，图标直接变空白（托盘"没图标"的常见原因）。
    """
    try:
        user32.GetDC.restype = wt.HDC
        user32.GetDC.argtypes = [wt.HWND]
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        gdi32.CreateCompatibleDC.restype = wt.HDC
        gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
        gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int,
                                                 ctypes.c_int]
        gdi32.SelectObject.restype = wt.HGDIOBJ
        gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        gdi32.CreateSolidBrush.restype = wt.HBRUSH
        gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
        gdi32.CreateFontW.restype = wt.HFONT
        gdi32.DeleteDC.argtypes = [wt.HDC]
        gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
        # HDC 是 64 位值，不声明 argtypes 会按 32 位传参，数值一大就
        # OverflowError（"图标时有时无"）
        gdi32.RoundRect.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int]
        gdi32.Rectangle.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int]
        gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
        gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
        user32.CreateIconIndirect.restype = wt.HICON
        user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
        user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int,
                                     ctypes.POINTER(wt.RECT), ctypes.c_uint]

        r, g, b = bg
        brush_bg = gdi32.CreateSolidBrush((b << 16) | (g << 8) | r)
        brush_black = gdi32.CreateSolidBrush(0)
        brush_white = gdi32.CreateSolidBrush(0x00FFFFFF)

        hdc = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, size, size)
        gdi32.SelectObject(mem, bmp)
        gdi32.SelectObject(mem, brush_bg)
        gdi32.RoundRect(mem, 0, 0, size, size, 9, 9)

        # AND mask：圆角外为白(bit=1 → 透明)，圆角内为黑(→ 不透明)
        mask = gdi32.CreateCompatibleBitmap(hdc, size, size)
        mm = gdi32.CreateCompatibleDC(hdc)
        gdi32.SelectObject(mm, mask)
        gdi32.SelectObject(mm, brush_white)
        gdi32.Rectangle(mm, 0, 0, size, size)
        gdi32.SelectObject(mm, brush_black)
        gdi32.RoundRect(mm, 0, 0, size, size, 9, 9)

        gdi32.SelectObject(mem, brush_bg)
        gdi32.SetBkMode(mem, 1)                      # TRANSPARENT
        gdi32.SetTextColor(mem, fg)
        font = gdi32.CreateFontW(int(size * 0.58), 0, 0, 0, 700, 0, 0, 0,
                                 1, 0, 0, 0, 0, FONT_FAMILY)
        gdi32.SelectObject(mem, font)
        rect = wt.RECT(0, 0, size, size)
        user32.DrawTextW(mem, text, -1, ctypes.byref(rect),
                         DT_CENTER | DT_VCENTER | DT_SINGLELINE)

        info = ICONINFO()
        info.fIcon = 1
        info.hbmMask = mask
        info.hbmColor = bmp
        icon = user32.CreateIconIndirect(ctypes.byref(info))
        gdi32.DeleteDC(mem)
        gdi32.DeleteDC(mm)
        user32.ReleaseDC(None, hdc)
        return icon
    except Exception:
        return None


def fallback_icon():
    try:
        user32.LoadIconW.restype = wt.HICON
        user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
        return user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
    except Exception:
        return None


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("hWnd", wt.HWND),
                ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                ("uCallbackMessage", ctypes.c_uint), ("hIcon", wt.HICON),
                ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_ulong),
                ("dwStateMask", ctypes.c_ulong),
                ("szInfo", ctypes.c_wchar * 256), ("uTimeout", ctypes.c_uint),
                ("uVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", ctypes.c_ulong), ("guidItem", GUID),
                ("hBalloonIcon", wt.HICON)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", wt.HICON),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 0x4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP, NIF_GUID = \
    0x1, 0x2, 0x4, 0x10, 0x80, 0x20
NOTIFYICON_VERSION_4 = 4


class Tray:
    """托盘图标 + 右键菜单。跑在独立线程（有自己的消息循环）"""

    def __init__(self, cmd_queue, tip="输入法提示条"):
        self.q = cmd_queue
        self.hwnd = None
        self.proc = None
        self.icon = None
        self.state = {"always": False, "transparent": False, "fullwidth": True,
                      "autostart": False, "log": False}
        threading.Thread(target=self._run, args=(tip,), daemon=True).start()

    def _run(self, tip):
        try:
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND,
                                         ctypes.c_uint, ctypes.c_ulonglong,
                                         ctypes.c_longlong)
            hInst = kernel32.GetModuleHandleW(None)

            def wndproc(h, msg, wp, lp):
                if msg == WM_TRAY:
                    if lp in (WM_RBUTTONUP, WM_LBUTTONDBLCLK):
                        pt = wt.POINT()
                        user32.GetCursorPos.restype = _BOOL
                        user32.GetCursorPos.argtypes = [
                            ctypes.POINTER(wt.POINT)]
                        user32.GetCursorPos(ctypes.byref(pt))
                        user32.SetForegroundWindow.restype = _BOOL
                        user32.SetForegroundWindow.argtypes = [LPVOID]
                        user32.SetForegroundWindow(h)
                        user32.CreatePopupMenu.restype = LPVOID
                        menu = user32.CreatePopupMenu()
                        f = MF_BYPOSITION
                        ck = lambda b: (MF_CHECKED if b else 0)

                        def add(pos, text, cmd, checked=False, sep=False):
                            user32.InsertMenuW.restype = _BOOL
                            user32.InsertMenuW.argtypes = [
                                LPVOID, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_size_t, wt.LPCWSTR]
                            user32.InsertMenuW(
                                menu, pos,
                                f | (MF_SEPARATOR if sep else 0) | ck(checked),
                                cmd, text)

                        add(0, "设置...", ID_SETTINGS)
                        add(1, "立即显示一次", ID_SHOW)
                        add(2, "", 0, sep=True)
                        add(3, "常驻显示（不自动隐藏）", ID_ALWAYS,
                            self.state["always"])
                        add(4, "透明背景（无面板）", ID_TRANSP,
                            self.state["transparent"])
                        add(5, "显示全角/半角", ID_FULLW,
                            self.state["fullwidth"])
                        add(6, "", 0, sep=True)
                        add(7, "开机自启", ID_AUTO, self.state["autostart"])
                        add(8, "记录日志 events.log", ID_LOG, self.state["log"])
                        add(9, "", 0, sep=True)
                        add(10, "退出", ID_EXIT)
                        user32.TrackPopupMenu.restype = _BOOL
                        user32.TrackPopupMenu.argtypes = [
                            LPVOID, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, LPVOID, LPVOID]
                        cmd = user32.TrackPopupMenu(menu, 0x0100, pt.x, pt.y,
                                                    0, h, None)
                        user32.PostMessageW.restype = _BOOL
                        user32.PostMessageW.argtypes = [
                            LPVOID, ctypes.c_uint, ctypes.c_size_t,
                            ctypes.c_ssize_t]
                        user32.PostMessageW(h, 0, 0, 0)
                        user32.DestroyMenu(menu)
                        if cmd:
                            self.q.put(cmd)
                elif msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                return user32.DefWindowProcW(h, msg, wp, lp)

            self.proc = WNDPROC(wndproc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(self.proc, ctypes.c_void_p)
            wc.hInstance = hInst
            wc.lpszClassName = "IMEIndicatorTrayCls"
            self.icon = make_icon() or fallback_icon()
            wc.hIcon = self.icon
            wc.hIconSm = self.icon
            user32.RegisterClassExW.restype = ctypes.c_ushort
            user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
            user32.RegisterClassExW(ctypes.byref(wc))
            user32.CreateWindowExW.restype = LPVOID
            user32.CreateWindowExW.argtypes = [
                ctypes.c_ulong, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                LPVOID, LPVOID, wt.HINSTANCE, LPVOID]
            self.hwnd = user32.CreateWindowExW(
                0, "IMEIndicatorTrayCls", "IMEIndicator", 0, 0, 0, 0, 0,
                None, None, hInst, None)

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP | NIF_GUID
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self.icon
            nid.szTip = tip[:127]
            nid.guidItem = guid_from_str(TRAY_GUID)
            shell32.Shell_NotifyIconW.restype = _BOOL
            shell32.Shell_NotifyIconW.argtypes = [ctypes.c_ulong,
                                                  ctypes.c_void_p]
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

            nid2 = NOTIFYICONDATAW()
            nid2.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid2.hWnd = self.hwnd
            nid2.uID = 1
            nid2.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid2))

            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log_crash("托盘线程", traceback.format_exc())

    def remove(self):
        # 用 GUID 注册的图标，删除时必须同样带 GUID，否则按 hWnd/uID 定位不到，
        # 退出后托盘里会留一个点不掉的幽灵图标
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_GUID
            nid.guidItem = guid_from_str(TRAY_GUID)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        try:
            if self.icon:
                user32.DestroyIcon.restype = _BOOL
                user32.DestroyIcon.argtypes = [wt.HICON]
                user32.DestroyIcon(self.icon)
                self.icon = None
        except Exception:
            pass


# ==========================================================================
# 键盘钩子：只负责"提速"，不负责"检测"
# ==========================================================================
class SwitchKeyHook:
    """Alt+Shift / Ctrl+Shift / Win+Space / Shift+Space 按下时通知主线程立即采样。

    钩子回调里**不做任何磁盘 I/O 和跨进程查询**：Windows 会因为回调超时
    静默卸载钩子，那之后就彻底没反应了。
    """

    def __init__(self, on_combo, on_shift_space=None):
        self.on_combo = on_combo
        self.on_shift_space = on_shift_space
        self.proc = None
        self._hook = None
        self._thread = None
        self._tid = 0
        self._ready = threading.Event()
        self.start()

    # 全屏游戏里要能卸掉：钩子存在时的按键延迟是实打实的输入延迟
    def is_running(self):
        return bool(self._thread and self._thread.is_alive() and self._tid)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(0.35)

    def stop(self):
        tid = self._tid
        self._tid = 0
        if tid:
            try:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(0.2)

    def _run(self):
        self._tid = kernel32.GetCurrentThreadId()
        try:
            HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                          ctypes.c_size_t, ctypes.c_size_t)

            def proc(ncode, wparam, lparam):
                try:
                    # 只在**按下**时处理：松开时也会收到 WM_KEYUP，而那时
                    # Shift 通常还按着，组合键条件依然成立。实测一次
                    # Shift+Space 会触发两遍 on_shift_space，把全角推断
                    # 翻两次、正好抵消 —— 表现为"全角永远判反"。
                    if ncode >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        kb = ctypes.cast(
                            lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = kb.vkCode
                        down = lambda k: bool(user32.GetAsyncKeyState(k) & 0x8000)
                        combo = False
                        if vk in MENU_KEYS + SHIFT_KEYS and down(VK_MENU) \
                                and down(VK_SHIFT):
                            combo = True
                        elif vk in CTRL_KEYS + SHIFT_KEYS and down(VK_CONTROL) \
                                and down(VK_SHIFT):
                            combo = True
                        elif vk in (VK_SPACE, VK_LWIN) and \
                                (down(VK_SHIFT) or down(VK_LWIN)):
                            combo = True
                        if combo:
                            self.on_combo()
                        if combo and vk == VK_SPACE and down(VK_SHIFT) \
                                and not down(VK_LWIN) and self.on_shift_space:
                            # 有些输入法（微软拼音、豆包）切全角不写 IMM 状态位，
                            # 只能靠这个按键推断，详见 FullWidthTracker
                            self.on_shift_space()
                except Exception:
                    pass
                return user32.CallNextHookEx(None, ncode, wparam, lparam)

            self.proc = HOOKPROC(proc)
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, ctypes.cast(self.proc, ctypes.c_void_p),
                kernel32.GetModuleHandleW(None), 0)
            self._ready.set()
            if not self._hook:
                return
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
        except Exception:
            log_crash("键盘钩子", traceback.format_exc())
        finally:
            self._ready.set()
            self._tid = 0


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_uint), ("scanCode", ctypes.c_uint),
                ("flags", ctypes.c_uint), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


# ==========================================================================
# 设置窗口
# ==========================================================================
class SettingsWindow:
    """改完点「保存」立即生效并写入 config.json；点「关闭」则恢复原样"""

    POLL_CHOICES = [20, 40, 60, 100, 200]
    POLICY_CHOICES = [("pause", "静默（推荐）"), ("reduce", "降频提示"),
                      ("normal", "正常")]

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.snap = dict(app.cfg)
        app.ui.pin(True)

        self.top = tk.Toplevel(app.ui.root)
        self.top.title("IME Indicator 设置  ·  v%s" % VERSION)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.top.update_idletasks()
        w = self.top.winfo_reqwidth()
        h = self.top.winfo_reqheight()
        self.top.geometry("+%d+%d" % ((self.top.winfo_screenwidth() - w) // 2,
                                      max(40, (self.top.winfo_screenheight() - h) // 3)))
        self.top.lift()
        self.top.focus_force()

    # ---------- 布局 ----------
    def _sec(self, parent, title, row):
        ttk.Label(parent, text=title,
                  font=(FONT_FAMILY, 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 4))

    def _scale(self, parent, row, label, key, lo, hi, fmt, unit="", mult=1.0):
        """滑块。`mult` 用于"界面显示值 = 配置值 × mult"（如不透明度 0.85 → 85%）"""
        var = tk.DoubleVar(value=float(self.cfg.get(key)) * mult)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           pady=2)
        box = ttk.Frame(parent)
        box.grid(row=row, column=1, sticky="ew", pady=2)
        lbl = ttk.Label(box, width=7, anchor="e",
                        text=(fmt % var.get()) + unit)

        def on_move(v):
            val = float(v)
            lbl.config(text=(fmt % val) + unit)
            real = val / mult
            self.cfg[key] = int(round(real)) if isinstance(lo, int) \
                else round(real, 3)
            self.app.preview_live(key)

        s = ttk.Scale(box, from_=lo, to=hi, orient="horizontal", length=170,
                      variable=var, command=on_move)
        s.pack(side="left")
        lbl.pack(side="left", padx=(8, 0))
        self._vars[key] = var
        return var

    def _check(self, parent, row, text, key, on_change=None):
        var = tk.BooleanVar(value=bool(self.cfg.get(key)))
        self._vars[key] = var

        def cmd():
            self.cfg[key] = bool(var.get())
            if on_change:
                on_change()
            self.app.preview_live(key)

        ttk.Checkbutton(parent, text=text, variable=var, command=cmd).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=1)

    def _build(self):
        self._vars = {}
        P = ttk.Frame(self.top, padding=16)
        P.pack(fill="both", expand=True)
        P.columnconfigure(0, weight=1)
        P.columnconfigure(1, weight=0)

        r = 0
        self._sec(P, "外观", r); r += 1
        self._scale(P, r, "停留时长", "duration", 0.6, 5.0, "%.1f", " 秒"); r += 1
        self._scale(P, r, "距屏幕底边", "margin_bottom", 0, 300, "%d", " px"); r += 1
        self._scale(P, r, "不透明度", "opacity", 30.0, 100.0, "%d", "%",
                    mult=100.0); r += 1
        self._check(P, r, "常驻显示（不自动隐藏）", "always_on"); r += 1
        self._check(P, r, "透明背景（只留文字，无面板）", "transparent_bg"); r += 1
        self._check(P, r, "跟随鼠标所在的显示器", "follow_mouse_monitor"); r += 1
        self._check(P, r, "显示全角 / 半角", "show_fullwidth"); r += 1

        self._sec(P, "什么时候提示", r); r += 1
        self._check(P, r, "中 / 英文切换", "show_on_mode_change"); r += 1
        self._check(P, r, "全角 / 半角切换", "show_on_fullwidth_change"); r += 1
        self._check(P, r, "大写锁定", "show_on_caps_change"); r += 1
        self._check(P, r, "切换窗口时提示（每切到新窗口弹一次）",
                    "show_on_window_change"); r += 1

        ttk.Label(P, text="检测间隔").grid(row=r, column=0, sticky="w", pady=2)
        self.v_poll = tk.IntVar(value=int(self.cfg.get("poll_ms", 60)))
        cb = ttk.Combobox(P, state="readonly", width=12,
                          values=["%d 毫秒" % v for v in self.POLL_CHOICES])
        cb.current(self.POLL_CHOICES.index(self.v_poll.get())
                   if self.v_poll.get() in self.POLL_CHOICES else 2)
        cb.grid(row=r, column=1, sticky="e", pady=2)
        self.cb_poll = cb
        r += 1

        ttk.Label(P, text="全屏游戏").grid(row=r, column=0, sticky="w", pady=2)
        pk = [k for k, _ in self.POLICY_CHOICES]
        cur = self.cfg.get("fullscreen_policy", "pause")
        self.v_pol = tk.StringVar(value=cur)
        cb2 = ttk.Combobox(P, state="readonly", width=12,
                           values=[t for _, t in self.POLICY_CHOICES])
        cb2.current(pk.index(cur) if cur in pk else 0)
        cb2.grid(row=r, column=1, sticky="e", pady=2)
        self.cb_pol = cb2
        r += 1

        self._sec(P, "其他", r); r += 1
        var_auto = tk.BooleanVar(value=get_autostart())
        self._vars["_autostart"] = var_auto

        def toggle_auto():
            set_autostart(bool(var_auto.get()))

        ttk.Checkbutton(P, text="开机自启", variable=var_auto,
                        command=toggle_auto).grid(row=r, column=0,
                                                  columnspan=2, sticky="w")
        r += 1
        var_log = tk.BooleanVar(value=bool(self.app.log_on))
        self._vars["_log"] = var_log

        def toggle_log():
            self.app.set_log(bool(var_log.get()))

        ttk.Checkbutton(P, text="记录日志 events.log（排查问题时用）",
                        variable=var_log, command=toggle_log).grid(
            row=r, column=0, columnspan=2, sticky="w")
        r += 1

        note = ("只识别「中文 / 英文」两种状态，不区分是哪个输入法。\n"
                "同一语言装了多个中文输入法时，Windows 不向外部程序暴露当前是哪一个，"
                "各输入法也几乎没有可用信号，因此不区分；建议只装一个中文输入法。")
        ttk.Label(P, text=note, foreground="#7A8598", wraplength=380,
                  font=(FONT_FAMILY, 8), justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(12, 0))
        r += 1

        btns = ttk.Frame(P)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="预览", command=self.app.refresh_now,
                   width=8).pack(side="left", padx=4)
        ttk.Button(btns, text="保存", command=self.save, width=8).pack(
            side="left", padx=4)
        ttk.Button(btns, text="关闭", command=self.close, width=8).pack(
            side="left", padx=4)

    # ---------- 动作 ----------
    def save(self):
        c = self.cfg
        try:
            c["duration"] = round(float(self._vars["duration"].get()), 2)
            c["margin_bottom"] = int(float(self._vars["margin_bottom"].get()))
            c["opacity"] = round(float(self._vars["opacity"].get()) / 100.0, 2)
        except Exception:
            pass
        idx = self.cb_poll.current()
        c["poll_ms"] = self.POLL_CHOICES[idx if idx >= 0 else 2]
        idx = self.cb_pol.current()
        c["fullscreen_policy"] = self.POLICY_CHOICES[idx if idx >= 0 else 0][0]
        save_config(c)
        self.app.apply_config()
        self._finish()

    def close(self):
        # 没保存就关：恢复原值，别把临时预览留下
        self.cfg.clear()
        self.cfg.update(self.snap)
        self.app.apply_config()
        self._finish()

    def _finish(self):
        self.app.settings = None
        try:
            self.top.destroy()
        except Exception:
            pass
        self.app.ui.pin(False)


# ==========================================================================
# 全角 / 半角
#
# IME_CMODE_FULLSHAPE(0x8) 不是所有输入法都上报（实测/资料一致）：
#   * 搜狗：半角 0x401、全角 0x409        → 能直接读
#   * 微软拼音：两种宽度都是 0x401       → 不上报，只能靠 Shift+Space 推断
#   * InputTip 干脆不显示全角（issue #77 至今开着）
# 所以这里做"每个 HKL 学一次"：按下 Shift+Space 后 1.2 秒内看这一位有没有
# 跟着翻。翻了 → 这个输入法会报（以后直接读）；没翻 → 不会报（以后用推断
# 值，直到下次再按）。学到的结果存进 config["ime_caps"]，下次启动不用重学。
#
# 注意：钩子只在**按下**时触发一次。上一版连 WM_KEYUP 也处理，一次按键会
# 触发两遍、把推断翻两次正好抵消 —— 这是"全角永远判反"的直接原因。
# ==========================================================================
class FullWidthTracker:
    VERIFY_S = 1.2

    def __init__(self, cfg, on_learn=None):
        self.on_learn = on_learn
        self.caps = {}
        for k, v in (cfg.get("ime_caps") or {}).items():
            self.caps[str(k).upper()] = bool(v)
        self.infer = None        # (hkl, value)  Shift+Space 推出的值（粘性）
        self.pending = None      # (hkl, hwnd, 期望值, ts) 刚按过，等这一位翻

    @staticmethod
    def key(hkl):
        return "%08X" % (int(hkl) & 0xFFFFFFFF)

    def _learn(self, k, capable):
        if not k or self.caps.get(k) is capable:
            return
        self.caps[k] = capable
        if self.on_learn:
            self.on_learn(dict(self.caps))

    def resolve(self, r):
        """imm 上报的全角位 + Shift+Space 推断 -> 最终显示的布尔值（或 None）

        **只在"刚按过 Shift+Space"的上下文里学习**。以前还加了一条"看到这一位
        翻过就认为会上报"，结果被窗口切换坑了：IME 的转换状态是按线程存的，
        切到另一个窗口 `conv` 自然不同，位就"翻"了 —— 于是把不上报的输入法
        误判成上报，之后按 Shift+Space 不再推断，彻底没有反馈。
        """
        langid = r.langid or 0
        if langid and not is_chinese_layout(langid):
            self.pending = None
            return None                      # 英语键盘没有全角这回事
        k = self.key(r.hkl) if r.hkl else ""
        bit = r.full if r.ok else None
        now = time.time()

        if self.pending and bit is not None:
            pk, phwnd, expect, before, at = self.pending
            if r.hwnd != phwnd:
                # 验证期间换了窗口：读数来自另一线程，不能拿来判定，直接作废
                self.pending = None
            elif pk == k:
                # 只有"这一位真的翻到了期望值"才算会上报。
                # before 是按下那一刻的**原始位**（不是推断值），否则
                # "本来就是半角、期望也是半角"会被误判成"跟着翻了"。
                if before is not None and bit == expect and bit != before:
                    self._learn(k, True)     # 这一位跟着翻了 → 会上报
                    self.infer = None
                    self.pending = None
                elif now - at > self.VERIFY_S:
                    # 按过了、这一位没动 → 不上报；推断值就是真相
                    self._learn(k, False)
                    self.pending = None
                    if self.infer is None:
                        self.infer = (k, expect)

        if self.infer and k and self.infer[0] != k:
            self.infer = None                # 换了键盘布局，旧推断作废

        if k and self.infer and self.infer[0] == k:
            if self.caps.get(k) is False:
                return self.infer[1]         # 确认不上报，用推断值
            if self.caps.get(k) is None:
                return self.infer[1]         # 还没学出来，先用推断值顶着
        return bit

    def toggle(self, r):
        """Shift+Space：翻转当前值 -> 新的显示值（None = 这次先不猜）

        * 确认会上报的输入法：不猜，等下一次采样读真值（几十毫秒）。
          但会挂一个 1.2s 的验证：真值没来就改判成"不上报"并补显示，
          所以学错了也能自己恢复。
        * 其他情况：先按翻转显示，再在 1.2s 内验证。
        """
        if not r or not is_chinese_layout(r.langid or 0):
            return None
        k = self.key(r.hkl)
        raw = (bool(r.conv & IME_CMODE_FULLSHAPE)
               if r.conv is not None else None)
        # 翻转的基准是**用户当前看到的值**（可能来自推断），不是原始位；
        # raw 只用于判断"这一位有没有真的跟着翻"。
        base = r.full
        if base is None:
            base = raw if raw is not None else self.resolve(r)
        val = (not base) if base is not None else True
        self.pending = (k, r.hwnd, val, raw, time.time())
        if self.caps.get(k) is True:
            return None
        self.infer = (k, val)
        return val


# ==========================================================================
# 状态去抖
#
# 新应用启动那几百毫秒里，会连续出现「读不到 → 中文 → 读不到 → 英文」这类
# 瞬态；游戏窗口里的跨进程读取更是随机失败。照着每次采样去比较并弹窗，
# 就会连弹好几下（"UI 异常刷新多次"）。这里的规则：
#   * 读不到（cn is None）的采样直接丢掉，绝不当作一次变化；
#   * 候选状态要连续稳定 SETTLE 秒、且至少被看到 2 次才算数；
#   * 换窗口（Alt+Tab 会连换好几个 hwnd）后 GRACE 秒内不弹；
#   * 只和"上一次真正弹出去的状态"比，不和上一次采样比；
#   * 两次弹出至少隔 MIN_GAP，快速抖动会被合并掉。
# InputTip 的做法同类：候选状态要间隔 200ms 被看到两次，换窗口后 200ms 内忽略。
# ==========================================================================
class StateTracker:
    SETTLE = 0.20
    GRACE = 0.25
    MIN_GAP = 0.15

    def __init__(self, cfg):
        self.cfg = cfg
        self.hwnd = None
        self.committed = None
        self.pending = None
        self.pending_since = 0.0
        self.pending_n = 0
        self.grace_until = 0.0
        self.window_event = False
        self.last_show = 0.0
        self.ever = False

    def soft_reset(self, now, grace=0.35):
        """从全屏游戏切回来：给一段静默期，别把游戏里的残留状态弹出来"""
        self.pending = None
        self.pending_n = 0
        self.grace_until = max(self.grace_until, now + grace)

    def note(self, sig, now):
        """手动弹过一次（Shift+Space 推断 / 预览）后同步状态机，避免重复再弹"""
        self.committed = sig
        self.pending = sig
        self.pending_since = now
        self.pending_n = 2
        self.last_show = now

    def _allowed(self, kind):
        if kind == "startup":
            return bool(self.cfg.get("show_on_startup", True))
        if kind == "mode":
            return bool(self.cfg.get("show_on_mode_change", True))
        if kind == "full":
            return bool(self.cfg.get("show_on_fullwidth_change", True))
        if kind == "caps":
            return bool(self.cfg.get("show_on_caps_change", False))
        if kind == "window":
            return bool(self.cfg.get("show_on_window_change", False))
        return True

    def feed(self, sig, hwnd, now):
        """sig=(cn, full, caps, langid) -> (kind, sig) 或 None"""
        if sig is None or sig[0] is None:
            return None                     # 没读到：不产生任何状态

        if hwnd != self.hwnd:
            self.hwnd = hwnd
            self.grace_until = now + self.GRACE
            self.pending = None
            self.pending_n = 0
            self.window_event = bool(self.cfg.get("show_on_window_change"))

        if sig != self.pending:
            self.pending = sig
            self.pending_since = now
            self.pending_n = 1
        else:
            self.pending_n += 1

        if now < self.grace_until:
            return None
        if self.pending_n < 2 or (now - self.pending_since) < self.SETTLE:
            return None

        if sig == self.committed:
            # 状态没变；只有"换窗口提示"开着时才补弹一次
            if self.window_event:
                self.window_event = False
                if now - self.last_show >= self.MIN_GAP:
                    self.last_show = now
                    return ("window", sig)
            return None

        prev = self.committed
        self.committed = sig
        self.window_event = False

        if not self.ever:
            self.ever = True
            kind = "startup"
        elif prev is None:
            kind = "startup"
        elif sig[3] != prev[3] or sig[0] != prev[0]:
            kind = "mode"
        elif sig[1] != prev[1] and None not in (sig[1], prev[1]):
            kind = "full"
        elif sig[2] != prev[2]:
            kind = "caps"
        else:
            kind = ""

        if not kind or not self._allowed(kind):
            self.last_show = now if kind else self.last_show
            return None
        if now - self.last_show < self.MIN_GAP:
            # 太密：吞掉这一下，但状态已经提交，不会过一会儿又补弹
            return None
        self.last_show = now
        return (kind, sig)


# ==========================================================================
# 主程序
# ==========================================================================
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cmd_q = queue.Queue()
        self.ui = Indicator(cfg)
        self.tray = Tray(self.cmd_q)
        self.tray.state.update(always=bool(cfg.get("always_on")),
                               transparent=bool(cfg.get("transparent_bg")),
                               fullwidth=bool(cfg.get("show_fullwidth")),
                               autostart=get_autostart(),
                               log=False)
        self.hook = None
        if cfg.get("hook_hotkeys", True):
            self.hook = SwitchKeyHook(self.on_hotkey, self.on_shift_space)
        self.settings = None
        self.last = None            # 上一次的 Reading（预览 / 日志用）
        self.tr = StateTracker(cfg)
        self.fw = FullWidthTracker(cfg, self._save_caps)
        self.fast_until = 0.0
        self.log_on = False
        self.running = True
        self._fs = (0.0, False, "no")
        self._log_bytes = 0
        self._fail = 0              # 连续读不到的次数（用于失败退避）
        self._hook_on = self.hook is not None

    # ---------- 日志 ----------
    def set_log(self, on):
        self.log_on = bool(on)
        self.tray.state["log"] = self.log_on
        if self.log_on:
            self.log("开启日志")

    def log(self, msg):
        if not self.log_on:
            return
        try:
            if self._log_bytes > MAX_LOG_BYTES:
                with open(EVENT_LOG, "w", encoding="utf-8") as f:
                    f.write("")
                self._log_bytes = 0
            line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
            with open(EVENT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
            self._log_bytes += len(line.encode("utf-8"))
        except Exception:
            pass

    # ---------- 全屏 ----------
    def fullscreen_now(self):
        """1 秒缓存：全屏判定要做 4 次窗口/显示器查询，别每轮都做"""
        now = time.time()
        if now - self._fs[0] < 1.0:
            return self._fs[1], self._fs[2]
        fs, why = detect_fullscreen()
        self._fs = (now, fs, why)
        return fs, why

    def _save_caps(self, caps):
        """学到"这个输入法报不报全角位"后落盘，下次启动不用重学"""
        self.cfg["ime_caps"] = dict(caps)
        save_config(self.cfg)

    # ---------- 采样 ----------
    def sample(self):
        r = read_state(foreground_window())
        if r.ok:
            r.caps = caps_lock_on() if self.cfg.get("show_on_caps_change") else False
            self._fail = 0
        else:
            self._fail += 1
        r.full = self.fw.resolve(r)
        return r

    # ---------- 事件 ----------
    def on_hotkey(self):
        # 钩子线程调用：只改一个标量，绝不做 I/O
        if self._fs[1]:
            # 全屏游戏里不加速轮询：加速的代价是每 20ms 往游戏线程塞两条
            # 同步消息（实测游戏窗口每条 12~18ms），这正是"最低帧突然掉到
            # 十几帧"的来源。60ms 一次已经够快。
            return
        self.fast_until = time.time() + 0.9

    def on_shift_space(self):
        """Shift+Space：全角切换。IME 不上报全角位时靠这里推断

        只对**中文键盘**生效。英语键盘没有全角这回事，那里的 Shift+Space
        就是打一个空格，当成切全角会显示一个并不存在的变化。
        """
        try:
            langid = foreground_langid(user32.GetForegroundWindow())
        except Exception:
            langid = 0
        cur = self.last
        if not is_chinese_layout(langid or (cur.langid if cur else 0)):
            return
        if not self._fs[1]:
            self.fast_until = time.time() + 0.9
        base = cur if cur is not None else read_state()
        val = self.fw.toggle(base)
        if val is None:
            return
        if self.cfg.get("show_on_fullwidth_change", True):
            shown = Reading(hwnd=base.hwnd, langid=base.langid, hkl=base.hkl,
                            cn=base.cn, full=val, caps=base.caps,
                            ok=True, source="infer", conv=base.conv,
                            open_status=base.open_status)
            self.last = shown
            self.tr.note((shown.cn, shown.full, shown.caps, shown.langid),
                         time.time())
            self.display(shown, "full")

    # ---------- 显示 ----------
    def display(self, cur, hl=""):
        self.ui.show(cur.cn, cur.full, cur.caps, hl=(hl,) if hl else (),
                     langid=cur.langid)

    def refresh_now(self):
        """设置窗口里点「预览」：重新采一次再画，别画旧状态"""
        r = self.sample()
        if r.cn is None and self.last is not None:
            r = self.last
        self.last = r
        self.tr.note((r.cn, r.full, r.caps, r.langid), time.time())
        self.display(r, "")

    def preview_live(self, key):
        """设置窗口里拖动控件时的实时预览（不写盘）"""
        if key == "opacity":
            if self.ui.visible:
                self.ui._apply_alpha()
        elif key == "margin_bottom":
            self.ui.place()
        elif key == "duration":
            self.ui.reschedule_hide()
        elif key == "always_on":
            self.ui.always_on = bool(self.cfg.get("always_on"))
            self.tray.state["always"] = self.ui.always_on
            self.ui.reschedule_hide()
        elif key == "transparent_bg":
            self.tray.state["transparent"] = bool(self.cfg.get("transparent_bg"))
            self.ui.apply_transparent_bg()
        elif key == "show_fullwidth":
            self.tray.state["fullwidth"] = bool(self.cfg.get("show_fullwidth"))
            self.refresh_now()

    def apply_config(self):
        cfg = self.cfg
        self.ui.cfg = cfg
        self.ui.always_on = bool(cfg.get("always_on"))
        self.ui.apply_transparent_bg()
        self.ui.place()
        if self.ui.visible:
            self.ui._apply_alpha()
            self.ui.reschedule_hide()
        self.tray.state.update(always=self.ui.always_on,
                               transparent=bool(cfg.get("transparent_bg")),
                               fullwidth=bool(cfg.get("show_fullwidth", True)),
                               autostart=get_autostart())

    # ---------- 主循环 ----------
    def _set_hook(self, on):
        """全屏游戏里把全局键盘钩子卸掉。

        WH_KEYBOARD_LL 是系统级钩子：每次按键，Windows 都要等我们的回调返回
        才把键交给前台程序。平时无所谓，游戏里这就是实打实的输入延迟 ——
        这也是"全屏时 UI 导致卡顿"的一部分。所以全屏期间一律不装。
        """
        if not self.hook:
            return
        if on == self._hook_on and (not on or self.hook.is_running()):
            return
        # 走到这里可能是：状态变了，或者钩子线程自己死了（Windows 因回调超时
        # 会静默卸载），两种情况都要重新装一次。
        try:
            if on:
                self.hook.start()
            else:
                self.hook.stop()
        except Exception:
            log_crash("键盘钩子", traceback.format_exc())
            return
        self._hook_on = on
        self.log("键盘钩子 %s" % ("装上" if on else "卸掉"))

    def tick(self):
        delay = 60
        try:
            now = time.time()
            policy = self.cfg.get("fullscreen_policy", "pause")
            prev_fs = self._fs[1]
            fs, why = self.fullscreen_now()
            if fs != prev_fs:
                self.log("全屏 -> %s (%s)" % ("是" if fs else "否", why))
            # 游戏里不做淡出：每一帧 alpha 变化都是一次 DWM 更新，
            # 而这时游戏正处在"被合成"的临界点上，能省一次是一次。
            self.ui.set_fade(not (fs and policy != "pause"))
            self.ui._in_game = bool(fs)

            if fs and policy != "normal":
                if policy == "pause":
                    # 完全静默：不采样、不碰 IMM、不重绘。游戏里这是唯一
                    # 真正"零卡顿"的做法（实测跨进程读一次要 12~18ms）。
                    if self.ui.visible:
                        self.ui.hide_now()
                    self._set_hook(False)
                    self.fast_until = 0
                    self.tr.soft_reset(now, grace=0.4)
                    delay = 700
                    self._handle_cmds()
                    return
                self._set_hook(False)
                delay = 600
            else:
                if prev_fs and not fs:
                    self.tr.soft_reset(now, grace=0.4)
                self._set_hook(bool(self.cfg.get("hook_hotkeys", True)))
                base_poll = max(20, int(self.cfg.get("poll_ms", 60)))
                if now < self.fast_until and not self._fail:
                    delay = 20
                else:
                    delay = base_poll
                    # 读不到就退避：别对无响应/受保护的目标每 60ms 撞一次
                    if self._fail:
                        delay = min(800, base_poll * (1 + min(self._fail, 6)))

            r = self.sample()
            self._handle_cmds()

            # 目标响应慢（例如窗口化游戏里一次采样要 20~30ms）就自动降频，
            # 否则 60ms 一轮里有一半时间在等跨进程消息，白白拖慢自己。
            if r.ms > 10:
                delay = max(delay, min(400, int(r.ms * 4)))

            if r.cn is None and self.last is not None:
                # 这次没读到：保持旧值，只更新缓存。绝不因为"读不到"弹一次 ——
                # 启动瞬间和游戏里的乱刷就是这么来的。
                prev = self.last
                r.cn = prev.cn
                r.full = prev.full if r.full is None else r.full
                r.ok = prev.ok
                r.source = "stale"
            self.last = r

            ev = self.tr.feed((r.cn, r.full, r.caps, r.langid), r.hwnd, now)
            if ev:
                kind, sig = ev
                cur = Reading(hwnd=r.hwnd, langid=sig[3], hkl=r.hkl,
                              cn=sig[0], full=sig[1], caps=sig[2],
                              ok=True, source=r.source, ms=r.ms)
                self.display(cur, "" if kind == "window" else kind)
                self.log("%s -> %s" % (kind, self._fmt(cur)))
            elif self.ui.always_on and not self.ui.visible \
                    and self.tr.committed is not None:
                # 常驻模式：等状态机提交过至少一次再画，否则第一帧会先闪一下
                # 未经确认的原始读数，紧接着再被 startup 覆盖。
                self.display(r, "")
        except Exception:
            log_crash("主循环", traceback.format_exc())
        finally:
            # 重调度必须放 finally：一次异常就永久停摆是最难查的坑
            try:
                if self.running:
                    self.ui.root.after(delay, self.tick)
            except Exception:
                pass

    @staticmethod
    def _fmt(cur):
        zh = is_chinese_layout(cur.langid or 0)
        cn = cur.cn
        if cn is True:
            s = "中文"
        elif cn is False:
            s = "英文(输入法内)" if zh else "英文(英语键盘)"
        else:
            s = "未知"
        if cur.langid and not zh:
            full = "无全角"
        else:
            full = {True: "全角", False: "半角", None: "?"}[cur.full]
        return "%s/%s" % (s, full)

    def _handle_cmds(self):
        while True:
            try:
                cmd = self.cmd_q.get_nowait()
            except queue.Empty:
                return
            cfg = self.cfg
            if cmd == ID_EXIT:
                self.running = False
                self.tray.remove()
                self.ui.root.after(60, self._quit)
            elif cmd == ID_ALWAYS:
                cfg["always_on"] = not cfg.get("always_on")
                self.ui.always_on = cfg["always_on"]
                self.tray.state["always"] = cfg["always_on"]
                save_config(cfg)
                if cfg["always_on"]:
                    self.refresh_now()
                else:
                    self.ui.reschedule_hide()
            elif cmd == ID_TRANSP:
                cfg["transparent_bg"] = not cfg.get("transparent_bg")
                self.tray.state["transparent"] = cfg["transparent_bg"]
                self.ui.apply_transparent_bg()
                save_config(cfg)
                self.refresh_now()
            elif cmd == ID_FULLW:
                cfg["show_fullwidth"] = not cfg.get("show_fullwidth", True)
                self.tray.state["fullwidth"] = cfg["show_fullwidth"]
                save_config(cfg)
                self.refresh_now()
            elif cmd == ID_AUTO:
                new = not get_autostart()
                set_autostart(new)
                self.tray.state["autostart"] = new
            elif cmd == ID_LOG:
                self.set_log(not self.log_on)
            elif cmd == ID_SETTINGS:
                self.open_settings()
            elif cmd == ID_SHOW:
                self.refresh_now()

    def open_settings(self):
        try:
            if self.settings and self.settings.top.winfo_exists():
                self.settings.top.lift()
                self.settings.top.focus_force()
                return
            self.settings = SettingsWindow(self)
        except Exception:
            log_crash("打开设置", traceback.format_exc())
            _crash_dialog("设置窗口打不开", traceback.format_exc())

    def _quit(self):
        try:
            self.tray.remove()
        except Exception:
            pass
        try:
            self.ui.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run(self):
        self.log("启动 v%s  轮询=%sms  全屏策略=%s  钩子=%s"
                 % (VERSION, self.cfg.get("poll_ms"),
                    self.cfg.get("fullscreen_policy"),
                    "开" if self.hook else "关"))
        # 启动时不再额外 refresh_now：第一帧由 tick 的状态机在稳定后画一次，
        # 否则会和 tick 抢着画，出现"刚启动闪两下"。
        self.ui.root.after(30, self.tick)
        self.ui.root.mainloop()


# ==========================================================================
# 命令行
# ==========================================================================
def run_once():
    hwnd = foreground_window()
    r = read_state(hwnd)
    fw = FullWidthTracker(load_config(), None)
    r.full = fw.resolve(r)
    fs, why = detect_fullscreen(hwnd)
    if r.langid and not is_chinese_layout(r.langid):
        fulltxt = "不适用（%s键盘没有全角）" % (lang_name(r.langid) or "该")
    else:
        fulltxt = {True: "是", False: "否", None: "?"}[r.full]
    print("前台窗口: 0x%X  %s" % (hwnd or 0, window_class(hwnd) if hwnd else "-"))
    print("输入语言: 0x%04X  %s   HKL=0x%08X" %
          (r.langid, lang_name(r.langid) or "未知", r.hkl))
    print("中文: %s   全角: %s   可靠: %s   来源: %s  耗时: %.2fms"
          % ({True: "是", False: "否", None: "?"}[r.cn], fulltxt, r.ok,
             r.source, r.ms))
    if r.conv is not None:
        print("IME 原始值: conv=0x%X  open=%s  FULLSHAPE(0x8)=%d"
              % (r.conv, r.open_status, 1 if r.conv & IME_CMODE_FULLSHAPE else 0))
    print("全屏判定: %s (%s)" % ("是" if fs else "否", why))
    print("大写锁定: %s" % ("开" if caps_lock_on() else "关"))
    return 0 if r.ok else 1


def run_debug(secs=0):
    """持续打印采样结果（含 IME 原始值），用来确认检测链路是否正常"""
    print("调试模式：切换输入法 / 中英文 / 全角 / 窗口，观察下面输出。")
    print("全角那一行看 conv 的 0x8 位：按 Shift+Space 时它会变，说明这个输入法"
          "会上报；一直不变就是不上报，靠推断。Ctrl+C 结束。\n")

    def learned(caps):
        print("        → 学到: %s" % caps, flush=True)

    fw = FullWidthTracker(load_config(), learned)
    end = time.time() + secs if secs else 0
    last = None
    try:
        while True:
            hwnd = foreground_window()
            r = read_state(hwnd)
            r.full = fw.resolve(r)
            fs, why = detect_fullscreen(hwnd)
            cur = (hwnd, r.cn, r.full, r.langid, r.conv)
            mark = "  <= 变化" if last is not None and cur != last else ""
            bit = ("-" if r.conv is None
                   else ("1" if r.conv & IME_CMODE_FULLSHAPE else "0"))
            print("[%s] %-18s %-4s 中=%-3s 全=%-3s | conv=%-6s open=%-4s 0x8=%s"
                  " | %-7s %5.2fms%s"
                  % (time.strftime("%H:%M:%S"),
                     (window_class(hwnd) or "-")[:18],
                     lang_name(r.langid) or "?",
                     {True: "是", False: "否", None: "?"}[r.cn],
                     {True: "是", False: "否", None: "?"}[r.full],
                     ("0x%X" % r.conv) if r.conv is not None else "-",
                     r.open_status, bit, r.source, r.ms, mark),
                  flush=True)
            last = cur
            if end and time.time() > end:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    return 0


def run_demo(cfg):
    """演示动画：把三种真实状态都过一遍，看配色和文案区别"""
    ui = Indicator(dict(cfg))
    cn_lay, en_lay = 0x0804, 0x0409
    seq = [
        (True, False, cn_lay, ()),              # 中文输入
        (False, False, cn_lay, ("mode",)),      # 中文输入法内切英文（琥珀）
        (False, False, en_lay, ("mode",)),      # 英语键盘（绿）
        (True, True, cn_lay, ("full",)),        # 全角
    ]
    i = [0]

    def step():
        cn, full, langid, hl = seq[i[0] % len(seq)]
        ui.show(cn, full, False, hl=hl, langid=langid)
        i[0] += 1
        ui.root.after(1400, step)

    ui.root.after(300, step)
    ui.root.mainloop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="中英文输入状态提示条")
    ap.add_argument("--once", action="store_true", help="读一次当前状态后退出")
    ap.add_argument("--debug", nargs="?", const=0, type=float, default=None,
                    metavar="秒", help="持续打印检测结果（默认无限）")
    ap.add_argument("--demo", action="store_true", help="演示提示条动画")
    ap.add_argument("--log", action="store_true", help="启动时记录 events.log")
    ap.add_argument("--version", action="store_true", help="显示版本")
    args = ap.parse_args(argv)

    if args.version:
        print("IME Indicator v%s" % VERSION)
        return 0
    if args.once:
        return run_once()
    if args.debug is not None:
        return run_debug(args.debug)
    if args.demo:
        return run_demo(load_config())

    install_crash_handler()
    if already_running():
        notify_already_running()
        return 0
    app = App(load_config())
    if args.log:
        app.log_on = True
        app.tray.state["log"] = True
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
