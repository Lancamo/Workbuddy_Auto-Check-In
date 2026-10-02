"""针对 2026-09-29 两处修复的验证（可反复运行，无需凭据的桩 + 一次真实短轮询）。

用法：在 `00_For_Mac/` 下 `python3 verify_notify_fixes.py`（任意 cwd 亦可）。

覆盖：
  A. _send_macos 的「假成功」修复 —— 非零 returncode 必须判失败、异常必须判失败、
     成功时必须清掉上次的错误、title 里的双引号不能再破坏 AppleScript。
  B. _blocked_state_skip_send 的配额保护逻辑 —— 边界逐条过。
  C. capture_inbound_once —— 真实短轮询一次，返回 bool 且耗时受控（不会像
     capture_context_token 那样等满 90 秒）。若此刻微信里恰好有待捕获的入站消息，
     还会顺带验证 context_token_ts 确实前进；没有则该项自动通过。
  D. flush_pending 队列守卫 + force 成功后的状态清理 —— 全程桩 + 临时状态文件，
     **不发真实消息、不占配额**。
  E. 运行时配置自洽 —— 补发间隔与每日上限必须相容（踩过：文件里 180、默认 240）。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notify  # noqa: E402

fails = []
checks = 0


def check(name, got, want):
    global checks
    checks += 1
    ok = got == want
    print("  %s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("       期望 %r，实际 %r" % (want, got))
        fails.append(name)


# ---------------------------------------------------------------- A. _send_macos
print("\n=== A. _send_macos（假成功修复）===")
_real_run = subprocess.run
try:
    def fake_nonzero(*a, **kw):
        return subprocess.CompletedProcess(a, 1, stdout=b"", stderr=b"boom: denied\n")

    def fake_zero(*a, **kw):
        return subprocess.CompletedProcess(a, 0, stdout=b"", stderr=b"")

    def fake_raise(*a, **kw):
        raise OSError("no GUI session")

    subprocess.run = fake_nonzero
    check("非零 returncode → False", notify._send_macos("t", "c"), False)
    check("非零 returncode → 记录 stderr", notify._last_macos_error, "boom: denied")
    check("退出码也带进 error 文本（stderr 为空时）",
          (setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
              a, 3, stdout=b"", stderr=b"")),
           notify._send_macos("t", "c"), notify._last_macos_error)[2], "osascript 退出码 3")

    subprocess.run = fake_zero
    check("returncode=0 → True", notify._send_macos("t", "c"), True)
    check("成功后清掉上次错误", notify._last_macos_error, "")

    subprocess.run = fake_raise
    check("异常 → False", notify._send_macos("t", "c"), False)
    check("异常也记 error", "no GUI session" in notify._last_macos_error, True)
finally:
    subprocess.run = _real_run

# title 里的双引号过去会生成非法 AppleScript（假成功掩盖）。
# 现在 title 已转义 → 真实调用必须成功。
notify._last_macos_error = ""
check("title 含双引号 → 真实调用仍成功（title 已转义）",
      notify._send_macos('bad " title', "c"), True)

# 真实调用
notify._last_macos_error = ""
check("真实调用（正常标题）→ True", notify._send_macos("验证用", "这条不会被看到太多次"), True)
check("真实调用后无残留错误", notify._last_macos_error, "")

# ------------------------------------------- B. _blocked_state_skip_send
print("\n=== B. _blocked_state_skip_send（配额保护）===")
cfg = dict(notify.DEFAULT_CONFIG)
now = time.time()
BLOCKED = "投递被拒：服务端拒绝为这条主动消息建立会话（ret=-2 prepare failed）"

# ★ 把「用户最后一次给机器人发消息的时刻」钉成已知值。默认实现 `_current_inbound_ts()`
#   会去读真实的 clawbot_state.json —— 本机只要刚收到过微信消息（2026-10-02 10:55 就是），
#   「已过 60 分钟」这类**造在过去**的场景就会被规则 1b（入站晚于上次失败 → 窗口已重开 →
#   放行）判成 0，断言随本机状态漂移。2026-10-02 实跑时正是这样挂掉一条。
_real_inbound = notify._current_inbound_ts
_inbound = {"ts": 0.0}
notify._current_inbound_ts = lambda: _inbound["ts"]

check("刚捕获到新 token → 不跳过",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now}, now, True), 0)
check("没有历史失败 → 不跳过",
      notify._blocked_state_skip_send(cfg, {}, now, False), 0)
check("网络类失败 → 不跳过",
      notify._blocked_state_skip_send(cfg, {"last_send_error": "请求超时",
                                            "last_send_error_ts": now}, now, False), 0)
check("缺 context_token → 跳过",
      notify._blocked_state_skip_send(
          cfg, {"last_send_error": "缺 context_token：服务端已受理",
                "last_send_error_ts": now}, now, False), 240)
check("投递被拒·刚失败 → 等满 240",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now}, now, False), 240)
check("投递被拒·已过 60 分钟 → 剩余 180",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now - 3600},
                                      now, False), 180)
check("投递被拒·已过 241 分钟 → 不跳过",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now - 241 * 60},
                                      now, False), 0)
check("关掉该保护（=0）→ 不跳过",
      notify._blocked_state_skip_send({**cfg, "clawbot_blocked_retry_minutes": 0},
                                      {"last_send_error": BLOCKED,
                                       "last_send_error_ts": now}, now, False), 0)
check("默认值就是 240", notify.DEFAULT_CONFIG.get("clawbot_blocked_retry_minutes"), 240)

# ---- 规则 1b：入站消息晚于上次失败 → 立刻放行（2026-10-02 修）
#   为什么不能只看 `refreshed`（本轮刚捕获到）：入站消息可能已被**别的路径**收走
#   （上一轮 tick / renew.py / 手工探测），游标一前进本次轮询就永远看不到它，
#   refreshed 恒为 False —— 用户按提示发完消息，仍要干等满 240 分钟。
_inbound["ts"] = now - 60
check("入站早于上次失败 → 不算新证据，仍按间隔跳过",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now}, now, False), 240)
_inbound["ts"] = now + 60
check("入站晚于上次失败 → 立刻放行（修掉的那个坑）",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now}, now, False), 0)
check("入站晚于失败 → 即便间隔还剩 200 分钟也放行",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now - 40 * 60},
                                      now, False), 0)
check("入站晚于失败 → 对「缺令牌」档同样放行",
      notify._blocked_state_skip_send(
          cfg, {"last_send_error": "缺 context_token：服务端已受理",
                "last_send_error_ts": now}, now, False), 0)
_inbound["ts"] = 0.0
check("没有入站记录（升级前的旧状态）→ 回落到按间隔跳过",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now}, now, False), 240)
check("入站规则不越过 refreshed 快路径",
      notify._blocked_state_skip_send(cfg, {"last_send_error": BLOCKED,
                                            "last_send_error_ts": now}, now, True), 0)

# 还原，免得影响后面的分组
notify._current_inbound_ts = _real_inbound

# ------------------------------------------- C. capture_inbound_once（真实短轮询）
print("\n=== C. capture_inbound_once（真实短轮询一次）===")
st_before = notify.clawbot.internal_state() or {}
ts_before = st_before.get("context_token_ts")
t0 = time.time()
got = notify.clawbot.capture_inbound_once(timeout=5)
elapsed = time.time() - t0
print("     返回 %r，耗时 %.1f 秒" % (got, elapsed))
check("返回值是 bool", isinstance(got, bool), True)
check("耗时不超过 15 秒（不该像 capture_context_token 那样等到 90 秒）", elapsed < 15, True)
st_after = notify.clawbot.internal_state() or {}
ts_after = st_after.get("context_token_ts")
check("捕获到消息时 context_token_ts 必须前进",
      (ts_after or 0) > (ts_before or 0) if got else True, True)
if got:
    print("     ✓ 捕获到入站消息 → context_token_ts 已从 %s 前进到 %s" % (ts_before, ts_after))

# ------------------------------------------- D. flush_pending 队列守卫
print("\n=== D. flush_pending 队列守卫 + force 状态清理（全桩，不发消息）===")
_STATE, _SEND, _CLAW = notify.STATE, notify.send, notify.clawbot
_SENDVIA, _MACOS = notify._send_via, notify._send_macos


class _FakeClaw:
    """只提供 notify 会碰到的接口，绝不联网。"""
    SESSION_EXPIRED_MARK = "会话已失效，请重新扫码"
    DELIVER_BLOCKED_MARK = "投递被拒"

    @staticmethod
    def load_channel():
        return {"base_url": "http://stub", "bot_token": "stub", "user_id": "stub"}

    @staticmethod
    def capture_inbound_once(*, timeout=5):
        return False


def _seed(**fields):
    base = {"pending": [], "sent": {}, "local_sent": {}, "daily": {}}
    base.update(fields)
    notify._save_state(base)


try:
    # 注意 STATE 是 pathlib.Path（_save_state 用 .write_text），别塞字符串
    notify.STATE = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    notify.clawbot = _FakeClaw
    notify._send_macos = lambda t, c: False      # 别真的弹通知
    calls = []

    # D1. 指纹已在 sent 表（= 已由别的路径送达）→ 丢弃，且不能再发一次
    notify.send = lambda *a, **k: (calls.append(a), {"wechat": False})[1]
    _seed(pending=[{"fp": "done-fp", "level": "success", "title": "T",
                    "content": "C", "ts": time.time()}],
          sent={"done-fp": time.time()})
    r = notify.flush_pending()
    check("已送达的条目 → 计入 already", r.get("already"), 1)
    check("已送达的条目 → 不再重复发送", len(calls), 0)
    check("已送达的条目 → 队列排空（这是修掉的卡死）", r.get("remaining"), 0)

    # D2. 未送达的条目 → 照旧走 send；失败则留在队列等下次
    calls[:] = []
    _seed(pending=[{"fp": "todo-fp", "level": "success", "title": "T2",
                    "content": "C2", "ts": time.time()}])
    r = notify.flush_pending()
    check("未送达的条目 → 确实调用了 send", len(calls), 1)
    check("未送达的条目 → 不误记 already", r.get("already"), 0)
    check("发送失败 → 保留在队列", r.get("remaining"), 1)

    # D3. force + 成功 → 必须清掉过期的失败标记与冷却
    notify._send_via = lambda ch, c_, t, c: (True, "clawbot 已送达（message_id=1）")
    _seed(last_send_error=BLOCKED, last_send_error_ts=time.time(),
          clawbot_cooldown_until=time.time() + 3600)
    # 必须用 _SEND（真身）：notify.send 已被 D1 替换成桩，这里要测真实分支
    r = _SEND("T3", "C3", level="success", force=True)
    stx = notify._load_state()
    check("force + 成功 → wechat=True", r.get("wechat"), True)
    check("force + 成功 → 清掉过期的 last_send_error", stx.get("last_send_error"), None)
    check("force + 成功 → 冷却解除",
          notify._clawbot_cooldown_remaining_min(stx, time.time()), 0)

    # D4. force + 失败 → 仍然不设冷却、不入队（force 的原语义不许被改坏）
    notify._send_via = lambda ch, c_, t, c: (False, BLOCKED)
    _seed()
    _SEND("T4", "C4", level="success", force=True)
    stx = notify._load_state()
    check("force + 失败 → 不设冷却",
          notify._clawbot_cooldown_remaining_min(stx, time.time()), 0)
    check("force + 失败 → 不入补发队列", len(stx.get("pending") or []), 0)
finally:
    notify.STATE, notify.send, notify.clawbot = _STATE, _SEND, _CLAW
    notify._send_via, notify._send_macos = _SENDVIA, _MACOS

# ------------------------------------------- E. 运行时配置自洽
print("\n=== E. 运行时配置自洽（补发间隔 vs 每日上限）===")
live = notify.load_config()
try:
    _retry = int(live.get("clawbot_blocked_retry_minutes")
                 or notify.DEFAULT_CONFIG["clawbot_blocked_retry_minutes"])
    _cap = int(live.get("max_pushes_per_day") or 8)
except (TypeError, ValueError):
    _retry, _cap = 0, 0
# 配额反推：每天最多「24h / 补发间隔」次补发，再加 2 次正常到账通知，不得越过每日上限。
# 否则补发会把正常通知的额度吃光 —— 2026-09-29 的真实事故正是这个形状。
print("     运行时：retry=%s 分钟，cap=%s 条/天" % (_retry, _cap))
check("补发间隔与每日上限自洽（补发次数 + 2 次正常 ≤ cap）",
      (24 * 60 // _retry + 2) <= _cap if _retry else False, True)
check("每日上限不超过 iLink 硬限 10", _cap <= 10, True)

print("\n================================")
print("%d 项检查，%d 项失败" % (checks, len(fails)))
if fails:
    for f in fails:
        print("  FAILED:", f)
sys.exit(1 if fails else 0)
