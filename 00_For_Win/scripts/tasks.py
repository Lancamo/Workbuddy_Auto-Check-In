#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tasks.py — WorkBuddy 成长任务：自动「接受」与「领取奖励」

为什么需要这个模块（2026-10-28 接入）：
  官方活动规则明写「**必须先点「去完成」才开始计数，领取任务之前的使用行为不计入进度**」。
  也就是说：你天天在用专家、模板、自动化，但只要没先「接受」任务，那些行为全部作废。
  实测印证：本账号 13 项任务在 accept 之前 progress 全是 None，accept 之后一律变成
  `0 / target` —— 过去几十次真实使用一次都没被记上。

本模块每天做两件事，都只针对**服务端已经存在的任务**：
  1. `accept`：把 `not_accepted` 的任务登记为已接受，让**后续**的真实使用开始计数；
  2. `claim`：把 `completed`（进度已达标）的任务奖励领掉。

**绝不创建任务**：本模块只调用 `/activity/growth/tasks/accept` 与
`/activity/growth/tasks/{code}/claim`，两者都要求 task_code 已存在。
不碰任何「新建 / 生成 / 提交」类接口，也不会伪造完成进度 —— 完成与否完全取决于
用户当天是否真的用了那些功能。这是刻意划的边界（用户 2026-10-28 明确要求）。

接口（从客户端 bundle `growthSpace-*.js` 的 API 模块读出，非猜测）：
  GET  /v2/activity/growth/tasks                 任务列表（含状态与进度）
  POST /activity/growth/tasks/accept             body {"task_codes": [...]}
  POST /activity/growth/tasks/{code}/claim       body {}
  认证：Authorization: Bearer <token> + X-User-Id: <uid>

状态机（前端枚举，与接口返回一致）：
  not_accepted → accepted → in_progress → completed → claimed
  只有 `completed` 能领；`claimed` 再领会返回成功但**不给分**。

关于 claim 的成功判据（这一条最容易写错）：
  已领过的任务再调 claim 会返回 `http=200, code=0`，但载荷是
  `{"already_claimed": true, "credit": 0, "energy": 0}` —— **看 code 就当成领到，
  会把「一分没有」记成「领取成功」**。所以判据是 credit/energy 真的有进项，
  而不是 code==0。反过来，未完成时是 `http=400, msg="task not completed"`，
  那是列表过期导致的竞态（前端刚领完、这边还拿着旧列表），跳过即可，不算失败。

与传闻中「大圣喵·火眼金睛」的关系：第三方攻略称该技能是「完成每日任务时 15% 概率双倍
Credits」，作用在**任务奖励**上 —— 如果为真，本模块领到的 credit 会高于任务标称值。
但该技能在官方物料里查不到（见 buddy.py 的 SKILLS 注释），所以这里不做任何依赖：
只如实记录服务端返回的 credit，不做二次计算，也不因为「在岗猫是谁」而改变行为。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credentials  # noqa: E402
import http_client   # noqa: E402

TASKS_PATH = "/v2/activity/growth/tasks"
ACCEPT_PATH = "/activity/growth/tasks/accept"
CLAIM_PATH = "/activity/growth/tasks/{code}/claim"

ST_NOT_ACCEPTED = "not_accepted"
ST_ACCEPTED = "accepted"
ST_IN_PROGRESS = "in_progress"
ST_COMPLETED = "completed"
ST_CLAIMED = "claimed"

UA = "WorkBuddy/5.3.14"   # 与 travel.py 保持一致，避免被服务端按 UA 差异化处理


def _result(status: str, **fields) -> dict:
    out = {"task": "tasks", "status": status}
    out.update(fields)
    return out


def _headers(uid: str) -> dict:
    return {"X-User-Id": uid}


# ---------------------------------------------------------------------------
# 有效期
# ---------------------------------------------------------------------------
def _now() -> datetime.datetime:
    """当前时间，**带本地时区**。

    接口给的 valid_start/valid_end 是 `+08:00` 的 aware 时间，而 `datetime.now()`
    返回 naive —— 两者直接比较会抛 TypeError（can't compare offset-naive and
    offset-aware）。所以本模块内部一律用 aware 时间。
    """
    return datetime.datetime.now().astimezone()


