#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doctor.py — 一键环境自检（Windows 版）

为什么需要它
------------
本套装会被拷到**另一台电脑**（没有安装过、没有被调试过）上从零跑起来。
而那台机器上：

  · 客户端装在哪、`settings.json` 写在哪、ClawBot 有没有绑定，全都不确定；
  · 出问题时没有控制台输出（计划任务 + pythonw），只能翻日志；
  · 一个环节没通，症状往往只是"微信没有消息"——看不出是哪一层断的。

所以必须有**一条命令把全部环节的实际探测结果摊开**。跑它，看哪一项是红的，照着提示修。

    python doctor.py            # 全部检查
    python doctor.py --json     # 机器可读

检查项（按依赖顺序，前面的不过，后面的必然不过）：
  ① 运行环境      Python 版本 / 平台 / 项目路径是否含中文与空格
  ② 客户端        装在哪、版本多少、asar 能不能读（接口发现的权威来源）
  ③ 登录态        WorkBuddy 桌面端是否已登录（脚本靠它拿 access_token）
  ④ 接口端点      当前应该调哪个路径（客户端现读 + 候选列表）
  ⑤ 账号状态      签到活动是否在进行中（真实联网只读查询）
  ⑥ 微信通道      ClawBot 凭据从哪读到、有没有 context_token
  ⑦ 定时任务      计划任务是否注册、关键设置是否真的生效
  ⑧ 最近运行      日志与 state.json 的时间戳、最近几条记录

本脚本**只读**：不改任何文件、不领取任何积分、不发任何消息。
唯一的外部请求是 ⑤ 的只读状态查询（不产生积分、不改变服务端状态）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import paths  # noqa: E402
sys.path.insert(0, str(DIR / "scripts"))

import winenv  # noqa: E402

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


