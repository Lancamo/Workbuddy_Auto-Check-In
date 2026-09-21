#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winenv.py — Windows 版「所有 OS 差异」的唯一集中地（跨平台适配层）

为什么单独搞一个文件
--------------------
本项目有两套：macOS 版（上级目录）与 Windows 版（本目录）。两套要求**长期同步演进**。
同步成本取决于「同名文件之间有多少差异」——如果 OS 相关代码散落在 catchup / notify /
renew / clawbot / api_discovery / http_client 六个文件里，每次同步都要逐个文件人肉比对，
迟早漏掉一处。所以约定：

    ★ 所有平台判断、路径候选、编码处理、原生通知、解释器探测，**只允许写在本文件**。
      其余文件除少数几行调用外，应与 macOS 版逐字一致。

这样同步时的动作就是机械的：`diff` 两套的同名文件，只看差异行，差异必然很小。

本文件提供的能力
----------------
| 函数 | 作用 |
|---|---|
| `IS_WIN` / `IS_MAC` / `IS_LINUX` | 平台判定 |
| `home()` / `appdata()` / `localappdata()` | 基础目录 |
| `settings_candidates()` | WorkBuddy 桌面端 `settings.json`（ClawBot 凭据来源）候选路径 |
| `claw_state_dirs()` | 桌面端 ClawBot 轮询游标目录候选（renew.py 用） |
| `client_asar_candidates()` | 桌面端安装目录下的 `app.asar` 候选路径（接口发现用） |
| `detect_client()` | 探测客户端：返回 asar 路径 + **版本号** |
| `read_asar_version()` | 从 asar 头部直接解析 `package.json` 版本（跨平台） |
| `default_python()` | 当前解释器 / 计划任务该用哪个解释器 |
| `selfcheck_hint()` / `login_hint()` | 通知与报错文案里的「请运行 xxx」指引（按平台生成） |
| `native_notify()` | 原生桌面通知（Windows Toast / macOS 通知中心） |
| `setup_stdio()` | 控制台编码 & 无控制台（计划任务）下的输出兜底 |
| `subprocess_env()` / `subprocess_flags()` | 子进程环境与「不弹黑窗」标志 |

路径覆盖（为什么给这么多候选）
------------------------------
本机无法验证另一台 Windows 的实际布局，所以不猜单一答案，而是**列全候选 + 逐个探测 +
打印命中的那个**。任何一处猜错，`doctor.py` 会立刻显示"哪些路径找过、哪些不存在"，
照着实测结果改 `win_paths.json` 覆盖即可，不需要改代码。

覆盖配置 `win_paths.json`（可选，优先级最高，空值表示不覆盖）
    {
      "client_asar": "",         # 例：C:\\Users\\me\\AppData\\Local\\Programs\\WorkBuddy\\resources\\app.asar
      "workbuddy_settings": "",  # 例：C:\\Users\\me\\.workbuddy\\settings.json
      "claw_state_dir": ""       # 例：C:\\Users\\me\\.workbuddy\\claw-state\\weixin
    }

