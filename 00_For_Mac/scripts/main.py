#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — WorkBuddy 积分助手 · 统一入口（Phase 4）

子命令：
  checkin  只执行 Buddy加油站签到
  travel   只执行 Buddy旅行（派猫猫）状态机
  all      先签到、后旅行（推荐日常用法）
  status   只读查询：签到状态 + 旅行状态（不执行任何领取/派遣动作）
  help     用法说明

用法：
  python3 main.py [checkin|travel|all|status]
  python3 main.py --json all     # 机器可读输出（默认即 JSON）

输出：统一 JSON（绝不包含 token / uid / 请求头等敏感信息）：
  单任务：直接返回该模块的结果结构
  all：    {"checkin": {...}, "travel": {...}}
  status：{"checkin_status": {...}, "travel_status": {...}}

结果日志：logs/result.log（位于本 Skill 目录），每次执行追加一行摘要，
仅记录任务/状态/关键数字，绝不记录 token 或响应原文。

设计原则（Phase 1–3 一致）：
  - 本入口不创建任何自动化任务（定时方案见 SKILL.md，由用户自行决定）
  - token 仅内存使用：不打印、不写日志、不落盘、不上传第三方
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_discovery  # noqa: E402
import checkin  # noqa: E402
import credentials  # noqa: E402
import http_client  # noqa: E402
import travel  # noqa: E402
import paths  # noqa: E402

LOG_DIR = str(paths.LOG_DIR)
LOG_FILE = str(paths.log_path("result.log"))

USAGE = """WorkBuddy 积分助手 · 统一入口
用法：python3 main.py [checkin|travel|all|status|preflight]
  checkin    只执行 Buddy加油站签到
  travel     只执行 Buddy旅行（派猫猫）状态机
  all        前置校验 → 签到 → 旅行（推荐日常用法）
  status     只读查询签到状态 + 旅行状态（不做任何领取/派遣）
  preflight  只做接口前置校验（端点发现 + 探活 + 退化检测，不领取任何东西）"""


