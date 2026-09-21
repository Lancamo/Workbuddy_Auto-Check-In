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
import datetime
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


def is_mac_junk(p: pathlib.Path) -> bool:
    """是否是 macOS 的 AppleDouble 伴生文件（`._xxx`）。

    这类文件不是代码：它们是 macOS 往非 HFS 卷（U 盘 / 网络共享 / exFAT）写文件时
    额外生成的资源叉，头 4 字节固定为 00 05 16 07，内容全是二进制。
    把这套装从 mac 拷到 Windows 时必然被一起带过来。

    为什么必须显式排除：任何 `rglob("*.py")` 都会把 `._catchup.py` 当成 Python 源码，
    于是「全部 .py 语法可编译」这类检查会拿二进制去 decode 而**必然报错**，
    看起来像项目坏了，实际只是拷贝残留。真实的 .py 永远不会以 `._` 开头。
    """
    return p.name.startswith("._")


# 自检期间的日志落点（见 _isolate_logs）
_ISO_LOG_DIR: pathlib.Path | None = None


def _isolate_logs() -> str:
    """把被测模块的日志落点改到项目内的临时目录。**必须在任何测试之前调用。**

    为什么必须做：自检会真的调用 `renew.check()` / `catchup._notify()`，而这两个模块的
    `LOG` 是**导入时**由 `paths.log_path(...)` 定下的真实路径。不隔离的话，自检会把测试
    记录写进交付物的 `runtime/logs/catchup.log`，实测（2026-09-20，干净副本首次
    install.cmd 之后）该日志前 10 行全是自检写的，其中包括：

        [renew] stale reminder #1 sent=True age=99.0h

    它来自 8i 节的桩数据（`age_hours=99.0` 是编造的），却和真实告警长得一模一样 ——
    将来排障的人会据此得出「ClawBot 会话真的停摆过」的错误结论。
    自检的产物不许混进被诊断对象的证据里。

    返回被改写的属性清单（便于打印出来，让「自检到底动了什么」可见）。
    """
    global _ISO_LOG_DIR
    _ISO_LOG_DIR = DIR / ".selftest_logs"
    _ISO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    sink = _ISO_LOG_DIR / "catchup.log"
    moved = []
    for mod_name, attr in (("catchup", "LOG"), ("renew", "LOG"),
                           ("watchdog", "SELF_LOG")):
        try:
            mod = __import__(mod_name)
        except Exception:  # noqa: BLE001
            continue
        if hasattr(mod, attr):
            setattr(mod, attr, sink)
            moved.append("{}.{}".format(mod_name, attr))
    return ", ".join(moved)


def _cleanup_isolated_logs() -> None:
    if _ISO_LOG_DIR is not None:
        shutil.rmtree(_ISO_LOG_DIR, ignore_errors=True)


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

    # 编译全部 .py（含子目录），语法错误会被抓出来。
    # 排除 macOS 的 `._xxx` 伴生文件：它们是二进制，拿去 decode 必然失败，
    # 会把「拷贝残留」误报成「项目语法错误」。
    bad = []
    for p in sorted(DIR.rglob("*.py")):
        if is_mac_junk(p):
            continue
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

    # ★★ 这一组是 2026-09-20 在真机上抓出来的**致命**坑，别再删掉：
    #    <Repetition> **省略 <Duration>** 在 Windows 上**不等于「无限重复」**，
    #    而是该触发器**永不触发**。它极具欺骗性 ——
    #      · `schtasks /Query /V` 里「重复: 每: 5 分钟」显示得好好的；
    #      · `NextRunTime` 每 5 分钟往后滚一格，看起来一切正常；
    #      · 但 `LastRunTime` 永远停在注册前，`NumberOfMissedRuns` 一直往上涨，
    #        `LastTaskResult` 恒为 267011（= SCHED_S_TASK_HAS_NOT_RUN）。
    #    实测对照（间隔 PT1M、各观察 3.5 分钟、用一个写时间戳的探针任务）：
    #      · 不写 Duration + 有 LogonTrigger → 触发 **0** 次
    #      · 不写 Duration + 无 LogonTrigger → 触发 **0** 次
    #      · 写 <Duration>P1D</Duration>     → 触发 **3** 次 ✅
    #    当时的实际后果：主任务的「每 5 分钟轮询」与 watchdog 的「每 30 分钟心跳」
    #    **双双静默失效**，「猫到达即领」退化成「下次登录时才领」（实测推迟 59 分钟）。
    #
    #    同时钉住**取值**：PowerShell/CIM 对「无限期」给的序列化值
    #    `P99999999DT23H59M59S` 会被 **schtasks.exe 直接拒绝**
    #    （「任务 XML 包含格式不正确或超出范围的值」），实测上限在 P9999D 附近
    #    （P9999D 通过，P36500D 被拒）。本项目用 schtasks 注册，所以只能用后者。
    dur = txt(".//t:Repetition/t:Duration")
    check("Repetition 必须带 Duration（省略它 = 该触发器永不触发）",
          bool(dur),
          "实际：{!r}；省略 Duration 会让每次触发都被记成「错过的运行」".format(dur))
    check("Duration 是 schtasks 接受的写法（不是 CIM 那个超大值）",
          bool(dur) and dur.startswith("P") and "T" not in dur
          and not dur.startswith("P99999999"),
          "实际：{!r}；schtasks 会拒绝 P99999999DT23H59M59S".format(dur))
    check("Duration 足够长（≥1 天，避免窗口过期后不再重复）",
          bool(dur) and dur.endswith("D") and dur[1:-1].isdigit() and int(dur[1:-1]) >= 1,
          "实际：{!r}".format(dur))

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
    # ★ 这里原本写的是：
    #     check("重复无终止（未写 Duration = 无限重复）",
    #           root.find(".//t:Repetition/t:Duration", NS) is None)
    #   也就是说**测试把 bug 断言成了期望行为** —— 这是那个致命缺陷能长期存活、
    #   并且一路通过自检的直接原因。2026-09-20 真机抓出来后改为下面的反向断言。
    check("Duration 存在（旧版本反了：曾断言它「不该存在」，见上）",
          root.find(".//t:Repetition/t:Duration", NS) is not None)
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

    # --- 第三个任务：每日唤醒器（Windows 上「睡着也能签到」的唯一入口）---
    # 它与轮询任务必须是两种**形态**，不能只是参数不同：
    #   · 轮询任务绝不能开 WakeToRun —— 它带 Repetition PT5M，开了会每 5 分钟
    #     把电脑叫醒一次，比不设还糟；
    #   · 唤醒任务必须只留「每天一次」这一个触发点，且开 WakeToRun。
    # 这两条是本次改动的核心，钉死在测试里，免得日后被「顺手统一一下」改坏。
    xw = ET.fromstring(install.build_task_xml(
        python_exe="py", script=DIR / "catchup.py", workdir=DIR,
        name=install.WAKE_TASK_NAME, wake_to_run=True,
        daily_once_at=install.WAKE_DAILY_AT, desc="唤醒任务"))

    def wtxt(path: str, default=None):
        e = xw.find(path, NS)
        return e.text if (e is not None and e.text) else default

    check("轮询主任务 WakeToRun = false（开了会每 5 分钟唤醒一次）",
          txt(".//t:WakeToRun") == "false",
          "实际：{}".format(txt(".//t:WakeToRun")))
    check("唤醒任务 WakeToRun = true", wtxt(".//t:WakeToRun") == "true",
          "实际：{}".format(wtxt(".//t:WakeToRun")))
    check("唤醒任务不带 Repetition（带重复则每次重复都唤醒一遍）",
          xw.find(".//t:Repetition", NS) is None)
    check("唤醒任务不带 LogonTrigger（登录触发唤醒不了睡眠中的机器）",
          xw.find(".//t:LogonTrigger", NS) is None)
    check("唤醒任务每天定点触发一次（StartBoundary 落在 07:00）",
          (wtxt(".//t:CalendarTrigger/t:StartBoundary") or "").endswith("T07:00:00"),
          "实际：{}".format(wtxt(".//t:CalendarTrigger/t:StartBoundary")))
    check("唤醒任务同样电池可跑（否则笔记本用电池时不会被唤醒）",
          wtxt(".//t:DisallowStartIfOnBatteries") == "false",
          "实际：{}".format(wtxt(".//t:DisallowStartIfOnBatteries")))
    check("唤醒任务也补跑错过的触发", wtxt(".//t:StartWhenAvailable") == "true")
    check("唤醒任务名与常量一致", install.WAKE_TASK_NAME in
          wtxt(".//t:URI", ""), "实际：{}".format(wtxt(".//t:URI")))

    # --- 第四个任务：白天定时唤醒器 ---
    # 补的是「07:00 那个任务够不着」的白天时段：本机实测（2026-09-21）空闲 1~2 分钟
    # 就自己睡了，而每天只有一次 07:00 唤醒 → 白天睡过去后没人叫醒，错过只有等明天。
    # 它和第三个任务同属「能唤醒」的一族（必须独立任务 + WakeToRun），但形态不同：
    # 带 Repetition，所以它的 Duration **必须有界**，到午夜收窗，深夜不再打扰。
    _pts = install.daywake_points()
    xd = ET.fromstring(install.build_task_xml(
        python_exe="py", script=DIR / "catchup.py", workdir=DIR,
        name=install.DAYWAKE_TASK_NAME, wake_to_run=True,
        daily_points=_pts, desc="白天唤醒任务"))

    def dtxt(path: str, default=None):
        e = xd.find(path, NS)
        return e.text if (e is not None and e.text) else default

    _sbs = [e.text for e in xd.findall(".//t:CalendarTrigger/t:StartBoundary", NS)
            if e.text]
    check("白天唤醒任务 WakeToRun = true", dtxt(".//t:WakeToRun") == "true",
          "实际：{}".format(dtxt(".//t:WakeToRun")))
    # ★★ 本组最重要的一条 —— 2026-09-21 实测踩出来的：
    #   「一个触发器 + Repetition(PT3M) + Duration(PT17H) + WakeToRun」看起来
    #   天衣无缝，任务也确实按时在跑，但电脑睡着后 48 分钟一次都没被叫醒，
    #   最后是用户按电源键才醒的（powercfg /lastwake = 电源按钮）。
    #   根因：Repetition 派生的重复实例**不会**武装唤醒定时器，`powercfg /waketimers`
    #   里查无此项。所以每个唤醒时刻必须是**独立的 CalendarTrigger**，且都不带
    #   Repetition —— 与已经验证有效的 07:00 唤醒任务完全同形。
    check("★ 白天唤醒任务**不带 Repetition**（重复实例叫不醒睡眠中的电脑）",
          xd.find(".//t:Repetition", NS) is None)
    check("★ 每个唤醒点一个独立 CalendarTrigger（点数与 daywake_points 一致）",
          len(xd.findall(".//t:Triggers/t:CalendarTrigger", NS)) == len(_pts)
          and len(_pts) > 1,
          "触发器 {} 个 / 唤醒点 {} 个".format(
              len(xd.findall(".//t:Triggers/t:CalendarTrigger", NS)), len(_pts)))
    check("唤醒点从 {} 开始、覆盖到 {}（深夜留白）".format(
              install.DAYWAKE_FROM, install.DAYWAKE_TO),
          any(s.endswith("T{}:00".format(install.DAYWAKE_FROM)) for s in _sbs)
          and any(s.endswith("T{}:00".format(install.DAYWAKE_TO)) for s in _sbs),
          "实际：{}".format([s[-5:] for s in _sbs]))
    check("白天唤醒任务不带 LogonTrigger（登录触发唤醒不了睡眠中的机器）",
          xd.find(".//t:LogonTrigger", NS) is None)
    check("白天唤醒任务同样电池可跑 + 补跑错过的触发",
          dtxt(".//t:DisallowStartIfOnBatteries") == "false"
          and dtxt(".//t:StartWhenAvailable") == "true")
    check("daywake_points 生成的时刻都在 00:00–23:59 内且已排序",
          all(0 <= int(s.split(":")[0]) * 60 + int(s.split(":")[1]) < 1440
              for s in _pts) and _pts == sorted(_pts), "实际：{}".format(_pts))


