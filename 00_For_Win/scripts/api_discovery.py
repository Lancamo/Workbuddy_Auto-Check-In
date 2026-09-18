#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_discovery.py — WorkBuddy 积分助手 · 接口前置校验（Pre-flight）

为什么需要这个模块（2026-09-17 的真实教训）：
  上游把签到状态接口从 `/billing/meter/checkin-status` 换成了
  `/v2/billing/meter/checkin-activity-status`。**旧路径没有下线** —— 它继续返回
  HTTP 200 + code 0，字段结构一模一样，只是**全部为零**
  （active=false、activity_name 空、checkin_dates 空、total_credits 0）。
  于是脚本"成功"地读到一份合法却空洞的数据，每天如实报告「当前没有进行中的活动」，
  积分一粒未进。这是最危险的一类失效：**不报错、不崩溃、日志全绿**。

  教训一：不能靠"接口没报错"判断接口可用。
  教训二：不能靠"多久换一次"预测 —— 它没有周期，跟客户端发版走。
  唯一可靠的做法：每次执行前回到**权威来源**核对一遍，看本机桌面端到底在调哪个路径。

权威来源（本模块读什么）：
  - <客户端安装目录>/resources/app.asar   → 打包的前端源码（明文包含接口路径）
  - 从 asar 头部解析出的 package.json     → 客户端版本
    这两处是**腾讯自己发布的产物**，明文包含接口路径，比任何猜测都可靠。

  ⚠️ Windows 版差异：具体路径的探测（安装目录候选、版本号来源）全部交给 `winenv.py`。
     Windows 上没有 Info.plist，版本号改为**直接解析 asar 头部的 package.json**
     （macOS 真机已用 298MB 的 asar 交叉验证，结果与 Info.plist 一致）。
     本文件除 `client_fingerprint()` 与 `_scan_asar` 的取路径方式外，与 macOS 版一致。

【切换机制】客户端源码原文（`packages/workbuddy-core` … `CloudAccountRepo`）：
     /**
      * billing/ambassador 等接口的路径前缀。
      * - Web（Cloud）：空串 → `/billing/meter/...`（走浏览器 cookie 认证）
      * - Desktop：覆盖为 `/v2` → `/v2/billing/meter/...`（走 IDE 网关 Bearer token）
      */
     get billingPrefix() { return ""; }
     …
     var DesktopAccountRepo = class extends CloudAccountRepo {
         /** Desktop 走 IDE 网关，billing/ambassador 等接口需要 /v2 前缀 */
         get billingPrefix() { return "/v2"; }
     }

  → 结论：存在**两种**变化，性质完全不同，都不要去预测周期：

    (1) 前缀分叉（结构性，几乎不变）
        同一批接口在 Web 与 Desktop 下走不同前缀。本脚本用 Bearer token，
        属 Desktop 那一支 → 必须带 `/v2`。这是**架构决定的**，不是轮换。

    (2) 接口改名 / 替换（功能迭代，无固定周期）
        `checkin-status` → `checkin-activity-status`（后者多返回活动名、主题、
        赛季、结束时间、可领积分等）。**跟客户端发版走**，腾讯发一版就变一次。
        新旧会并存一段时间：本机 app.asar（客户端 5.5.6）里两套字符串都在，
        旧路径由网关兜底并返回退化数据 —— 这就是"静默失效"的温床。

  因此本模块**不猜周期**，而是每次跑之前从 app.asar 现读一遍当前端点，
  再探活 + 做"全零载荷"退化检测。客户端一升级，这里立刻跟上。

本模块提供的能力：
  - discover()            从本机客户端提取当前端点（按客户端指纹缓存，避免每次扫 298MB）
  - status_candidates()   状态查询端点候选（按客户端权威顺序）
  - claim_candidates()    领取端点候选
  - client_version()      客户端版本（供 User-Agent 用，避免伪装成老版本）
  - payload_trustworthy() 「全零载荷」判定 —— 本模块的核心保险
  - preflight()           探活 + 退化检测 + 变更检测，返回结构化报告
  - 变更历史写进 api_endpoints.json，用于事后回答"到底多久换一次"

降级策略：读不到客户端（未安装 / 非 macOS / 权限不足）时退回内置候选，
标记 source="fallback"，**不阻断主流程**（宁可照旧跑，也不能因校验失败而不签到）。

安全：只读本机文件；探活请求不发任何新东西、不改变服务端状态；不触碰领取接口
（领取接口一旦调用就可能真的领了，绝不做"探针调用"，其正确性由客户端源码背书）。
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))   # 上一层 = 项目根，winenv.py 在那里

