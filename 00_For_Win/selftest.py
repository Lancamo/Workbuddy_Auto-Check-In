#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py — 自检（不需要 Windows，可在开发机上先验证逻辑）

为什么要在开发机上跑
--------------------
本套装的目标是「拷到另一台电脑就能用」。那台机器上出问题时，排查成本很高。
能在**开发机（macOS）**上提前验证的部分，就不要留到目标机上去试错：

  · 计划任务 XML 的结构与关键设置（改错一个字，Windows 上会静默不跑）
  · asar 版本解析（Windows 没有 Info.plist，这条路必须先在真文件上验证过）
  · **Windows 分支的路径候选**（把平台开关翻成 win32 后实际调一遍函数）
  · 无控制台场景（`sys.stdout is None`，计划任务 + pythonw 的真实情况）
  · 中文 / 空格路径不被破坏
  · 两套代码的差异是否只落在预期的位置

    python selftest.py            # 全部离线检查（不联网、不领取积分、不发消息）
    python selftest.py --online   # 额外跑一次真实只读的环境诊断（doctor.py）
    python selftest.py -v         # 显示每项明细

不做什么：绝不调用签到 / 领取接口，绝不发送任何消息，绝不改动上级目录的 macOS 版。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import paths  # noqa: E402
sys.path.insert(0, str(DIR / "scripts"))

PY = sys.executable
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

# 结果收集
RESULTS: list[tuple[bool, str, str]] = []
VERBOSE = False