class Report:
    """收集检查项，最后统一输出（按依赖顺序排列）。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, name: str, detail, fix: str = "") -> None:
        self.items.append({"level": level, "check": name, "detail": detail, "fix": fix})

    # ---- 便捷封装 ----
    def ok(self, name, detail, fix="") -> None:
        self.add(OK, name, detail, fix)

    def warn(self, name, detail, fix="") -> None:
        self.add(WARN, name, detail, fix)

    def fail(self, name, detail, fix="") -> None:
        self.add(FAIL, name, detail, fix)

    def skip(self, name, detail, fix="") -> None:
        self.add(SKIP, name, detail, fix)

    @property
    def failed(self) -> list[dict]:
        return [i for i in self.items if i["level"] == FAIL]

    @property
    def warned(self) -> list[dict]:
        return [i for i in self.items if i["level"] == WARN]


# ---------------------------------------------------------------------------
# ① 运行环境
# ---------------------------------------------------------------------------
def check_env(r: Report) -> None:
    r.ok("Python 版本", "{}（{}）".format(sys.version.split()[0], sys.executable))
    r.ok("操作系统", "{}（windows={}）".format(sys.platform, winenv.IS_WIN))

    if "windowsapps" in sys.executable.lower():
        r.fail("用的是 Microsoft Store 的 Python 占位程序", sys.executable,
               "这是 Windows 的应用执行别名，不是真正的 Python。"
               "请从 python.org 安装 Python 3.10+ 并勾选「Add python.exe to PATH」，"
               "或用 `py -3` 重新注册计划任务（install.py --python）。")

    exe = winenv.default_python(windowless=True)
    # 变量名刻意不叫 `paths`：本模块顶部 `import paths`（第 43 行），
    # 用同名局部变量会把它遮住 —— 现在只在这一行用到所以没炸，
    # 但只要以后在本函数里写一句 `paths.log_path(...)`，就会静默变成
    # 「dict 没有 log_path 属性」，而且只在运行时才暴露。
    exe_paths = winenv.python_exe_paths()
    r.ok("计划任务将使用的解释器", exe)
    if winenv.IS_WIN and pathlib.Path(exe).name.lower() != "pythonw.exe":
        r.warn("解释器不是 pythonw.exe",
               "计划任务会用 {}，可能闪出黑窗".format(pathlib.Path(exe).name),
               "一般不影响功能；若嫌闪窗，用 --python 指定 pythonw.exe 重新 install。")
    else:
        r.ok("解释器类型", "{}（无控制台，不闪窗）".format(pathlib.Path(exe).name))

    s = str(DIR)
    odd = [c for c in s if ord(c) > 127]
    if " " in s or odd:
        r.warn("项目路径含空格/中文", s,
               "本套装已对空格与中文做了引号与 UTF-16 处理，正常可用；"
               "但若遇到诡异问题，优先怀疑这里——换成纯英文无空格的路径可排除干扰。")
    else:
        r.ok("项目路径", s)
    r.ok("Python 探测结果", exe_paths)


# ---------------------------------------------------------------------------
# ② 客户端
# ---------------------------------------------------------------------------
def check_client(r: Report) -> dict:
    c = winenv.detect_client()
    if c.get("available"):
        r.ok("客户端已定位",
             "{}（版本 {}，来源 {}，asar {:.1f} MB）".format(
                 c.get("asar"), c.get("version") or "未知",
                 c.get("version_source") or "-",
                 (c.get("asar_size") or 0) / 1024 / 1024))
        if not c.get("version"):
            r.warn("客户端版本读不出来", "asar 头部解析与 Info.plist 都没拿到版本",
                   "只影响 User-Agent 与指纹缓存，不影响签到。")
    else:
        r.fail("找不到 WorkBuddy 客户端", "所有候选路径都不存在",
               "确认本机装了 WorkBuddy 桌面端。若装在别处，"
               "把 app.asar 的绝对路径填进 win_paths.json 的 client_asar 字段。")
        r.add(WARN, "客户端候选路径", winenv.describe_paths(winenv.client_asar_candidates()),
              "对照上面的 exists 列，找出真实安装位置。")
    return c


# ---------------------------------------------------------------------------
# ③ 登录态
# ---------------------------------------------------------------------------
def check_credentials(r: Report) -> dict:
    try:
        import credentials
    except Exception as e:  # noqa: BLE001
        r.fail("引擎模块导入失败", repr(e)[:200],
               "scripts/ 目录不完整。重新解压整个 00_For_Win 文件夹。")
        return {}
    try:
        cred = credentials.load_credentials()
    except Exception as e:  # noqa: BLE001
        r.fail("读不到登录态", str(e)[:300],
               "① 确认 WorkBuddy 桌面端已安装并**已登录**；② 打开一次桌面端；"
               "③ 若曾用旧版客户端，确认 workbuddy-desktop.info 存在。")
        r.add(WARN, "登录态候选路径",
              winenv.describe_paths([
                  pathlib.Path(p) for p in credentials.desktop_info_candidates()
              ]), "这两个路径至少要有一个存在且可读。")
        return {}
    d = credentials.describe(cred)
    r.ok("登录态已读取",
         "来源 {}，token {}，uid {}".format(d.get("source"), d.get("access_token"),
                                            d.get("uid")))
    return cred


# ---------------------------------------------------------------------------
# ④ 接口端点
# ---------------------------------------------------------------------------
def check_endpoints(r: Report, cred: dict) -> dict:
    try:
        import api_discovery
    except Exception as e:  # noqa: BLE001
        r.fail("接口发现模块加载失败", repr(e)[:200])
        return {}
    try:
        rep = api_discovery.preflight(cred=cred or None, probe=bool(cred))
    except Exception as e:  # noqa: BLE001
        r.fail("接口前置校验异常", repr(e)[:250])
        return {}

    r.ok("端点来源", "{}（客户端 {}）".format(
        rep.get("source"), rep.get("client_version") or "-"))
    r.ok("状态查询候选", (rep.get("status_names") or []) or "(走内置兜底列表)")
    r.ok("领取候选", (rep.get("claim_names") or []) or "(走内置兜底列表)")
    if rep.get("changed"):
        r.warn("检测到端点已变化", rep.get("changed_from"),
               "脚本会自动改用新端点，无需操作。这条只是告知。")
    else:
        r.ok("端点无变化", "与上次记录一致")

    if not cred:
        r.skip("接口探活", "登录态不可用，跳过（只读查询需要 token）")
        return rep
    if rep.get("ok"):
        r.ok("接口探活通过", "实测可用端点：{}".format(rep.get("resolved_status")))
    elif rep.get("degraded_fallback"):
        r.fail("接口探活全部退化",
               "所有候选都返回了「合法但全零」的数据 —— 与 2026-09-17 "
               "旧接口失效的特征一致",
               "多半是活动接口又换了。看探活明细里每个候选的返回，"
               "或到客户端 app.asar 里核对新端点名。")
    elif rep.get("unauthorized"):
        r.fail("令牌被拒（401）", "登录态已过期",
               "打开一次 WorkBuddy 桌面端刷新登录态，然后重跑。")
    else:
        r.fail("接口探活失败", rep.get("error") or "所有候选都没有可信返回",
               "检查本机网络是否能访问 copilot.tencent.com / www.workbuddy.cn。")
    r.add(OK if rep.get("ok") else WARN, "探活明细",
          [{"path": t.get("path"), "http": t.get("http"), "code": t.get("code"),
            "trustworthy": t.get("trustworthy"), "error": t.get("error")}
           for t in (rep.get("tried") or [])])
    return rep


# ---------------------------------------------------------------------------
# ⑤ 账号状态（真实联网只读查询）
# ---------------------------------------------------------------------------
def check_activity(r: Report, cred: dict) -> None:
    if not cred:
        r.skip("签到活动状态", "登录态不可用，跳过")
        return
    try:
        import checkin
        data = checkin.query_activity_status(cred)
    except Exception as e:  # noqa: BLE001
        r.warn("签到活动状态查询异常", repr(e)[:200], "多为网络问题，稍后会自动重试。")
        return
    if "__error__" in data:
        r.warn("签到活动状态查询失败", data["__error__"], "稍后自动重试。")
        return
    if data.get("__degraded__"):
        r.fail("活动数据全为零（不可信）", "疑似接口退化，签到逻辑会报 suspect 并告警",
               "核对端点；不要把它当成'没有活动'。")
        return
    active = data.get("active")
    r.ok("签到活动状态",
         "active={}，活动={}，今日已签={}，连续={}，累计={}，结束={}".format(
             active, data.get("activity_name") or "-", data.get("today_checked_in"),
             data.get("streak_days"), data.get("total_credits"), data.get("end_time") or "-"))
    if active is False:
        r.warn("当前没有进行中的活动", "空档期不产生积分，脚本仍会每日巡检。")
    r.ok("实际调用的端点", data.get("__endpoint__"))


# ---------------------------------------------------------------------------
# ⑥ 微信通道
# ---------------------------------------------------------------------------
def check_clawbot(r: Report) -> None:
    try:
        import clawbot
    except Exception as e:  # noqa: BLE001
        r.warn("clawbot 模块加载失败", repr(e)[:200])
        return
    cands = winenv.describe_paths(clawbot.settings_candidates())
    hit = next((c for c in cands if c["exists"]), None)
    if hit:
        r.ok("settings.json 已定位", hit["path"])
    else:
        r.warn("找不到 settings.json", "所有候选都不存在",
               "不影响签到；但微信推送会降级成本机通知。"
               "若已绑定「微信助理」，请用 win_paths.json 指定实际路径。")
    r.add(WARN, "settings.json 候选路径", cands, "")

    ch = clawbot.load_channel()
    if not ch:
        r.warn("没有 ClawBot 凭据", "既无本地 login 凭据，settings.json 里也没解析出通道",
               "不影响签到；若要微信推送，请在 WorkBuddy 设置 → 远程通道里"
               "连接「微信助理」，或运行 login.cmd 扫码自建凭据。")
        return
    r.ok("ClawBot 凭据", "来源 {}，bot {}，user {}".format(
        ch.get("source"), clawbot.mask(ch.get("bot_token")), ch.get("user_id")))

    st = clawbot.internal_state()
    ctx = st.get("context_token")
    ts = st.get("context_token_ts")
    age = (round((datetime.datetime.now().timestamp() - ts) / 3600, 1)
           if isinstance(ts, (int, float)) else None)
    if ctx:
        r.ok("context_token 已有", "捕获于 {} 小时前".format(age))
    else:
        r.warn("context_token 缺失",
               "主动推送会被服务端受理（照样占配额）但**不会出现在微信里**",
               "在微信里给该机器人发一条消息（如「1」），"
               "然后运行 wait_token.cmd（= python clawbot.py wait 60）。")
    if st.get("last_session_expired_ts"):
        r.warn("历史上出现过会话失效（-14）",
               datetime.datetime.fromtimestamp(
                   st["last_session_expired_ts"]).strftime("%Y-%m-%d %H:%M"),
               "若现在仍收不到，按上面 context_token 的提示做。")


# ---------------------------------------------------------------------------
# ⑦ 定时任务
# ---------------------------------------------------------------------------
def _script_from_args(arguments: str | None) -> str:
    """从计划任务的 `Arguments` 里取出脚本绝对路径（容忍引号与额外参数）。

    任务 XML 里写的是 `"E:\\...\\catchup.py"`（install.py 生成时**一律带引号**，
    因为路径常含空格与中文），但也可能是未加引号 + 参数的形式。
    取不到就返回空串 —— 调用方据此降级成 WARN，不会误报成 FAIL。
    """
    s = (arguments or "").strip()
    if not s:
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 1 else s.strip('"')
    parts = s.split()
    return parts[0] if parts else ""


def _same_file(a: str | pathlib.Path, b: str | pathlib.Path) -> bool:
    """两个路径是否指向同一个文件（Windows 大小写不敏感；resolve 失败则退化比较）。"""
    try:
        return pathlib.Path(a).resolve() == pathlib.Path(b).resolve()
    except Exception:  # noqa: BLE001
        return str(a).strip().lower() == str(b).strip().lower()


def check_task(r: Report) -> None:
    if not winenv.IS_WIN:
        r.skip("计划任务", "非 Windows 环境（本文件在 mac 上跑只用于审阅）")
        return
    try:
        import install
    except Exception as e:  # noqa: BLE001
        r.warn("install 模块加载失败", repr(e)[:200])
        return

    name = install.DEFAULT_TASK_NAME
    exists, xml_or_err = install.task_query_xml(name)
    if not exists:
        r.fail("计划任务未注册", str(xml_or_err)[:200],
               "运行 install.cmd（= python install.py install）完成注册。")
        return
    try:
        settings = install.task_settings_from_xml(xml_or_err)
        interval = settings.get("interval")
        swa = settings.get("start_when_available")
        bat = settings.get("disallow_start_if_on_batteries")
        cmd = settings.get("command")
        arg = settings.get("arguments")

        # Some Windows builds omit optional false-valued elements from the XML
        # returned by schtasks. Read the effective settings through PowerShell
        # before deciding that a required setting is missing.
        if swa is None or bat is None:
            live = install.task_live_settings(name) or {}
            if swa is None:
                swa = live.get("start_when_available")
            if bat is None:
                bat = live.get("disallow_start_if_on_batteries")
            if live:
                r.ok("计划任务设置回读", "schtasks XML 未返回全部字段；PowerShell 已回读有效设置")

        r.ok("计划任务已注册", "任务名 {}，间隔 {}".format(name, interval))
        if swa is None:
            r.warn("错过补跑（StartWhenAvailable）", "无法回读该设置",
                   "重跑 install.cmd 覆盖注册；若仍无法回读，再提供完整 doctor 输出。")
        else:
            (r.ok if swa == "true" else r.fail)(
                "错过补跑（StartWhenAvailable）", str(swa),
                "必须为 true，否则睡眠/关机期间错过的触发不会补跑。"
                "重新 install 即可修正。")
        if bat is None:
            r.warn("电池下仍运行（DisallowStartIfOnBatteries）", "无法回读该设置",
                   "重跑 install.cmd 覆盖注册；若仍无法回读，再提供完整 doctor 输出。")
        else:
            (r.ok if bat == "false" else r.fail)(
                "电池下仍运行（DisallowStartIfOnBatteries）", str(bat),
                "必须为 false。默认 true 会让笔记本用电池时静默跳过整个任务。")
        r.ok("执行体", "{} {}".format(cmd, arg))

        # ★ 2026-09-20 新增：任务里的脚本路径必须就是**当前目录**下的那一个。
        #   计划任务存的是绝对路径 —— 把文件夹挪走/改名之后，任务仍指向旧位置，
        #   表现为每次触发都以 0x8007010B（目录名无效）失败，而且**完全静默**：
        #   pythonw 没有控制台，旧位置的日志也不会再被写。
        #   实测：搬迁后三个任务连续失败，而旧版 doctor 只把执行体打印出来、从不比对，
        #   于是「计划任务已注册」一路绿灯 —— 正是本项目最忌讳的那类静默失效。
        want = DIR / "catchup.py"
        script = _script_from_args(arg)
        if not script:
            r.warn("任务脚本路径无法核对", "Arguments = {!r}".format(arg),
                   "重跑 install.cmd 覆盖注册后再看这一项。")
        elif _same_file(script, want):
            r.ok("任务指向当前目录", str(want))
        else:
            r.fail("计划任务指向的是旧路径（搬迁后没有重新注册）",
                   "任务里写的是：{}\n当前实际位置：{}".format(script, want),
                   "在新位置重跑一次 install.cmd（= python install.py install）覆盖注册。"
                   "否则任务每次触发都会以 0x8007010B（目录名无效）静默失败，"
                   "签到不会发生，而任务列表里它看起来一切正常。")
    except Exception as e:  # noqa: BLE001
        r.warn("任务 XML 解析失败", repr(e)[:160], "任务已注册，但无法核对设置。")


# ---------------------------------------------------------------------------
# ⑧ 最近运行
# ---------------------------------------------------------------------------
def check_runs(r: Report) -> None:
    log = paths.log_path("catchup.log")
    if log.is_file():
        mt = datetime.datetime.fromtimestamp(log.stat().st_mtime)
        age_h = (datetime.datetime.now() - mt).total_seconds() / 3600
        detail = "最后写入 {}（{:.1f} 小时前）".format(
            mt.strftime("%Y-%m-%d %H:%M:%S"), age_h)
        if age_h > 6:
            r.warn("补跑日志偏旧", detail,
                   "若电脑一直开着，超过 6 小时没有新记录说明计划任务没在跑"
                   "（或当日两个窗口都已完成、脚本按设计空转，也会写日志）。"
                   "用 `python install.py trigger` 手动触发一次看日志是否更新。")
        else:
            r.ok("补跑日志", detail)
        r.add(OK, "最近 8 条日志", log.read_text(encoding="utf-8", errors="replace")
              .splitlines()[-8:])
    else:
        r.warn("还没有补跑日志", str(log),
               "从未运行过。运行 install.cmd 或 install.py run 即可生成。")

    stdio = paths.log_path("stdio.log")
    if stdio.is_file():
        r.add(OK, "计划任务输出（logs/stdio.log 末尾）",
              stdio.read_text(encoding="utf-8", errors="replace").splitlines()[-8:])
    else:
        r.add(SKIP, "计划任务输出", "logs/stdio.log 还不存在（手动运行时不会生成）")

    st = paths.state_path("state.json")
    if st.is_file():
        try:
            d = json.loads(st.read_text(encoding="utf-8"))
            r.ok("当日进度", d)
        except Exception:  # noqa: BLE001
            r.warn("state.json 解析失败", str(st), "可安全删除，下次运行会重建。")
    else:
        r.add(SKIP, "当日进度", "state.json 还不存在")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarise(r: Report) -> dict:
    if r.failed:
        verdict, advice = "有阻塞性问题", "先修上面的 FAIL 项，修完重跑 doctor.py。"
    elif r.warned:
        verdict, advice = "可用，但有需要注意的项", "WARN 不影响签到，通常只影响微信推送。"
    else:
        verdict, advice = "一切正常", "无需操作。"
    return {
        "verdict": verdict,
        "advice": advice,
        "counts": {"OK": sum(1 for i in r.items if i["level"] == OK),
                   "WARN": len(r.warned),
                   "FAIL": len(r.failed),
                   "SKIP": sum(1 for i in r.items if i["level"] == SKIP)},
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="doctor.py", add_help=True)
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    r = Report()
    check_env(r)
    check_client(r)
    cred = check_credentials(r)
    check_endpoints(r, cred)
    check_activity(r, cred)
    check_clawbot(r)
    check_task(r)
    check_runs(r)
    summary = summarise(r)

    if args.json:
        print(json.dumps({"summary": summary, "checks": r.items},
                         ensure_ascii=False, indent=2))
        return 1 if r.failed else 0

    icon = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}
    print("=" * 72)
    print("WorkBuddy 积分助手 · 环境自检（Windows 版）")
    print("项目目录：{}".format(DIR))
    print("时间：{}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 72)
    for i in r.items:
        print("{} {}".format(icon.get(i["level"], i["level"]), i["check"]))
        det = i["detail"]
        if isinstance(det, str):
            for line in det.splitlines() or [""]:
                print("        " + line)
        else:
            for line in json.dumps(det, ensure_ascii=False, indent=2).splitlines():
                print("        " + line)
        if i.get("fix"):
            print("        → " + i["fix"])
    print("-" * 72)
    print("结论：{}　（OK {} / WARN {} / FAIL {} / SKIP {}）".format(
        summary["verdict"], summary["counts"]["OK"], summary["counts"]["WARN"],
        summary["counts"]["FAIL"], summary["counts"]["SKIP"]))
    print(summary["advice"])
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