import http_client  # noqa: E402
import winenv       # noqa: E402  跨平台适配层（客户端路径 / 版本探测）
import paths        # noqa: E402

HERE = _HERE
CACHE = paths.cache_path("api_endpoints.json")

# app.asar 读不到时的兜底候选（顺序即优先级）
FALLBACK_STATUS = (
    "/v2/billing/meter/checkin-activity-status",
    "/billing/meter/checkin-activity-status",
    "/v2/billing/meter/checkin-status",
    "/billing/meter/checkin-status",
)
FALLBACK_CLAIM = (
    "/v2/billing/meter/daily-checkin",
    "/billing/meter/daily-checkin",
)

# 载荷可信度判据用到的字段：只要有任意一个非空/非零，就说明这是"这个账号的真实数据"
# 而不是网关返回的空壳。2026-09-17 的旧接口正是**全零**。
TRUST_FIELDS = (
    "activity_name", "theme_name", "end_time", "start_time",
    "checkin_dates", "total_credits", "streak_days", "daily_credit",
    "today_credit", "claim_button_text", "season",
)

# ---------------------------------------------------------------- 客户端指纹

_RE_PREFIX = re.compile(rb'get\s+billingPrefix\s*\(\s*\)\s*\{\s*return\s+"([^"]*)"')
_RE_METER_NAME = re.compile(rb'billing/meter/([A-Za-z0-9_\-]+)')
_RE_VERSION = re.compile(rb'(\d+\.\d+\.\d+)')


def client_fingerprint() -> dict:
    """客户端指纹：版本 + asar 大小 + asar mtime。

    三者任一变化即认为客户端被升级/替换过 —— 此时必须重新扫描，
    因为这正是"接口可能换了"的时刻。

    ★ Windows 版：路径与版本的探测全部交给 `winenv.detect_client()`，
      本函数只做结构转换，与 macOS 版语义完全一致。
    """
    c = winenv.detect_client()
    return {
        "app": c.get("root"),
        "path": c.get("asar"),
        "version": c.get("version"),
        "version_source": c.get("version_source"),
        "asar_size": c.get("asar_size"),
        "asar_mtime": c.get("asar_mtime"),
        "available": bool(c.get("available")),
    }


def client_version() -> str | None:
    return client_fingerprint().get("version")


# ---------------------------------------------------------------- app.asar 扫描

def _scan_asar(path: str) -> dict:
    """扫描 app.asar，提取 billingPrefix 取值与所有 billing/meter 端点名。

    只做正则提取，不执行任何客户端代码。返回值不含敏感信息。
    """
    raw = pathlib.Path(path).read_bytes()

    prefixes: list[str] = []
    for m in _RE_PREFIX.finditer(raw):
        v = m.group(1).decode("utf-8", "replace")
        if v not in prefixes:
            prefixes.append(v)

    names: list[str] = []
    for m in _RE_METER_NAME.finditer(raw):
        n = m.group(1).decode("utf-8", "replace")
        if n not in names:
            names.append(n)

    return {"prefixes": prefixes, "meter_names": names}


def _build_paths(names: list[str], prefixes: list[str]) -> list[str]:
    """把端点名 + 前缀组合成候选路径列表（去重、保持顺序）。"""
    out: list[str] = []
    for n in names:
        for p in prefixes:
            path = p + "/billing/meter/" + n
            if path not in out:
                out.append(path)
    return out


def _status_names(meter_names: list[str]) -> list[str]:
    """挑出"状态查询"类端点名，按可信度排序（名字里带 activity 的最优）。

    这是本模块的**前瞻性**设计：不做硬编码白名单，而是按语义规则排序。
    将来腾讯若再改名（例如加个 `-v3` 后缀），只要名字里还有 checkin/activity，
    这里就能自动认出来，不需要改代码。
    """
    cands = [n for n in meter_names if "checkin" in n and "daily" not in n]
    # activity 版返回完整活动数据；纯 status 版已被证实会返回全零
    cands.sort(key=lambda n: (0 if "activity" in n else 1, len(n)))
    return cands


def _claim_names(meter_names: list[str]) -> list[str]:
    """挑出"领取"类端点名（daily-checkin / checkin-claim 之类）。"""
    cands = [n for n in meter_names
             if "checkin" in n and ("daily" in n or "claim" in n or n == "checkin")]
    cands.sort(key=lambda n: (0 if "daily" in n else 1, len(n)))
    return cands