def _parse_dt(raw: str | None):
    """把 `2026-11-13T23:59:00+08:00` 解析成带时区的 datetime；空/坏值返回 None。"""
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:      # 万一服务端哪天给了不带时区的串，按本地时区补齐
        dt = dt.replace(tzinfo=_now().tzinfo)
    return dt


def _in_window(t: dict, now: datetime.datetime) -> bool:
    """任务当前是否处在有效期内。valid_start/valid_end 为 null 表示不限。

    为什么要判：过期任务（如限时专家活动）accept 了也没意义，还会让「已接受」的
    数量虚高；未开始的任务提前 accept 同样没意义。判掉才让日志说人话。
    """
    start = _parse_dt(t.get("valid_start"))
    end = _parse_dt(t.get("valid_end"))
    if start and now < start:
        return False
    if end and now > end:
        return False
    return True


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
def fetch_tasks(host: str, token: str, uid: str, timeout: int = 20) -> list[dict]:
    code, body = http_client.get_json(
        host + TASKS_PATH, token=token, extra_headers=_headers(uid),
        timeout=timeout, user_agent=UA)
    if code != 200 or body.get("code") != 0:
        raise RuntimeError("任务列表接口失败：http={} code={} msg={}".format(
            code, body.get("code"), body.get("msg")))
    return (body.get("data") or {}).get("tasks") or []


def accept_tasks(host: str, token: str, uid: str, codes: list[str],
                 timeout: int = 20) -> dict:
    """批量接受任务。返回 {code: status}。

    这是官方「去完成」按钮做的事（前端一次性传整批 task_codes），不是自造行为。
    """
    code, body = http_client.post_json(
        host + ACCEPT_PATH, body={"task_codes": codes}, token=token,
        extra_headers=_headers(uid), timeout=timeout, user_agent=UA)
    if code != 200 or body.get("code") != 0:
        raise RuntimeError("接受任务失败：http={} code={} msg={}".format(
            code, body.get("code"), body.get("msg")))
    out = {}
    for r in ((body.get("data") or {}).get("results") or []):
        if r.get("task_code"):
            out[r["task_code"]] = r.get("status")
    return out


def claim_task(host: str, token: str, uid: str, task_code: str,
               timeout: int = 20) -> tuple[str, dict]:
    """领一个任务。返回 (判定, 明细)，判定取值：

      "claimed"  → 真的有进项（credit 或 energy > 0）
      "empty"    → 接口成功但没有进项（already_claimed，之前就领过）
      "not_yet"  → 未完成（http 400 task not completed，属列表过期竞态）
      "error"    → 其他失败，明细里带原因
    """
    try:
        code, body = http_client.post_json(
            host + CLAIM_PATH.format(code=task_code), body={}, token=token,
            extra_headers=_headers(uid), timeout=timeout, user_agent=UA)
    except Exception as e:  # noqa: BLE001 — 单个任务失败不该中断整批
        return "error", {"reason": "请求异常：{}".format(str(e)[:160])}

    msg = str(body.get("msg") or "")
    if code == 400 and "not completed" in msg.lower():
        return "not_yet", {"reason": msg}
    if code != 200 or body.get("code") != 0:
        return "error", {"reason": "http={} code={} msg={}".format(
            code, body.get("code"), msg)}

    d = body.get("data") or {}
    credit = int(d.get("credit") or 0)
    energy = int(d.get("energy") or 0)
    if credit or energy:
        return "claimed", {"credit": credit, "energy": energy}
    # 走到这里通常是 already_claimed：接口成功但一分没给 —— 不是领取成果。
    return "empty", {"already_claimed": bool(d.get("already_claimed"))}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _pick_accept(tasks: list[dict], now: datetime.datetime) -> list[dict]:
    """该接受的任务：未接受 + 未锁定 + 在有效期内。

    `locked` 为 True 的是前置任务没完成的（如首领任务未做时的进阶任务），
    接受它没有意义，等服务端解锁即可。
    """
    return [t for t in tasks
            if t.get("accept_status") == ST_NOT_ACCEPTED
            and not t.get("locked")
            and t.get("task_code")
            and _in_window(t, now)]


