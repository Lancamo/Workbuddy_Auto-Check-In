#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install.py — 注册 / 卸载 / 查看 Windows 计划任务（Task Scheduler）

这是 macOS 版 `com.workbuddy.wb-reward-catchup.plist`（LaunchAgent）的 Windows 等价物。

    macOS                                  Windows
    ─────────────────────────────────────  ─────────────────────────────────────────
    ~/Library/LaunchAgents/*.plist         计划任务「WorkBuddyRewardCatchup」
    launchctl bootstrap                    schtasks /Create /XML
    RunAtLoad=true                         LogonTrigger（登录后延迟 1 分钟跑一次）
    StartInterval=300                      CalendarTrigger + Repetition PT5M
    launchd 唤醒后补发过期任务              StartWhenAvailable=true ← 等价物
    （launchd 无对应项）                    DisallowStartIfOnBatteries=false ← 必须显式关，
                                           否则笔记本一拔电源任务就静默不跑

★ 三个容易踩的坑（都在本文件里处理掉了）
  1. **默认「仅在使用交流电时启动」** —— Windows 计划任务的默认值是 true，
     笔记本用电池时任务会被静默跳过，且不会报错。必须显式设 false。
  2. **默认不补跑错过的触发** —— `StartWhenAvailable` 默认 false。
     不打开就退化成「必须整点开机才跑」，比 mac 版弱得多。必须设 true。
  3. **cmd/PowerShell 的中文与引号** —— 任务 XML 用 **UTF-16 编码**写出，
     彻底避开代码页问题；解释器与脚本路径**一律带引号**（路径里常含空格与中文）。
     执行体用 `pythonw.exe`（无控制台）→ 不闪黑窗，输出自动落到 `logs/stdio.log`。

命令
----
    python install.py install            # 注册两个计划任务（主任务 + watchdog）并跑一次自检
    python install.py install --dry-run  # 只生成 XML 并打印，不注册
    python install.py install --interval 15
    python install.py install --no-watchdog        # 只装主任务
    python install.py uninstall          # 卸载（两个任务一起删）
    python install.py status             # 查看两个任务的定义与最近运行痕迹
    python install.py run                # 立即手动跑一次（前台，看得到输出）
    python install.py enable / disable   # 启用 / 停用（不删除）
    python install.py trigger            # 让主任务立即执行一次（验证任务本身可运行）

★ 会注册**两个**任务（对应 mac 版的 plist 也是两个）
  1. WorkBuddyRewardCatchup   —— 每 5 分钟；真正干活的主脚本
  2. WorkBuddyRewardWatchdog  —— 每 30 分钟；只盯主脚本还活着没，异常时弹本机通知
  watchdog 存在的意义：主脚本的所有告警都以「它自己跑起来了」为前提。若计划任务
  被停用/删除、Python 被卸载、项目目录被移走，主脚本会**连告警机制一起静默死掉**。
  唯一出路就是一个完全独立的第二个任务（不共享任何代码）来盯它。

本文件不依赖 Windows —— 在 mac/linux 上也能跑 `install.py install --dry-run`
查看生成的 XML，便于先在 mac 上审阅（自检脚本 selftest.py 就是这么做的）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import xml.etree.ElementTree as ET

DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))

import winenv  # noqa: E402
import paths   # noqa: E402

DEFAULT_TASK_NAME = "WorkBuddyRewardCatchup"
# 5 分钟：与 mac 版 plist 的 StartInterval=300 对齐。
# 为什么这么密：触发周期的唯一价值是「事件发生后多快被发现」；闸门未开时
# 只做本地判断就退出、零网络请求，所以代价极小，换来「到达即领」的精度。
DEFAULT_INTERVAL_MIN = 5
TASK_XML = paths.cache_path("task.xml")  # 注册时实际提交的 XML（UTF-16），留档便于人工核对
ENTRY = DIR / "catchup.py"
LOG = paths.log_path("catchup.log")

# ── 第二个计划任务：watchdog（对应 mac 版 com.workbuddy.wb-reward-watchdog.plist）──
# 它盯的是「主脚本还活着没」，所以必须与主任务**完全独立**：不同的脚本、
# 不同的任务名、不同的触发周期。30 分钟一次的采样，足够在 90 分钟心跳阈值附近
# 及时报警，又不会因为自己太频繁而变成噪音源。
WATCHDOG_TASK_NAME = "WorkBuddyRewardWatchdog"
WATCHDOG_ENTRY = DIR / "watchdog.py"
WATCHDOG_XML = paths.cache_path("task_watchdog.xml")
DEFAULT_WATCHDOG_INTERVAL_MIN = 30
WATCHDOG_LOG = paths.log_path("watchdog.log")


def migrate_runtime() -> None:
    """Move legacy root-level state/config/log files into runtime/ once."""
    paths.ensure_dirs()
    for name in ("state.json", "state.bak.json", "catchup.lock",
                 "notify_state.json", "renew_state.json", "watchdog_state.json",
                 "clawbot_state.json"):
        src, dst = DIR / name, paths.STATE_DIR / name
        if src.is_file() and not dst.exists():
            src.replace(dst)
    for name in ("notify_config.json", "renew_config.json"):
        src, dst = DIR / name, paths.CONFIG_DIR / name
        if src.is_file() and not dst.exists():
            src.replace(dst)
    for name in ("catchup.log", "watchdog.log", "stdio.log",
                 "notify_fallback.log"):
        src, dst = DIR / name, paths.LOG_DIR / name
        if src.is_file() and not dst.exists():
            src.replace(dst)


# ---------------------------------------------------------------------------
# 任务 XML
# ---------------------------------------------------------------------------
def _current_user_id() -> str:
    """返回 `DOMAIN\\user` 形式；取不到返回空串（空则 XML 里省略 UserId，作用于当前用户）。"""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if user and domain:
        return "{}\\{}".format(domain, user)
    return user


def build_task_xml(*, python_exe: str, script: pathlib.Path,
                   workdir: pathlib.Path, interval_min: int = DEFAULT_INTERVAL_MIN,
                   name: str = DEFAULT_TASK_NAME,
                   start_boundary: str | None = None,
                   user_id: str | None = None,
                   desc: str | None = None) -> str:
    """生成计划任务 XML（字符串）。纯函数，可在任何平台上测试。

    参数
      python_exe      执行体（建议 `pythonw.exe`：无控制台、不闪窗）
      script          要跑的脚本（catchup.py 绝对路径）
      workdir         工作目录（= 项目目录；脚本内也用 __file__ 定位，双保险）
      interval_min    触发间隔（分钟）
      name            任务名
      start_boundary  重复触发的起点，默认今天 00:00:00
      user_id         `DOMAIN\\user`，None/空 则省略该元素
      desc            任务描述；None 则用主任务的默认描述
    """
    today = datetime.date.today().strftime("%Y-%m-%dT00:00:00")
    sb = start_boundary or today
    uid = user_id if user_id is not None else _current_user_id()
    if desc is None:
        desc = ("WorkBuddy 积分自动签到 / 旅行领奖补跑器。每 {interval} 分钟检查一次；"
                "07:00 后签到并派遣、猫到达即领且各自推送；当日已完成则空转、零网络请求。"
                "错过的时间窗在开机/唤醒后自动补跑。").format(interval=int(interval_min))

    def esc(s: str) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    user_xml = ""
    principal_user_xml = ""
    if uid:
        user_xml = "      <UserId>{}</UserId>\n".format(esc(uid))
        principal_user_xml = "      <UserId>{}</UserId>\n".format(esc(uid))

    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{date}</Date>
    <Author>{author}</Author>
    <Description>{desc}</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <!-- 登录后延迟 1 分钟跑一次（等网络与桌面端就绪） -->
    <LogonTrigger>
      <Enabled>true</Enabled>
{user_xml}      <Delay>PT1M</Delay>
    </LogonTrigger>
    <!-- 每 {interval} 分钟一次；不写 Duration = 无限重复 -->
    <CalendarTrigger>
      <StartBoundary>{sb}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT{interval}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
{principal_user_xml}      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <!-- 上一次还没跑完就不再叠加新的实例 -->
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <!-- ★ 必须 false：默认 true 会让笔记本用电池时静默跳过 -->
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <!-- ★ 必须 true：睡眠/关机期间错过的触发，恢复后立即补跑（= launchd 的补发） -->
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT10M</Interval>
      <Count>2</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>"{python}"</Command>
      <Arguments>"{script}"</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".format(
        date=datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        author=esc(_current_user_id() or "WorkBuddy 积分助手"),
        interval=int(interval_min),
        name=esc(name),
        user_xml=user_xml,
        principal_user_xml=principal_user_xml,
        sb=esc(sb),
        desc=esc(desc),
        python=esc(python_exe),
        script=esc(str(script)),
        workdir=esc(str(workdir)),
    )


def write_task_xml(xml: str, path: pathlib.Path = TASK_XML) -> pathlib.Path:
    """把 XML 以 UTF-16（带 BOM）写出 —— schtasks 读得最稳、且中文不会乱码。"""
    path.write_bytes(xml.encode("utf-16"))
    return path


# ---------------------------------------------------------------------------
# schtasks 封装
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
        env=winenv.subprocess_env(), **winenv.subprocess_flags(),
    )


def task_query_xml(name: str) -> tuple[bool, str]:
    """查询任务定义（XML 输出与系统语言无关，便于程序解析）。"""
    try:
        p = _run(["schtasks", "/Query", "/TN", name, "/XML"])
    except Exception as e:  # noqa: BLE001
        return False, "查询失败：" + str(e)[:160]
    if p.returncode != 0:
        return False, ((p.stderr or p.stdout or "").strip()[:400] or "任务不存在")
    return True, p.stdout or ""


def task_raw_query(name: str) -> str:
    """原始 LIST 输出（含"上次运行时间/结果"，但字段名随系统语言变化，仅供人工看）。"""
    try:
        p = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"])
    except Exception as e:  # noqa: BLE001
        return "查询失败：" + str(e)[:160]
    return (p.stdout or p.stderr or "").strip()


def install_task(name: str, xml_path: pathlib.Path) -> tuple[bool, str]:
    """注册（已存在则覆盖）。"""
    try:
        p = _run(["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"])
    except Exception as e:  # noqa: BLE001
        return False, "注册失败：" + str(e)[:200]
    ok = p.returncode == 0
    msg = (p.stdout or "").strip() or (p.stderr or "").strip()
    return ok, msg[:500]


def delete_task(name: str) -> tuple[bool, str]:
    try:
        p = _run(["schtasks", "/Delete", "/TN", name, "/F"])
    except Exception as e:  # noqa: BLE001
        return False, "删除失败：" + str(e)[:200]
    return p.returncode == 0, ((p.stdout or "").strip() or (p.stderr or "").strip())[:400]


def change_task(name: str, enable: bool) -> tuple[bool, str]:
    flag = "/ENABLE" if enable else "/DISABLE"
    try:
        p = _run(["schtasks", "/Change", "/TN", name, flag])
    except Exception as e:  # noqa: BLE001
        return False, "操作失败：" + str(e)[:200]
    return p.returncode == 0, ((p.stdout or "").strip() or (p.stderr or "").strip())[:400]


def run_task_now(name: str) -> tuple[bool, str]:
    try:
        p = _run(["schtasks", "/Run", "/TN", name], timeout=90)
    except Exception as e:  # noqa: BLE001
        return False, "触发失败：" + str(e)[:200]
    return p.returncode == 0, ((p.stdout or "").strip() or (p.stderr or "").strip())[:400]


# ---------------------------------------------------------------------------
# 自检 / 状态
# ---------------------------------------------------------------------------
def _tail(path: pathlib.Path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def run_entry_now() -> dict:
    """前台跑一次 catchup.py（用户手动验证用，输出能直接看到）。"""
    py = winenv.default_python(windowless=False)
    try:
        p = subprocess.run(
            [py, str(ENTRY)], capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace", cwd=str(DIR),
            env=winenv.subprocess_env(), **winenv.subprocess_flags(),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    out = (p.stdout or "").strip()
    summary = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                summary = json.loads(line)
                break
            except Exception:  # noqa: BLE001
                continue
    return {"ok": p.returncode == 0, "returncode": p.returncode,
            "stdout_tail": out[-1200:], "stderr_tail": (p.stderr or "")[-800:],
            "summary": summary}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _task_specs(args: argparse.Namespace) -> list[dict]:
    """要注册/查询的任务清单：主任务 + watchdog（`--no-watchdog` 可去掉后者）。

    两个任务共用同一份 XML 生成逻辑，只是脚本、任务名、间隔、描述不同 ——
    这保证 watchdog 也自动获得 LogonTrigger / StartWhenAvailable / 电池可跑 /
    无控制台窗口这些「不设就静默失效」的关键设置。
    """
    out_xml = pathlib.Path(args.xml_out) if args.xml_out else TASK_XML
    specs = [{
        "kind": "main",
        "name": args.name,
        "script": ENTRY,
        "interval": args.interval,
        "xml": out_xml,
        "desc": None,
        "log": LOG,
    }]
    if not getattr(args, "no_watchdog", False):
        wname = getattr(args, "watchdog_name", WATCHDOG_TASK_NAME)
        winterval = getattr(args, "watchdog_interval", DEFAULT_WATCHDOG_INTERVAL_MIN)
        specs.append({
            "kind": "watchdog",
            "name": wname,
            "script": WATCHDOG_ENTRY,
            "interval": winterval,
            "xml": (out_xml.with_name("task_watchdog.xml") if args.xml_out
                    else WATCHDOG_XML),
            "desc": ("WorkBuddy 积分脚本存活监控（watchdog）。每 {} 分钟检查一次主任务"
                     "是否仍在运行、state.json 心跳是否新鲜；异常时只用本机通知报警。"
                     "刻意独立于主脚本，是「主脚本静默死掉」的唯一出路。").format(
                         int(winterval)),
            "log": WATCHDOG_LOG,
        })
    return specs


def cmd_install(args: argparse.Namespace) -> int:
    migrate_runtime()
    if args.python:
        python_exe = args.python
    else:
        python_exe = winenv.default_python(windowless=True)
    if not pathlib.Path(python_exe).is_file():
        _print({"ok": False,
                "error": "解释器不存在：{}".format(python_exe),
                "hint": "用 --python 指定完整路径，例如 --python \"C:\\Python312\\pythonw.exe\""})
        return 1

    results: list[dict] = []
    for sp in _task_specs(args):
        item: dict = {"kind": sp["kind"], "task_name": sp["name"],
                      "python": python_exe, "interval_min": sp["interval"]}
        if not sp["script"].is_file():
            item.update({"ok": False, "error": "找不到脚本：{}".format(sp["script"])})
            results.append(item)
            continue

        xml = build_task_xml(python_exe=python_exe, script=sp["script"], workdir=DIR,
                             interval_min=sp["interval"], name=sp["name"], desc=sp["desc"])
        # 先在内存里解析一遍，保证 XML 合法（不合法的 XML 会让 schtasks 报难懂的错）
        try:
            ET.fromstring(xml)
        except ET.ParseError as e:
            item.update({"ok": False, "error": "生成的 XML 不合法：" + str(e)})
            results.append(item)
            continue

        path = write_task_xml(xml, sp["xml"])
        item["xml_file"] = str(path)

        if args.dry_run:
            item.update({"ok": True, "dry_run": True, "xml_preview": xml[:1600]})
            results.append(item)
            continue

        if not winenv.IS_WIN:
            item.update({"ok": False,
                         "error": "当前不是 Windows，无法注册计划任务（已生成 XML 供审阅）"})
            results.append(item)
            continue

        ok, msg = install_task(sp["name"], path)
        item["ok"] = ok
        item["schtasks"] = msg
        if ok and sp["kind"] == "main":
            # 立即跑一次，让 state.json / 日志 一次到位（也是最好的自检）
            item["first_run"] = run_entry_now()
        results.append(item)

    main_res = next((r for r in results if r.get("kind") == "main"), None)
    wd_res = next((r for r in results if r.get("kind") == "watchdog"), None)
    all_ok = bool(results) and all(r.get("ok") for r in results)

    if main_res is None:
        _print({"ok": False, "error": "找不到入口脚本：{}".format(ENTRY),
                "tasks": results})
        return 1

    # 顶层保持向后兼容（沿用主任务的字段），watchdog 额外挂在 tasks / watchdog 下
    out = dict(main_res)
    out["dry_run"] = bool(args.dry_run)
    out["ok"] = all_ok
    out["tasks"] = results
    out["watchdog"] = wd_res

    if not all_ok:
        out.setdefault("hint", ("注册失败。常见原因：① 任务名已存在且被占用 → 先 uninstall；"
                                "② 无权限 → 用管理员身份的终端重试；"
                                "③ 执行体路径含中文且引号被吃掉 → 检查 task.xml。"))
        _print(out)
        return 1

    if not args.dry_run:
        out["next_step"] = ("安装完成。主任务每 {} 分钟跑一次，watchdog 每 {} 分钟跑一次，"
                            "登录时各跑一次。可随时用 `python install.py status` 查看。"
                            ).format(args.interval, getattr(args, "watchdog_interval",
                                                           DEFAULT_WATCHDOG_INTERVAL_MIN))
    _print(out)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not winenv.IS_WIN:
        _print({"ok": False, "error": "当前不是 Windows"})
        return 1
    names = [args.name]
    if not getattr(args, "no_watchdog", False):
        names.append(getattr(args, "watchdog_name", WATCHDOG_TASK_NAME))
    tasks = []
    for n in names:
        ok, msg = delete_task(n)
        tasks.append({"task_name": n, "ok": ok, "schtasks": msg})
    # 两个任务里至少删掉一个就算成功；都没删掉（例如本来就没装）也能接受，
    # 因为 uninstall 的语义是「确保不再自动触发」，是幂等的。
    any_ok = any(t["ok"] for t in tasks)
    _print({"ok": any_ok, "tasks": tasks,
            "note": "项目文件未被删除，只是取消了自动触发。"})
    return 0 if any_ok else 1


_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _task_block(name: str, xml_path: pathlib.Path, log_path: pathlib.Path) -> dict:
    """单个计划任务的状态快照（供 status / 自检复用）。"""
    exists, xml_or_err = task_query_xml(name) if winenv.IS_WIN else (False, "非 Windows 环境")
    blk: dict = {
        "task_name": name,
        "task_registered": bool(exists),
        "xml_archive": str(xml_path),
        "xml_archive_exists": xml_path.is_file(),
        "log_file": str(log_path),
        "log_exists": log_path.is_file(),
        "log_mtime": (datetime.datetime.fromtimestamp(log_path.stat().st_mtime)
                      .strftime("%Y-%m-%d %H:%M:%S") if log_path.is_file() else None),
        "log_tail": _tail(log_path, 10),
    }
    if exists:
        # 从 XML 解析出关键设置（与系统语言无关），逐项核对是否真的生效
        try:
            root = ET.fromstring(xml_or_err)

            def txt(p, d=None):
                e = root.find(p, _NS)
                return e.text if (e is not None and e.text) else d
            blk["task_settings"] = {
                "interval": txt(".//t:Repetition/t:Interval"),
                "start_when_available": txt(".//t:StartWhenAvailable"),
                "disallow_start_if_on_batteries": txt(".//t:DisallowStartIfOnBatteries"),
                "run_level": txt(".//t:Principal/t:RunLevel"),
                "logon_type": txt(".//t:Principal/t:LogonType"),
                "command": txt(".//t:Actions/t:Exec/t:Command"),
                "arguments": txt(".//t:Actions/t:Exec/t:Arguments"),
                "working_directory": txt(".//t:Actions/t:Exec/t:WorkingDirectory"),
            }
            blk["task_settings_note"] = ("核对三项：interval 应为 PT{n}M；"
                                         "start_when_available 应为 true（错过的触发会补跑）；"
                                         "disallow_start_if_on_batteries 应为 false（电池下也跑）"
                                         ).format(n=blk["task_settings"].get("interval"))
        except Exception as e:  # noqa: BLE001
            blk["task_xml_parse_error"] = repr(e)[:160]
    else:
        blk["task_query_message"] = str(xml_or_err)[:300]

    blk["raw_query"] = task_raw_query(name) if winenv.IS_WIN else "(非 Windows 环境跳过)"
    return blk


def cmd_status(args: argparse.Namespace) -> int:
    specs = _task_specs(args)
    main_spec = specs[0]
    out: dict = {
        "platform": sys.platform,
        "entry_script": str(ENTRY),
        "entry_exists": ENTRY.is_file(),
        "python_for_task": winenv.default_python(windowless=True),
        "state_file": str(paths.state_path("state.json")),
        "tasks": [_task_block(sp["name"], sp["xml"], sp["log"]) for sp in specs],
    }
    # 顶层保留主任务的常用字段（向后兼容既有用法）
    main_blk = out["tasks"][0]
    out["task_name"] = main_blk["task_name"]
    out["task_registered"] = main_blk["task_registered"]
    out["xml_archive"] = main_blk["xml_archive"]
    out["xml_archive_exists"] = main_blk["xml_archive_exists"]
    out["catchup_log_exists"] = main_blk["log_exists"]
    out["catchup_log_mtime"] = main_blk["log_mtime"]
    out["catchup_log_tail"] = main_blk["log_tail"]
    out["task_settings"] = main_blk.get("task_settings")
    out["task_settings_note"] = main_blk.get("task_settings_note")
    out["raw_query"] = main_blk.get("raw_query")

    if not main_blk["task_registered"]:
        out["task_query_message"] = main_blk.get("task_query_message")
        out["hint"] = "任务未注册 → 运行 `python install.py install`"

    if len(out["tasks"]) > 1:
        wd = out["tasks"][1]
        out["watchdog_task_name"] = wd["task_name"]
        out["watchdog_registered"] = wd["task_registered"]
        out["watchdog_log_tail"] = wd["log_tail"]

    _print(out)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    r = run_entry_now()
    _print(r)
    return 0 if r.get("ok") else 1


def cmd_toggle(args: argparse.Namespace, enable: bool) -> int:
    if not winenv.IS_WIN:
        _print({"ok": False, "error": "当前不是 Windows"})
        return 1
    ok, msg = change_task(args.name, enable)
    _print({"ok": ok, "task_name": args.name,
            "action": "enabled" if enable else "disabled", "schtasks": msg})
    return 0 if ok else 1


def cmd_trigger(args: argparse.Namespace) -> int:
    """让计划任务**立即执行**（验证任务本身能不能跑起来，而不是验证脚本）。"""
    if not winenv.IS_WIN:
        _print({"ok": False, "error": "当前不是 Windows"})
        return 1
    ok, msg = run_task_now(args.name)
    _print({"ok": ok, "task_name": args.name, "schtasks": msg,
            "note": "已请求计划任务立即运行。稍等几秒后看 runtime/logs/stdio.log 与 runtime/logs/catchup.log 是否更新。"})
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print(__doc__ or "")
        return 0

    action, rest = argv[0], argv[1:]
    ap = argparse.ArgumentParser(prog="install.py", add_help=True)
    ap.add_argument("--name", default=DEFAULT_TASK_NAME, help="计划任务名")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN, help="触发间隔（分钟）")
    ap.add_argument("--python", default="", help="指定解释器完整路径（默认用当前解释器）")
    ap.add_argument("--dry-run", action="store_true", help="只生成 XML，不注册")
    ap.add_argument("--xml-out", default="",
                    help="生成的 task.xml 写到别处（默认写在项目目录，作为注册原文留档）")
    ap.add_argument("--watchdog-name", default=WATCHDOG_TASK_NAME,
                    help="watchdog 计划任务名（默认 {}）".format(WATCHDOG_TASK_NAME))
    ap.add_argument("--watchdog-interval", type=int, default=DEFAULT_WATCHDOG_INTERVAL_MIN,
                    help="watchdog 触发间隔（分钟，默认 {}）".format(DEFAULT_WATCHDOG_INTERVAL_MIN))
    ap.add_argument("--no-watchdog", action="store_true",
                    help="不注册 watchdog（默认会一并注册第二个计划任务）")

    try:
        args = ap.parse_args(rest)
    except SystemExit:
        return 2

    if action == "install":
        return cmd_install(args)
    if action == "uninstall":
        return cmd_uninstall(args)
    if action == "status":
        return cmd_status(args)
    if action == "run":
        return cmd_run(args)
    if action == "enable":
        return cmd_toggle(args, True)
    if action == "disable":
        return cmd_toggle(args, False)
    if action == "trigger":
        return cmd_trigger(args)

    _print({"ok": False, "error": "未知动作：{}".format(action),
            "actions": ["install", "uninstall", "status", "run",
                        "enable", "disable", "trigger"]})
    return 2


if __name__ == "__main__":
    sys.exit(main())