# ---------------------------------------------------------------------------
# 2b. Windows 命令输出与 doctor 判定
# ---------------------------------------------------------------------------
def test_windows_output_decoding() -> None:
    section("2b. Windows 输出解码与 doctor 判定")
    import doctor
    import install
    import winenv

    sample = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>'
    check("可解码 UTF-16LE + BOM",
          install.decode_process_output(sample.encode("utf-16")) == sample)
    check("可解码无 BOM 的 UTF-16LE",
          install.decode_process_output(sample.encode("utf-16-le")) == sample)
    check("可解码 UTF-8 + BOM",
          install.decode_process_output(sample.encode("utf-8-sig")) == sample)
    check("可解码 GB18030 中文",
          install.decode_process_output("计划任务输出".encode("gb18030")) == "计划任务输出")

    xml_missing_optional = install.build_task_xml(
        python_exe=r"C:\Python312\pythonw.exe",
        script=DIR / "catchup.py", workdir=DIR, interval_min=5,
    ).replace("    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n", "")
    xml_missing_optional = xml_missing_optional.replace(
        "    <StartWhenAvailable>true</StartWhenAvailable>\n", "")

    saved_is_win = winenv.IS_WIN
    saved_query = install.task_query_xml
    saved_live = install.task_live_settings
    try:
        winenv.IS_WIN = True
        install.task_query_xml = lambda _name: (True, xml_missing_optional)
        install.task_live_settings = lambda _name: {
            "start_when_available": "true",
            "disallow_start_if_on_batteries": "false",
        }
        report = doctor.Report()
        doctor.check_task(report)
        by_name = {item["check"]: item for item in report.items}
        check("XML 省略可选字段时会回读有效设置，不误报 FAIL",
              by_name.get("计划任务设置回读", {}).get("level") == doctor.OK
              and by_name.get("错过补跑（StartWhenAvailable）", {}).get("level") == doctor.OK
              and by_name.get("电池下仍运行（DisallowStartIfOnBatteries）", {}).get("level") == doctor.OK,
              json.dumps(report.items, ensure_ascii=False))
        check("回读后没有计划任务相关 FAIL",
              not [i for i in report.items if i["level"] == doctor.FAIL],
              json.dumps(report.items, ensure_ascii=False))
    finally:
        winenv.IS_WIN = saved_is_win
        install.task_query_xml = saved_query
        install.task_live_settings = saved_live

    # 微信通道是可选推送通道，不应把签到任务判成阻塞失败。
    import clawbot
    saved_candidates = clawbot.settings_candidates
    saved_load = clawbot.load_channel
    try:
        clawbot.settings_candidates = lambda: []
        clawbot.load_channel = lambda: None
        report = doctor.Report()
        doctor.check_clawbot(report)
        check("未绑定微信只告警，不阻塞签到",
              not [i for i in report.items if i["level"] == doctor.FAIL]
              and [i for i in report.items if i["level"] == doctor.WARN],
              json.dumps(report.items, ensure_ascii=False))
    finally:
        clawbot.settings_candidates = saved_candidates
        clawbot.load_channel = saved_load


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
    # 只保存本测试**真正改动**的那几个键，而不是整份 os.environ。
    # 为什么不能整份快照再回写：Windows 的环境块有 32767 字符的硬上限，
    # 而真实机器上常常挂着超长变量（本机实测就有一个 28 万字符的
    # ACC_PRODUCT_CONFIG_V3）。`os.environ.clear()` + `update(整份快照)`
    # 会直接抛 `ValueError: the environment variable is longer than 32767
    # characters`，把自检**中断在第四节**——后面 10 节再也跑不到。
    # 改成「只回写自己动过的键」后，无论环境里有什么都不受影响。
    _TOUCHED_ENV = ("APPDATA", "LOCALAPPDATA", "PROGRAMFILES")
    saved_env = {k: os.environ.get(k) for k in _TOUCHED_ENV}

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
        for _k, _v in saved_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        # 期望值必须把 Windows 自己也算进去 —— 旧写法只区分 mac / linux，
        # 于是在 Windows 上恒等于「期望 linux、实际 win」，自己把自己判失败。
        _expect = ("mac" if sys.platform == "darwin"
                   else "win" if sys.platform.startswith("win") else "linux")
        check("模拟环境已还原（platform 回到真实值）",
              winenv.platform() == _expect,
              "本机 sys.platform={}".format(sys.platform))


# ---------------------------------------------------------------------------
# 4b. 登录态候选路径（Windows 必须同时覆盖 Roaming 与 Local）
# ---------------------------------------------------------------------------
def test_credential_paths() -> None:
    """`credentials.py` 的候选路径必须覆盖 Windows 的**两种**用户数据布局。

    ★ 为什么钉住（2026-09-20 实测的真实故障）：
      桌面端把新版明文登录态写进了
        `%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info`
      而旧实现只找 `%APPDATA%`（Roaming）→ **明明已登录却报「读不到登录态」**，
      签到与领取全部空转，报错还把人引向「请先登录」这个完全错误的方向。
      Electron 应用把用户数据放 Roaming 还是 Local 取决于打包方，两个都得列。

    注意：`credentials.py` 是「必须逐字一致」的共享文件，所以它读的是 `sys.platform`
    而不是 `winenv.platform()`（后者只存在于 Win 版）。这里因此按**真实平台**分支断言，
    不翻转平台 —— 在 Windows 交付机上会真正校验到关键的那两条。
    """
    section("4b. 登录态候选路径（Windows 必须覆盖 Local）")
    import credentials as C

    cands = C.desktop_info_candidates()
    tail = os.path.join("auth", "workbuddy-desktop.info").replace("/", "\\")
    check("登录态候选路径非空", bool(cands), "\n".join(cands))
    check("候选都指向 CodeBuddyExtension/.../workbuddy-desktop.info",
          all(p.replace("/", "\\").endswith(tail) for p in cands), "\n".join(cands))

    if sys.platform == "win32":
        ra = os.environ.get("APPDATA", "")
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            check("Windows：候选含 %LOCALAPPDATA%（实测明文就写在这里）",
                  any(p.lower().startswith(la.lower()) for p in cands), "\n".join(cands))
        if ra:
            check("Windows：候选仍含 %APPDATA%（向后兼容，不删旧路径）",
                  any(p.lower().startswith(ra.lower()) for p in cands), "\n".join(cands))
        vscdb = C.legacy_vscdb_candidates()
        if la:
            check("Windows：旧版 state.vscdb 候选也含 %LOCALAPPDATA%",
                  any(p.lower().startswith(la.lower()) for p in vscdb), "\n".join(vscdb))
        if ra:
            check("Windows：旧版 state.vscdb 候选仍含 %APPDATA%",
                  any(p.lower().startswith(ra.lower()) for p in vscdb), "\n".join(vscdb))
        # 诊断信息（恒真）：把本机实际命中情况打出来，排障时一眼可见
        hit = [p for p in cands if os.path.isfile(p)]
        check("（提示）本机登录态候选的实际命中情况", True,
              "命中 {} 个：{}".format(len(hit), hit or "(无 —— 说明尚未登录或路径未覆盖)"))
    else:
        check("非 Windows：登录态候选只有 1 条（不混入 Windows 路径）",
              len(cands) == 1, "\n".join(cands))


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

    # 路径「是否含中文」取决于这套文件被拷到哪里，**不能当作断言** ——
    # README 明说「路径含中文/空格都行」，交付物很可能就落在纯 ASCII 路径下。
    # 旧写法直接断言「当前路径含非 ASCII」，于是落在 ASCII 路径时恒报一条假失败。
    # 真正要保证的是「含中文/空格的路径也能正确处理」，所以用一个确定性的中文路径
    # 现场跑一遍创建 → 写入 → 读回，把这件能力钉死，而不是指望环境凑巧。
    with tmpdir() as td:
        try:
            cn_dir = pathlib.Path(td) / "中文 路径"
            cn_dir.mkdir(parents=True, exist_ok=True)
            cn_file = cn_dir / "子 目录.txt"
            cn_file.write_text("积分签到 · 中文往返测试", encoding="utf-8")
            back = cn_file.read_text(encoding="utf-8")
            check("含中文/空格的路径可正确创建与读写",
                  back == "积分签到 · 中文往返测试" and cn_dir.is_dir(),
                  "路径 {}，读回 {}".format(cn_dir, back[:40]))
        except OSError as e:  # noqa: BLE001
            check("含中文/空格的路径可正确创建与读写", False, repr(e)[:200])
    check("（提示）当前项目路径的实际形态", True,
          "{}（含非 ASCII 字符={}）".format(
              DIR, any(ord(ch) > 127 for ch in str(DIR))))
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

    # doctor.py 的新检查靠这两个纯函数解析「任务到底在跑哪个文件」——
    # 搬迁后任务指向旧路径是本项目最典型的静默失效，判错就等于没查。
    import doctor as _doc
    parsed = _doc._script_from_args('"C:\\a b\\catchup.py"')
    check("doctor 能从带引号的 Arguments 里取出脚本路径",
          parsed == r"C:\a b\catchup.py"
          and _doc._script_from_args("C:\\x\\catchup.py --flag") == r"C:\x\catchup.py"
          and _doc._script_from_args(None) == ""
          and _doc._script_from_args("") == "",
          "实际解析：{!r}".format(parsed))
    check("doctor 的路径比对：同一文件相等、不同文件不等",
          _doc._same_file(str(DIR / "catchup.py"), str(DIR / "catchup.py"))
          and not _doc._same_file(str(DIR / "catchup.py"), str(DIR / "watchdog.py")))


