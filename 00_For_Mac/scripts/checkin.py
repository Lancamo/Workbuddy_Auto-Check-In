#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checkin.py — WorkBuddy 积分助手 · Buddy加油站每日签到模块（Phase 2）

本模块实现 WorkBuddy 桌面端每日签到流程，并保持接口调用、状态判断与结果
返回的独立职责。

职责（本模块只做这几件事）：
  1. 通过 credentials.py 获取登录态（access_token）
  2. 调用签到相关接口（checkin-status / daily-checkin）
  3. 判断签到状态（含幂等 code=10001）
  4. 返回结构化结果（绝不含 access_token / refresh_token / 完整响应敏感字段）

接口（host 取登录态 domain，回退 workbuddy.cn / copilot.tencent.com）：
  - POST /v2/billing/meter/checkin-activity-status   查活动与签到状态（body {}）【现行】
  - POST /v2/billing/meter/daily-checkin             执行领取（body {}）        【现行】
  - 认证：Authorization: Bearer <accessToken>（+ X-User-Id，部分环境需要）

⚠️ 2026-09-17 重大接口变更（本模块的修复根因，勿回退）：
  旧接口 `/billing/meter/checkin-status` 现在**恒返回 active=false 且字段全空**
  （实测：active=false、streak_days=0、total_credits=0、checkin_dates=null、
   activity_name 空 —— 一份**合法但空洞**的数据），但活动其实正在进行。
  症状是脚本每天报「当前没有进行中的活动」，且因 catchup 把 no_activity 记为
  「已完成」，当天不再重试，**积分一粒未进**。
  桌面端实际调用的是 `/v2/billing/meter/checkin-activity-status`（返回完整活动数据：
  activity_name / theme_name / season / claim_button_text / daily_credit / checkin_dates /
  start_time / end_time），且 `/v2/...` 与去掉 `/v2` 的路径服务端都接受。

🔁 端点不由本文件硬编码决定 —— 见 `api_discovery.py`
  端点会随腾讯发版而变（无固定周期），硬编码迟早再次失效。因此候选顺序改为：
  ① `api_discovery` 每次运行前从**本机客户端 app.asar** 现读当前端点（权威来源）；
  ② 读不到客户端时，退回本文件内置的 FALLBACK 候选。
  同时提供「全零载荷」退化判别：若某个候选返回的数据全为零，**不采信**，
  继续试下一个；全部候选都退化时返回 `suspect`（而非 no_activity），
  让上层立刻告警而不是静默空转。

幂等规则（必须保留）：
  - today_checked_in 仅作"快速短路"，真正的幂等兜底是 daily-checkin 返回 code=10001
    （实测该码伴随 HTTP 400 返回，msg="今天已签到，请明天再来"）。
    注意：客户端源码里的 mapCheckinStatus 只映射 1001/1002/1003，**与实测的 10001 不同**，
    以实测为准 —— 不要照抄客户端那张表。
  - code=10001 表示"今日已签到"，**不视为失败**，返回 already_checked。

返回结构（统一，可直接被上层/自动化消费）：
  成功：  {"task":"checkin","status":"success","credit":100,"streak_days":5,
           "activity":"高校新生攻略","theme":"Buddy加油站","endpoint":"/v2/..."}
  已签到：{"task":"checkin","status":"already_checked","message":"今日已签到"}
  无活动：{"task":"checkin","status":"no_activity","message":"...","activity":"","theme":""}
  可疑：  {"task":"checkin","status":"suspect","reason":"...","endpoint":"..."}
          —— 接口返回了合法但全零的数据，无法确认活动状态（2026-09-17 故障特征）
  失败：  {"task":"checkin","status":"failed","reason":"..."}

安全（继承 Phase 1）：
  - access_token 仅在内存中使用，绝不打印 / 写日志 / 落盘 / 上传第三方
  - 本模块只调用 copilot.tencent.com 官方接口