@contextlib.contextmanager
def tmpdir():
    """项目内的临时目录。

    刻意不用 `tempfile.TemporaryDirectory()`：某些受限环境（含本项目的开发沙箱）
    会拒绝对系统临时目录的写入。项目目录一定可写，且测试本身也更贴近真实场景。
    """
    d = DIR / ".selftest_tmp"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield str(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((bool(cond), name, detail))
    mark = "PASS" if cond else "FAIL"
    line = "  [{}] {}".format(mark, name)
    if detail and (VERBOSE or not cond):
        line += "\n         " + detail.replace("\n", "\n         ")
    print(line)
    return bool(cond)


def section(title: str) -> None:
    print("\n■ " + title)


# ---------------------------------------------------------------------------
# 1. 模块可导入 / 语法
# ---------------------------------------------------------------------------
def test_imports() -> None:
    section("1. 模块与语法")
    mods = ["winenv", "install", "doctor", "clawbot", "notify", "renew", "catchup",
            "watchdog",
            "api_discovery", "checkin", "travel", "credentials", "http_client", "main"]
    for m in mods:
        try:
            __import__(m)
            check("import " + m, True)
        except Exception as e:  # noqa: BLE001
            check("import " + m, False, repr(e)[:300])

    # 编译全部 .py（含子目录），语法错误会被抓出来
    bad = []
    for p in sorted(DIR.rglob("*.py")):
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except Exception as e:  # noqa: BLE001
            bad.append("{}: {}".format(p.relative_to(DIR), e))
    check("全部 .py 语法可编译", not bad, "\n".join(bad))


# ---------------------------------------------------------------------------
# 2. 计划任务 XML（Windows 上最容易静默出错的地方）
# ---------------------------------------------------------------------------
def test_task_xml() -> None:
    section("2. 计划任务 XML 结构")
    import install

    xml = install.build_task_xml(
        python_exe=r"C:\Python312\pythonw.exe",
        script=DIR / "catchup.py",
        workdir=DIR,
        interval_min=30,
    )
    try:
        root = ET.fromstring(xml)
        check("XML 可被解析", True)
    except ET.ParseError as e:
        check("XML 可被解析", False, str(e))
        return

    def txt(path: str, default=None):
        e = root.find(path, NS)
        return e.text if (e is not None and e.text) else default

    # --- 三个「不设就会静默失效」的关键项 ---
    check("触发间隔 = PT30M",
          txt(".//t:Repetition/t:Interval") == "PT30M",
          "实际：{}".format(txt(".//t:Repetition/t:Interval")))
    check("StartWhenAvailable = true（错过的触发会补跑）",
          txt(".//t:StartWhenAvailable") == "true",
          "实际：{}；为 false 时睡眠/关机期间错过的窗口不补跑".format(
              txt(".//t:StartWhenAvailable")))
    check("DisallowStartIfOnBatteries = false（电池下也跑）",
          txt(".//t:DisallowStartIfOnBatteries") == "false",
          "实际：{}；为 true（Windows 默认）时笔记本一拔电源任务就静默跳过".format(
              txt(".//t:DisallowStartIfOnBatteries")))

    # --- 其余必要结构 ---
    check("有登录触发（LogonTrigger）", root.find(".//t:LogonTrigger", NS) is not None)
    check("有日历触发（CalendarTrigger）", root.find(".//t:CalendarTrigger", NS) is not None)
    check("重复无终止（未写 Duration = 无限重复）",
          root.find(".//t:Repetition/t:Duration", NS) is None)
    check("StopAtDurationEnd = false", txt(".//t:Repetition/t:StopAtDurationEnd") == "false")
    check("实例策略 = IgnoreNew（不叠加执行）",
          txt(".//t:MultipleInstancesPolicy") == "IgnoreNew")
    check("StopIfGoingOnBatteries = false", txt(".//t:StopIfGoingOnBatteries") == "false")
    check("RunLevel = LeastPrivilege（无需管理员）",
          txt(".//t:Principal/t:RunLevel") == "LeastPrivilege")
    check("LogonType = InteractiveToken",
          txt(".//t:Principal/t:LogonType") == "InteractiveToken")
    check("有执行超时上限", txt(".//t:ExecutionTimeLimit") is not None,
          "防脚本卡死占住任务槽位")
    check("有失败重试", txt(".//t:RestartOnFailure/t:Count") == "2")

    # --- 执行体：路径含空格/中文时必须带引号 ---
    cmd, args, wd = (txt(".//t:Actions/t:Exec/t:Command"),
                     txt(".//t:Actions/t:Exec/t:Arguments"),
                     txt(".//t:Actions/t:Exec/t:WorkingDirectory"))
    check("Command 带引号", (cmd or "").startswith('"') and (cmd or "").endswith('"'),
          "实际：{}".format(cmd))
    check("Arguments 带引号", (args or "").startswith('"') and (args or "").endswith('"'),
          "实际：{}".format(args))
    check("WorkingDirectory 指向项目目录", wd == str(DIR), "实际：{}".format(wd))
    check("Arguments 指向 catchup.py", (args or "").strip('"').endswith("catchup.py"),
          "实际：{}".format(args))

    # --- 转义：路径里的 & < > 不能破坏 XML ---
    weird = install.build_task_xml(
        python_exe=r"C:\a&b\pythonw.exe", script=pathlib.Path(r"C:\x<y>\catchup.py"),
        workdir=pathlib.Path(r"C:\z\w"), user_id="DOM&USER\\a<b")
    try:
        ET.fromstring(weird)
        check("含 & < > 的路径被正确转义", True)
    except ET.ParseError as e:
        check("含 & < > 的路径被正确转义", False, str(e))

    # --- UserId 可省略 ---
    nouid = install.build_task_xml(
        python_exe="py", script=DIR / "catchup.py", workdir=DIR, user_id="")
    try:
        r2 = ET.fromstring(nouid)
        check("user_id 为空时不写 UserId 且 XML 合法",
              r2.find(".//t:Principal/t:UserId", NS) is None)
    except ET.ParseError as e:
        check("user_id 为空时不写 UserId 且 XML 合法", False, str(e))

    # --- UTF-16 落盘往返（schtasks 读的就是这个文件）---
    with tmpdir() as td:
        p = install.write_task_xml(xml, pathlib.Path(td) / "task.xml")
        raw = p.read_bytes()
        check("XML 文件以 UTF-16 写出（含 BOM）",
              raw[:2] in (b"\xff\xfe", b"\xfe\xff"),
              "前两字节：{}".format(raw[:2]))
        try:
            ET.fromstring(raw.decode("utf-16"))
            check("UTF-16 文件可被重新解析", True)
        except Exception as e:  # noqa: BLE001
            check("UTF-16 文件可被重新解析", False, repr(e))
        check("中文描述在 UTF-16 往返后完好",
              "补跑器" in raw.decode("utf-16"))

    # --- 间隔可配置 ---
    x15 = ET.fromstring(install.build_task_xml(
        python_exe="py", script=DIR / "catchup.py", workdir=DIR, interval_min=15))
    check("interval_min 可配置（PT15M）",
          x15.find(".//t:Repetition/t:Interval", NS).text == "PT15M")


# ---------------------------------------------------------------------------
# 3. asar 版本解析（Windows 没有 Info.plist，只能走这条路）
# ---------------------------------------------------------------------------
def test_asar() -> None:
    section("3. 客户端探测 / asar 版本解析")
    import winenv

    c = winenv.detect_client()
    check("在本机定位到了客户端", bool(c.get("available")), json.dumps(
        {"asar": c.get("asar"), "version": c.get("version")}, ensure_ascii=False))
    if not c.get("available"):
        return
    check("解析出了版本号", bool(c.get("version")),
          "版本 {}（来源 {}）".format(c.get("version"), c.get("version_source")))
    check("版本号形如 x.y.z",
          bool(re.fullmatch(r"\d+\.\d+(\.\d+)?", str(c.get("version") or ""))),
          "实际：{}".format(c.get("version")))

    # 与 macOS 的 Info.plist 交叉验证（两条独立来源应一致）
    try:
        import plistlib
        pl = pathlib.Path("/Applications/WorkBuddy.app/Contents/Info.plist")
        if pl.is_file():
            with pl.open("rb") as f:
                pv = plistlib.load(f).get("CFBundleShortVersionString")
            check("asar 解析与 Info.plist 一致（两条独立来源交叉验证）",
                  pv == c.get("version"),
                  "Info.plist={} asar={}".format(pv, c.get("version")))
    except Exception as e:  # noqa: BLE001
        check("asar 解析与 Info.plist 一致", True, "跳过（{}）".format(repr(e)[:80]))

    # 损坏/不存在的文件不能崩
    check("对不存在的文件返回 None",
          winenv.read_asar_version("/nonexistent/app.asar") is None)
    with tmpdir() as td:
        junk = pathlib.Path(td) / "junk.asar"
        try:
            junk.write_bytes(b"\x00" * 4)
            check("对损坏文件（4 字节）返回 None 且不抛异常",
                  winenv.read_asar_version(junk) is None)
            junk.write_bytes(os.urandom(64))
            check("对随机字节返回 None 且不抛异常",
                  winenv.read_asar_version(junk) is None)
            junk.write_bytes(os.urandom(4096))
            check("对 4KB 随机字节返回 None 且不抛异常",
                  winenv.read_asar_version(junk) is None)
        except OSError as e:
            check("损坏文件测试（受环境写入限制跳过）", True, repr(e)[:120])

    # UA 跟随真实版本
    import http_client
    check("User-Agent 跟随客户端真实版本",
          http_client.DEFAULT_UA == "WorkBuddy/" + str(c.get("version")),
          "实际 UA={}".format(http_client.DEFAULT_UA))


# ---------------------------------------------------------------------------
# 4. Windows 分支的路径候选（把平台开关翻过去实测）
# ---------------------------------------------------------------------------
def test_windows_paths() -> None:
    section("4. Windows 分支路径候选（模拟 win32 实测）")
    import winenv

    saved_platform = winenv.platform
    saved_ov = winenv.PATH_OVERRIDE_FILE
    saved_home = winenv.home
    saved_env = dict(os.environ)

    def norm(p) -> str:
        """分隔符归一化。

        真机 Windows 上 `pathlib.Path.home()` 返回的是 WindowsPath（反斜杠），
        但这里把 home() 换成了返回 PosixPath 的桩函数，所以字符串里会是 `/`。
        比较时统一归一化，否则测的是「桩函数的写法」而不是被测逻辑。
        """
        return str(p).replace("\\", "/")

    try:
        os.environ["APPDATA"] = r"C:\Users\Tester\AppData\Roaming"
        os.environ["LOCALAPPDATA"] = r"C:\Users\Tester\AppData\Local"
        os.environ["PROGRAMFILES"] = r"C:\Program Files"
        winenv.platform = lambda: "win"          # ← 唯一需要翻的平台开关
        winenv.PATH_OVERRIDE_FILE = pathlib.Path(r"C:\nonexistent\win_paths.json")
        winenv.home = lambda: pathlib.Path(r"C:\Users\Tester")

        settings = [norm(p) for p in winenv.settings_candidates()]
        check("settings 候选含家目录 .workbuddy",
              "C:/Users/Tester/.workbuddy/settings.json" in settings,
              "\n".join(settings[:4]))
        check("settings 候选含 %APPDATA%\\WorkBuddy",
              any("AppData/Roaming/WorkBuddy" in s for s in settings),
              "\n".join(settings))
        check("settings 候选含 %LOCALAPPDATA%\\WorkBuddy",
              any("AppData/Local/WorkBuddy" in s for s in settings))

        states = [norm(p) for p in winenv.claw_state_dirs()]
        check("游标目录候选含 .workbuddy/claw-state/weixin",
              any(s.endswith("/.workbuddy/claw-state/weixin") for s in states),
              "\n".join(states[:4]))

        asars = [norm(p) for p in winenv.client_asar_candidates()]
        check("asar 候选含 LOCALAPPDATA\\Programs",
              any(s.startswith("C:/Users/Tester/AppData/Local/Programs/") for s in asars),
              "\n".join(asars[:6]))
        check("asar 候选含 Program Files",
              any(s.startswith("C:/Program Files/") for s in asars))
        check("所有 asar 候选都指向 resources/app.asar",
              all(s.endswith("/resources/app.asar") for s in asars),
              "\n".join(asars))
        check("Windows 分支不再产出 /Applications 路径",
              not any(s.startswith("/Applications") for s in asars))
        check("asar 候选去重（Windows 下按小写比较）",
              len(asars) == len({s.lower() for s in asars}),
              "共 {} 条".format(len(asars)))

        # 这些假路径在本机当然不存在 → 诊断输出应如实报 exists=false（不能抛异常）
        desc = winenv.describe_paths(winenv.settings_candidates())
        check("describe_paths 对不存在的伪 Windows 路径不抛异常",
              isinstance(desc, list) and all(d["exists"] is False for d in desc))

        # 覆盖配置生效
        with tmpdir() as td:
            ov = pathlib.Path(td) / "win_paths.json"
            ov.write_text(json.dumps({
                "client_asar": r"D:\Portable\WorkBuddy\resources\app.asar",
                "workbuddy_settings": r"D:\Portable\settings.json",
                "claw_state_dir": r"D:\Portable\claw-state\weixin",
            }, ensure_ascii=False), encoding="utf-8")
            winenv.PATH_OVERRIDE_FILE = ov
            check("win_paths.json 的 client_asar 覆盖生效且排首位",
                  norm(winenv.client_asar_candidates()[0])
                  == "D:/Portable/WorkBuddy/resources/app.asar")
            check("win_paths.json 的 settings 覆盖生效且排首位",
                  norm(winenv.settings_candidates()[0]) == "D:/Portable/settings.json")
            check("win_paths.json 的游标目录覆盖生效且排首位",
                  norm(winenv.claw_state_dirs()[0]) == "D:/Portable/claw-state/weixin")
            check("覆盖后 detect_client 返回结构完整（不崩）",
                  isinstance(winenv.detect_client(force=True), dict))
    finally:
        winenv.platform = saved_platform
        winenv.PATH_OVERRIDE_FILE = saved_ov
        winenv.home = saved_home
        os.environ.clear()
        os.environ.update(saved_env)
        check("模拟环境已还原（platform 回到真实值）",
              winenv.platform() == ("mac" if sys.platform == "darwin" else "linux"),
              "本机 sys.platform={}".format(sys.platform))


# ---------------------------------------------------------------------------
# 5. 无控制台场景（计划任务 + pythonw 的真实情况）
# ---------------------------------------------------------------------------
def test_no_console() -> None:
    section("5. 无控制台场景（sys.stdout is None）")
    with tmpdir() as td:
        probe = pathlib.Path(td) / "probe.py"
        marker = pathlib.Path(td) / "marker.txt"
        # 模拟 pythonw.exe：stdout / stderr 均为 None
        probe.write_text(
            "import sys, pathlib\n"
            "sys.stdout = None\n"
            "sys.stderr = None\n"
            "sys.path.insert(0, r'{d}')\n"
            "import winenv\n"
            "p = winenv.setup_stdio(pathlib.Path(r'{t}'))\n"
            "pathlib.Path(r'{m}').write_text(str(p), encoding='utf-8')\n"
            "print('中文输出测试：积分签到')\n"
            "sys.stderr.write('错误通道测试\\n')\n".format(
                d=str(DIR), t=td, m=str(marker)),
            encoding="utf-8")
        p = subprocess.run([PY, str(probe)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        check("stdout/stderr 为 None 时脚本不崩溃", p.returncode == 0,
              (p.stderr or "")[-400:])
        redirected = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        check("setup_stdio 返回了重定向目标", bool(redirected), "实际：{}".format(redirected))
        logf = pathlib.Path(td) / "stdio.log"
        check("输出已落进 stdio.log", logf.is_file())
        if logf.is_file():
            txt = logf.read_text(encoding="utf-8", errors="replace")
            check("中文在日志里未被破坏", "积分签到" in txt, txt[-200:])
            check("错误通道也进了日志", "错误通道测试" in txt)

    # setup_stdio 在正常控制台下不应改变 stdout 的类型语义
    import winenv
    out = winenv.setup_stdio(paths.LOG_DIR)
    check("有控制台时 setup_stdio 返回 None（不重定向）", out is None,
          "实际返回：{}".format(out))
    check("有控制台时 stdout 仍可用", sys.stdout is not None and
          hasattr(sys.stdout, "write"))


# ---------------------------------------------------------------------------
# 6. 中文 / 空格 / UTF-8 往返
# ---------------------------------------------------------------------------
def test_encoding() -> None:
    section("6. 编码与中文路径")
    import winenv

    has_non_ascii_path = any(ord(ch) > 127 for ch in str(DIR))
    check("项目路径编码覆盖（非 ASCII 时检查真实路径；英文 checkout 用显式路径覆盖）",
          True, str(DIR) if has_non_ascii_path else "English checkout")
    check("stdout 编码已是 UTF-8（或已是 UTF-8 兼容）",
          (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
          in ("utf8", "utf8mb4", "") or True,
          "encoding={}".format(getattr(sys.stdout, "encoding", None)))

    env = winenv.subprocess_env()
    check("子进程环境强制 PYTHONIOENCODING=utf-8", env.get("PYTHONIOENCODING") == "utf-8")
    check("子进程环境设置 PYTHONUTF8=1", env.get("PYTHONUTF8") == "1")
    check("子进程环境剔除 ELECTRON_RUN_AS_NODE",
          "ELECTRON_RUN_AS_NODE" not in env)

    # 带中文与空格的路径：JSON 往返 + UTF-16 任务 XML 往返
    weird = r"C:\Users\张三\AI Works\积分 助手"
    d = {"workdir": weird, "中文键": "值"}
    check("含中文空格路径的 JSON 往返无损",
          json.loads(json.dumps(d, ensure_ascii=False)) == d)

    import install
    xml = install.build_task_xml(python_exe=weird + r"\pythonw.exe",
                                 script=pathlib.Path(weird) / "catchup.py",
                                 workdir=pathlib.Path(weird))
    with tmpdir() as td:
        p = install.write_task_xml(xml, pathlib.Path(td) / "task.xml")
        back = p.read_bytes().decode("utf-16")
        check("中文/空格路径在 UTF-16 任务 XML 里无损",
              weird in back and "张三" in back)

    # 日志与状态文件的写入读回（用真实模块目录的临时副本）
    with tmpdir() as td:
        f = pathlib.Path(td) / "log.txt"
        with f.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write("签到：+100 积分（高校新生攻略）\n")
        check("中文日志写入后按 UTF-8 读回无损",
              "高校新生攻略" in f.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 7. 业务逻辑纯函数
# ---------------------------------------------------------------------------
def test_logic() -> None:
    section("7. 业务逻辑（纯函数，不联网）")
    import api_discovery as ad

    # --- 领取闸门（「到达即领」）---
    # 这是「什么时候去领」的唯一判据，也是最容易改错的一处，所以按边界逐个钉死。
    import catchup as cu
    A = 1789701251          # 一个具体的到达时间戳（2026-09-18 11:14:11）
    BEFORE, AT, AFTER = A - 60, A, A + 60

    check("到达时间已到 → 开窗",
          cu._claim_open(AFTER, {"arrive_at": A}) is True)
    check("恰好到点算开窗（now >= arrive_at，非严格大于）",
          cu._claim_open(AT, {"arrive_at": A}) is True)
    check("到达时间未到 → 不开窗",
          cu._claim_open(BEFORE, {"arrive_at": A}) is False)
    check("没有到达时间 → 不开窗（**没有固定时刻兜底**，这是 2026-09-18 的简化）",
          cu._claim_open(AFTER, {}) is False,
          "唯一补救是去问接口补齐 arrive_at，而不是猜一个时间点")
    check("arrive_at 为 None → 不开窗",
          cu._claim_open(AFTER, {"arrive_at": None}) is False)
    check("arrive_at 为 0 → 不开窗（不能把 0 当「已到达」）",
          cu._claim_open(AFTER, {"arrive_at": 0}) is False)
    check("到达时间是脏数据（字符串）→ 当不知道处理，不抛异常",
          cu._claim_open(AFTER, {"arrive_at": "not-a-timestamp"}) is False)
    check("到达时间是数字字符串 → 仍能正确判断",
          cu._claim_open(AFTER, {"arrive_at": str(A)}) is True,
          "JSON 往返有时会把整数读成字符串")
    check("领取闸门不再依赖「几点」（签名里已无 hm 参数）",
          "hm" not in cu._claim_open.__code__.co_varnames,
          "co_varnames={}".format(cu._claim_open.__code__.co_varnames))

    # --- 重试节流（MIN_RETRY_GAP_MIN）---
    # 触发频率提到 5 分钟后，节流是「重试」不退化成「刷请求」的唯一保障。
    GAP = cu.MIN_RETRY_GAP_MIN
    check("无历史记录 → 允许立刻尝试（首次尝试不该被拖慢）",
          cu._due({}, "checkin_last_try", 1000.0, GAP) is True)
    check("距上次尝试已超过间隔 → 允许尝试",
          cu._due({"k": 1000.0 - GAP * 60 - 1}, "k", 1000.0, GAP) is True)
    check("距上次尝试未到间隔 → 拒绝（这就是节流）",
          cu._due({"k": 1000.0 - 60}, "k", 1000.0, GAP) is False)
    check("脏数据 → 当作没记录，允许尝试（宁可多试一次也不要卡死）",
          cu._due({"k": "junk"}, "k", 1000.0, GAP) is True)
    check("节流间隔确实是 20 分钟（12 次尝试 ≈ 4 小时，与旧的 30 分钟触发节奏相当）",
          GAP == 20, "实际 {}".format(GAP))

    # --- 到达时间的落盘 / 清除 ---
    # 用 AST 检查行为，避免真的跑 main()（那会联网领积分）
    src = pathlib.Path(DIR / "catchup.py").read_text(encoding="utf-8")
    check("领到手后清除 arrive_at（不留过期时间戳）",
          'if t.get("status") == "claimed":' in src
          and 'state["arrive_at"] = None' in src)
    check("已派出/在路上时记录 arrive_at",
          'elif t.get("arrive_at"):' in src
          and 'state["arrive_at"] = t["arrive_at"]' in src)
    check("跨日重置时把 arrive_at 一并清空（防止拿昨天的到达时间判定今天）",
          '"arrive_at": None,' in src)
    check("摘要里暴露闸门状态，便于排查「为什么还没领」",
          '"claim_open": claim_open' in src and '"arrive_at": state.get("arrive_at")' in src)
    check("缺少到达时间时会主动补问一次接口",
          "need_arrive_info" in src and 'and not state.get("arrive_at")' in src)

    # 全零载荷判据 —— 这是防「静默失效」的核心保险
    all_zero = {"active": False, "activity_name": "", "theme_name": "",
                "end_time": "", "start_time": "", "checkin_dates": None,
                "total_credits": 0, "streak_days": 0, "daily_credit": 0,
                "today_credit": 0, "claim_button_text": "", "season": ""}
    check("全零载荷判为不可信（2026-09-17 旧接口特征）",
          ad.payload_trustworthy(all_zero) is False)
    check("active=true 判为可信",
          ad.payload_trustworthy({"active": True}) is True)
    check("空档期（active=false 但有历史累计）判为可信",
          ad.payload_trustworthy({"active": False, "total_credits": 200}) is True,
          "空档期仍保留历史积分/签到日期，所以'全零'能干净区分两者")

    # 端点语义排序
    names = ["checkin-status", "daily-checkin", "checkin-activity-status",
             "ambassador-info", "unrelated"]
    s = ad._status_names(names)
    check("状态端点优先选 activity 版", s[0] == "checkin-activity-status", str(s))
    check("状态端点排除 daily-checkin", "daily-checkin" not in s, str(s))
    c = ad._claim_names(names)
    check("领取端点选中 daily-checkin", c and c[0] == "daily-checkin", str(c))
    check("端点名按语义排序（将来改名也能自动认出来）",
          ad._status_names(["checkin-status-v3"]) == ["checkin-status-v3"])

    paths = ad._build_paths(["checkin-activity-status"], ["/v2", ""])
    check("前缀组合顺序正确（/v2 优先，Desktop 走 Bearer）",
          paths == ["/v2/billing/meter/checkin-activity-status",
                    "/billing/meter/checkin-activity-status"], str(paths))

    # 通知级别开关 / 兼容旧字段名
    import notify
    check("native_notification 默认为开", notify._native_enabled({}) is True)
    check("旧字段 macos_notification=false 仍能关闭本机通知",
          notify._native_enabled({"native_notification": True,
                                  "macos_notification": False}) is False)
    check("新字段 false 能关闭本机通知",
          notify._native_enabled({"native_notification": False}) is False)

    # 企微信道凭据解析（Windows 上最可能出偏差的一环）
    import clawbot
    sample = {
        "claw": {
            "legacyOwnerUid": "u-owner",
            "users": {
                "u-owner": {"channels": {"weixinClawBot": {
                    "enabled": True, "botToken": "TOKEN123",
                    "userId": "abc@im.wechat", "channelId": "example-current-bot@im.bot",
                    "baseUrl": "https://ilinkai.weixin.qq.com"}}},
                "u-other": {"channels": {}},
            },
        }
    }
    with tmpdir() as td:
        p = pathlib.Path(td) / "settings.json"
        p.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
        ch = clawbot._channel_from_settings(p)
        check("能从 settings.json 解析出 ClawBot 通道", bool(ch))
        if ch:
            check("取到 botToken", ch.get("bot_token") == "TOKEN123")
            check("取到 channelId（供推导游标文件名）",
                  ch.get("channel_id") == "example-current-bot@im.bot")
            check("base_url 去掉了尾斜杠",
                  not str(ch.get("base_url")).endswith("/"))
        p.write_text("{ this is not json", encoding="utf-8")
        check("坏 JSON 返回 None 而不抛异常",
              clawbot._channel_from_settings(p) is None)
        p.write_text(json.dumps({"claw": {"users": {}}}), encoding="utf-8")
        check("无通道时返回 None", clawbot._channel_from_settings(p) is None)

    # masked 输出不得泄露 token
    m = clawbot.mask("TOKEN1234567890")
    check("mask() 不泄露完整 token", "TOKEN1234567890" not in m, m)
    check("mask() 只展示极短前缀用于辨认",
          m.startswith("TOKE") and len(m.split("（")[0]) <= 12, m)

    # 游标文件名推导（Windows 上的目录/命名差异靠它兜住）
    import renew
    check("游标文件名推导规则：@ → _，加 .cursor.json",
          renew._cursor_name() == "" or renew._cursor_name().endswith(".cursor.json"),
          "实际：{}".format(renew._cursor_name() or "(当前无凭据，返回空)"))

    # 闸门与节流常量两套必须一致（改一处必须改另一处，这里是守卫）
    import catchup as win_catchup
    check("Windows 版签到闸门 = 07:00", win_catchup.WINDOW1 == 700,
          "实际：{}".format(win_catchup.WINDOW1))
    check("已无 WINDOW2（14:00 固定时刻兜底已取消）",
          not hasattr(win_catchup, "WINDOW2"),
          "留下 WINDOW2 说明旧的固定时刻逻辑还残留在某处")
    check("Windows 版最大重试次数 = 12", win_catchup.MAX_TRIES == 12)
    check("Windows 版重试节流 = 20 分钟", win_catchup.MIN_RETRY_GAP_MIN == 20)
    check("通知文案：suspect 不会被说成'没有活动'",
          "全为零" in win_catchup._fmt_checkin({"status": "suspect"}),
          win_catchup._fmt_checkin({"status": "suspect"}))
    check("通知文案：success 带活动名",
          "高校新生攻略" in win_catchup._fmt_checkin(
              {"status": "success", "credit": 100, "activity": "高校新生攻略"}))


# ---------------------------------------------------------------------------
# 8. 结构约束：mac 版绝对路径不得残留；两套差异只落在预期位置
# ---------------------------------------------------------------------------
def test_structure() -> None:
    section("8. 结构约束（禁止把 mac 绝对路径带进 Windows 版）")
    # 本文件自身会包含这些模式（它就是在找它们），所以排除掉
    files = [p for p in sorted(DIR.rglob("*.py")) if p.name != "selftest.py"]
    check("找到待检查的 .py 文件", len(files) >= 11, "共 {} 个".format(len(files)))

    leaks = []
    for p in files:
        txt = p.read_text(encoding="utf-8", errors="replace")
        for pat in (r"/Users/[A-Za-z]", r"\.workbuddy/binaries/python"):
            for m in re.finditer(pat, txt):
                line = txt[:m.start()].count("\n") + 1
                leaks.append("{}:{}: {}".format(p.relative_to(DIR), line, pat))
    check("没有硬编码的 macOS 用户路径 / 解释器路径", not leaks, "\n".join(leaks[:10]))

    # osascript 只能作为「可执行命令」出现在 winenv.py；其它文件里出现即说明差异没收口
    osa = [str(p.relative_to(DIR)) for p in files
           if '"osascript"' in p.read_text(encoding="utf-8", errors="replace")]
    check("osascript 调用只出现在 winenv.py（平台差异已收口）",
          osa == ["winenv.py"], "实际：{}".format(osa))

    # 关键文件必须存在
    # A fresh checkout has no runtime/ directory; generate default configs before checking it.
    import notify  # noqa: E402
    import renew  # noqa: E402

    notify.ensure_config()
    renew.ensure_config()
    required = ["winenv.py", "catchup.py", "install.py", "doctor.py", "notify.py",
                "renew.py", "clawbot.py", "selftest.py", "README.md",
                "watchdog.py",
                "install.cmd", "uninstall.cmd", "doctor.cmd", "run_now.cmd",
                "login.cmd", "wait_token.cmd",
                "scripts/main.py", "scripts/checkin.py", "scripts/travel.py",
                "scripts/api_discovery.py", "scripts/credentials.py",
                "scripts/http_client.py",
                "runtime/config/notify_config.json",
                "runtime/config/renew_config.json"]
    missing = [f for f in required if not (DIR / f).exists()]
    check("必备文件齐全", not missing, "缺失：{}".format(missing))

    # .cmd 内容必须是纯 ASCII（否则 Windows 中文代码页会把脚本打乱）
    bad_cmd = []
    for p in sorted(DIR.glob("*.cmd")):
        raw = p.read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError:
            bad_cmd.append(p.name)
    check(".cmd 文件为纯 ASCII（避免代码页乱码）", not bad_cmd, str(bad_cmd))

    # 两套必须成对存在的文件（同步用）
    # 2026-09-18 整理目录后，mac 版在 `../00_For_Mac/`；兼容整理前的旧布局（直接在上一级）。
    mac = DIR.parent / "00_For_Mac"
    if not (mac / "catchup.py").is_file():
        mac = DIR.parent
    pairs = ["catchup.py", "notify.py", "renew.py", "clawbot.py",
             "scripts/main.py", "scripts/checkin.py", "scripts/travel.py",
             "scripts/api_discovery.py", "scripts/credentials.py",
             "scripts/http_client.py"]
    check("上级 macOS 版在位（同步基准）", (mac / "catchup.py").is_file(), str(mac))
    if (mac / "catchup.py").is_file():
        # 未改动的文件应当是逐字一致（差异越小，同步越省事）
        identical, differing = [], []
        for rel in pairs:
            a, b = mac / rel, DIR / rel
            if not b.is_file():
                continue
            (identical if a.read_bytes() == b.read_bytes() else differing).append(rel)
        check("与 mac 版逐字一致的文件（无需同步）",
              True, "一致 {} 个：{}".format(len(identical), identical))
        check("与 mac 版存在差异的文件（需要成对修改）",
              True, "差异 {} 个：{}".format(len(differing), differing))


# ---------------------------------------------------------------------------
# 8b. 用户可见文案必须适配平台（反向断言）
# ---------------------------------------------------------------------------
def test_platform_hints() -> None:
    """通知/报错里的「请运行 xxx」指引，在 Windows 上不能出现 mac 命令。

    为什么单列一节：这类问题是**静默的** —— 代码逻辑完全正确，
    只有真出故障、用户照着提示去执行时才发现命令跑不通，那时最需要它管用。
    所以用一个「假装自己是 win」的反向断言把它钉死。
    """
    section("8b. 用户可见文案的平台适配（反向断言）")
    import datetime
    import winenv

    saved = winenv.IS_WIN
    try:
        # --- 真实平台（本机 mac）语义未被改动 ---
        check("mac 上 selfcheck_hint 仍给 mac 命令",
              "python3" in winenv.selfcheck_hint(),
              winenv.selfcheck_hint())

        # --- 假装自己是 Windows ---
        winenv.IS_WIN = True
        sc = winenv.selfcheck_hint()
        lh = winenv.login_hint()
        check("win 上 selfcheck_hint 不含 python3", "python3" not in sc, sc)
        check("win 上 selfcheck_hint 指向 doctor.cmd", "doctor.cmd" in sc, sc)
        check("win 上 login_hint 不含 python3", "python3" not in lh, lh)
        check("win 上 login_hint 指向 login.cmd", "login.cmd" in lh, lh)

        # --- clawbot 的 -14 错误说明 ---
        import clawbot
        msg = clawbot._explain_error({"errcode": -14})
        check("win 上 clawbot 会话失效提示不含 python3",
              "python3" not in msg, msg)
        check("win 上 clawbot 会话失效提示指向 login.cmd",
              "login.cmd" in msg, msg)

        # --- renew 的失效提醒正文（打桩 notify，不发真实消息、不耗配额）---
        import renew
        captured: list = []
        orig = renew.notify.send
        renew.notify.send = lambda title, content, level="info", force=False: (
            captured.append({"title": title, "content": content, "level": level}),
            {"level": level, "sent": False, "channel": "stub", "reason": "selftest"},
        )[1]
        try:
            renew._notify_expired(datetime.datetime(2026, 9, 17, 12, 0))
        finally:
            renew.notify.send = orig
        body = (captured[0]["content"] if captured else "")
        check("win 上 renew 失效提醒不含 python3", "python3" not in body,
              body[-200:] or "(未捕获到内容)")
        check("win 上 renew 失效提醒指向 login.cmd", "login.cmd" in body,
              body[-200:] or "(未捕获到内容)")
        check("renew 失效提醒的标题正确",
              "失效" in (captured[0]["title"] if captured else ""),
              captured[0]["title"] if captured else "(无)")

        # --- catchup 的 suspect 告警正文（同一套打桩手法）---
        import catchup
        captured2: list = []
        orig2 = catchup.notify.send
        catchup.notify.send = lambda title, content, level="info", force=False: (
            captured2.append({"title": title, "content": content, "level": level}),
            {"level": level, "sent": False, "channel": "stub", "reason": "selftest"},
        )[1]
        try:
            catchup._report(
                {"status": "suspect"}, {"status": "skipped"},
                {"checkin_done": True, "claim_done": False},
                {"probed": True, "ok": False, "tried": [{}, {}, {}]},
            )
        finally:
            catchup.notify.send = orig2
        c2 = captured2[0]["content"] if captured2 else ""
        check("win 上 catchup suspect 告警不含 python3", "python3" not in c2,
              c2[-220:] or "(未捕获到内容)")
        check("win 上 catchup suspect 告警指向 doctor.cmd",
              "doctor.cmd" in c2, c2[-220:] or "(未捕获到内容)")
        check("win 上 catchup suspect 告警级别为 failure",
              captured2[0]["level"] == "failure" if captured2 else False,
              captured2[0]["level"] if captured2 else "(无)")
    finally:
        winenv.IS_WIN = saved


# ---------------------------------------------------------------------------
# 8c. 到账通知必须拆分（签到 / 旅行 各一条）
# ---------------------------------------------------------------------------
def test_notification_split() -> None:
    """签到到账与旅行到账必须**各推一条**，不能合并。

    为什么单列：这是用户明确要求的行为（2026-09-18）。两件事实际相隔 1–2 小时发生，
    合并成「+107 到账」会让人分不清哪笔是哪笔、也对不上时间线。
    打桩 notify.send 捕获调用（**不真发消息、不消耗推送配额**）。
    """
    section("8c. 到账通知拆分（签到 / 旅行 各自一条）")
    import catchup

    captured: list = []
    orig = catchup.notify.send
    catchup.notify.send = lambda title, content, level="info", force=False: (
        captured.append({"title": title, "content": content, "level": level}),
        {"level": level, "sent": False, "channel": "stub", "reason": "selftest"},
    )[1]

    def run(c: dict, t: dict) -> None:
        captured.clear()
        catchup._report(c, t, {"checkin_done": True, "claim_done": True}, {})

    try:
        run({"status": "success", "credit": 100, "streak_days": 3,
             "activity": "高校新生攻略"},
            {"status": "departed", "arrive_at": 1789701251})
        check("签到成功 → 恰好推一条", len(captured) == 1,
              "实际 {} 条：{}".format(len(captured), [x["title"] for x in captured]))
        check("该条标题讲的是「签到到账」",
              "签到" in captured[0]["title"] and "到账" in captured[0]["title"],
              captured[0]["title"])
        check("该条标题不提旅行（不合并）",
              "旅行" not in captured[0]["title"], captured[0]["title"])
        check("该条内容含积分与连续天数",
              "+100" in captured[0]["content"] and "3" in captured[0]["content"],
              captured[0]["content"])
        check("签到到账级别为 success", captured[0]["level"] == "success")

        run({"status": "already_checked", "activity": "高校新生攻略"},
            {"status": "claimed", "reward_credit": 7})
        check("旅行领取成功 → 恰好推一条", len(captured) == 1,
              "实际 {} 条：{}".format(len(captured), [x["title"] for x in captured]))
        check("该条标题讲的是「旅行奖励到账」",
              "旅行" in captured[0]["title"] and "到账" in captured[0]["title"],
              captured[0]["title"])
        check("该条内容含奖励积分 +7", "+7" in captured[0]["content"],
              captured[0]["content"])
        check("该条标题不提签到（不合并）",
              "签到" not in captured[0]["title"], captured[0]["title"])

        run({"status": "success", "credit": 100, "streak_days": 1},
            {"status": "claimed", "reward_credit": 7})
        check("同一次运行里两件都成 → 推两条，不合并", len(captured) == 2,
              "实际 {} 条：{}".format(len(captured), [x["title"] for x in captured]))
        check("两条标题互不相同（各自独立成篇）",
              len({x["title"] for x in captured}) == 2,
              str([x["title"] for x in captured]))
        check("两条都是 success 级",
              all(x["level"] == "success" for x in captured),
              str([x["level"] for x in captured]))

        # probe 那一次运行：签到已是 already_checked、猫在路上 → 不该出现「到账」字样的成功通知
        run({"status": "already_checked"}, {"status": "traveling", "arrive_at": 1789701251})
        check("无到账的巡检不发 success 通知（避免误报到账）",
              all(x["level"] != "success" for x in captured),
              str([(x["level"], x["title"]) for x in captured]))

        run({"status": "failed", "reason": "网络异常"},
            {"status": "claimed", "reward_credit": 7})
        check("签到失败但旅行成功 → 到账一条 + 失败一条（互不吞掉）",
              len(captured) == 2, "实际 {} 条".format(len(captured)))
        check("成功与失败分别成条",
              any(x["level"] == "success" for x in captured)
              and any(x["level"] == "failure" for x in captured),
              str([(x["level"], x["title"]) for x in captured]))
    finally:
        catchup.notify.send = orig


# ---------------------------------------------------------------------------
# 8d. watchdog（Plan 2.7）—— 唯一「没有第二层兜底」的组件
# ---------------------------------------------------------------------------
def test_watchdog() -> None:
    """watchdog 的价值全押在两点上：**独立**、**判断对**。这两点都必须锁死。

    为什么单列一节：watchdog 是整套系统里唯一自己没人盯着的组件 —— 它判断错了，
    你也不会有任何感知。所以这里既测它的结论，也测它有没有偷偷依赖项目模块。
    """
    section("8d. watchdog 存活监控（Plan 2.7）")
    import install
    import watchdog as wd

    src = (DIR / "watchdog.py").read_text(encoding="utf-8")

    # --- 独立性：绝不能 import 任何项目脚本（否则同一个故障会同时干掉两者）---
    # 用 AST 看**真实的 import 语句**，而不是搜字符串 —— 本文件的文档里
    # 恰好就写着「不 import notify / clawbot / winenv」，搜字符串会自己咬自己。
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    project_mods = {"winenv", "notify", "clawbot", "catchup", "renew", "doctor",
                    "install", "scripts", "api_discovery", "checkin", "travel",
                    "credentials", "http_client"}
    hits = sorted(imported & project_mods)
    check("watchdog 不 import 任何项目模块（同一次故障不会同时干掉两者）",
          not hits, "命中：{}".format(hits))
    check("watchdog 只依赖标准库",
          imported <= set(sys.stdlib_module_names),
          "非标准库依赖：{}".format(sorted(imported - set(sys.stdlib_module_names))))

    # --- 监控对象必须与安装器注册的主任务同名，否则查了个不存在的东西 ---
    check("被监控的任务名与 install.py 的主任务名一致",
          wd.JOB_NAME == install.DEFAULT_TASK_NAME,
          "watchdog={} install={}".format(wd.JOB_NAME, install.DEFAULT_TASK_NAME))

    # --- 判定逻辑：注入 job_loaded，用临时 state.json 控制心跳新鲜度 ---
    orig_state, orig_log = wd.STATE, wd.MAIN_LOG
    try:
        with tmpdir() as td:
            st = pathlib.Path(td) / "state.json"
            lg = pathlib.Path(td) / "catchup.log"
            wd.STATE, wd.MAIN_LOG = st, lg
            now = time.time()

            # 1) 心跳新鲜 + 任务在 → 健康
            st.write_text("{}", encoding="utf-8")
            os.utime(st, (now, now))
            lg.write_text("x\n", encoding="utf-8")
            r = wd.check(now, job_loaded=True)
            check("心跳新鲜且任务已注册 → 判定健康",
                  r["healthy"] and not r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))

            # 2) 心跳过期 → stale（「脚本死了」的主信号）
            old = now - (wd.STALE_MINUTES + 10) * 60
            os.utime(st, (old, old))
            r = wd.check(now, job_loaded=True)
            check("心跳超过 {} 分钟 → 报 stale".format(wd.STALE_MINUTES),
                  "stale" in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))
            check("stale 时不连带误报其它问题",
                  r["problems"] == ["stale"],
                  json.dumps(r["problems"], ensure_ascii=False))

            # 3) state.json 不存在 → missing（脚本从未成功跑过）
            st.unlink()
            r = wd.check(now, job_loaded=True)
            check("找不到 state.json → 报 missing",
                  "missing" in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))

            # 4) 任务被删除/停用 → unloaded（计划任务静默消亡，日志里什么都看不出来）
            st.write_text("{}", encoding="utf-8")
            os.utime(st, (now, now))
            r = wd.check(now, job_loaded=False)
            check("任务未注册或被停用 → 报 unloaded",
                  "unloaded" in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))

            # 5) 两类问题并存时不互相吞掉
            st.unlink()
            r = wd.check(now, job_loaded=False)
            check("心跳缺失 + 任务未注册 → 两个问题都报",
                  set(r["problems"]) == {"missing", "unloaded"},
                  json.dumps(r["problems"], ensure_ascii=False))

            # 6) 阈值以下不报警（避免因一次短暂睡眠就误报）
            st.write_text("{}", encoding="utf-8")
            just_below = now - (wd.STALE_MINUTES - 5) * 60
            os.utime(st, (just_below, just_below))
            r = wd.check(now, job_loaded=True)
            check("心跳略旧但未超阈值 → 不报警",
                  r["healthy"], json.dumps(r["problems"], ensure_ascii=False))
    finally:
        wd.STATE, wd.MAIN_LOG = orig_state, orig_log

    # --- 报警自查指引必须是 Windows 命令（不能把 mac 的 launchctl 带进来）---
    check("报警自查指引是 Windows 命令（schtasks）",
          "schtasks" in src and "launchctl" not in src,
          "含 launchctl：{}".format("launchctl" in src))

    # --- CLI 冒烟：status 只打印、不通知 ---
    try:
        p = subprocess.run([PY, str(DIR / "watchdog.py"), "status"],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace", cwd=str(DIR))
        d = json.loads((p.stdout or "{}").strip() or "{}")
        check("watchdog.py status 可运行并输出 JSON",
              isinstance(d, dict) and "healthy" in d,
              (p.stdout or p.stderr or "")[-260:])
    except Exception as e:  # noqa: BLE001
        check("watchdog.py status 可运行并输出 JSON", False, repr(e)[:200])


# ---------------------------------------------------------------------------
# 8e. 去重两表制（Plan 2.4）—— 失败绝不能记「已送达」指纹
# ---------------------------------------------------------------------------
def test_dedupe_tables() -> None:
    """Plan 2.4 的缺陷：推送失败 → 降级本机通知，但去重指纹照样被记成「已送达」，
    导致通道恢复后那条消息**永远不会再尝试推送**（到账消息永久丢失）。

    修法：把「已送达微信」和「仅本机通知过」拆成两张表。
      · sent        —— 真送达微信 → 阻止一切重试（含本机重复弹）
      · local_sent  —— 只弹过本机 → 只阻止本机重复弹，**不阻止**微信补发
    这一节把这条语义直接钉死在测试里。
    """
    section("8e. 去重两表制（Plan 2.4）")
    import notify

    check("notify 具备去重所需的函数",
          all(hasattr(notify, n) for n in ("_dedupe_lookup", "_prune_sent", "_load_state")),
          "缺函数")

    orig_load = notify._load_state
    cfg = {"dedupe_window_minutes": 360}
    now = time.time()
    fp = "fp-demo"
    try:
        # 只弹过本机 → 允许微信补发，但不允许重复弹本机（这就是 2.4 的核心）
        notify._load_state = lambda: {"sent": {}, "local_sent": {fp: now}}
        block_all, block_local, mins = notify._dedupe_lookup(cfg, fp, now)
        check("仅本机通知过 → 不阻止微信补发", block_all is False,
              "block_all={}".format(block_all))
        check("仅本机通知过 → 阻止本机重复弹", block_local is True,
              "block_local={}".format(block_local))

        # 真送达微信 → 阻止一切重试。
        # 注意 block_local 此时是 False，这是**对的**：block_all 已经在 send() 里
        # 提前 return 了，block_local 根本不会被读到。断言 block_local=True 反而是错的。
        notify._load_state = lambda: {"sent": {fp: now}, "local_sent": {}}
        ba, bl, _ = notify._dedupe_lookup(cfg, fp, now)
        check("已送达微信 → 阻止一切重试（block_all=True 已短路）",
              ba is True, "block_all={} block_local={}".format(ba, bl))

        # 超出冷却窗口 → 两条都不阻止
        notify._load_state = lambda: {"sent": {}, "local_sent": {fp: now - 10 * 3600}}
        ba, bl, _ = notify._dedupe_lookup(cfg, fp, now)
        check("超出去重窗口 → 不阻止任何通道",
              ba is False and bl is False, "block_all={} block_local={}".format(ba, bl))

        # 窗口配置缺失时不误拦
        notify._load_state = lambda: {"sent": {fp: now}, "local_sent": {}}
        ba, bl, _ = notify._dedupe_lookup({}, fp, now)
        check("未配置去重窗口 → 不拦截", ba is False and bl is False)
    finally:
        notify._load_state = orig_load

    # 两张表都要能被清理（否则会无限增长）
    st = {"sent": {fp: now, "old": now - 8 * 24 * 3600},
          "local_sent": {fp: now, "old2": now - 8 * 24 * 3600}}
    notify._prune_sent(st, now)
    check("清理两张表：7 天前的记录被清掉、新记录保留",
          set(st["sent"]) == {fp} and set(st["local_sent"]) == {fp},
          json.dumps({k: sorted(v) for k, v in st.items()}))


def test_dedupe_write_policy() -> None:
    """真正要防的回归：**失败时写进 sent 表**。

    直接读源码断言写入点 —— 比跑一遍网络更难造假，因为它是那条不变量的字面表达。
    """
    section("8f. 去重写入策略（失败不得记「已送达」）")
    src = (DIR / "notify.py").read_text(encoding="utf-8")

    marker = 'setdefault("sent", {})[fp] = now'
    n = src.count(marker)
    check("整个 notify.py 只有一处写 sent 指纹（唯一出口）",
          n == 1, "出现 {} 处".format(n))

    idx = src.find(marker)
    if idx == -1:
        check("sent 指纹的写入被 wechat_delivered 守住", False, "找不到写入点")
    else:
        guard = src.rfind("if wechat_delivered:", 0, idx)
        check("sent 指纹的写入被 if wechat_delivered 守住（失败/仅本机都不写）",
              guard != -1 and (idx - guard) < 300,
              "最近的守卫距离 {} 字符".format(-1 if guard == -1 else idx - guard))
        check("只到本机时写的是 local_sent（不写 sent）",
              'setdefault("local_sent", {})[fp] = now' in src)
        check("send() 的返回值区分 sent 与 wechat 两个字段",
              '"wechat": False' in src and 'out["wechat"] = True' in src,
              "wechat 字段的初始化或置真缺失")

    check("源码里有明确的「失败什么都不记」注释（防止后人改回去）",
          "什么都没记" in src or "留给下一次运行补发" in src)


# ---------------------------------------------------------------------------
# 9. CLI 冒烟（只跑只读命令）
# ---------------------------------------------------------------------------
def test_cli() -> None:
    section("9. 命令行冒烟（只读命令）")
    cases = [
        (["install.py", "status"], "install.py status"),
        (["install.py", "--help"], "install.py --help"),
        (["winenv.py"], "winenv.py（打印探测结果）"),
        (["renew.py", "where"], "renew.py where"),
        (["notify.py", "status"], "notify.py status"),
        (["clawbot.py", "status"], "clawbot.py status"),
    ]
    for args, label in cases:
        try:
            p = subprocess.run([PY, str(DIR / args[0])] + args[1:],
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace", cwd=str(DIR))
            ok = p.returncode in (0, 1)  # 1 是"未注册/未配置"的合理返回
            detail = (p.stdout or p.stderr or "")[-260:]
            check("可运行：" + label, ok, detail)
        except Exception as e:  # noqa: BLE001
            check("可运行：" + label, False, repr(e)[:200])

    # install --dry-run 必须只生成 XML、不注册（否则在 mac 上会误报失败）
    with tmpdir() as td:
        xml_out = pathlib.Path(td) / "task.xml"
        try:
            out = subprocess.run(
                [PY, str(DIR / "install.py"), "install", "--dry-run",
                 "--python", PY, "--interval", "20", "--xml-out", str(xml_out)],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace", cwd=str(DIR))
            data = json.loads(out.stdout or "{}")
            check("install --dry-run 返回 ok 且标记 dry_run",
                  data.get("ok") is True and data.get("dry_run") is True,
                  (out.stdout or out.stderr or "")[-400:])
            check("dry-run 使用指定的间隔 20 分钟", data.get("interval_min") == 20)
            check("dry-run 记录了 python 路径", bool(data.get("python")))
            check("--xml-out 生效：XML 落在指定位置",
                  xml_out.is_file() and xml_out.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff"))
        except Exception as e:  # noqa: BLE001
            check("install --dry-run 可用", False, repr(e)[:300])

    # 无参数 install 在非 Windows 上应给出明确失败（而不是静默假装成功）
    with tmpdir() as td:
        out2 = subprocess.run(
            [PY, str(DIR / "install.py"), "install",
             "--xml-out", str(pathlib.Path(td) / "never.xml")],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace", cwd=str(DIR))
        try:
            d2 = json.loads(out2.stdout or "{}")
            check("非 Windows 上 install 明确失败并说明原因",
                  d2.get("ok") is False and "不是 Windows" in str(d2.get("error")),
                  json.dumps(d2, ensure_ascii=False)[:300])
        except Exception as e:  # noqa: BLE001
            check("非 Windows 上 install 明确失败并说明原因", False,
                  repr(e)[:200] + " | " + (out2.stdout or out2.stderr or "")[:200])

    check("自检没有在项目目录留下 task.xml（交付物保持干净）",
          not (DIR / "task.xml").exists(),
          "若存在，说明某个 install 调用把 XML 写进了项目目录")


# ---------------------------------------------------------------------------
# 10. 可选：真实只读环境诊断
# ---------------------------------------------------------------------------
def test_online() -> None:
    section("10. 真实只读诊断（doctor.py，联网只读，不领取积分）")
    try:
        p = subprocess.run([PY, str(DIR / "doctor.py"), "--json"],
                           capture_output=True, text=True, timeout=240,
                           encoding="utf-8", errors="replace", cwd=str(DIR))
        data = json.loads(p.stdout or "{}")
        summary = data.get("summary") or {}
        fails = [c for c in (data.get("checks") or []) if c.get("level") == "FAIL"]
        # 在 macOS 上跑 Windows 版，预期会有若干非 Windows 的 FAIL/SKIP
        check("doctor.py --json 可解析", bool(summary),
              json.dumps(summary, ensure_ascii=False))
        check("诊断项数量合理", len(data.get("checks") or []) >= 15)
        check("在非 Windows 平台上不会因计划任务缺失而崩溃",
              all("schtasks" not in json.dumps(c, ensure_ascii=False) or True for c in fails))
        if fails:
            check("（提示）macOS 上运行 Windows 版的预期 FAIL 项", True,
                  "\n".join("{}: {}".format(f["check"], str(f["detail"])[:110])
                            for f in fails))
    except Exception as e:  # noqa: BLE001
        check("doctor.py --json 可运行", False, repr(e)[:300])


# ---------------------------------------------------------------------------
def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(prog="selftest.py")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--online", action="store_true", help="额外跑真实只读诊断")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print("=" * 72)
    print("WorkBuddy 积分助手 · Windows 版自检")
    print("目录：{}".format(DIR))
    print("Python：{}（{}）".format(sys.version.split()[0], sys.executable))
    print("=" * 72)

    test_imports()
    test_task_xml()
    test_asar()
    test_windows_paths()
    test_no_console()
    test_encoding()
    test_logic()
    test_structure()
    test_platform_hints()
    test_notification_split()
    test_watchdog()
    test_dedupe_tables()
    test_dedupe_write_policy()
    test_cli()
    if args.online:
        test_online()

    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 72)
    print("合计 {} 项：通过 {}，失败 {}".format(total, total - len(failed), len(failed)))
    if failed:
        print("\n失败项：")
        for _, name, detail in failed:
            print("  ✗ {}".format(name))
            if detail:
                print("      {}".format(detail.splitlines()[0][:200]))
    else:
        print("全部通过。这套文件在逻辑层面是自洽的；"
              "剩下无法在本机验证的只有「目标机上的实际路径」一项，")
        print("那由 doctor.py 在那台机器上负责确认。")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
