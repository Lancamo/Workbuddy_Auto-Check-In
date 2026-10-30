#!/usr/bin/env python3
"""通知排版与合并的离线桩测（2026-10-28 改版）。

覆盖三件容易出错的事：
  1. 第二条消息只列新变化，但**不能因此漏掉内容**（没变化时退回完整版）；
  2. 失败告警在「精简模式」下仍必须完整 —— 故障时用户正需要对照全部三项；
  3. tasks 与 growth 合并成一条，但 growth 没收益时 tasks 必须被补发，
     不能让「省钱」变成「少报收益」。

不联网、不发真实消息：只桩 `_notify` 与两个子模块的 `run_daily`。
"""

from __future__ import annotations

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))
sys.path.insert(0, HERE)

import catchup as cu  # noqa: E402

FAILED: list[str] = []
SENT: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ {}  {}".format(name, detail))
        FAILED.append(name)


def fake_notify(title: str, content: str, level: str) -> dict:
    SENT.append((title, content, level))
    return {"sent": True, "wechat": True}


def fake_log(msg: str) -> None:
    """桩掉日志：`_maybe_run_*` 内部会写 catchup.log，跑测试不该污染真实日志
    （踩过：假记录里出现「growth 兑换 14d 档」，事后看日志会误判真兑过）。"""
    LOGGED.append(msg)


LOGGED: list[str] = []


class FakeCredits:
    """桩掉余额查询：S4 走 `_report`，那里会真去查一次积分余额 —— 桩测必须离线。"""
    @staticmethod
    def query_balance(*_a, **_k) -> dict:
        return {"total": 2599, "raw": "2599.02", "plan": "体验版"}


def reset() -> None:
    SENT.clear()


def s1() -> None:
    print("S1 第一条：标题带累计连签，正文写本月连登")
    reset()
    st = {"checkin_done": True, "claim_done": False,
          "duty": {"name": "暴富喵", "rarity": "UR"}}
    c = {"status": "success", "credit": 100, "streak_days": 29, "activity": "Buddy应用"}
    t = {"status": "departed", "duty": {"status": "ok",
                                        "current": {"name": "暴富喵", "rarity": "UR"}}}
    ima = {"body": "成功，+50 算力，本周已签 3 天", "done": True, "new": True}

    title = cu._snapshot_title(st, ima, 29)
    body = cu._compose(st, c, t, ima, "2026-10-28 10:34", month_days=28,
                       balance=2599)
    check("标题含「连续 29 天」", "连续 29 天" in title, title)
    check("标题含完成度", "2/3 完成" in title, title)
    check("正文写「本月连续签到 28 天」", "本月连续签到 28 天" in body, body)
    check("正文不再出现「连续签到 29 天」", "连续签到 29 天" not in body)
    check("三项齐全", body.count("、WorkBuddy") == 2 and "ima 算力" in body, body)
    check("保留进度与时间", "今日进度" in body and "时间：" in body)
    check("余额行在「今日进度」与「时间」之间",
          body.index("今日进度") < body.index("积分余额：2599") < body.index("时间："),
          body)


def s2() -> None:
    print("S2 第二条：只列新变化（旅行到账），里程不再重复")
    st = {"checkin_done": True, "claim_done": True, "msg_first_sent": True,
          "duty": {"name": "暴富喵", "rarity": "UR"}}
    c = {"status": "already_checked", "activity": "Buddy应用"}
    t = {"status": "claimed", "reward_credit": 7}
    ima = {"body": "今日已领过，本周已签 3 天", "done": True, "new": False}
    body = cu._compose(st, c, t, ima, "2026-10-28 11:34", month_days=28, brief=True)
    check("只出现旅行一项", body.count("、WorkBuddy") == 1, body)
    check("旅行是第 1 项", body.startswith("1、WorkBuddy 旅行："), body.split("\n")[0])
    check("没有「今日已领过」的签到行", "今日已领过（Buddy应用）" not in body)
    check("没有 ima 重复行", "本周已签 3 天" not in body)
    check("仍保留今日进度（三项）", body.count("/") == 2, body)
    check("仍保留时间", "时间：" in body)
    check("没有空编号", "2、" not in body)


def s3() -> None:
    print("S3 第二条但三项都没新变化：退回完整版，不发空消息")
    st = {"checkin_done": True, "claim_done": True, "msg_first_sent": True}
    c = {"status": "already_checked"}
    t = {"status": "traveling"}
    ima = {"body": "今日已领过", "done": True, "new": False}
    body = cu._compose(st, c, t, ima, "2026-10-28 12:00", month_days=28, brief=True)
    check("退回完整版（含签到项）", "WorkBuddy 签到" in body, body)
    check("行数不为空", len(body.strip().split("\n")) >= 4)