"""

from __future__ import annotations

import os
import sys

# 允许以脚本方式或直接 import 运行时找到同目录的 credentials / http_client
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api_discovery  # noqa: E402
import credentials  # noqa: E402
import http_client  # noqa: E402

CHECKIN_HOST = "https://copilot.tencent.com"   # 默认候选（保留向后兼容）
# 接口 host 候选：登录态 auth.domain 优先，其余为已知可用域名。
# 注意：不要只看单一域名 —— 实测 copilot.tencent.com 与 www.workbuddy.cn 均可用，
# 网上「某域名已 404」的说法与实测不符，因此改为探测式，避免硬编码失效。
CHECKIN_HOSTS = ("https://copilot.tencent.com", "https://www.workbuddy.cn")

# 【兜底】内置候选 —— 仅在 api_discovery 读不到本机客户端时才使用。
# 【现行】首选由 api_discovery 从 app.asar 现读（见 _candidates）。
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
STATUS_PATH_LEGACY = "/billing/meter/checkin-status"

CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
CHECKIN_PATH_LEGACY = "/billing/meter/daily-checkin"

STATUS_PATHS = (STATUS_PATH, "/billing/meter/checkin-activity-status", STATUS_PATH_LEGACY)
CHECKIN_PATHS = (CHECKIN_PATH, CHECKIN_PATH_LEGACY)

CODE_SUCCESS = 0
CODE_ALREADY_CHECKED = 10001  # 实测幂等码（客户端 mapCheckinStatus 的 1001 是另一套，勿混用）

_host_cache: str | None = None


def _candidates(kind: str) -> tuple:
    """候选路径：① 从本机客户端现读（权威）→ ② 内置兜底。

    kind = "status" | "claim"。任何异常都退回内置常量，绝不因"发现失败"而不签到。
    """
    try:
        d = api_discovery.discover()
        paths = d.get("status_paths" if kind == "status" else "claim_paths")
        if paths:
            return tuple(paths)
    except Exception:  # noqa: BLE001
        pass
    return STATUS_PATHS if kind == "status" else CHECKIN_PATHS


def candidate_hosts(cred: dict) -> list[str]:
    """接口 host 候选列表：登录态 auth.domain 优先，其后为已知可用域名。"""
    out: list[str] = []
    d = (cred.get("domain") or "").rstrip("/")
    if d:
        out.append(d)
    for h in CHECKIN_HOSTS:
        if h not in out:
            out.append(h)
    return out


def resolve_host(cred: dict, timeout: int = http_client.DEFAULT_TIMEOUT) -> str:
    """探测第一个可用的接口 host（进程内缓存）。

    判定为「域名可用」的条件：HTTP 200 且业务码为 0，或 HTTP 401（域名/鉴权入口存在，
    只是 token 有问题 —— 换域名也救不了）。全部失败时返回首个候选。
    用**首个状态候选**（而非硬编码路径）探测，保证探的路径本身也是当前有效的。
    """
    global _host_cache
    if _host_cache:
        return _host_cache
    probe_path = _candidates("status")[0]
    cands = candidate_hosts(cred)
    for h in cands:
        try:
            code, body = http_client.post_json(
                h + probe_path, body={}, token=cred.get("access_token", ""), timeout=timeout
            )
        except http_client.HttpTransportError:
            continue
        if code == 401 or (code == 200 and body.get("code") == 0):
            _host_cache = h
            return h
    return cands[0]


def _result(status: str, **fields) -> dict:
    out = {"task": "checkin", "status": status}
    out.update(fields)
    return out


def _post_first_ok(paths: tuple | None, host: str, token: str, timeout: int):
    """按候选顺序 POST，返回第一个「业务码为 0」的结果。

    paths 传 None 时使用 `_candidates("claim")`。
    返回 (status_kind, code, body, path)：
      - "ok"          业务码 0，body 有效
      - "unauthorized" 401（令牌失效，换路径也救不了）
      - "transport"    全部候选都网络异常
      - "biz_error"    全部候选都返回了非 0 业务码（取最后一个）
    """
    if paths is None:
        paths = _candidates("claim")
    last = ("biz_error", None, {}, paths[-1])
    for p in paths:
        try:
            code, body = http_client.post_json(host + p, body={}, token=token, timeout=timeout)
        except http_client.HttpTransportError as e:
            last = ("transport", None, {"reason": str(e)}, p)
            continue
        if code == 401:
            return ("unauthorized", 401, body, p)
        if body.get("code") == CODE_SUCCESS:
            return ("ok", code, body, p)
        last = ("biz_error", code, body, p)
    return last


def query_activity_status(cred: dict, timeout: int = http_client.DEFAULT_TIMEOUT) -> dict:
    """只读查询活动与签到状态，返回规范化的 data 字典（绝不含 token）。

    候选顺序来自本机客户端（`_candidates("status")`），并按**载荷可信度**择优：
    逐个候选尝试，一旦某个返回**可信数据**（非全零）就用它并停止；
    若所有候选都只返回全零载荷，则退回第一个可用的结果，并打上
    `__degraded__=True` —— 由 run_checkin 升级为 `suspect` 告警，
    **绝不**把它当成「今天没有活动」放过去（这正是 2026-09-17 静默失效的成因）。

    供 main.py 的 status 子命令与 run_checkin 共用，避免两处实现漂移。
    失败时返回 {"__error__": "..."}。
    """
    token = cred["access_token"]
    host = resolve_host(cred, timeout)
    paths = _candidates("status")

    first_ok = None          # (path, data) —— 第一个业务码为 0 的结果（可能退化）
    chosen = None            # (path, data) —— 明确可信的结果
    biz_err = None
    transport_err = None

    for p in paths:
        try:
            code, body = http_client.post_json(host + p, body={}, token=token, timeout=timeout)
        except http_client.HttpTransportError as e:
            transport_err = str(e)
            continue
        if code == 401:
            return {"__error__": "令牌已过期（401），请打开 WorkBuddy 桌面端刷新登录态后重试"}
        if body.get("code") != CODE_SUCCESS:
            biz_err = "code={} msg={}".format(body.get("code"), body.get("msg"))
            continue
        data = dict(body.get("data") or {})
        if first_ok is None:
            first_ok = (p, data)
        if api_discovery.payload_trustworthy(data):
            chosen = (p, data)
            break

    if chosen is None:
        if first_ok is None:
            if transport_err:
                return {"__error__": "查询签到活动状态失败（网络异常）：" + transport_err}
            return {"__error__": "查询签到活动状态失败：" + (biz_err or "无可用候选端点")}
        # 只有退化结果可用 —— 保留它，但明确标记不可信
        path, data = first_ok
        data["__degraded__"] = True
    else:
        path, data = chosen
        data["__degraded__"] = False

    data["__endpoint__"] = path
    return data


def run_checkin(timeout: int = http_client.DEFAULT_TIMEOUT) -> dict:
    """执行一次签到流程，返回统一结构化结果（不含任何敏感信息）。"""

    # 1. 获取登录态（统一走 credentials.py）
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return _result("failed", reason="登录态读取失败：" + str(e))
    token = cred["access_token"]  # 仅内存使用，绝不打印/落盘
    host = resolve_host(cred, timeout)

    # 2. 查询活动 / 签到状态（新接口优先，旧接口兜底）
    st_data = query_activity_status(cred, timeout)
    if "__error__" in st_data:
        return _result("failed", reason=st_data["__error__"])

    activity = st_data.get("activity_name") or ""
    theme = st_data.get("theme_name") or ""
    daily_credit = st_data.get("daily_credit")
    streak_days = st_data.get("streak_days")
    endpoint = st_data.get("__endpoint__")
    degraded = bool(st_data.get("__degraded__"))

    # 2.1 交叉比对「查」与「领」的结论（2026-09-18 新增）。
    # active=false 与 today_checked_in=true 不可能同时成立 —— 一旦同时出现，
    # 说明两个端点里有一个已经不是我们在用的那个（服务端灰度/部分回滚）。
    # 这种自相矛盾比单纯的「没活动」危险得多：当成 no_activity 放过去就是静默空转。
    if st_data.get("active") is False and st_data.get("today_checked_in") is True:
        return _result(
            "suspect",
            message="接口自相矛盾：既说没有活动，又说今天已签到",
            reason="状态接口同时返回 active=false 与 today_checked_in=true，两个字段不可能"
                   "同时成立。多半是「查」与「领」两个端点只切了其中一个，属于接口漂移。"
                   "请核对 scripts/api_discovery.py 的发现结果。",
            endpoint=endpoint,
        )

    # 2.2 活动是否在进行中 —— 必须先判断，否则会把「无活动」误报成「已签到」。
    if st_data.get("active") is False:
        if degraded:
            # 关键保险：接口返回了「合法但全零」的数据 —— 没能采信到真实活动信息。
            # 2026-09-17 的故障正是这个特征（旧接口恒返回全零，脚本据此报了一个月的
            # 「当前没有进行中的活动」）。此时**不能**报 no_activity（那会静默空转），
            # 而要升级为 suspect 让上层立刻告警。
            return _result(
                "suspect",
                message="接口返回的数据全部为零，无法确认活动状态",
                reason="所有候选状态接口都只返回了全零载荷（active=false 且活动名/结束时间/"
                       "累计积分/签到日期全为空），与 2026-09-17 旧接口失效时的特征一致。"
                       "可能是活动接口又变了，请核对 scripts/api_discovery.py 的发现结果。",
                endpoint=endpoint,
            )
        return _result(
            "no_activity",
            message="当前没有进行中的签到活动（空档期不产生积分）",
            activity=activity,
            theme=theme,
            endpoint=endpoint,
        )

    if st_data.get("today_checked_in") is True:
        # 快速短路：明确已签到则不再发起领取请求
        return _result(
            "already_checked",
            message="今日已签到",
            activity=activity,
            theme=theme,
            credit=daily_credit,
            streak_days=streak_days,
            endpoint=endpoint,
        )

    # 3. 执行签到/领取（候选路径 = 本机客户端现读 + 内置兜底）
    kind, c_code, c_body, used = _post_first_ok(None, host, token, timeout)

    if kind == "unauthorized":
        return _result(
            "failed",
            reason="令牌已过期（401），请打开 WorkBuddy 桌面端刷新登录态后重试",
        )
    if kind == "transport":
        return _result("failed", reason="签到请求失败（网络异常）：" + str(c_body.get("reason")))

    code = c_body.get("code")
    # 领取接口的成功返回体有两种可能形态：`{code,data:{credit,...}}` 或扁平
    # `{code,credit,...}`（客户端源码读的是顶层 `resp.credit`）。两种都兼容，
    # 否则会出现「领到了但通知显示 +? 积分」。
    data = c_body.get("data")
    if not isinstance(data, dict):
        data = c_body

    if code == CODE_SUCCESS:
        return _result(
            "success",
            credit=data.get("credit"),
            streak_days=data.get("streak_days"),
            is_streak_day=data.get("is_streak_day"),
            activity=activity,
            theme=theme,
            endpoint=used,
        )
    if code == CODE_ALREADY_CHECKED:
        # 幂等：今日已签到（HTTP 400 + code=10001），不视为失败
        return _result(
            "already_checked",
            message="今日已签到",
            activity=activity,
            theme=theme,
            credit=daily_credit,
            streak_days=streak_days,
            endpoint=used,
        )

    # 3.1 交叉比对：状态接口说「有活动且未签到」，领取接口却返回我们不认识的失败码 ——
    # 两边合起来看就是「端点漂移」（服务端只切了其中一个）。单独标出来，
    # 让上层的告警说人话（"接口可能变了，去核对端点" 而不是 "签到失败，重试"）。
    miss = api_discovery.looks_like_route_miss(code, c_body)
    res = _result(
        "failed",
        reason="签到未成功：code={} msg={}".format(code, c_body.get("msg")),
        endpoint=used,
    )
    if miss:
        res["drift_suspect"] = miss
        res["reason"] += "；" + miss
    return res


if __name__ == "__main__":
    # 以结构化 JSON 输出结果（不含 token），供上层/自动化读取。
    import json

    print(json.dumps(run_checkin(), ensure_ascii=False))