安全：本模块只读本机文件、只发本机通知，不做任何网络请求，不涉及任何凭据内容。
"""

from __future__ import annotations

import base64
import codecs
import json
import os
import pathlib
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 平台
# ---------------------------------------------------------------------------
def platform() -> str:
    """返回 "win" / "mac" / "linux" —— **三选一，互斥**。

    为什么不用 `sys.platform` 直接散在各处判断：
      · 集中一处，语义唯一（`sys.platform.startswith("win")` 与
        `sys.platform == "darwin"` 是两个彼此独立的条件，同时为真的组合
        在逻辑上是可能的，散着写迟早写出「先命中 mac 分支」这类隐蔽 bug）；
      · **可测试** —— 自检脚本 `selftest.py` 会把本函数换成 `lambda: "win"`，
        从而在没有 Windows 的机器上真实执行一遍 Windows 分支
        （路径候选、通知后端、不弹窗标志等）。
    """
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


# 便捷常量：仅用于「读一眼就懂」的场景；**内部逻辑一律调 platform()**，
# 这样测试才能把平台翻过去。
IS_WIN = platform() == "win"
IS_MAC = platform() == "mac"
IS_LINUX = platform() == "linux"

HERE = pathlib.Path(__file__).resolve().parent
PATH_OVERRIDE_FILE = HERE / "win_paths.json"

# 桌面端产品名候选（WorkBuddy 桌面端基于 CodeBuddy 外壳，安装目录名两种都出现过）
APP_DIR_NAMES = ("WorkBuddy", "workbuddy", "CodeBuddy CN", "CodeBuddy", "CodeBuddyCN")


# ---------------------------------------------------------------------------
# 基础目录
# ---------------------------------------------------------------------------
def home() -> pathlib.Path:
    return pathlib.Path.home()


def _env_path(name: str) -> pathlib.Path | None:
    v = os.environ.get(name)
    return pathlib.Path(v) if v else None


def appdata() -> pathlib.Path | None:
    """Windows: %APPDATA%（Roaming）。其它平台返回 None。"""
    return _env_path("APPDATA")


def localappdata() -> pathlib.Path | None:
    """Windows: %LOCALAPPDATA%。"""
    return _env_path("LOCALAPPDATA")


def programfiles() -> list[pathlib.Path]:
    r"""Program Files 目录候选（Windows）。

    ★ 为什么环境变量之外还必须有硬编码兜底（2026-09-20 修的真实缺陷）：
      原先只读 `PROGRAMFILES` / `PROGRAMFILES(X86)` / `ProgramW6432` 三个环境变量。
      但**环境变量并不总是存在** —— 实测在一台正常装了 WorkBuddy 的 Windows 上，
      这三个变量全都是空的（精简启动器 / 沙箱 / 计划任务上下文都可能只传一小撮变量），
      于是客户端明明装在 `C:\Program Files\WorkBuddy` 却探测不到，连锁后果是：
        · doctor.py 报「找不到 WorkBuddy 客户端」；
        · api_discovery 退回内置端点表（拿不到 App 里的权威端点）；
        · 版本号读不到 → User-Agent 退化。
      而 `C:\Program Files` 与 `C:\Program Files (x86)` 是 Windows 的**固定约定**，
      不依赖任何变量。把两者都列上，环境变量在就用它的（支持自定义系统盘），
      不在也能命中默认布局。候选是「多列几个逐个探测」，列全没有副作用。
    """
    out: list[pathlib.Path] = []
    for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramW6432"):
        p = _env_path(k)
        if p and p not in out:
            out.append(p)
    if platform() == "win":
        for raw in ("C:\\Program Files", "C:\\Program Files (x86)"):
            p = pathlib.Path(raw)
            if p not in out:
                out.append(p)
    return out


# ---------------------------------------------------------------------------
# 覆盖配置
# ---------------------------------------------------------------------------
def load_overrides() -> dict:
    """读取 win_paths.json（不存在/损坏即返回空，绝不抛异常）。"""
    try:
        if PATH_OVERRIDE_FILE.exists():
            d = json.loads(PATH_OVERRIDE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {k: v for k, v in d.items()
                        if isinstance(v, str) and v.strip() and not k.startswith("_")}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _override(key: str) -> pathlib.Path | None:
    v = load_overrides().get(key)
    return pathlib.Path(v) if v else None


# ---------------------------------------------------------------------------
# 候选路径：WorkBuddy settings.json（ClawBot 凭据）
# ---------------------------------------------------------------------------
def settings_candidates() -> list[pathlib.Path]:
    """WorkBuddy 桌面端 `settings.json` 候选路径（**顺序即优先级**）。

    macOS 实测命中第一项 `~/.workbuddy/settings.json`。Windows 桌面端由同一套
    Electron 代码构建，大概率同样写在家目录的 `.workbuddy` 下；但 Electron 应用
    在 Windows 上也可能落到 `%APPDATA%`，故一并列出。
    """
    ov = _override("workbuddy_settings")
    out: list[pathlib.Path] = []
    if ov:
        out.append(ov)
    out.append(home() / ".workbuddy" / "settings.json")
    for base in (appdata(), localappdata()):
        if base:
            for name in ("WorkBuddy", "workbuddy"):
                out.append(base / name / "settings.json")
    out.append(home() / ".codebuddy" / "settings.json")
    return _dedupe(out)


# ---------------------------------------------------------------------------
# 候选路径：ClawBot 轮询游标目录（renew.py 的监测对象）
# ---------------------------------------------------------------------------
CLAW_STATE_SUBPATH = ("claw-state", "weixin")


def claw_state_dirs() -> list[pathlib.Path]:
    """桌面端存放 ClawBot 轮询游标的目录候选。

    桌面端每次成功轮询都会刷新 `<dir>/<bot_id>.cursor.json`，
    其 mtime 即「会话最后一次被维持」的时间 —— 这是 renew.py 的核心判据。
    """
    ov = _override("claw_state_dir")
    out: list[pathlib.Path] = []
    if ov:
        out.append(ov)
    out.append(home().joinpath(".workbuddy", *CLAW_STATE_SUBPATH))
    for base in (appdata(), localappdata()):
        if base:
            for name in ("WorkBuddy", "workbuddy"):
                out.append(base.joinpath(name, *CLAW_STATE_SUBPATH))
    return _dedupe(out)


# ---------------------------------------------------------------------------
# 候选路径：客户端安装目录 / app.asar
# ---------------------------------------------------------------------------
def client_asar_candidates() -> list[pathlib.Path]:
    """客户端 `app.asar` 候选路径（接口发现的权威来源）。"""
    ov = _override("client_asar")
    out: list[pathlib.Path] = []
    if ov:
        out.append(ov)

    roots: list[pathlib.Path] = []
    plat = platform()
    if plat == "mac":
        roots.append(pathlib.Path("/Applications"))
    elif plat == "win":
        la = localappdata()
        if la:
            roots += [la / "Programs", la / "Programs" / "WorkBuddy", la]
        roots += programfiles()
    else:
        roots += [pathlib.Path("/usr/lib"), pathlib.Path("/opt"),
                  home() / ".local" / "share"]

    for r in roots:
        for name in APP_DIR_NAMES:
            out.append(r / name / "resources" / "app.asar")
    if plat == "mac":
        for name in APP_DIR_NAMES:
            out.append(pathlib.Path("/Applications") / (name + ".app")
                       / "Contents" / "Resources" / "app.asar")
    return _dedupe(out)


def client_info_plist_candidates() -> list[pathlib.Path]:
    """macOS 专用：`Info.plist`（版本号来源之一）。其它平台返回空。"""
    if platform() != "mac":
        return []
    out = []
    for name in APP_DIR_NAMES:
        out.append(pathlib.Path("/Applications") / (name + ".app")
                   / "Contents" / "Info.plist")
    return out


# ---------------------------------------------------------------------------
# 版本号：从 asar 头部直接解析（跨平台，无需解包、无需客户端运行）
# ---------------------------------------------------------------------------
_ASAR_MAX_JSON = 64 * 1024 * 1024   # JSON 头部合理性上限，防脏文件导致巨量分配


def read_asar_version(asar_path: str | pathlib.Path) -> str | None:
    """读取 asar 内 `package.json` 的 `version`。

    asar 文件结构（Electron 官方格式，只读前几 KB，不解包整个文件）：
        偏移 0   uint32le  = 4                          （第一个字段的长度）
        偏移 4   uint32le  = headerPickleSize            （整个 header pickle 的字节数）
        偏移 8   uint32le  = 4
        偏移 12  uint32le  = jsonSize                    （header JSON 的字节数）
        偏移 16  jsonSize 字节的 JSON（文件索引，只含元数据）
        数据段起始 = 8 + headerPickleSize                ← ★ 实测校准值
        某文件内容偏移 = 数据段起始 + entry.offset

    为什么不用 Info.plist：Windows 上没有它。而 asar 是**两个平台都有的同一份产物**，
    因此这条路在 Windows 上同样成立（本函数已在 macOS 真机用 298MB 的 asar 交叉验证，
    解析结果 5.5.6 与 Info.plist 完全一致）。

    任何异常都返回 None —— 版本号只影响 User-Agent 与指纹，绝不阻断主流程。
    """
    try:
        with open(asar_path, "rb") as f:
            head = f.read(16)
            if len(head) < 16:
                return None
            _, header_pickle_size, _, json_size = struct.unpack("<IIII", head)
            if not (0 < json_size <= _ASAR_MAX_JSON) or header_pickle_size < json_size:
                return None
            header = json.loads(f.read(json_size).decode("utf-8", "replace"))
            entry = (header.get("files") or {}).get("package.json")
            if not isinstance(entry, dict):
                return None
            base = 8 + header_pickle_size
            f.seek(base + int(entry.get("offset") or 0))
            pkg = json.loads(f.read(int(entry.get("size") or 0)).decode("utf-8", "replace"))
            v = pkg.get("version")
            return str(v) if v else None
    except Exception:  # noqa: BLE001
        return None


# 版本号文本兜底（asar 解析失败时用；Info.plist 亦失败时才会走到）
def _version_from_replacements(asar_path: pathlib.Path) -> str | None:
    """从 asar 同级的 `app-update.yml` / `resources` 目录旁找版本线索。

    这是兜底路径，命中率不高。真正的手段是 `read_asar_version()`。
    """
    for name in ("app-update.yml", "version", "package.json"):
        p = asar_path.parent / name
        try:
            if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if name == "package.json":
            try:
                v = json.loads(txt).get("version")
                if v:
                    return str(v)
            except Exception:  # noqa: BLE001
                pass
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def _version_from_plist() -> str | None:
    try:
        import plistlib
        for p in client_info_plist_candidates():
            if p.is_file():
                with p.open("rb") as f:
                    info = plistlib.load(f)
                v = (info.get("CFBundleShortVersionString")
                     or info.get("CFBundleVersion"))
                if v:
                    return str(v)
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# 客户端探测（api_discovery / http_client 共用）
# ---------------------------------------------------------------------------
def detect_client(force: bool = False) -> dict:
    """定位本机 WorkBuddy 客户端并读出其版本。返回结构化结果（含诊断用字段）。

    返回：
        {
          "available": bool,          # 是否找到可读的 app.asar
          "asar": str|None,           # 命中的 asar 绝对路径
          "root": str|None,           # 客户端安装根目录
          "version": str|None,        # 客户端版本（asar → plist → 旁路文件）
          "version_source": str|None, # 版本是从哪儿读到的
          "asar_size": int|None,
          "asar_mtime": int|None,
          "tried": [ {"path":…, "exists":bool} … ]   # 找过哪些路径（排障用）
        }

    `force` 目前不做额外事，保留参数以对齐调用方习惯。
    """
    out: dict = {
        "available": False, "asar": None, "root": None,
        "version": None, "version_source": None,
        "asar_size": None, "asar_mtime": None, "tried": [],
    }
    asar: pathlib.Path | None = None
    for p in client_asar_candidates():
        exists = False
        try:
            exists = p.is_file()
        except OSError:
            exists = False
        out["tried"].append({"path": str(p), "exists": exists})
        if exists and asar is None:
            asar = p

    if asar is None:
        out["version"] = _version_from_plist()
        if out["version"]:
            out["version_source"] = "Info.plist"
        return out

    out["asar"] = str(asar)
    out["root"] = str(asar.parent.parent)
    try:
        st = asar.stat()
        out["asar_size"] = st.st_size
        out["asar_mtime"] = int(st.st_mtime)
    except OSError:
        pass
    out["available"] = True

    v = read_asar_version(asar)
    if v:
        out["version"], out["version_source"] = v, "asar:package.json"
    else:
        v = _version_from_plist() or _version_from_replacements(asar)
        if v:
            out["version"], out["version_source"] = v, "fallback"
    return out


_client_cache: dict | None = None


def client_cached() -> dict:
    """进程内缓存版（一次运行只探测一次）。"""
    global _client_cache
    if _client_cache is None:
        _client_cache = detect_client()
    return _client_cache


# ---------------------------------------------------------------------------
# Python 解释器
# ---------------------------------------------------------------------------
def python_exe_paths() -> dict:
    """返回 {'console': 有控制台的解释器, 'windowless': 无窗口解释器}。

    Windows 计划任务用 `pythonw.exe`：它不分配控制台，因此**不会闪黑窗**，
    也不会因控制台代码页把中文输出打成乱码（本脚本会把输出重定向到日志）。
    手动运行时用 `python.exe`，方便看输出。
    """
    exe = sys.executable or ""
    out = {"console": exe, "windowless": exe}
    if not exe:
        return out
    p = pathlib.Path(exe)
    if platform() == "win" and p.name.lower() == "python.exe":
        w = p.with_name("pythonw.exe")
        if w.is_file():
            out["windowless"] = str(w)
    if platform() == "win" and p.name.lower() == "pythonw.exe":
        c = p.with_name("python.exe")
        if c.is_file():
            out["console"] = str(c)
    return out


def default_python(windowless: bool = False) -> str:
    """当前应使用的解释器绝对路径。

    优先级：环境变量 WB_REWARD_PYTHON → 当前解释器（按需切换 python/pythonw）。

    两侧语义一致：**用运行自己的那个解释器**，拷到任何机器都成立，无需改任何常量。
    mac 版等价实现是 `catchup.py` 里的 `PY = sys.executable`；
    Windows 这里多一层 python.exe ↔ pythonw.exe 的切换（计划任务要无控制台的 pythonw）。
    """
    env = os.environ.get("WB_REWARD_PYTHON")
    if env and pathlib.Path(env).is_file():
        return env
    paths = python_exe_paths()
    return paths["windowless"] if windowless else paths["console"]


# ---------------------------------------------------------------------------
# 给用户看的「该执行什么」指引（按平台生成）
# ---------------------------------------------------------------------------
# 为什么放这里：通知和报错文案里会带上「请运行 xxx」的指引，
# mac 版写的是 `python3 scripts/api_discovery.py`，这在 Windows 上跑不通
# （既没有 `python3` 命令，api_discovery.py 也不是可执行入口）。
# 按规则「平台差异只许写在 winenv.py」，把这两句收在这里，
# 其余文件只调用，不出现任何平台判断。
def selfcheck_hint() -> str:
    """接口疑似变更时，让用户自查端点的方式。

    ★ 必须调 `platform()` 而不是模块级常量 `IS_WIN`：常量在导入时就固定，
      自检把平台翻成 mac 后它不会跟着变，于是「文案是否按平台生成」这条不变量
      根本无法被验证 —— 自检第 8b 节正是因此长期报 FAIL（见 selftest.py）。
      本模块自己的约定就是「内部逻辑一律调 platform()」，这里之前漏了。
    """
    if platform() == "win":
        return "双击本文件夹下的 doctor.cmd"
    return "python3 scripts/api_discovery.py"


def login_hint() -> str:
    """微信会话失效（-14）时，让用户重新登录的方式。

    同样必须调 `platform()`（理由见 selfcheck_hint）。
    """
    if platform() == "win":
        return "双击本文件夹下的 login.cmd"
    return '"{}" clawbot.py login'.format(default_python())


# ---------------------------------------------------------------------------
# 控制台 / 编码
# ---------------------------------------------------------------------------
def setup_stdio(log_dir: pathlib.Path | None = None) -> str | None:
    """把 stdout/stderr 调成 UTF-8，并在**没有 stdout 时**（计划任务 + pythonw）重定向到日志。

    为什么必须做：
      · Windows 中文默认代码页是 GBK，脚本大量输出中文 JSON，不调编码会 UnicodeEncodeError；
      · `pythonw.exe` 跑起来时 `sys.stdout is None`，任何 `print()` 都会直接抛异常；
      · 计划任务没有控制台，看不到任何输出 → 必须落到文件才能事后排障。

    返回重定向到的日志路径（未重定向则 None）。
    """
    redirected = None
    log_dir = log_dir or (HERE / "runtime" / "logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = HERE

    def _reopen(name: str) -> None:
        nonlocal redirected
        p = log_dir / "stdio.log"
        try:
            f = open(p, "a", encoding="utf-8", errors="replace")
        except OSError:
            return
        redirected = str(p)
        setattr(sys, name, f)

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            _reopen(name)
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — 旧版本 / 已被接管：忽略
            pass
    return redirected


def decode_console(raw: bytes) -> str:
    """把 Windows 命令（reg / schtasks / tasklist / powershell …）的输出解码成 str。

    ★ 为什么不能图省事写 `subprocess.run(..., text=True)`：
      那样是按 `locale.getpreferredencoding(False)` 解码的，而**这个值会随环境漂移**。
      在开了 UTF-8 模式的进程里（`PYTHONUTF8=1` 或 `LANG=C.UTF-8`；本项目给子进程
      设的就是 PYTHONUTF8=1）它返回 utf-8，可 Windows 命令吐出来的是**控制台代码页**
      （中文系统 = GBK）。两者一错位，reader 线程就抛 UnicodeDecodeError：
        · 用户看到一段吓人的 traceback；
        · 那个值被静默当成「读不到」。
      2026-09-20 实测：`notify.py status` 读 Toast 注册表开关时就是这么崩的
      （`reg query` 查不到键时连**报错信息**都是中文 GBK，0xb4 开头）。
      同一类错误在 watchdog 里也犯过（那边按「不许 import 项目模块」的约定自带一份实现）。

    顺序：BOM 判定 → 无 BOM 的 UTF-16（ASCII 内容每隔一字节一个 NUL）→
          严格 UTF-8 → 中文代码页 → 系统 locale → utf-8/replace。
    **永不抛异常**，永远返回 str。
    """
    if not raw:
        return ""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"),
                     (codecs.BOM_UTF16_LE, "utf-16"),
                     (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                break
    sample = raw[:4096]
    even, odd = sample[::2].count(b"\x00"), sample[1::2].count(b"\x00")
    if even or odd:
        try:
            return raw.decode("utf-16-be" if even > odd else "utf-16-le")
        except UnicodeDecodeError:
            pass
    # 严格 UTF-8 优先（纯 ASCII 与真 UTF-8 都走这条）；失败再按中文代码页。
    # 顺序不能反：GBK 中文几乎必然是非法 UTF-8，反之则会「解码成功但全是乱码」。
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        import locale
        return raw.decode(locale.getpreferredencoding(False))
    except Exception:  # noqa: BLE001
        pass
    return raw.decode("utf-8", errors="replace")


def subprocess_env() -> dict:
    """子进程环境：强制 UTF-8，避免中文在管道里被按 GBK 编码。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("ELECTRON_RUN_AS_NODE", None)  # 继承自 Electron 时会打乱 python 行为
    return env