# ---------------------------------------------------------------------------
# 结果日志（仅摘要，绝无 token / 响应原文）
# ---------------------------------------------------------------------------
def _log_result(task: str, result: dict) -> None:
    """把单任务结果摘要追加到 logs/result.log（失败时静默跳过，不影响主流程）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        status = result.get("status", "unknown")
        extras = []
        for key in ("credit", "streak_days", "reward_credit", "record_id", "arrive_at"):
            if result.get(key) is not None:
                extras.append("{}={}".format(key, result[key]))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] {} {}{}".format(
            ts, task, status, (" " + " ".join(extras)) if extras else ""
        )
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 日志写失败不影响业务结果


# ---------------------------------------------------------------------------
# status：只读查询（不做任何领取/派遣）
# ---------------------------------------------------------------------------
def _checkin_status(cred: dict) -> dict:
    """只读查询活动/签到状态（复用 checkin.query_activity_status，避免两处实现漂移）。

    today_checked_in 仅作参考展示；权威判据是 daily-checkin 的 code=10001。
    """
    data = checkin.query_activity_status(cred)
    if "__error__" in data:
        return {"query": "checkin_status", "status": "failed", "reason": data["__error__"]}
    return {
        "query": "checkin_status",
        "status": "ok",
        "endpoint": data.get("__endpoint__"),
        # degraded=true 表示：所有候选接口都只返回了「合法但全零」的数据，
        # 这份结果**不可信** —— 必须与「真的没活动」区分开，否则会静默空转。
        "degraded": bool(data.get("__degraded__")),
        # active=false 表示当前没有进行中的签到活动 —— 此时签到不产生积分，
        # 必须与「已签到」区分开，否则会把空转误报成正常。
        "active": data.get("active"),
        "activity_name": data.get("activity_name"),
        "theme_name": data.get("theme_name"),
        "season": data.get("season"),
        "today_checked_in": data.get("today_checked_in"),  # 参考：实测可能不可靠
        "daily_credit": data.get("daily_credit"),
        "today_credit": data.get("today_credit"),
        "streak_days": data.get("streak_days"),
        "total_credits": data.get("total_credits"),
        "checkin_dates": data.get("checkin_dates"),
        "end_time": data.get("end_time"),
    }


def _travel_status(cred: dict) -> dict:
    """只读查询旅行状态（不 depart、不 claim）。"""
    headers = {"X-User-Id": cred.get("uid", "")}
    try:
        code, body = http_client.get_json(
            travel.resolve_host(cred) + travel.STATUS_PATH, token=cred["access_token"],
            extra_headers=headers, user_agent=travel.TRAVEL_UA,
        )
    except http_client.HttpTransportError as e:
        return {"query": "travel_status", "status": "failed",
                "reason": "网络异常：" + str(e)}
    if code == 401:
        return {"query": "travel_status", "status": "failed",
                "reason": "令牌已过期（401），请打开 WorkBuddy 桌面端刷新登录态"}
    if body.get("code") != 0:
        return {"query": "travel_status", "status": "failed",
                "reason": "业务错误：code={} msg={}".format(body.get("code"), body.get("msg"))}
    data = body.get("data") or {}
    return {
        "query": "travel_status",
        "status": "ok",
        "state": data.get("state"),
        "daily_limit_reached": data.get("daily_limit_reached"),
        "arrive_at": data.get("arrive_at"),
    }


def run_status() -> dict:
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return {"status": "failed", "reason": "登录态读取失败：" + str(e)}
    return {
        "checkin_status": _checkin_status(cred),
        "travel_status": _travel_status(cred),
    }


# ---------------------------------------------------------------------------
# 任务调度
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 前置校验：每次真正执行领取动作**之前**，先确认端点还是对的
# ---------------------------------------------------------------------------
def _preflight(cred: dict) -> dict:
    """接口前置校验（接口发现 + 探活 + 全零载荷退化检测）。

    这是 2026-09-17「接口悄悄换掉、脚本静默空转一整天」之后加的工作流：
    不能靠"接口没报错"判断接口可用 —— 旧接口会返回**合法但全零**的数据。
    所以每次领取前，回到权威来源（本机客户端 app.asar）核对端点，并实测一次。

    任何异常都吞掉并标记，**绝不因为校验失败而放弃签到**。
    """
    try:
        return api_discovery.preflight(cred=cred, probe=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200]}


def run_task(task: str) -> dict:
    if task == "checkin":
        result = checkin.run_checkin()
        _log_result("checkin", result)
        return result
    if task == "travel":
        result = travel.run_travel()
        _log_result("travel", result)
        return result
    if task == "all":
        try:
            cred = credentials.load_credentials()
        except credentials.CredentialError as e:
            err = {"checkin": {"task": "checkin", "status": "failed",
                               "reason": "登录态读取失败：" + str(e)},
                   "travel": {"task": "travel", "status": "failed",
                              "reason": "登录态读取失败：" + str(e)}}
            _log_result("checkin", err["checkin"])
            return err
        pf = _preflight(cred)          # ① 先校验端点（不改变任何服务端状态）
        c = checkin.run_checkin()      # ② 再领取
        _log_result("checkin", c)
        t = travel.run_travel()
        _log_result("travel", t)
        return {"checkin": c, "travel": t, "preflight": pf}
    if task == "status":
        return run_status()  # 只读查询不写结果日志
    if task == "preflight":
        try:
            cred = credentials.load_credentials()
        except credentials.CredentialError as e:
            return {"status": "failed", "reason": "登录态读取失败：" + str(e)}
        pf = _preflight(cred)
        return {"query": "preflight", "status": "ok" if pf.get("ok") else "failed",
                **pf}
    raise ValueError("未知任务：" + task)


def main(argv: list[str]) -> int:
    task = argv[1] if len(argv) > 1 else "all"
    if task in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if task not in ("checkin", "travel", "all", "status", "preflight"):
        print(json.dumps(
            {"task": task, "status": "failed",
             "reason": "未知子命令（可选：checkin/travel/all/status/preflight）"},
            ensure_ascii=False))
        return 1
    result = run_task(task)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