def run_daily(host: str | None = None, timeout: int = 20) -> dict:
    """一次完整巡检：接受待接受的任务 → 领取已达标的奖励。

    幂等：已接受的不再 accept，已领取的不再 claim（服务端会返回 empty，本地也算无进项）。
    因此可以被高频调用而不产生副作用 —— 调用方负责节流（catchup 里 60 分钟一次）。
    """
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return _result("failed", reason="登录态读取失败：" + str(e))
    if host is None:
        import travel   # 惰性导入：host 解析复用 travel（它已有探测与缓存）
        host = travel.resolve_host(cred)
    token = cred.get("access_token", "")
    uid = cred.get("uid", "")
    now = _now()

    try:
        tasks = fetch_tasks(host, token, uid, timeout)
    except Exception as e:  # noqa: BLE001 — 网络/接口问题只影响本模块
        return _result("failed", reason=str(e)[:200])

    accepted: list[dict] = []
    to_accept = _pick_accept(tasks, now)
    if to_accept:
        codes = [t["task_code"] for t in to_accept]
        try:
            res = accept_tasks(host, token, uid, codes, timeout)
            title_of = {t["task_code"]: t.get("title") for t in to_accept}
            for code in codes:
                if res.get(code) == ST_ACCEPTED:
                    accepted.append({"code": code, "title": title_of.get(code)})
            # 接受会重置进度并可能解锁前置任务 → 重新拉一次，拿到最新状态再决定领什么
            tasks = fetch_tasks(host, token, uid, timeout)
        except Exception as e:  # noqa: BLE001
            # 接受失败不该影响「领取已完成的」，继续往下走
            accepted_error = str(e)[:160]
        else:
            accepted_error = None
    else:
        accepted_error = None

    claimed: list[dict] = []
    errors: list[str] = []
    if accepted_error:
        errors.append("接受失败：" + accepted_error)

    for t in tasks:
        if t.get("accept_status") != ST_COMPLETED or not t.get("task_code"):
            continue
        verdict, detail = claim_task(host, token, uid, t["task_code"], timeout)
        if verdict == "claimed":
            claimed.append({"code": t["task_code"], "title": t.get("title"),
                            "credit": detail.get("credit", 0),
                            "energy": detail.get("energy", 0)})
        elif verdict == "error":
            errors.append("{}：{}".format(t.get("title") or t["task_code"],
                                          detail.get("reason")))

    waiting = [t for t in tasks
               if t.get("accept_status") in (ST_ACCEPTED, ST_IN_PROGRESS)]
    return _result(
        "ok" if not errors or claimed or accepted else "failed",
        total=len(tasks),
        accepted=accepted,
        claimed=claimed,
        credit=sum(c["credit"] for c in claimed),
        energy=sum(c["energy"] for c in claimed),
        waiting=len(waiting),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# CLI（排查用）
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    cred = credentials.load_credentials()
    import travel
    host = travel.resolve_host(cred)
    token, uid = cred.get("access_token", ""), cred.get("uid", "")
    now = _now()

    if cmd == "status":
        try:
            tasks = fetch_tasks(host, token, uid)
        except Exception as e:  # noqa: BLE001
            print(json.dumps(_result("failed", reason=str(e)[:200]), ensure_ascii=False))
            return 1
        rows = [{"code": t.get("task_code"), "title": t.get("title"),
                 "status": t.get("accept_status"),
                 "progress": "{}/{}".format((t.get("progress") or {}).get("current"),
                                            (t.get("progress") or {}).get("target")),
                 "credit": t.get("reward_credit"),
                 "in_window": _in_window(t, now), "locked": t.get("locked")}
                for t in tasks]
        print(json.dumps(_result("ok", total=len(tasks), tasks=rows), ensure_ascii=False))
        return 0

    if cmd == "run":
        r = run_daily(host)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("status") == "ok" else 1

    print(json.dumps(_result("failed", reason="未知子命令（status / run）"),
                     ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