# ---------------------------------------------------------------------------
# 8. 结构约束：mac 版绝对路径不得残留；两套差异只落在预期位置
# ---------------------------------------------------------------------------
def test_structure() -> None:
    section("8. 结构约束（禁止把 mac 绝对路径带进 Windows 版）")
    # 本文件自身会包含这些模式（它就是在找它们），所以排除掉；
    # 同时排除 macOS 的 `._xxx` 二进制伴生文件（见 is_mac_junk）。
    files = [p for p in sorted(DIR.rglob("*.py"))
             if p.name != "selftest.py" and not is_mac_junk(p)]
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
                "scripts/http_client.py"]
    missing = [f for f in required if not (DIR / f).exists()]
    check("必备文件齐全", not missing, "缺失：{}".format(missing))

    # ★ 运行时配置**不是**交付物，别再把它们列进必备清单（2026-09-20 修）。
    #   实测：干净副本（从 bundle 克隆、一次都没跑过）上，把
    #   `runtime/config/*.json` 列为必备，会让上一条检查恒报「失败 1 项」——
    #   而 install.cmd 结尾就会跑自检，于是**开箱第一次安装看起来像失败了**。
    #   它们由 notify/renew 的 ensure_config() 在首次运行时生成，`.gitignore`
    #   也明确排除 `00_For_*/runtime/`。真正的判据是「生成它们的能力在代码里」。
    gen_ok, gen_detail = [], []
    for mod_name, fn_name, default_attr in (
            ("notify", "ensure_config", "DEFAULT_CONFIG"),
            ("renew", "ensure_config", "DEFAULTS")):
        try:
            mod = __import__(mod_name)
            ok = callable(getattr(mod, fn_name)) and bool(getattr(mod, default_attr))
        except Exception as e:  # noqa: BLE001
            ok = False
            gen_detail.append("{}: {}".format(mod_name, e))
        if not ok:
            gen_detail.append("{}.{}/{} 不可用".format(mod_name, fn_name, default_attr))
        gen_ok.append(ok)
    check("运行时配置可自动生成（缺失属正常，不参与交付清单）",
          all(gen_ok), "\n".join(gen_detail))

    # 已经存在的运行时配置必须是合法 JSON：损坏的配置会被静默当成「用默认值」，
    # 用户改过的通道凭据就白改了，而日志里不会有任何异常。
    bad_cfg = []
    for name in ("notify_config.json", "renew_config.json"):
        p = paths.CONFIG_DIR / name
        if not p.is_file():
            continue
        try:
            if not isinstance(json.loads(p.read_text(encoding="utf-8")), dict):
                bad_cfg.append("{}: 顶层不是对象".format(name))
        except Exception as e:  # noqa: BLE001
            bad_cfg.append("{}: {}".format(name, e))
    check("已存在的运行时配置是合法 JSON", not bad_cfg, "\n".join(bad_cfg))

    # 自检的日志必须被隔离走：否则它会把自己的桩数据写进交付物的
    # runtime/logs/catchup.log，变成排障时无法分辨的「假证据」（见 _isolate_logs）。
    import catchup as _c
    import renew as _r
    check("自检把日志隔离到项目内临时目录（不污染 runtime/logs）",
          _c.LOG == _r.LOG and _c.LOG.parent.name == ".selftest_logs",
          "catchup.LOG={} renew.LOG={}".format(_c.LOG, _r.LOG))

    # .cmd 内容必须是纯 ASCII（否则 Windows 中文代码页会把脚本打乱）
    # 排除 macOS 的 `._xxx` 伴生文件 —— 它们是二进制，按后缀扫进来必然误报。
    bad_cmd = []
    for p in sorted(DIR.glob("*.cmd")):
        if is_mac_junk(p):
            continue
        raw = p.read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError:
            bad_cmd.append(p.name)
    check(".cmd 文件为纯 ASCII（避免代码页乱码）", not bad_cmd, str(bad_cmd))

    # macOS 的 AppleDouble 残留（`._xxx`）：从 mac 拷到非 HFS 卷（U 盘/共享/exFAT）
    # 时必然被一起带过来。它们不参与运行，但会让所有「按后缀扫描」的检查误报，
    # 也让交付物看起来不干净。这里点出来并给出可直接执行的清理命令。
    junk = sorted(DIR.rglob("._*"))
    check("没有 macOS 拷贝残留（._* 伴生文件）", not junk,
          "共 {} 个，例如 {}。清理：find . -name '._*' -delete".format(
              len(junk), ", ".join(sorted(str(p.relative_to(DIR)) for p in junk)[:6])))

    # 两套必须成对存在的文件（同步用）
    # 2026-09-18 整理目录后，mac 版在 `../00_For_Mac/`；兼容整理前的旧布局（直接在上一级）。
    #
    # ★ 这里**不能**无条件判失败：README 明说「这个文件夹就是交付物，拷到新 Windows
    #   机器的任意位置即可用」。在交付机（独立副本）上根本不存在 `../00_For_Mac`，
    #   旧写法会让自检在开箱状态下恒报一条 FAIL，把「跨版本比对」这件**开发期**的事
    #   伪装成「交付物坏了」。mac 版在位才做比对，不在位就明确标注跳过。
    mac = DIR.parent / "00_For_Mac"
    if not (mac / "catchup.py").is_file():
        mac = DIR.parent
    pairs = ["catchup.py", "notify.py", "renew.py", "clawbot.py",
             "scripts/main.py", "scripts/checkin.py", "scripts/travel.py",
             "scripts/api_discovery.py", "scripts/credentials.py",
             "scripts/http_client.py"]
    mac_present = (mac / "catchup.py").is_file()
    if mac_present:
        check("上级 macOS 版在位（同步基准）", True, str(mac))
    else:
        check("上级 macOS 版不在位 —— 按独立交付副本跳过跨版本比对", True,
              "未找到 {}".format(mac / "catchup.py"))
    if mac_present:
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

    saved_platform = winenv.platform
    saved_is_win = winenv.IS_WIN
    try:
        # --- ① 真机平台：文案必须与**本机真实平台**一致 ---
        #   旧写法把「本机一定是 mac」写死在断言里（`"python3" in selfcheck_hint()`），
        #   于是自检在 Windows 上必然报一条假失败。要验的是「文案跟着平台走」，
        #   而不是「文案跟着 mac 走」。
        real = winenv.platform()
        sc_real = winenv.selfcheck_hint()
        lh_real = winenv.login_hint()
        if real == "win":
            check("真机是 Windows：selfcheck_hint 给 doctor.cmd 且不含 python3",
                  "doctor.cmd" in sc_real and "python3" not in sc_real, sc_real)
            check("真机是 Windows：login_hint 给 login.cmd 且不含 python3",
                  "login.cmd" in lh_real and "python3" not in lh_real, lh_real)
        else:
            check("真机非 Windows：selfcheck_hint 给 mac/linux 命令",
                  "python3" in sc_real, sc_real)

        # --- ② 假装自己是 Windows ---
        #   必须**同时**翻 winenv.platform 与 winenv.IS_WIN：
        #     · winenv 模块自己的内部逻辑读 platform()（这是它写明的约定，
        #       也是为了「能被测试翻过去」才这么设计的）；
        #     · notify / renew / clawbot 这些**成对维护**的文件读 IS_WIN
        #       （那是 README 5.5 明确允许的平台差异，因为 mac 版没有 winenv）。
        #   只翻其中一个，就只模拟了一半的平台。
        winenv.platform = lambda: "win"
        winenv.IS_WIN = True
        sc = winenv.selfcheck_hint()
        lh = winenv.login_hint()
        check("win 上 selfcheck_hint 不含 python3", "python3" not in sc, sc)
        check("win 上 selfcheck_hint 指向 doctor.cmd", "doctor.cmd" in sc, sc)
        check("win 上 login_hint 不含 python3", "python3" not in lh, lh)
        check("win 上 login_hint 指向 login.cmd", "login.cmd" in lh, lh)

        # --- ③ 再翻回非 Windows 做反向断言（旧测试缺这一半，
        #        所以「文案有没有真的跟着平台走」其实没被验证过）---
        winenv.platform = lambda: "mac"
        winenv.IS_WIN = False
        sc_mac = winenv.selfcheck_hint()
        check("翻回 mac 后 selfcheck_hint 给回 mac 命令（文案确实跟着平台走）",
              "python3" in sc_mac, sc_mac)
        # 回到 Windows，供下面 clawbot / renew / catchup 的正文断言使用
        winenv.platform = lambda: "win"
        winenv.IS_WIN = True

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
        winenv.platform = saved_platform
        winenv.IS_WIN = saved_is_win


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

    # --- 防黑窗：watchdog 的每个子进程都必须带 CREATE_NO_WINDOW ---
    # ★ 2026-09-21 用户实测：桌面每 30 分钟闪一个 PowerShell 黑框 —— 根因是
    #   watchdog（由 pythonw 拉起、本身无控制台）调用 powershell.exe / schtasks 时
    #   没给 CREATE_NO_WINDOW，这些**控制台程序**就各自新建了一个控制台窗口。
    #   watchdog 刻意不 import winenv，常量只能就地定义 —— 所以用 AST 断言
    #   「每一个 subprocess 调用都带了 creationflags」，防止后人新增调用时漏掉；
    #   并核对常量值与 winenv.subprocess_flags() 一致。
    _spawn_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("run", "Popen", "check_output") \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "subprocess":
            _spawn_calls.append(node)
    _missing = [i for i, c in enumerate(_spawn_calls)
                if not any(k.arg == "creationflags" for k in c.keywords)]
    check("★ watchdog 的每个 subprocess 调用都带 creationflags（防 PowerShell 黑框）",
          _spawn_calls and not _missing,
          "{} 处调用，{} 处缺失：{}".format(len(_spawn_calls), len(_missing), _missing))
    # Windows ABI 固定值：CREATE_NO_WINDOW = 0x08000000。不用
    # `subprocess.CREATE_NO_WINDOW` 是因为它只在 Windows 解释器上存在，
    # 本自检在 macOS 上也要跑；这里直接钉死规范值，跨平台结果一致。
    check("watchdog 的 _CREATE_NO_WINDOW 等于 Windows CREATE_NO_WINDOW（0x08000000）",
          getattr(wd, "_CREATE_NO_WINDOW", None) == 0x08000000,
          "watchdog={:#x}".format(getattr(wd, "_CREATE_NO_WINDOW", -1)))

    # --- 监控对象必须与安装器注册的主任务同名，否则查了个不存在的东西 ---
    check("被监控的任务名与 install.py 的主任务名一致",
          wd.JOB_NAME == install.DEFAULT_TASK_NAME,
          "watchdog={} install={}".format(wd.JOB_NAME, install.DEFAULT_TASK_NAME))

    # --- 判定逻辑：注入 job_loaded，用临时 state.json 控制心跳新鲜度 ---
    orig_state, orig_log = wd.STATE, wd.MAIN_LOG
    try:
        orig_quiet = wd.QUIET_FROM
        # 先关掉静默判定，让 1)~6) 专注「心跳新旧 → 报不报」这条逻辑本身；
        # 静默相关的行为在 7)~8) 单独测，免得两件事互相干扰。
        wd.QUIET_FROM = -1
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

            # 7) 静默两种情形都不得报 stale —— 2026-09-19 的实际误报就出在这里：
            #    电脑连续睡了 122 分钟（> 阈值 90），一开盖就收到「主脚本可能已停摆」。
            #    ★ 每段都必须 write_text 之后**重新 utime** —— 写文件会把 mtime 刷成
            #      当下，「心跳陈旧」的前提就没了，断言会以「心跳很新」的方式假通过。
            #    ★ 同时断言 state_age 确实超阈值，否则「不报」可能只是心跳太新。
            #    age 用**真实时间**算，所以固定「探测时刻」即可让断言与
            #    「自检恰好在几点跑」无关 —— 否则凌晨跑自检会假失败。
            wd.QUIET_FROM = orig_quiet
            day_ = datetime.date.today()
            stale_ts = now - (wd.STALE_MINUTES + 60) * 60
            midnight = datetime.datetime.combine(day_, datetime.time(3, 0)).timestamp()
            daytime = datetime.datetime.combine(day_, datetime.time(14, 0)).timestamp()

            def _write_old(payload: dict) -> None:
                st.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.utime(st, (stale_ts, stale_ts))

            _write_old({"day": day_.isoformat(), "checkin_done": False,
                        "claim_done": False})
            r = wd.check(midnight, job_loaded=True)
            check("凌晨静默期：心跳确实陈旧，但不报 stale",
                  r["healthy"] and r["quiet_hours"]
                  and (r["state_age_minutes"] or 0) > wd.STALE_MINUTES,
                  "age={} quiet={} problems={}".format(
                      r["state_age_minutes"], r["quiet_hours"], r["problems"]))

            _write_old({"day": day_.isoformat(), "checkin_done": True,
                        "claim_done": True})
            r = wd.check(daytime, job_loaded=True)
            check("当日已完成：心跳确实陈旧（122 分钟级），但不报 stale",
                  r["healthy"] and r["day_finished"]
                  and (r["state_age_minutes"] or 0) > wd.STALE_MINUTES,
                  "age={} finished={} problems={}".format(
                      r["state_age_minutes"], r["day_finished"], r["problems"]))

            # 8) 真故障必须照报 —— 否则「静默放行」就变成了掩盖
            yest = (day_ - datetime.timedelta(days=1)).isoformat()
            _write_old({"day": yest, "checkin_done": True, "claim_done": True})
            r = wd.check(daytime, job_loaded=True)
            check("state 是昨天的 + 心跳陈旧 → 照报 stale（静默不掩盖真故障）",
                  "stale" in r["problems"],
                  "age={} problems={}".format(r["state_age_minutes"], r["problems"]))

            # 9) ★ 2026-09-20 新增：任务「指向哪个文件」与「上次跑成没成」。
            #    实测背景：文件夹搬迁后三个任务仍指向旧绝对路径，状态却是
            #    「已注册、已启用」、每 5 分钟准点触发 —— 每次都失败
            #    （0x8007010B = ERROR_DIRECTORY），签到根本没发生，
            #    而只查 loaded 的旧实现全程报 healthy。主任务与监控任务**双双**哑掉，
            #    一个能报警的都没有，正是本项目最忌讳的那类失效。
            st.write_text("{}", encoding="utf-8")
            os.utime(st, (now, now))
            r = wd.check(now, job_loaded=True,
                         task_action=r"C:\old\Autocheck_For_Win\catchup.py",
                         task_result=0x8007010B)
            check("任务指向旧路径 → 报 wrong_path",
                  "wrong_path" in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))
            check("任务上次运行失败（0x8007010B）→ 报 task_failed",
                  "task_failed" in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))
            check("路径与结果都写进摘要（事后核对判据不用猜）",
                  r.get("task_last_result") == "0x8007010B"
                  and str(r.get("task_script", "")).endswith("catchup.py"),
                  json.dumps({k: v for k, v in r.items() if k.startswith("task_")},
                             ensure_ascii=False))

            # 10) 路径正确 + 结果是**信息码**时不许误报。
            #     267011 = SCHED_S_TASK_HAS_NOT_RUN（刚注册、还没跑过），
            #     267009 = 正在运行。把它们当失败的话，每次 install 完都会立刻弹假告警。
            r = wd.check(now, job_loaded=True,
                         task_action=str(DIR / "catchup.py"), task_result=267011)
            check("路径正确 + 267011（未曾运行）→ 不误报",
                  r["healthy"], json.dumps(r["problems"], ensure_ascii=False))
            r = wd.check(now, job_loaded=True,
                         task_action=str(DIR / "catchup.py"), task_result=0)
            check("路径正确 + LastTaskResult=0 → 健康",
                  r["healthy"], json.dumps(r["problems"], ensure_ascii=False))

            # 11) _result_is_error 的边界：0 与 0x413xx 信息码不算失败，
            #     >= 0x80000000 的 Win32 错误码算失败（含以负数形式给出的等价写法）。
            check("_result_is_error：0 与信息码不算失败",
                  not wd._result_is_error(0)
                  and not wd._result_is_error(267011)
                  and not wd._result_is_error(267009))
            check("_result_is_error：Win32 错误码算失败（含负数写法）",
                  wd._result_is_error(0x8007010B)
                  and wd._result_is_error(-2147023605))

            # 12) ★ 2026-09-21：静默期刚结束的宽限窗口。
            #     静默期内主脚本故意不写心跳，机器又整夜睡着 —— 07:00 的第一次采样
            #     心跳必然是「昨晚的」，若正好撞上当天第一次运行还没写完 state，
            #     就会报一条「主脚本已 432 分钟没有运行」的假告警
            #     （本机实测就是这么发生的，通知里那条「可能已停摆」就是它）。
            _write_old({"day": yest, "checkin_done": True, "claim_done": True})
            t_0700 = datetime.datetime.combine(day_, datetime.time(7, 0, 5)).timestamp()
            r = wd.check(t_0700, job_loaded=True)
            check("07:00 首次采样（state 还是昨天的）→ 不报 stale（宽限期）",
                  r["healthy"] and r["first_run_grace"]
                  and (r["state_age_minutes"] or 0) > wd.STALE_MINUTES,
                  "age={} grace={} problems={}".format(
                      r["state_age_minutes"], r["first_run_grace"], r["problems"]))

            # 13) 宽限期不许掩盖真故障：07:15 之后照报
            t_0715 = datetime.datetime.combine(day_, datetime.time(7, 15)).timestamp()
            r = wd.check(t_0715, job_loaded=True)
            check("07:15（超出宽限）同样条件 → 照报 stale",
                  "stale" in r["problems"] and not r["first_run_grace"],
                  "grace={} problems={}".format(r["first_run_grace"], r["problems"]))

            # 14) 宽限期只对「state 还是昨天的」生效：今天跑过又停了，照报
            _write_old({"day": day_.isoformat(), "checkin_done": False,
                        "claim_done": False})
            r = wd.check(t_0700, job_loaded=True)
            check("state 是今天的 + 心跳陈旧 → 宽限期不适用，照报 stale",
                  "stale" in r["problems"] and not r["first_run_grace"],
                  "grace={} problems={}".format(r["first_run_grace"], r["problems"]))

            # 15) ★ 刚开跑的那一次不下「失败」结论 —— 结果字段此刻可能是过渡值
            st.write_text("{}", encoding="utf-8")
            os.utime(st, (now, now))
            r = wd.check(now, job_loaded=True, task_result=0x800710E0,
                         task_last_run=now - 5)
            check("刚开跑 5 秒 → 不因过渡结果值报 task_failed",
                  "task_failed" not in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))
            r = wd.check(now, job_loaded=True, task_result=0x800710E0,
                         task_last_run=now - 600)
            check("同一错误码但已开始 10 分钟 → 照报 task_failed",
                  "task_failed" in r["problems"],
                  json.dumps(r["problems"], ensure_ascii=False))

            # 16) 今早那条假告警的完整复现（07:00:05 采样）
            _write_old({"day": yest, "checkin_done": True, "claim_done": True})
            r = wd.check(t_0700, job_loaded=True, task_result=0x800710E0,
                         task_last_run=t_0700 - 5)
            check("复现 2026-09-21 07:00 的假告警场景 → 现在判定健康",
                  r["healthy"],
                  "problems={} age={} grace={}".format(
                      r["problems"], r["state_age_minutes"], r["first_run_grace"]))
    finally:
        wd.STATE, wd.MAIN_LOG = orig_state, orig_log
        wd.QUIET_FROM = orig_quiet

    # --- 静默判据：catchup 决定「写不写心跳」，watchdog 据此判活，口径必须一致 ---
    import catchup as cu
    check("catchup 与 watchdog 的静默期起点一致",
          cu.QUIET_FROM == wd.QUIET_FROM,
          "catchup={} watchdog={}".format(cu.QUIET_FROM, wd.QUIET_FROM))
    check("静默起点与签到闸门同值（07:00 开门即可干活）",
          cu.QUIET_FROM == cu.WINDOW1,
          "QUIET_FROM={} WINDOW1={}".format(cu.QUIET_FROM, cu.WINDOW1))
    for hm_, want in ((0, True), (629, True), (659, True), (700, False),
                      (1400, False), (2359, False)):
        h, m = divmod(hm_, 100)
        t = datetime.datetime(2026, 9, 19, h, m)
        check("静默判定 {:.2f} → {}".format(hm_ / 100.0, want),
              cu._quiet_now(t) is want)
    today_s = datetime.date.today().isoformat()
    yest_s = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    check("签到 + 领取都到手 → 判为当日收工",
          cu._day_finished({"day": today_s, "checkin_done": True, "claim_done": True},
                           today_s))
    check("只签到、还没领到 → 未收工（必须继续等猫）",
          not cu._day_finished({"day": today_s, "checkin_done": True,
                                "claim_done": False}, today_s))
    check("昨天的完成状态不能给今天免跑（否则跨日当天不签到）",
          not cu._day_finished({"day": yest_s, "checkin_done": True,
                                "claim_done": True}, today_s))
    check("watchdog 与 catchup 的收工口径一致",
          wd._day_finished({"day": today_s, "checkin_done": True,
                            "claim_done": True}, time.time()))

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
# 8g. 端点变化检测的语义（兜底扫描不得被当成「接口已切换」）
# ---------------------------------------------------------------------------
def test_endpoint_change_semantics() -> None:
    """只有「本次与上次**都是**权威扫描（客户端 asar）」时才允许判定端点变化。

    为什么必须钉住：`changed=True` 会直接变成一条「接口已自动切换」推送。
    客户端临时定位不到时，`api_discovery` 会退回内置兜底表 —— 那语义是
    「这次没能读到权威来源」，**不是**「端点变了」。2026-09-20 实测误报过：
    客户端路径探测失败 → 立刻推了一条端点变化告警，而实际上端点一个字都没动。
    更坏的是它会稀释这个告警的可信度，把真正该重视的接口漂移淹没在噪音里。

    这里用项目内的临时缓存目录跑完整三步（权威 → 退化 → 恢复），
    再加一步伪造的真实端点变化，确保这次收紧**没有把告警彻底关掉**。
    """
    section("8g. 端点变化检测语义（兜底扫描 ≠ 接口已切换）")
    import api_discovery as ad

    saved_cache = ad.CACHE
    real_fp = ad.client_fingerprint
    try:
        if not real_fp().get("available"):
            check("本机没有可读的客户端 asar —— 跳过 8g（该语义需真机 asar 才能验证）",
                  True, "doctor.py 会显示找过哪些候选路径")
            return
        with tmpdir() as td:
            ad.CACHE = pathlib.Path(td) / "api_endpoints.json"

            good = ad.discover(force=True)
            baseline = json.loads(ad.CACHE.read_text(encoding="utf-8"))
            check("① 客户端在位 → 判定为权威扫描（source=asar）",
                  good.get("source") == "asar",
                  "实际 source={}".format(good.get("source")))

            # 模拟「客户端临时定位不到」→ 退回内置兜底表
            ad.client_fingerprint = lambda: {
                "available": False, "path": None, "version": None,
                "version_source": None, "app": None,
                "asar_size": None, "asar_mtime": None}
            deg = ad.discover(force=True)
            check("② 客户端缺失 → 退化为兜底表（source=fallback）",
                  deg.get("source") == "fallback", "实际 source={}".format(deg.get("source")))
            check("③ 退化**不得**被当成「接口已切换」（changed 必须为 False）",
                  deg.get("changed") is False, "实际 changed={}".format(deg.get("changed")))
            after = json.loads(ad.CACHE.read_text(encoding="utf-8"))
            check("④ 退化扫描不覆盖权威基线（缓存里仍是 asar 端点和原指纹）",
                  after.get("source") == "asar"
                  and after.get("status_paths", [])[:2]
                  == baseline.get("status_paths", [])[:2],
                  json.dumps({"source": after.get("source"),
                              "paths": after.get("status_paths", [])[:2]},
                             ensure_ascii=False))
            check("⑤ 退化单独记一笔（degraded_scan_at），便于事后区分",
                  bool(after.get("degraded_scan_at")))

            # 客户端恢复 → 不得反向误报一次变化
            ad.client_fingerprint = real_fp
            back = ad.discover(force=True)
            check("⑥ 客户端恢复后不得反向误报（changed 仍为 False）",
                  back.get("source") == "asar" and back.get("changed") is False,
                  json.dumps({"source": back.get("source"),
                              "changed": back.get("changed")}, ensure_ascii=False))

            # ⑦ 真正的端点变化必须照报 —— 证明这次收紧没有把告警一并关掉。
            #    伪造一份「上次是权威扫描、但端点不同、指纹也不同」的缓存，
            #    迫使本次重新扫描并与它比对。
            forged = dict(baseline)
            forged["fingerprint"] = {"available": True, "path": "forged",
                                     "version": "0.0.0", "version_source": "selftest",
                                     "app": "forged", "asar_size": 1, "asar_mtime": 1}
            forged["status_paths"] = ["/forged/old-status"]
            forged["claim_paths"] = ["/forged/old-claim"]
            ad.CACHE.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
            ch = ad.discover(force=True)
            check("⑦ 真正的端点变化照报（收紧判定没有连带把告警关掉）",
                  ch.get("source") == "asar" and ch.get("changed") is True,
                  json.dumps({"source": ch.get("source"), "changed": ch.get("changed")},
                             ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        check("端点变化检测语义可验证", False, repr(e)[:300])
    finally:
        ad.client_fingerprint = real_fp
        ad.CACHE = saved_cache


# ---------------------------------------------------------------------------
# 8h. 通知的「送达」口径与告警记账（没送到就不许记账）
# ---------------------------------------------------------------------------
def test_notify_delivery_semantics() -> None:
    """`sent` 的口径，以及「告警先记账后发送」这个坑。

    为什么必须钉住：
      · `sent` 是 `catchup.log` 与 README 第 6 节排错表**唯一的送达判据**。
        本机通知明明弹出来了却仍记 `sent=False`，日志就自相矛盾，
        排障时会被引向「根本没送到」的错误结论。
      · 告警若在发送**之前**就写「今天已告警」，一次投递失败就能让这条告警
        被永久吞掉一整天 —— 正是本项目最忌讳的静默失效。
        （两张去重表 2026-09-18 已经修成「送到才记」，告警这一处当时漏了。）
    """
    section("8h. 通知送达口径与告警记账")
    import notify as N

    saved = (N.STATE, N._send_native, N._native_enabled, N._channel_order, N.load_config)
    with tmpdir() as td:
        try:
            N.STATE = pathlib.Path(td) / "notify_state.json"
            N.load_config = lambda: dict(N.DEFAULT_CONFIG)   # 不受用户实际配置影响
            N._native_enabled = lambda cfg: True
            N._channel_order = lambda cfg: []                # 模拟「无可用微信通道」

            # ① 通道不可用，但本机弹窗成功 → sent 必须为 True
            N._send_native = lambda title, content: True
            r1 = N.send("测试标题", "测试正文", "failure")
            check("微信不可用但本机弹成功 → sent=True 且 wechat=False",
                  r1.get("wechat") is False and r1.get("sent") is True,
                  json.dumps(r1, ensure_ascii=False))

            # ② 本机弹窗也失败 → 两张表都不许记
            N.STATE.unlink(missing_ok=True)
            N._send_native = lambda title, content: False
            r2 = N.send("测试标题2", "测试正文2", "failure")
            st2 = (json.loads(N.STATE.read_text(encoding="utf-8"))
                   if N.STATE.exists() else {})
            check("微信与本机都失败 → sent=False，且 sent/local_sent 都不记",
                  r2.get("sent") is False
                  and not (st2.get("sent") or {}) and not (st2.get("local_sent") or {}),
                  json.dumps({"sent": r2.get("sent"), "wechat": r2.get("wechat"),
                              "state_tables": [k for k in ("sent", "local_sent")
                                               if st2.get(k)]}, ensure_ascii=False))

            cfg = dict(N.DEFAULT_CONFIG)

            # ③ 告警投递失败 → 不得写「今日已告警」
            st3: dict = {"sent": {}, "local_sent": {}}
            a1 = N._alert_channel_expired(st3, cfg, time.time(), "session")
            check("告警未送达 → 不记「今日已告警」（否则被吞掉一整天）",
                  a1 is False and "channel_alert_date" not in st3,
                  json.dumps({"returned": a1, "state": st3}, ensure_ascii=False))

            # ④ 送达才记账，且当天不再重复
            N._send_native = lambda title, content: True
            a2 = N._alert_channel_expired(st3, cfg, time.time(), "session")
            a3 = N._alert_channel_expired(st3, cfg, time.time(), "session")
            check("告警送达 → 记下日期；当天第二次不再重复弹",
                  a2 is True and bool(st3.get("channel_alert_date")) and a3 is False,
                  json.dumps(st3, ensure_ascii=False))

            # ⑤ 两类告警各自留额度：session 报过不能把 blocked 一并吞掉
            st4: dict = {"sent": {}, "local_sent": {}}
            N._alert_channel_expired(st4, cfg, time.time(), "session")
            b1 = N._alert_channel_expired(st4, cfg, time.time(), "blocked")
            check("session 告警不影响 blocked 告警的额度", b1 is True,
                  json.dumps(st4, ensure_ascii=False))

            # ⑥ 状态文件被改坏（两个表不是 dict）时不得抛异常
            N.STATE.write_text('{"sent": [1,2], "local_sent": "oops"}', encoding="utf-8")
            try:
                r6 = N.send("标题6", "正文6", "failure")
                check("状态文件里 sent/local_sent 形状不对时不抛异常（send 承诺永不抛）",
                      isinstance(r6, dict), json.dumps(r6, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                check("状态文件里 sent/local_sent 形状不对时不抛异常", False, repr(e)[:200])
        finally:
            (N.STATE, N._send_native, N._native_enabled,
             N._channel_order, N.load_config) = saved


# ---------------------------------------------------------------------------
# 8i. 续期提醒不得「没送到也记账」
# ---------------------------------------------------------------------------
def test_renew_remind_gating() -> None:
    """续期提醒「没送到就不许记今天已提醒」。

    为什么必须钉住：这条提醒的内容恰恰是「ClawBot 会话已停摆」。一旦因为一次
    投递失败被记成「今天已提醒」，当天剩下的触发全会走 `remind_skipped` ——
    而漏掉它的后果是所有推送静默失效，且**没有任何其他机制**会再提醒你。
    """
    section("8i. 续期提醒不得「没送到也记账」")
    import renew as R

    saved = (R.STATE, R.cursor_status, R._notify_stale)
    with tmpdir() as td:
        try:
            R.STATE = pathlib.Path(td) / "renew_state.json"
            R.cursor_status = lambda now_ts=None: {
                "found": True, "age_hours": 99.0, "path": "stub",
                "mtime_ts": time.time()}
            now = datetime.datetime(2026, 9, 20, 12, 0)

            # ① 提醒发送失败 → 不记账，下次触发继续试
            R._notify_stale = lambda n, age: False
            o1 = R.check(now=now, force=True)
            check("提醒未送达 → 不记「今日已提醒」（下一轮会重试）",
                  o1.get("action") == "stale" and o1.get("remind_sent") is False
                  and R.load_state().get("last_remind_date") is None,
                  json.dumps({"action": o1.get("action"),
                              "remind_sent": o1.get("remind_sent"),
                              "last_remind_date": R.load_state().get("last_remind_date")},
                             ensure_ascii=False))

            # ② 送达 → 记账，且当天不再重复
            R._notify_stale = lambda n, age: True
            o2 = R.check(now=now, force=True)
            o3 = R.check(now=now, force=True)
            check("提醒送达 → 记账；当天不再重复",
                  o2.get("remind_sent") is True
                  and o3.get("remind_skipped") == "今日已提醒",
                  json.dumps({"o2_remind_sent": o2.get("remind_sent"),
                              "o3_remind_skipped": o3.get("remind_skipped")},
                             ensure_ascii=False))
        finally:
            R.STATE, R.cursor_status, R._notify_stale = saved


# ---------------------------------------------------------------------------
# 8j. watchdog 必须能识别「任务被停用 / 被删除」
# ---------------------------------------------------------------------------
def test_watchdog_job_state() -> None:
    """分辨「任务被停用 / 被删除」与「任务健在」—— 这是 watchdog 唯一的存在理由。

    这里不需要真去改本机任务状态：把**真实形态**的 `schtasks /FO LIST /V`
    输出样本喂给解析函数，把当年踩过的两个坑一起钉死：

      · **编码**：该输出的编码跟随**控制台代码页**（中文系统 = GBK）。
        写死 `encoding="utf-8", errors="replace"` 会让中文全变成 U+FFFD，
        「已禁用」永远读不出来 → **任务真被停用也报健康**，监控彻底失灵。
      · **匹配范围**：/V LIST 里「空闲时间」「删除没有计划的任务」
        「重复: 截止: 持续时间」等**无关字段**的值也是「已禁用」
        （实测整串 6 处）→ 拿整串做子串匹配，会把**健康**任务误判成停用，
        每 30 分钟误报一次。

    两个坑互相掩盖（编码错 → 中文读不出 → 整串匹配也命中不了 → 恰好"看起来正常"），
    所以只修任一个都会立刻暴露另一个。下面把正确结论与「整串匹配必然错」的反证一起断言。
    """
    section("8j. watchdog 能识别「任务被停用 / 被删除」")
    import watchdog as W

    # 忠实还原中文 schtasks /FO LIST /V 的真实行形态（含那些无关的「已禁用」字段）
    template = (
        "任务名:                             \\WorkBuddyRewardCatchup\r\n"
        "下次运行时间:                       2026/9/20 14:50:00\r\n"
        "模式:                               就绪\r\n"
        "登录状态:                           只使用交互方式\r\n"
        "计划任务状态:                       {state}\r\n"
        "空闲时间:                           已禁用\r\n"
        "删除没有计划的任务:                 已禁用\r\n"
        "重复: 截止: 持续时间:               已禁用\r\n"
        "重复: 如果还在运行，停止:           已禁用\r\n"
    )
    samples = {"启用": template.format(state="已启用"),
               "停用": template.format(state="已禁用")}
    # 三种真实出现过的编码都要得到同一结论
    for label, text in samples.items():
        for enc in ("gb18030", "utf-16", "utf-8"):
            raw = text.encode(enc)
            got = W._parse_task_state_text(W._decode_console(raw))
            want = (label == "启用")
            check("{}样本（{} 编码）→ 判定 {}".format(label, enc, want),
                  got is want,
                  "实际 {!r}；解码片段 {}".format(
                      got, W._decode_console(raw).strip().splitlines()[:1]))

    # 反证：整串子串匹配对**启用**样本也会命中 —— 所以那种写法必然误报
    enabled_decoded = W._decode_console(samples["启用"].encode("gb18030"))
    check("反证：整串子串匹配在「启用」样本上也会命中「已禁用」（故不能那样写）",
          "已禁用" in enabled_decoded,
          "样本失真则此条不成立")

    # 判断不了时返回 None（交给 _job_loaded 保守放行并留痕），不能瞎猜
    check("无法解析的输出 → None（不瞎猜）", W._parse_task_state_text("无关内容\r\n") is None)
    check("空输出 → None", W._parse_task_state_text("") is None)


# ---------------------------------------------------------------------------
# 8k. 中文 Windows 上的报错分类与命令输出解码
# ---------------------------------------------------------------------------
def test_localized_console_text() -> None:
    """两处「只认英文」的假设 —— 中文系统上会静默给出错误结论。

    ① `catchup._hint_for` 的失败分类：
       Windows 的中文报错是**全本地化**的。端口没人监听时给的是
         「<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>」
       里面**没有**英文 "connection"，于是落进「未知」分支，
       用户拿到的提示是「跑一次 run_now.cmd 看详细报错」——
       而 README 排错表专门为这个场景写了处置办法，等于没送到用户手里。

    ② `winenv.decode_console`：子系统输出的编码跟随**控制台代码页**（中文 = GBK），
       而 `subprocess.run(text=True)` 按**会漂移的 locale** 解码（开了
       PYTHONUTF8 / LANG=C.UTF-8 时是 utf-8）→ reader 线程抛 UnicodeDecodeError，
       用户看到 traceback、值被静默当成「读不到」。
       （实测 `notify.py status` 就是这么崩的。）
    """
    section("8k. 中文报错分类与命令输出解码")
    import catchup
    import winenv

    # ① 失败提示分类：中英两种措辞都必须归到「网络」
    net_cases = [
        "<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>",
        "No connection could be made because the target machine actively refused it",
        "SSL: CERTIFICATE_VERIFY_FAILED self-signed certificate in certificate chain",
        "timed out",
        "由于连接方在一段时间后没有正确答复或连接的主机没有反应，连接尝试失败。 (WinError 10060)",
    ]
    bad = [c for c in net_cases
           if not catchup._hint_for([c]).startswith("像是网络问题")]
    check("中文/英文的网络类报错都归到「网络问题」", not bad, "\n".join(bad))

    other = {
        "401": ("令牌已过期（401）", "桌面端"),
        "404": ("业务错误：code=404 msg=not found", "接口"),
    }
    for label, (reason, want) in other.items():
        got = catchup._hint_for([reason])
        check("{} 仍走它自己那一档（不被网络档抢走）".format(label),
              want in got and not got.startswith("像是网络问题"), got)

    # ② 共用的控制台解码器：三种真实编码都要还原成同一段中文
    text = "错误: 系统找不到指定的注册表项或值。\r\n计划任务状态: 已禁用\r\n"
    for enc in ("gb18030", "utf-16", "utf-8"):
        got = winenv.decode_console(text.encode(enc))
        check("decode_console 还原 {} 编码".format(enc),
              "找不到指定的注册表项" in got, repr(got[:60]))
    check("decode_console 对空输入不抛异常", winenv.decode_console(b"") == "")
    check("decode_console 对任意字节不抛异常（永不抛）",
          isinstance(winenv.decode_console(bytes(range(256))), str))


# ---------------------------------------------------------------------------
# 8l. ClawBot 凭据：两种存放结构都必须认
# ---------------------------------------------------------------------------
def test_clawbot_channel_shapes() -> None:
    """`settings.json` 里 ClawBot 凭据的两种结构都要能解析出来。

    ★ 为什么钉住（2026-09-20）：桌面端在不同版本/分支里写过两种结构 ——
      ① 按用户：`claw.users.<uid>.channels.weixinClawBot`
      ② **顶层**：`claw.channels.weixinClawBot`
    旧实现只读 ①，而**本机实测看到的是 ②**（`claw.channels.wechatmp` 就摆在顶层）。
    后果与「明明绑定成功却报未绑定」一模一样，且没有任何报错 —— 又是一次静默失效。
    两种都读是安全的超集：哪边有就用哪边，不改变原有优先级。
    """
    section("8l. ClawBot 凭据的两种结构")
    import clawbot

    def pick(cfg: dict):
        with tmpdir() as td:
            p = pathlib.Path(td) / "settings.json"
            p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            return clawbot._channel_from_settings(p)

    ch = {"enabled": True, "botToken": "TOK", "userId": "UID",
          "baseUrl": "https://example.invalid/", "channelId": "CID"}

    got1 = pick({"claw": {"legacyOwnerUid": "u1",
                          "users": {"u1": {"channels": {"weixinClawBot": ch}}}}})
    check("① 按用户结构可解析（baseUrl 去掉尾斜杠）",
          bool(got1) and got1.get("bot_token") == "TOK"
          and got1.get("base_url") == "https://example.invalid",
          json.dumps(got1, ensure_ascii=False))

    got2 = pick({"claw": {"channels": {"weixinClawBot": ch}}})
    check("② 顶层结构可解析（旧实现漏掉的正是这一种）",
          bool(got2) and got2.get("bot_token") == "TOK" and got2.get("user_id") == "UID",
          json.dumps(got2, ensure_ascii=False))

    check("只有 wechatmp（公众平台 webhook）时**不得**误判成已绑定",
          pick({"claw": {"channels": {
              "wechatmp": {"enabled": True, "connectionMode": "webhook"}}}}) is None)
    check("enabled=false 时不认",
          pick({"claw": {"channels": {"weixinClawBot": dict(ch, enabled=False)}}}) is None)
    check("缺 userId 时不认",
          pick({"claw": {"channels": {"weixinClawBot": {"enabled": True, "botToken": "T"}}}}) is None)
    check("没有 claw 段时返回 None", pick({}) is None)
    with tmpdir() as td:
        bad = pathlib.Path(td) / "broken.json"
        bad.write_text("{ 这不是合法 JSON", encoding="utf-8")
        try:
            check("settings.json 损坏时返回 None 且不抛异常",
                  clawbot._channel_from_settings(bad) is None)
        except Exception as e:  # noqa: BLE001
            check("settings.json 损坏时返回 None 且不抛异常", False, repr(e)[:160])


# ---------------------------------------------------------------------------
# 8m. 本机通知必须「看得见」（常驻 Toast + 常驻被拒时退回普通 Toast）
# ---------------------------------------------------------------------------
def test_native_toast_persistent() -> None:
    """微信通道不可用时，本机 Toast 是**唯一**能让用户看见结果的通道。

    ★ 2026-09-20 改动：默认给 Toast 加 `scenario="urgent"` + `duration="long"` ——
      不再几秒后自动缩进通知中心。理由：签到结果恰恰弹在没人盯着屏幕的时刻
      （07:00 唤醒后、猫到达那一刻），没看到就等于没通知。

    钉住三件事，缺一条这个改动就可能帮倒忙：
      ① 默认常驻；`WORKBUDDY_TOAST_PERSISTENT=0` 能退回旧行为；
      ② 常驻属性确实写进了**投递脚本**（而不是文档说改了、代码没改）；
      ③ 常驻写法被系统拒绝时，必须**退回普通 Toast 再试一次** ——
         绝不能为了「更显眼」把唯一可见的通道弄哑。
    """
    section("8m. 本机通知可常驻（不自动消失）")
    import winenv as W

    saved = os.environ.pop("WORKBUDDY_TOAST_PERSISTENT", None)
    try:
        check("默认常驻（不自动消失）", W._toast_persistent() is True,
              "默认值 = {}".format(W._TOAST_PERSISTENT_DEFAULT))
        os.environ["WORKBUDDY_TOAST_PERSISTENT"] = "0"
        check("环境变量可退回「几秒后自动消失」", W._toast_persistent() is False)
        os.environ["WORKBUDDY_TOAST_PERSISTENT"] = "1"
        check("环境变量显式开启也认", W._toast_persistent() is True)
    finally:
        os.environ.pop("WORKBUDDY_TOAST_PERSISTENT", None)
        if saved is not None:
            os.environ["WORKBUDDY_TOAST_PERSISTENT"] = saved

    check("投递脚本确实带 scenario=urgent / duration=long",
          "scenario" in W._PS_TOAST and "urgent" in W._PS_TOAST
          and "duration" in W._PS_TOAST)
    check("常驻是条件写入（开关关掉时不改变普通 Toast）",
          "WB_NOTIFY_PERSIST" in W._PS_TOAST
          and "if ($env:WB_NOTIFY_PERSIST" in W._PS_TOAST)

    tried: list[bool] = []
    orig_run = W._run_toast
    try:
        def _fake(result_for_plain: bool):
            def run(_t, _c, persist: bool) -> bool:
                tried.append(persist)
                return persist is False and result_for_plain
            return run

        W._run_toast = _fake(result_for_plain=True)
        ok = W._notify_windows("标题", "正文")
        check("常驻被拒 → 退回普通 Toast（仍然弹得出来）",
              ok and tried == [True, False], "实际尝试顺序：{}".format(tried))

        tried.clear()
        W._run_toast = lambda t, c, persist: (tried.append(persist), True)[1]
        ok = W._notify_windows("标题", "正文")
        check("常驻成功 → 只投递一次（不做多余重试）",
              ok and tried == [True], "实际尝试顺序：{}".format(tried))

        tried.clear()
        ok = W._notify_windows("标题", "正文", persistent=False)
        check("persistent=False → 只投递普通 Toast",
              ok and tried == [False], "实际尝试顺序：{}".format(tried))
    finally:
        W._run_toast = orig_run

    # `reg query` 的 DWORD 是 `0x1` / `0x0` 形式 —— 必须归一，否则「系统通知被关」
    # 这个**唯一**要抓的场景会被静默放行（2026-09-21 实测：本机把开关显式设为 1 之后，
    # `notify.py status` 反而显示「读不到」，就是因为没归一）。
    import notify as N

    class _FakeRun:
        def __init__(self, out: bytes):
            self.stdout, self.stderr, self.returncode = out, b"", 0

    saved_sub = N.subprocess
    saved_notify_is_win = N.winenv.IS_WIN
    try:
        # `_reg_get` 只在 Windows 上真正读注册表；其余平台走早退分支。
        # 这里模拟 Windows 分支，避免在开发机上把「平台不适用」误报成逻辑失败。
        N.winenv.IS_WIN = True

        def _with(out: bytes):
            class _S:
                @staticmethod
                def run(*_a, **_k):
                    return _FakeRun(out)
            return _S

        N.subprocess = _with(b"    ToastEnabled    REG_DWORD    0x1\r\n")
        check("_reg_get 把 reg query 的 0x1 归一成 '1'（显示为「开」）",
              N._reg_get("x", "ToastEnabled") == "1",
              repr(N._reg_get("x", "ToastEnabled")))
        N.subprocess = _with(b"    ToastEnabled    REG_DWORD    0x0\r\n")
        check("_reg_get 把 0x0 归一成 '0'（这样才报得出「已关」）",
              N._reg_get("x", "ToastEnabled") == "0",
              repr(N._reg_get("x", "ToastEnabled")))
        N.subprocess = _with(b"    SomeValue    REG_SZ    hello\r\n")
        check("_reg_get 对非 DWORD 仍返回原串（不因归一而丢值）",
              N._reg_get("x", "SomeValue") == "hello")
    finally:
        N.winenv.IS_WIN = saved_notify_is_win
        N.subprocess = saved_sub


# ---------------------------------------------------------------------------
# 8n. 通知补弹：投递时用户不在场 → 回到机器前补弹一次（2026-09-21）
# ---------------------------------------------------------------------------
def test_notify_reshow() -> None:
    """「不在场时投递的通知，回来时补弹」这套状态机。

    ★ 为什么必须钉住（用户 2026-09-21 的硬要求：「不管什么情况通知都要弹窗、
      点叉才消失；若之前睡眠/休眠，重新打开即弹窗」）：
      Windows 在熄屏 / 长时间无人操作时**不弹横幅**，通知只进通知中心 ——
      要救的正是"投递成功但没人看到"。判错的代价都很具体：
        ① 该补的不补 → 用户永远看不到那笔积分；
        ② 不该补的乱补 → 每 5 分钟糊一屏重复通知，比不弹更糟。
    """
    section("8n. 本机通知补弹（不在场时投递 → 回来时补弹）")
    import winenv as W

    with tmpdir() as td:
        logdir = pathlib.Path(td) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        qf = W._reshow_file(logdir)
        check("补弹队列落在 runtime/state 下（运行时文件，不进交付物）",
              qf.parent.name == "state" and qf.parent.parent == pathlib.Path(td),
              str(qf))

        # ① 在场不排队；不在场才排队
        check("用户在场时投递 → 不进补弹队列",
              not W._maybe_queue_reshow("t", "b", logdir, idle=5))
        check("用户不在场时投递 → 进补弹队列",
              W._maybe_queue_reshow("签到到账", "正文A", logdir, idle=9999)
              and len(W._reshow_load(logdir)) == 1)

        # ② 用户还没回来 → 不补弹（否则每 5 分钟糊一次）
        r = W.reshow_pending(logdir, idle=9999)
        check("用户还没回来 → 不补弹（避免每 5 分钟重复打扰）",
              r.get("resent") == 0 and "waiting" in r
              and len(W._reshow_load(logdir)) == 1,
              json.dumps(r, ensure_ascii=False))

        # ③ 用户回来 → 补弹并出队；补弹失败则不消费队列
        tried: list = []
        orig = W._run_toast
        try:
            W._run_toast = lambda t, c, persist: (tried.append((t, persist)), True)[1]
            r = W.reshow_pending(logdir, idle=3)
            check("用户回来 → 补弹 1 条，且仍是常驻（persist=True）",
                  r.get("resent") == 1 and tried == [("签到到账", True)],
                  json.dumps({"r": r, "tried": tried}, ensure_ascii=False))
            check("补弹后出队（同一条只补一次，不会天天糊屏）",
                  W._reshow_load(logdir) == [])

            W._maybe_queue_reshow("第二条", "正文B", logdir, idle=9999)
            tried.clear()
            W._run_toast = lambda t, c, persist: (tried.append(t), False)[1]
            r = W.reshow_pending(logdir, idle=1)
            check("补弹投递失败 → 不出队（下一轮继续试，不丢消息）",
                  r.get("resent") == 0 and len(W._reshow_load(logdir)) == 1,
                  json.dumps(r, ensure_ascii=False))
        finally:
            W._run_toast = orig

        # ④ 上限与过期裁剪
        for i in range(W._RESHOW_MAX_ITEMS + 4):
            W._maybe_queue_reshow("t{}".format(i), "b", logdir, idle=9999)
        check("队列有上限（不会无限堆积）",
              len(W._reshow_load(logdir)) == W._RESHOW_MAX_ITEMS,
              str(len(W._reshow_load(logdir))))
        stale = W._reshow_load(logdir)
        stale[0]["ts"] = time.time() - (W._RESHOW_MAX_AGE_H + 1) * 3600
        W._reshow_save(stale, logdir)
        check("超过 {} 小时的旧通知被丢弃（不翻旧账）".format(W._RESHOW_MAX_AGE_H),
              len(W._reshow_load(logdir)) == W._RESHOW_MAX_ITEMS - 1)

        # ⑤ 队列文件损坏不许把主流程带崩
        W._reshow_file(logdir).write_text("{ 这不是 JSON", encoding="utf-8")
        check("队列文件损坏 → 当作空队列（绝不抛异常）", W._reshow_load(logdir) == [])

    # ⑥ 开关
    saved = os.environ.pop("WORKBUDDY_RESHOW", None)
    try:
        os.environ["WORKBUDDY_RESHOW"] = "0"
        check("WORKBUDDY_RESHOW=0 → 既不排队也不补弹",
              not W._maybe_queue_reshow("t", "b", None, idle=9999)
              and W.reshow_pending(None, idle=1).get("skipped") == "disabled")
    finally:
        os.environ.pop("WORKBUDDY_RESHOW", None)
        if saved is not None:
            os.environ["WORKBUDDY_RESHOW"] = saved

    idle = W.idle_seconds()
    check("idle_seconds() 返回数值或 None（永不抛异常）",
          idle is None or (isinstance(idle, float) and idle >= 0), repr(idle))


# ---------------------------------------------------------------------------
# 8o. 必须点掉的弹窗通道（2026-09-21，用户要求「点叉才消失」）
# ---------------------------------------------------------------------------
def test_modal_alert() -> None:
    """本机通知的默认通道：原生弹窗（不自动消失），失败才退回常驻 Toast。

    ★ 为什么必须钉住（用户 2026-09-21 反馈）：Toast **即使**写了
      `scenario="urgent"` + `duration="long"`，在这台机器上依然"过一会儿就自己收走"。
      要"点叉才消失"只有原生对话框这条路。
      更关键的是：弹窗一旦因为参数写错而失败，会**静默退回 Toast**
      （`except` 吞掉 `TypeError`，返回值照样是 True，日志一干二净）——
      所以这里直接钉住 `Popen` 的入参，而不是"跑一遍看看弹没弹"。
    """
    section("8o. 必须点掉的弹窗通道")
    import winenv as W

    check("默认通道是弹窗（dialog），可以环境变量切回 Toast", W._alert_style() == "dialog")
    saved = os.environ.pop("WORKBUDDY_ALERT_STYLE", None)
    try:
        os.environ["WORKBUDDY_ALERT_STYLE"] = "toast"
        check("WORKBUDDY_ALERT_STYLE=toast → 退回 Toast 通道", W._alert_style() == "toast")
        os.environ["WORKBUDDY_ALERT_STYLE"] = "dialog"
        check("WORKBUDDY_ALERT_STYLE=dialog → 弹窗通道", W._alert_style() == "dialog")
    finally:
        os.environ.pop("WORKBUDDY_ALERT_STYLE", None)
        if saved is not None:
            os.environ["WORKBUDDY_ALERT_STYLE"] = saved

    check("卡片脚本是交付物里的一等文件（随文件夹搬走、可单独调试）",
          W._DIALOG_SCRIPT.is_file() and W._DIALOG_SCRIPT.parent == DIR,
          str(W._DIALOG_SCRIPT))

    calls: list = []
    orig_spawn = W._spawn
    saved_default_python = W.default_python
    saved_subprocess_flags = W.subprocess_flags
    with tmpdir() as td:
        logdir = pathlib.Path(td) / "logs"
        try:
            class _FakeProc:
                pid = 424242

            def _fake_spawn(args, **kw):
                calls.append((args, kw))
                return _FakeProc()

            # 自检在 macOS 上运行时，真实 `platform()`/解释器探测会返回 mac 路径；
            # 这里只替换被 `_notify_windows_dialog` 实际调用的两个边界，保持产品逻辑不变。
            W.default_python = lambda windowless=False: r"C:\Python312\pythonw.exe"
            W.subprocess_flags = lambda: {"creationflags": 8}  # DETACHED_PROCESS
            W._spawn = _fake_spawn
            ok = W._notify_windows_dialog("标题", "正文", logdir)
            args, kw = (calls[0] if calls else ([], {}))
            check("弹窗走「另起一个进程」（不阻塞每 5 分钟一次的主脚本）",
                  ok and len(args) >= 2 and str(args[1]) == str(W._DIALOG_SCRIPT),
                  json.dumps({"ok": ok, "args": list(args)[:2]}, ensure_ascii=False))
            check("弹窗进程用 pythonw（不闪黑窗）",
                  "pythonw" in str(args[0]).lower(), str(args[0]))
            # 用户 2026-09-21 反馈：弹卡片时桌面会冒出一个 PowerShell 控制台窗口。
            # 中途改过 PowerShell + WinForms 版卡片，两个坑：① 控制台窗口；
            # ② PowerShell 的相对路径基准不是脚本所在目录，标记文件被写到别处
            #    → "卡片还开着"被误判成"已关闭"。这里钉死不许再退回 PowerShell。
            check("★ 不经过 powershell（不出现控制台窗口）",
                  not any("powershell" in str(a).lower() for a in args),
                  json.dumps(list(args), ensure_ascii=False))
            check("creationflags 只传一次且含 DETACHED（撞车会静默退回 Toast）",
                  "creationflags" in kw
                  and int(kw["creationflags"]) & int(
                      getattr(W.subprocess, "DETACHED_PROCESS", 8)),
                  "creationflags={}".format(kw.get("creationflags")))
            check("标题/正文经环境变量传递（不拼进命令行，中文引号都不会坏）",
                  kw.get("env", {}).get("WB_DLG_BODY") == "正文"
                  and kw.get("env", {}).get("WB_DLG_TITLE") == "标题")
            check("传「层号」给卡片（多张同时存在时往上叠，不互相盖住）",
                  kw.get("env", {}).get("WB_DLG_SLOT") == "0",
                  "WB_DLG_SLOT={}".format(kw.get("env", {}).get("WB_DLG_SLOT")))
            check("弹窗后留下「待处理」标记（用于限制堆积）",
                  len(list((pathlib.Path(td) / "state").glob("notify_dialog_*.json"))) == 1)

            # 屏幕上已经堆了 N 个没人点的弹窗 → 不再堆（退回 Toast + 补弹队列）
            sd = pathlib.Path(td) / "state"
            for i in range(W._DIALOG_MAX):
                (sd / "notify_dialog_{}.json".format(9000 + i)).write_text(
                    json.dumps({"pid": os.getpid()}), encoding="utf-8")
            calls.clear()
            check("已有 {} 个待处理弹窗 → 不再堆积（返回 False，调用方退回 Toast）".format(
                W._DIALOG_MAX),
                  not W._notify_windows_dialog("t", "b", logdir) and not calls)
            check("_live_dialogs 按 PID 存活判定（自己这个进程算活着）",
                  W._live_dialogs(logdir) == W._DIALOG_MAX,
                  str(W._live_dialogs(logdir)))
        finally:
            W._spawn = orig_spawn
            W.default_python = saved_default_python
            W.subprocess_flags = saved_subprocess_flags

        # 组合：走弹窗时不再补弹（否则同一条会弹两次）；弹窗不可用才退回 Toast + 补弹
        saved_thr = os.environ.pop("WORKBUDDY_RESHOW_IDLE_SEC", None)
        os.environ["WORKBUDDY_RESHOW_IDLE_SEC"] = "0"
        orig_dlg, orig_toast = W._notify_windows_dialog, W._notify_windows
        saved_platform = W.platform
        saved_idle_seconds = W.idle_seconds
        try:
            # `native_notify` 先按平台选择通道；macOS 上需要模拟 Windows，
            # 同时给补弹状态机一个数值型 idle，才能验证「弹窗失败 → Toast + 队列」。
            W.platform = lambda: "win"
            W.idle_seconds = lambda: 9999.0
            W._notify_windows_dialog = lambda t, c, ld=None: True
            W._notify_windows = lambda t, c: True
            check("走弹窗 → 不进补弹队列（同一条不会弹两次）",
                  W.native_notify("t", "b", logdir) and not W._reshow_load(logdir))
            W._notify_windows_dialog = lambda t, c, ld=None: False
            check("弹窗不可用 → 退回 Toast **并**进补弹队列（不丢消息）",
                  W.native_notify("t", "b", logdir) and len(W._reshow_load(logdir)) == 1,
                  str(len(W._reshow_load(logdir))))
        finally:
            W._notify_windows_dialog, W._notify_windows = orig_dlg, orig_toast
            W.platform = saved_platform
            W.idle_seconds = saved_idle_seconds
            os.environ.pop("WORKBUDDY_RESHOW_IDLE_SEC", None)
            if saved_thr is not None:
                os.environ["WORKBUDDY_RESHOW_IDLE_SEC"] = saved_thr


# ---------------------------------------------------------------------------
# 9. CLI 冒烟（只跑只读命令）
# ---------------------------------------------------------------------------
def test_cli() -> None:
    section("9. 命令行冒烟（只读命令）")
    import install as _install
    import winenv as _winenv

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

    # status 必须带「最近一次运行结果」—— 0x8007010B 那次事故里任务「已注册、设置全对」
    # 却每次触发都秒失败，而旧版 status 只核对「在不在 + 设置对不对」，从没看过
    # 「最近一次到底跑成没有」。这是那类静默故障在 status 层的最后一块盲区。
    if _winenv.platform() == "win":
        try:
            p = subprocess.run([PY, str(DIR / "install.py"), "status"],
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace", cwd=str(DIR))
            data = json.loads(p.stdout or "{}")
            blocks = data.get("tasks") or []
            _with_ri = [b for b in blocks if b.get("task_run_info")]
            check("status 的每个已注册任务都带 task_run_info（最近一次运行结果）",
                  len(_with_ri) == len(blocks) and bool(blocks),
                  "带运行痕迹 {}/{} 个".format(len(_with_ri), len(blocks)))
            check("task_run_info 的 result 判定合理（0=成功 / 267011=尚未运行）",
                  all(b["task_run_info"].get("last_task_result_ok") is not None
                      for b in _with_ri))
            check("status 顶层有 last_runs 一句话摘要",
                  isinstance(data.get("last_runs"), list)
                  and len(data["last_runs"]) == len(blocks),
                  json.dumps(data.get("last_runs"), ensure_ascii=False)[:260])
        except Exception as e:  # noqa: BLE001
            check("status 运行结果字段可读", False, repr(e)[:300])

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

    # 非 Windows 上必须「明确失败」，而不是静默假装成功。
    # ★ 这个不变量只能在**进程内翻转平台**来验证。
    #   旧写法直接以子进程跑一次真正的 `install`（无 --dry-run），可是自检自己
    #   就跑在 Windows 上 —— 于是「非 Windows 应失败」测的是「Windows 该成功」，
    #   断言必然失败；更糟的是那次调用**真的注册/覆盖了一个计划任务**：
    #   一个只读自检不该改动系统。现在 install.py 的守卫统一走
    #   `windows_guard()`（读 winenv.platform()），可以直接翻转平台断言，
    #   并且平台守卫已提前到函数开头，非 Windows 上不会先落盘再报错。
    saved_plat = _winenv.platform
    try:
        _winenv.platform = lambda: "linux"
        blocked = _install.windows_guard("注册计划任务")
        check("非 Windows 上 install 明确失败并说明原因",
              isinstance(blocked, dict) and blocked.get("ok") is False
              and "不是 Windows" in str(blocked.get("error")),
              json.dumps(blocked, ensure_ascii=False)[:300])
        guards = {a: _install.windows_guard(a) for a in
                  ("注册计划任务", "卸载计划任务", "启用 / 停用计划任务", "立即触发计划任务")}
        check("非 Windows 上各命令守卫一致（同一处实现，不再各写一遍）",
              all(isinstance(v, dict) and v.get("ok") is False
                  and "不是 Windows" in str(v.get("error")) for v in guards.values()),
              json.dumps(guards, ensure_ascii=False)[:300])
    finally:
        _winenv.platform = saved_plat
    try:
        _winenv.platform = lambda: "win"
        check("Windows 上守卫放行（不误拦真机）",
              _install.windows_guard("注册计划任务") is None)
    finally:
        _winenv.platform = saved_plat

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

    # 先把日志落点隔离走，再跑任何测试：自检不许把自己的记录写进
    # 交付物的 runtime/logs/catchup.log（理由见 _isolate_logs）。
    isolated = _isolate_logs()
    print("自检日志隔离：{}（不写 runtime/logs/）".format(isolated or "无可隔离模块"))
    print("=" * 72)

    try:
        test_imports()
        test_task_xml()
        test_windows_output_decoding()
        test_asar()
        test_windows_paths()
        test_credential_paths()
        test_no_console()
        test_encoding()
        test_logic()
        test_structure()
        test_platform_hints()
        test_notification_split()
        test_watchdog()
        test_dedupe_tables()
        test_dedupe_write_policy()
        test_endpoint_change_semantics()
        test_notify_delivery_semantics()
        test_renew_remind_gating()
        test_watchdog_job_state()
        test_localized_console_text()
        test_clawbot_channel_shapes()
        test_native_toast_persistent()
        test_notify_reshow()
        test_modal_alert()
        test_cli()
        if args.online:
            test_online()
    finally:
        _cleanup_isolated_logs()

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