def s4() -> None:
    print("S4 失败告警即使处于精简模式，正文仍完整")
    reset()
    cu._notify = fake_notify
    st = {"checkin_done": True, "claim_done": False, "msg_first_sent": True}
    c = {"status": "already_checked"}
    t = {"status": "failed", "reason": "网络错误"}
    cu._report(c, t, st, {}, None)
    check("发了 1 条", len(SENT) == 1, str(len(SENT)))
    title, body, level = SENT[0]
    check("级别是 failure", level == "failure", level)
    check("正文含签到项（完整版）", "WorkBuddy 签到" in body, body)
    check("正文含旅行项", "WorkBuddy 旅行" in body)
    check("带了处理建议", "处理建议" in body, body)


def s5() -> None:
    print("S5 tasks 有成果 → 先攒着，不立即发")
    reset()
    cu._notify = fake_notify
    cu.tasks = type("T", (), {"run_daily": staticmethod(lambda: {
        "claimed": [{"title": "体验优秀灵感", "credit": 100, "energy": 5}],
        "credit": 100, "energy": 5})})()
    st = {}
    cu._maybe_run_tasks(st, 0, "2026-10-28")
    check("没发消息", len(SENT) == 0, str(len(SENT)))
    check("攒进了 tasks_pending", bool(st.get("tasks_pending")), str(st.keys()))
    check("记了明细行", "体验优秀灵感" in (st["tasks_pending"]["lines"][0]))


def s6() -> None:
    print("S6 growth 同轮也有收益 → 合并成一条")
    reset()
    cu._notify = fake_notify
    st = {"tasks_pending": {"lines": ["1、体验优秀灵感：+100 积分 +5 能量"],
                            "amt": ["+100 积分"], "count": 1}}
    cu.growth = type("G", (), {"run_daily": staticmethod(lambda: {
        "changed": True, "credit": 50, "energy": 2,
        "redeemed": [{"tier": "14d", "credit": 50, "energy": 2, "cards": 1, "chances": 1}],
        "madeup": [], "wins": [], "opened": None, "physical": []})})()
    cu._maybe_run_growth(st, 0, "2026-10-28")
    check("只发 1 条", len(SENT) == 1, str(len(SENT)))
    title, body, _ = SENT[0]
    check("标题点明两件事", "任务" in title and "连登" in title, title)
    check("正文含任务行", "体验优秀灵感" in body, body)
    check("正文含兑换行", "连登兑换" in body, body)
    check("只有一个时间行", body.count("时间：") == 1, str(body.count("时间：")))
    check("pending 已清空", "tasks_pending" not in st)


def s7() -> None:
    print("S7 growth 本轮无收益 → tasks 必须被补发（不能丢）")
    reset()
    cu._notify = fake_notify
    st = {"tasks_pending": {"lines": ["1、体验优秀灵感：+100 积分"],
                            "amt": ["+100 积分"], "count": 1}}
    cu._flush_growth_pending(st)
    check("补发了 1 条", len(SENT) == 1, str(len(SENT)))
    title, body, level = SENT[0]
    check("标题写任务金额", "+100 积分" in title, title)
    check("正文含明细", "体验优秀灵感" in body, body)
    check("带时间", "时间：" in body)
    check("pending 已清空", "tasks_pending" not in st)
    # 再 flush 一次不应重复
    cu._flush_growth_pending(st)
    check("二次 flush 不重复发", len(SENT) == 1, str(len(SENT)))


def s8() -> None:
    print("S8 没有 pending 时 flush 静默")
    reset()
    cu._notify = fake_notify
    cu._flush_growth_pending({})
    check("不发消息", len(SENT) == 0, str(len(SENT)))


def s9() -> None:
    print("S9 余额查不到：整行不显示，不留占位、不影响其余内容")
    st = {"checkin_done": True, "claim_done": True}
    c = {"status": "success", "credit": 100, "streak_days": 31}
    t = {"status": "claimed", "reward_credit": 5}
    ima = {"body": "已领取，+50 算力，本周已签 5 天", "done": True, "new": True}
    body = cu._compose(st, c, t, ima, "2026-10-30 07:05", month_days=22, balance=None)
    check("没有「积分余额」字样", "积分余额" not in body, body)
    check("不写「暂不可用」类占位", "暂不可用" not in body, body)
    check("进度与时间仍在", "今日进度" in body and "时间：" in body, body)


def main() -> int:
    print("通知排版与合并桩测\n")
    cu._notify = fake_notify
    cu.log = fake_log          # 别往真实 catchup.log 里写假记录
    cu.credits = FakeCredits   # 别真去查积分余额（离线）
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9):
        fn()
        print()
    if FAILED:
        print("失败 {} 项：{}".format(len(FAILED), "、".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