def subprocess_flags() -> dict:
    """子进程「不弹黑窗」标志（仅 Windows 有意义，其它平台返回空 dict）。

    计划任务里若用 pythonw 调 python.exe 子进程，不加这个标志会闪出黑窗。
    """
    if platform() != "win":
        return {}
    try:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    except AttributeError:  # 旧 Python / 非 Windows
        return {}


# ---------------------------------------------------------------------------
# 原生桌面通知（微信通道不可用时的兜底）
# ---------------------------------------------------------------------------
# Windows Toast 需要一个已注册的 AppUserModelID，否则部分系统版本会静默失败。
# PowerShell 自身的 AppID 是系统预注册的，无需安装任何东西即可投递成功；
# 代价是通知来源显示为「Windows PowerShell」。这是免注册方案里最稳的做法。
_PS_APPID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe")

_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $tpl.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($tpl.CreateTextNode($env:WB_NOTIFY_TITLE)) | Out-Null
$nodes.Item(1).AppendChild($tpl.CreateTextNode($env:WB_NOTIFY_BODY)) | Out-Null
if ($env:WB_NOTIFY_PERSIST -eq '1') {
    $root = $tpl.DocumentElement
    $root.SetAttribute('scenario', 'urgent') | Out-Null
    $root.SetAttribute('duration', 'long') | Out-Null
}
$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:WB_NOTIFY_APPID).Show($toast)
"""

# ★ 本机通知默认**常驻**（`scenario="urgent"` + `duration="long"`）：不自动消失，
#   必须手动关掉。为什么这么改（2026-09-20）：
#   微信通道当前不可用，本机 Toast 是**唯一**能让用户看见结果的通道；而默认 Toast
#   只在屏幕上停留几秒就缩进「通知中心」，可签到结果偏偏是在没人盯着屏幕的时刻
#   （07:00 唤醒后 / 猫到达那一刻）弹出来的 —— 一旦没看到，就等于没通知。
#   代价：需要手动点一次才能关掉。想恢复「几秒后自动消失」：
#   设环境变量 `WORKBUDDY_TOAST_PERSISTENT=0` 后重启计划任务（或本机手动跑一次）。
_TOAST_PERSISTENT_DEFAULT = True


def _toast_persistent() -> bool:
    """本机 Toast 是否常驻（不自动消失）。环境变量可覆盖，默认常驻。"""
    v = os.environ.get("WORKBUDDY_TOAST_PERSISTENT", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return _TOAST_PERSISTENT_DEFAULT


def _run_toast(title: str, content: str, persist: bool) -> bool:
    """投递一条 Toast。persist=True 时附加常驻属性。失败返回 False（不抛异常）。"""
    try:
        enc = base64.b64encode(_PS_TOAST.encode("utf-16-le")).decode("ascii")
        env = subprocess_env()
        env.update({
            "WB_NOTIFY_TITLE": title[:120],
            "WB_NOTIFY_BODY": content[:600],
            "WB_NOTIFY_APPID": _PS_APPID,
            "WB_NOTIFY_PERSIST": "1" if persist else "0",
        })
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            capture_output=True, timeout=25, env=env, **subprocess_flags(),
        )
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _notify_windows(title: str, content: str, persistent: bool | None = None) -> bool:
    """Windows 10/11 原生 Toast。用 `-EncodedCommand` 传脚本，彻底绕开引号与代码页问题。

    参数经**环境变量**传入（而不是拼进脚本字符串），因此标题/正文里的引号、
    换行、emoji 都不会破坏语法。

    persistent → `scenario="urgent"` + `duration="long"`，**不自动消失**。

    ★ 必须「先试常驻、失败再退回普通 Toast」：`scenario` 在个别系统版本上可能被拒，
      若因为它而整条通知发不出去，就等于为了「更显眼」把唯一可见的通道弄哑了 ——
      这是最不能接受的交易。宁可退化成几秒后自动消失，也不能一条都弹不出来。
    """
    want = _toast_persistent() if persistent is None else bool(persistent)
    for mode in ((True, False) if want else (False,)):
        if _run_toast(title, content, mode):
            return True
    return False


# ---------------------------------------------------------------------------
# 必须点掉才消失的弹窗（Windows 上唯一做得到的"不自动消失"）
# ---------------------------------------------------------------------------
# ★ 2026-09-21（用户反馈 + 实测）：Toast **即使**写了 `scenario="urgent"` +
#   `duration="long"`，在这台机器上依然"过一会儿就自己收走"（横幅约 25 秒后消失，
#   只会留在通知中心）。要真正做到「点叉才消失」，只有**原生对话框**这条路 ——
#   它是窗口而不是通知，没人点它就一直在那儿。
#
# 设计要点：
#   · 用 `pythonw.exe -c <代码>` **另起一个进程**弹窗：主脚本（计划任务每 5 分钟一次）
#     绝不能被一个没人点的对话框卡住。
#   · 窗口置顶（MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL），压在其它窗口之上。
#   · 屏幕上已经有 N 个没人处理的弹窗时**不再堆**（退回 Toast + 补弹队列），
#     免得离开几天回来被几十个窗口糊满。
#   · 每个弹窗开一个 `runtime/state/notify_dialog_<pid>.json` 标记，进程退出时自删；
#     残留标记由 `_live_dialogs()` 按 PID 存活清理（同理见 winenv.pid_alive）。
#   · 文字走**环境变量**传给子进程（不拼进命令行），中文/引号/换行都不会出问题；
#     传给 `-c` 的代码本身是**纯 ASCII**（自检会钉住这条）。
_DIALOG_MAX = 3          # 屏幕上最多同时存在几个待处理弹窗
# 卡片的界面代码放在**独立文件** `notify_card.py`（随交付物一起搬走，可单独调试）。
# ★ 用 **pythonw** 跑它：pythonw 是窗口子系统程序，**不会有控制台窗口**
#   （2026-09-21 用户要求：弹卡片时别弹出 PowerShell/控制台黑框）。
#   试过 PowerShell + WinForms 版本，两个问题：① 会闪一个控制台窗口；
#   ② 它的相对路径基准不是脚本目录，标记文件会被写到别处 —— Python 版都没有。
_DIALOG_SCRIPT = HERE / "notify_card.py"


def _alert_style() -> str:
    """本机通知用哪种通道：`dialog`（默认，必须点掉）或 `toast`（几秒后自动消失）。"""
    v = os.environ.get("WORKBUDDY_ALERT_STYLE", "").strip().lower()
    return v if v in ("dialog", "toast") else "dialog"


def _dialog_markers(log_dir: pathlib.Path | None = None) -> list[pathlib.Path]:
    try:
        return sorted(_reshow_file(log_dir).parent.glob("notify_dialog_*.json"))
    except OSError:
        return []


def _live_dialogs(log_dir: pathlib.Path | None = None) -> int:
    """屏幕上还有几个"没人点掉的"弹窗（顺带清理已退出进程留下的标记）。"""
    live = 0
    for p in _dialog_markers(log_dir):
        pid = 0
        try:
            pid = int(json.loads(p.read_text(encoding="utf-8")).get("pid") or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        if pid > 0 and pid_alive(pid):
            live += 1
            continue
        try:
            p.unlink()
        except OSError:
            pass
    return live


def _spawn(args: list, **kwargs):
    """起一个子进程。**单独包一层是为了可测试** —— 自检只替换 `winenv._spawn`，
    不去动 `subprocess.Popen` 这个全局模块属性（替换它会影响整个进程里的其它调用，
    包括运行环境自己的保护层）。
    """
    return subprocess.Popen(args, **kwargs)


def _notify_windows_dialog(title: str, content: str,
                           log_dir: pathlib.Path | None = None) -> bool:
    """在**右下角**弹一张通知式卡片，**只有点右上角 ✕ 才会消失**。

    弹不出来返回 False（调用方退回常驻 Toast）。另起一个进程，绝不阻塞主脚本；
    不出现控制台窗口（`-WindowStyle Hidden` + `CREATE_NO_WINDOW`）。
    """
    try:
        if _live_dialogs(log_dir) >= _DIALOG_MAX:
            return False
        # ★ 必须用**绝对路径**：PowerShell 的相对路径基准不是本脚本所在目录
        #   （实测：传相对路径时它把标记写去了别处，于是"卡片还开着"被判成已关闭）。
        mark = (_reshow_file(log_dir).parent
                / "notify_dialog_{}.json".format(os.getpid())).resolve()
        mark.parent.mkdir(parents=True, exist_ok=True)
        env = subprocess_env()
        env.update({
            "WB_DLG_TITLE": title[:120],
            "WB_DLG_BODY": content[:600],
            "WB_DLG_MARK": str(mark),
            # 屏幕上已存在的卡片张数 = 这一张的「层号」（0 最靠下，往上叠）
            "WB_DLG_SLOT": str(_live_dialogs(log_dir)),
        })
        if os.environ.get("WORKBUDDY_ALERT_DEBUG"):
            env["WB_DLG_DEBUG"] = os.environ["WORKBUDDY_ALERT_DEBUG"]
        # ★ creationflags 只能传一次：`subprocess_flags()` 自己就带这个键，
        #   再显式传一个 → `TypeError: got multiple values for keyword argument`
        #   → 被这里的 except 吞掉 → **静默退回 Toast**（弹窗永远不出现，
        #   而日志、退出码、自检全都看不出来）。2026-09-21 实测踩到过，所以合并成一个。
        #   （CREATE_NO_WINDOW 与 DETACHED_PROCESS 同时给时前者会被系统忽略，无害。）
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | int(subprocess_flags().get("creationflags", 0)))
        # pythonw 本身没有控制台，再叠上 subprocess_flags() 的 CREATE_NO_WINDOW：双保险。
        # （用户 2026-09-21 要求：弹卡片时不要出现 PowerShell / 黑框控制台窗口。）
        # ★ 标记必须在 spawn **之前**写（只作占位），卡片启动后会用自己的 PID 覆盖它。
        #   反过来（先 spawn 后写）会把卡片刚写进去的真实 PID 覆盖掉 —— 而 Popen.pid
        #   在本机经代理层起进程时指向的是**中间进程**，于是"卡片还开着"会被误判成
        #   "已关闭"（本机实测踩到：卡片明明在屏幕上，计数却是 0）。
        mark.write_text(json.dumps({"pid": 0, "ts": time.time()}, ensure_ascii=False),
                        encoding="utf-8")
        _spawn(
            [default_python(windowless=True), str(_DIALOG_SCRIPT)],
            env=env, close_fds=True, creationflags=flags, cwd=str(HERE),
        )
        # 卡片关闭时会自己删标记（见 notify_card.py 的 finally）
        return True
    except Exception:  # noqa: BLE001
        return False


def _notify_macos(title: str, content: str) -> bool:
    """macOS 通知中心（保留，便于本目录在 mac 上被测试/复用）。"""
    try:
        safe = content.replace('"', "'").replace("\\", "/")[:200]
        subprocess.run(
            ["osascript", "-e",
             'display notification "{}" with title "{}"'.format(safe, title)],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _notify_linux(title: str, content: str) -> bool:
    try:
        subprocess.run(["notify-send", title, content], capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def pid_alive(pid: int) -> bool:
    """某个 PID 的进程是否还活着。

    ★ 为什么必须收进 winenv：POSIX 上 `os.kill(pid, 0)` 是**只探测、不发信号**的
      标准写法；但**Windows 上 `os.kill` 对任何非特殊信号都会直接 TerminateProcess** ——
      照抄 POSIX 写法会真的把那个进程杀掉（如果 PID 被复用，杀的就是无辜进程）。
      所以 Windows 改用 `tasklist` 查询。

    用途：catchup.py 的排它锁判断「残留锁的持锁进程是否还在」。

    ★ 这里**故意**用模块级常量 `IS_WIN` 而不是 `platform()`（本模块其它地方一律
      要求调 `platform()`）：它是本模块唯一的例外，理由是**安全**。
      `os.kill(pid, 0)` 在本机是 Windows 时真的会 TerminateProcess 掉那个进程，
      而 `IS_WIN` 反映的是**真实操作系统**、不受任何测试桩影响。若改用 `platform()`，
      一旦有测试把平台翻成 mac/linux，Windows 上就会走到 os.kill 分支 → 误杀进程。
      换言之：这里要的是「我到底在什么系统上」，不是「测试希望我是什么系统」。
    """
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            # 取 bytes 自己解，不用 text=True（理由见 decode_console）：
            # 这个函数决定「残留锁的持锁进程是否还活着」，判错会让两个实例并发写 state。
            r = subprocess.run(["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
                               capture_output=True, timeout=10)
            return str(pid) in decode_console(r.stdout or b"")
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def native_notify(title: str, content: str, log_dir: pathlib.Path | None = None) -> bool:
    """发一条本机桌面通知。**永不抛异常**。

    投递失败时把内容追加到 `logs/notify_fallback.log` —— 宁可留下文本，也不能丢信息
    （这是「微信通道 + 本地通知」两级兜底里的最后一级）。
    """
    ok = False
    may_be_missed = False   # 这个通道「用户有可能没看到」→ 才需要进补弹队列
    try:
        plat = platform()
        if plat == "win":
            if _alert_style() == "dialog":
                # ★ 默认通道：**必须点掉才消失**的弹窗（用户 2026-09-21 要求）。
                #   它一直待在屏幕上等人处理 → 不需要、也不该再补弹一次。
                ok = _notify_windows_dialog(title, content, log_dir)
                if not ok:                      # 弹窗被拒/堆太多 → 退回常驻 Toast
                    ok = _notify_windows(title, content)
                    may_be_missed = ok
            else:
                ok = _notify_windows(title, content)
                may_be_missed = ok
        elif plat == "mac":
            ok = _notify_macos(title, content)
        else:
            ok = _notify_linux(title, content)
    except Exception:  # noqa: BLE001
        ok = False

    if not ok:
        try:
            p = (log_dir or (HERE / "runtime" / "logs")) / "notify_fallback.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            import datetime as _dt
            with p.open("a", encoding="utf-8") as f:
                f.write("[{}] {}\n{}\n{}\n".format(
                    _dt.datetime.now().strftime("%F %T"), "本地通知投递失败，原文如下：",
                    title, content))
        except Exception:  # noqa: BLE001
            pass
    elif may_be_missed:
        # 只有"可能被忽略"的通道（Toast）才进补弹队列：
        # 没人看屏幕时 Windows 只把 Toast 塞进通知中心，屏幕上不会出现横幅。
        try:
            _maybe_queue_reshow(title, content, log_dir)
        except Exception:  # noqa: BLE001
            pass
    return ok


# ---------------------------------------------------------------------------
# 「投递时用户不在场」的通知：回到机器前后补弹一次（2026-09-21，用户要求）
# ---------------------------------------------------------------------------
# 背景（实测）：Windows 在**显示器关闭 / 长时间无人操作**时不会弹横幅，通知只进
# 「通知中心」。2026-09-21 的 07:00 签到通知、08:05 领奖通知就是这样"投递成功但
# 没人看到"的 —— 光把通知设成常驻（scenario=urgent）也没用：它压根没在屏幕上出现过。
#
# 这一层的做法：
#   ① 投递成功时看一眼「距离最近一次键鼠输入多久」（idle_seconds）；
#   ② 若用户明显不在场（idle > 阈值），把这条**原文**记进补弹队列
#      （`runtime/state/notify_reshow.json`，属于运行时文件，不进交付物/不参与同步）；
#   ③ 之后每次触发（catchup 每 5 分钟一次）问一句「用户回来了吗」——
#      一回来就把积压的通知**原样再弹一遍**（依旧常驻，点叉才消失），然后出队。
#
# 这样即使机器刚睡醒/刚熄屏恢复，也不会漏看；重复弹的代价被"一条只补一次"限制住。
_RESHOW_IDLE_SEC = 300       # 投递时判定「用户不在场」的阈值（秒）
# 为什么是 5 分钟而不是更短：横幅本身已经**常驻**（点叉才消失），
# 短暂离开（< 5 分钟）回来时横幅仍在屏幕上，不需要再补弹一次；
# 会漏的是「熄屏 / 锁屏 / 睡眠」这类长离开 —— 那时横幅压根没出现过。
# 阈值越小越容易重复打扰，越大越容易漏看；5 分钟与常见熄屏超时同量级。
_RESHOW_MAX_ITEMS = 8        # 队列上限
_RESHOW_MAX_AGE_H = 72       # 超过这么久就丢弃（不翻旧账）
_RESHOW_PER_RUN = 3          # 每次最多补弹几条，别一次糊满屏幕


def idle_seconds() -> float | None:
    """距离最近一次键鼠输入过去了多少秒。非 Windows / 取不到时返回 None。

    用 `GetLastInputInfo`（系统级、会话内有效），不依赖任何第三方库。
    取不到就返回 None —— 调用方按「不知道」处理，**不猜**（猜错会导致误补弹）。
    """
    if platform() != "win":
        return None
    try:
        import ctypes

        class _LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        li = _LastInputInfo()
        li.cbSize = ctypes.sizeof(li)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        kernel32 = ctypes.windll.kernel32
        # GetTickCount 是 32 位、约 49.7 天回绕；按 2^32 取模差值即可
        kernel32.GetTickCount.restype = ctypes.c_uint32
        delta = (int(kernel32.GetTickCount()) - int(li.dwTime)) & 0xFFFFFFFF
        return delta / 1000.0
    except Exception:  # noqa: BLE001
        return None


def _reshow_file(log_dir: pathlib.Path | None = None) -> pathlib.Path:
    base = pathlib.Path(log_dir) if log_dir else (HERE / "runtime" / "logs")
    return base.parent / "state" / "notify_reshow.json"


def _reshow_enabled() -> bool:
    v = os.environ.get("WORKBUDDY_RESHOW", "").strip().lower()
    return v not in ("0", "false", "no", "off")


def _reshow_threshold() -> float:
    raw = os.environ.get("WORKBUDDY_RESHOW_IDLE_SEC", "").strip()
    if raw:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return float(_RESHOW_IDLE_SEC)


def _reshow_load(log_dir: pathlib.Path | None = None) -> list:
    try:
        data = json.loads(_reshow_file(log_dir).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    now = time.time()
    keep = []
    for it in data:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        try:
            ts = float(it.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if now - ts > _RESHOW_MAX_AGE_H * 3600:
            continue
        keep.append(it)
    return keep[-_RESHOW_MAX_ITEMS:]


def _reshow_save(items: list, log_dir: pathlib.Path | None = None) -> None:
    p = _reshow_file(log_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(list(items), ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError:
        pass


def _maybe_queue_reshow(title: str, content: str, log_dir=None, idle=None) -> bool:
    """投递时若「用户不在场」，把这条记进补弹队列。返回是否入队。

    idle 只用于自检注入（真实调用走 `idle_seconds()`）。
    """
    if not _reshow_enabled():
        return False
    idle = idle_seconds() if idle is None else idle
    if idle is None or idle <= _reshow_threshold():
        return False    # 用户在场：这条应当已经看到，不排队（避免重复打扰）
    items = _reshow_load(log_dir)
    items.append({"title": title[:120], "body": content[:600], "ts": time.time()})
    _reshow_save(items[-_RESHOW_MAX_ITEMS:], log_dir)
    return True


def reshow_pending(log_dir=None, idle=None, now=None) -> dict:
    """用户回到机器前了 → 把「投递时不在场」的通知补弹一次（常驻、点叉才消失）。

    返回 `{"resent": n, "left": m}`；用户还没回来时 `resent=0` 并附 `waiting` 说明。
    idle / now 只用于自检注入。
    """
    if not _reshow_enabled():
        return {"resent": 0, "skipped": "disabled"}
    items = _reshow_load(log_dir)
    if not items:
        return {"resent": 0}
    if idle is None:
        idle = idle_seconds()
    if idle is None or idle > _reshow_threshold():
        return {"resent": 0, "waiting": "用户还没回来（idle={}）".format(
            "unknown" if idle is None else round(float(idle)))}
    sent = 0
    rest = list(items)
    for i, it in enumerate(items[:_RESHOW_PER_RUN]):
        if not _run_toast(str(it.get("title", "")), str(it.get("body", "")), True):
            break
        sent += 1
        rest = items[i + 1:]
    _reshow_save(rest, log_dir)
    return {"resent": sent, "left": len(rest)}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _dedupe(items: list[pathlib.Path]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    seen = set()
    for p in items:
        # Windows 文件系统不区分大小写 → 去重也按小写比较
        k = str(p).lower() if platform() == "win" else str(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def first_existing(paths: list[pathlib.Path]) -> pathlib.Path | None:
    for p in paths:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def describe_paths(paths: list[pathlib.Path]) -> list[dict]:
    """给 doctor.py 用：把候选路径列表转成「路径 + 是否存在」的可打印结构。"""
    out = []
    for p in paths:
        try:
            ok = p.exists()
        except OSError:
            ok = False
        out.append({"path": str(p), "exists": ok})
    return out


# ---------------------------------------------------------------------------
# CLI（排障用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    info = {
        "platform": sys.platform,
        "is_windows": IS_WIN,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "python_for_task": default_python(windowless=True),
        "override_file": str(PATH_OVERRIDE_FILE),
        "overrides": load_overrides(),
        "client": detect_client(),
        "settings_candidates": describe_paths(settings_candidates()),
        "claw_state_dirs": describe_paths(claw_state_dirs()),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
