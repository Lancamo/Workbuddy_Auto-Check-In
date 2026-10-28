#!/usr/bin/env python3
"""晚间活跃看门狗：晚上看一眼「今天热力墙点亮没有」，黑了就提醒一句。

────────────────────────────────────────────────────────────────────────────
为什么需要它
────────────────────────────────────────────────────────────────────────────
连登（streak）的门槛极低：当天活跃分 **> 0** 即算点亮，1 分也算。但实测
（`GET /activity/growth/heatmap/detail?date=`，见 Knowledge/桌宠与旅行收益.md）：

    chat_request_send   发一次对话请求     2 分   ← 唯一常规来源
    skill_info          查看技能信息       3 分
    expert_actual_use   真正使用专家       3 分
    growth_task_completed 完成成长任务     5 分
    buddy_open          开盲盒             0 分
    签到 / 旅行 / 兑换 / 补登              **不计分**

也就是说：**不打开 WorkBuddy 的那一天，热力墙必然是黑的**，连登当场断掉。
断 1 天就要花 1 张补登卡才可能接回（且卡是稀缺资源、每月上限 4 张），
而整月巅峰档（28 天）只有一次机会。

补登是事后补救，这个看门狗是**事前拦截**：21:30 还来得及，23:40 是最后一道。

────────────────────────────────────────────────────────────────────────────
它只读、不写业务数据
────────────────────────────────────────────────────────────────────────────
绝不替用户发对话去刷活跃 —— 那既是伪造使用行为，也没有收益（分数高低不兑换
任何奖励，见上表说明）。这里只做两件事：读一次活跃分、发一条提醒。

────────────────────────────────────────────────────────────────────────────
判定
────────────────────────────────────────────────────────────────────────────
    过了 21:30 且本档未提醒过 → 查今天活跃分
        分数 > 0  → 记「今日已点亮」，两档都不再查（省请求）
        分数 == 0 → 发提醒，记本档已提醒；23:40 仍会再查一次（那时更紧急）

取「最后一个已到点且未提醒的档」，所以 21:35 运行发 21:30 档的文案，
23:45 运行直接发 23:40 档的文案（不会补发一条已经过时的 21:30 提醒）。

────────────────────────────────────────────────────────────────────────────
为什么用独立状态文件
────────────────────────────────────────────────────────────────────────────
主 state（`runtime/state/state.json`）在跨日时会**整体重建成固定键的字典**
（catchup `_run()` 里那句 `state = {"day": today, ...}`），看门狗的标记写进去
会被清掉。最典型的撞车场景：一整天没开机，21:35 首次开机触发 —— 标记刚写就被
重置，于是同一晚会重复提醒。所以用自己的 `activity_state.json`。

接口（`GET /activity/growth/heatmap` 与 `/activity/growth/streak`）都是只读的，
失败时**不写 state**，等下一轮 5 分钟后重试。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

# 兼顾「被 catchup 导入」和「直接 python3 scripts/activity_watch.py」两种跑法
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: E402

try:
    import credentials
    import http_client
    import travel
    _DEPS_ERROR = None
except Exception as _e:  # noqa: BLE001 — 依赖缺失时模块仍可被 import，check() 报错
    credentials = http_client = travel = None
    _DEPS_ERROR = repr(_e)


UA = "Mozilla/5.0"
HEATMAP_PATH = "/activity/growth/heatmap"
STREAK_PATH = "/activity/growth/streak"
STATE_NAME = "activity_state.json"

# (时, 分, 档位标识)。按时间升序；判定时取「最后一个已到点且未提醒」的那个。
SLOTS = ((21, 30, "2130"), (23, 40, "2340"))


def _headers(uid: str) -> dict:
    return {"X-User-Id": uid, "Accept": "application/json"}


def _load_state() -> dict:
    try:
        p = paths.state_path(STATE_NAME)
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — 不存在/损坏一律当空，不影响主流程
        return {}


def _save_state(st: dict) -> None:
    try:
        p = paths.state_path(STATE_NAME)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def _get(path: str, timeout: int) -> dict:
    cred = credentials.load_credentials()
    host = travel.resolve_host(cred)
    token, uid = cred.get("access_token", ""), cred.get("uid", "")
    code, body = http_client.get_json(
        host + path, token=token, extra_headers=_headers(uid),
        timeout=timeout, user_agent=UA)
    if code != 200 or not isinstance(body, dict) or body.get("code") not in (0, None):
        raise RuntimeError("{} -> http={} code={}".format(
            path, code, (body or {}).get("code")))
    return body.get("data") or {}


def fetch_today_score(timeout: int = 20) -> int | None:
    """今天的活跃分。取不到返回 None（不当作 0，否则会误报「没点亮」）。"""
    data = _get(HEATMAP_PATH, timeout)
    today = data.get("today") or {}
    score = today.get("score")
    if score is None:
        return None
    return int(score)


def fetch_streak_days(timeout: int = 20) -> int | None:
    """当前连登天数。仅用于让提醒更有说服力，取不到就算了，不影响是否提醒。"""
    try:
        data = _get(STREAK_PATH, timeout)
        days = (data.get("streak") or {}).get("days")
        return int(days) if days is not None else None
    except Exception:  # noqa: BLE001
        return None


def _compose(slot: str, days: int | None, now: datetime.datetime) -> tuple[str, str]:
    """提醒文案。两档必须**不一样** —— notify 按 title+content 指纹去重，
    两个档位文案相同的话，第二条会被自己的去重逻辑吞掉。"""
    if slot == "2340":
        title = "最后 20 分钟，热力墙还是黑的"
        lines = ["今天活跃分还是 0，再不发一句话，连登就断在今天。",
                 "一句话就够（2 分即点亮），不用做别的。"]
    else:
        title = "今天热力墙还没点亮"
        lines = ["今天活跃分还是 0，热力墙是黑的。",
                 "连登门槛只要 1 分：发一句话（2 分）就点亮。"]
    if days:
        lines.append("当前连登 {} 天 —— 断了要花 1 张补登卡才可能接回来。".format(days))
    lines.append("（签到、旅行、兑换都不计活跃分，这一步只能你自己来）")
    lines.append("时间：" + now.strftime("%Y-%m-%d %H:%M"))
    return title, "\n".join(lines)


def check(now: datetime.datetime | None = None, timeout: int = 20) -> dict:
    """判断这一轮要不要提醒。返回 dict，`action` 取值：

      "idle"  → 未到点 / 今日已点亮 / 本档已提醒过，什么都不用做
      "lit"   → 查了，今天已点亮（会记住，今天不再查）
      "alert" → 查了，今天 0 分，附带 title/body 让调用方去发
      "error" → 读不到活跃分，本轮跳过（不写 state，下轮重试）
    """
    if _DEPS_ERROR:
        return {"action": "error", "reason": _DEPS_ERROR}
    now = now or datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")

    st = _load_state()
    if st.get("day") != today:
        st = {"day": today, "lit": False, "alerted": []}
    if st.get("lit"):
        return {"action": "idle", "reason": "今日已点亮"}

    hm = now.hour * 100 + now.minute
    slot = None
    for h, m, key in SLOTS:
        if hm >= h * 100 + m and key not in (st.get("alerted") or []):
            slot = key          # 不 break：取最后一个已到点的档
    if slot is None:
        return {"action": "idle", "reason": "未到提醒时间"}

    try:
        score = fetch_today_score(timeout)
    except Exception as e:  # noqa: BLE001
        return {"action": "error", "reason": "读活跃分失败：" + str(e)[:120]}
    if score is None:
        return {"action": "error", "reason": "活跃分字段缺失"}

    if score > 0:
        st["lit"] = True
        _save_state(st)
        return {"action": "lit", "score": score, "slot": slot}

    days = fetch_streak_days(timeout)
    st["alerted"] = sorted(set((st.get("alerted") or []) + [slot]))
    _save_state(st)
    title, body = _compose(slot, days, now)
    return {"action": "alert", "slot": slot, "score": score,
            "streak_days": days, "title": title, "body": body}


def snapshot(now: datetime.datetime | None = None) -> dict:
    """只读快照（排查用）：现在几点、两档状态、今天几分。不写任何文件。"""
    now = now or datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    st = _load_state()
    fresh = st.get("day") != today
    out = {
        "today": today,
        "now": now.strftime("%H:%M"),
        "slots": ["{:02d}:{:02d}".format(h, m) for h, m, _ in SLOTS],
        "state_day": st.get("day"),
        "lit": False if fresh else bool(st.get("lit")),
        "alerted": [] if fresh else (st.get("alerted") or []),
        "stale_state": fresh,
    }
    try:
        out["score"] = fetch_today_score()
    except Exception as e:  # noqa: BLE001
        out["score_error"] = str(e)[:160]
    try:
        out["streak_days"] = fetch_streak_days()
    except Exception as e:  # noqa: BLE001
        out["streak_error"] = str(e)[:160]
    return out


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(snapshot(), ensure_ascii=False))
        return 0
    if cmd == "check":
        # 手动跑一次判定；不会自动发通知（发不发由 catchup 决定）
        print(json.dumps(check(), ensure_ascii=False))
        return 0
    print(json.dumps({"error": "未知子命令（status / check）"}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