def _read_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _scan(force: bool = False) -> dict:
    """带缓存的扫描。缓存键 = 客户端指纹；指纹没变就不重扫。

    关于 `changed`：只有**真的重新扫描过**才可能为 True。
    缓存命中时恒为 False —— 否则「端点已变化」会在之后每次运行都重复报一次。
    变更事件追加进 `history`（只增不删，保留最近 40 条），用于事后回答
    "这套接口到底多久变一次"。
    """
    fp = client_fingerprint()
    cached = _read_cache()

    if not force and cached and cached.get("fingerprint") == fp and cached.get("status_paths"):
        out = dict(cached)
        out["changed"] = False        # 指纹未变 ⇒ 相对上次没有变化
        out["cached"] = True
        out.setdefault("history", [])
        return out

    prev = cached

    result: dict = {
        "fingerprint": fp,
        "source": "fallback",
        "billing_prefix": None,
        "status_paths": list(FALLBACK_STATUS),
        "claim_paths": list(FALLBACK_CLAIM),
        "preferred_status": FALLBACK_STATUS[0],
        "preferred_claim": FALLBACK_CLAIM[0],
        "scanned_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cached": False,
    }

    if fp["available"]:
        try:
            scan = _scan_asar(fp["path"])
            prefixes = scan["prefixes"] or [""]
            # Desktop 分支返回 "/v2" —— 我们用 Bearer token，属这一支，优先它
            if "/v2" in prefixes:
                prefixes = ["/v2"] + [p for p in prefixes if p != "/v2"]
            s_names = _status_names(scan["meter_names"])
            c_names = _claim_names(scan["meter_names"])
            if s_names and c_names:
                s_paths = _build_paths(s_names, prefixes)
                c_paths = _build_paths(c_names, prefixes)
                # 末尾补上兜底候选，保证候选集永不缩小
                s_paths += [p for p in FALLBACK_STATUS if p not in s_paths]
                c_paths += [p for p in FALLBACK_CLAIM if p not in c_paths]
                result.update({
                    "source": "asar",
                    "billing_prefix": prefixes[0],
                    "status_names": s_names,
                    "claim_names": c_names,
                    "status_paths": s_paths,
                    "claim_paths": c_paths,
                    "preferred_status": s_paths[0],
                    "preferred_claim": c_paths[0],
                })
        except Exception as e:  # noqa: BLE001 — 扫描失败退回兜底，不阻断
            result["scan_error"] = repr(e)[:200]

    # ---- 变更检测：与上一次记录的端点集比对，记录变更历史 ----
    old_status = prev.get("status_paths") or []
    old_claim = prev.get("claim_paths") or []
    hist = list(prev.get("history") or [])
    changed = bool(old_status and (old_status != result["status_paths"]
                                   or old_claim != result["claim_paths"]))
    result["changed"] = changed
    if changed:
        result["changed_from"] = {"status_paths": old_status, "claim_paths": old_claim}
        hist.append({
            "at": result["scanned_at"],
            "client_version": (fp.get("version") or ""),
            "from": {"status": old_status[:3], "claim": old_claim[:3]},
            "to": {"status": result["status_paths"][:3],
                   "claim": result["claim_paths"][:3]},
        })
    result["history"] = hist[-40:]

    try:
        CACHE.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


def discover(force: bool = False) -> dict:
    """公开入口：返回当前应当使用的端点集。"""
    return _scan(force=force)


def status_candidates(force: bool = False) -> tuple:
    return tuple(discover(force=force)["status_paths"])


def claim_candidates(force: bool = False) -> tuple:
    return tuple(discover(force=force)["claim_paths"])


# ---------------------------------------------------------------- 退化检测

def payload_trustworthy(d: dict) -> bool:
    """载荷是否可信 —— 本模块的核心保险。

    判据：只要有一个"真实数据"字段非空/非零，就认为这是本账号的真实数据。
    全零（active=false + 其余全空）则不可信：这正是 2026-09-17 旧接口的样子。

    为什么要这样判：活动**空档期**返回的 active 也是 false，但空档期里
    用户的累计积分 / 历史签到日期仍然存在 → 不会是全零。
    所以"全零"能干净地把「真的没活动」和「接口退化」区分开，误报率极低。
    """
    if d.get("active") is True:
        return True
    return any(d.get(k) for k in TRUST_FIELDS)


# 「路由不存在」类措辞 —— 服务端换了路径时通常这么回
_ROUTE_MISS_HINTS = ("not found", "no such", "not exist", "不存在", "unknown path",
                     "method not allowed", "invalid path", "unsupported",
                     "已下线", "已废弃", "deprecated")


def looks_like_route_miss(code, body: dict) -> str:
    """判断一次失败是「打错了路由」（= 接口漂移）还是「业务拒绝」。

    为什么必须分开：`code != 0` 的失败在用户眼里长得一样，但处置完全相反 ——
    业务拒绝（如「今天已签到」）重试无用但无害；路由不存在说明**端点变了**，
    必须立刻告警并要求核对端点。2026-09-17 就是没区分这两者，
    结果用一个已失效的旧端点静默空转了一整轮活动。

    返回 "" 表示「不像漂移」；否则返回一句可读的判据，供上层拼进告警文案。

    判据刻意保守（宁可漏报也不误报）：只在"响应根本不是我们这个接口的形态"时才认定
    —— 没有业务码字段、HTTP 级 404/405、或 errmsg 里出现明确的"不存在"措辞。
    """
    msg = str((body or {}).get("msg") or "")
    low = msg.lower()
    if code is None:
        return "响应里没有业务码字段（多半打到了别的路由）"
    if isinstance(code, int) and code in (404, 405):
        return "业务码 {}（路径或方法不存在）".format(code)
    for h in _ROUTE_MISS_HINTS:
        if h in low:
            return "服务端返回「{}」".format(msg[:60])
    return ""


# ---------------------------------------------------------------- 前置校验

def preflight(cred: dict | None = None, timeout: int = 12,
              probe: bool = True, force_scan: bool = False) -> dict:
    """领取前的完整前置校验。

    做三件事：
      1. 从本机客户端读出"当前应该是哪个端点"（客户端指纹变了就重新扫）
      2. 探活：逐个候选发一次**只读**的状态查询，找出第一个返回可信数据的
      3. 报告：端点是否相对上次发生了变化、是否只剩兜底可用

    绝不调用领取接口 —— 那会真的把积分领掉。领取路径的正确性由客户端源码背书，
    并由领取时的幂等码（10001=今日已领）兜底。

    返回结构化报告（不含 token）；任何异常都被吞掉并标记，避免影响主流程。
    """
    rep: dict = {"at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        d = discover(force=force_scan)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200], "at": rep["at"]}

    rep.update({
        "source": d.get("source"),
        "client_version": (d.get("fingerprint") or {}).get("version"),
        "billing_prefix": d.get("billing_prefix"),
        "status_names": d.get("status_names"),
        "claim_names": d.get("claim_names"),
        "preferred_status": d.get("preferred_status"),
        "preferred_claim": d.get("preferred_claim"),
        "changed": bool(d.get("changed")),
        "history_len": len(d.get("history") or []),
    })
    if d.get("changed"):
        rep["changed_from"] = d.get("changed_from")

    if not probe or not cred:
        rep["probed"] = False
        return rep

    host = (cred.get("domain") or "https://www.workbuddy.cn").rstrip("/")
    token = cred.get("access_token") or ""
    uid = cred.get("uid") or ""
    ua = "WorkBuddy/" + (rep.get("client_version") or "0")

    tried = []
    resolved = None
    for p in d["status_paths"]:
        item = {"path": p}
        try:
            code, body = http_client.post_json(
                host + p, body={}, token=token,
                extra_headers={"X-User-Id": uid}, user_agent=ua, timeout=timeout)
        except http_client.HttpTransportError as e:
            item.update({"http": None, "error": str(e)[:120]})
            tried.append(item)
            continue
        data = body.get("data") or {}
        item.update({"http": code,
                     "code": body.get("code"),
                     "trustworthy": bool(body.get("code") == 0
                                         and payload_trustworthy(data))})
        tried.append(item)
        if item["trustworthy"]:
            resolved = p
            break
        if code == 401:
            rep["unauthorized"] = True
            break

    rep["probed"] = True
    rep["tried"] = tried
    rep["resolved_status"] = resolved
    rep["degraded_fallback"] = bool(resolved is None and any(
        t.get("code") == 0 for t in tried))
    rep["ok"] = bool(resolved)
    return rep


# ---------------------------------------------------------------- CLI

def main() -> None:
    force = "--force" in sys.argv
    cred = None
    try:
        import credentials  # 局部导入：CLI 才需要
        cred = credentials.load_credentials()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"warn": "登录态不可用，仅做本地发现：" + repr(e)[:120]},
                         ensure_ascii=False))
    rep = preflight(cred=cred, probe=cred is not None, force_scan=force)
    print(json.dumps(rep, ensure_ascii=False, indent=2))

    if rep.get("source") == "asar":
        print("\n客户端声明的端点：")
        print("  状态查询（首选）：", rep.get("preferred_status"))
        print("  领取（首选）：    ", rep.get("preferred_claim"))
        if rep.get("resolved_status"):
            print("  实测可用：        ", rep["resolved_status"])
        if rep.get("changed"):
            print("\n⚠️ 端点相对上次已变化 —— 变更历史：")
            for h in (discover().get("history") or [])[-5:]:
                print("  ", h)


if __name__ == "__main__":
    main()
