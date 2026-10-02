#!/bin/bash
# ============================================================
# daily_wake.sh —— 每日定时唤醒（macOS `pmset repeat`）
#
#   bash  daily_wake.sh status              # 看当前设没设（不需要 sudo）
#   sudo bash daily_wake.sh enable [HH:MM]  # 设置（默认 07:05）
#   sudo bash daily_wake.sh disable         # 取消
#   sudo bash daily_wake.sh test [分钟]     # 一次性验证（默认 3 分钟后）
#
# ── 为什么需要它 ─────────────────────────────────────────────
# 主脚本靠 LaunchAgent 的 StartInterval 调度，而**系统睡眠期间 launchd 不执行
# 任务**。所以「合盖时能不能签到」完全取决于 macOS 当次给的维护唤醒
# （DarkWake）窗口有多长，实测两者差一个数量级：
#
#   · `rtc/Maintenance` 型   常给 45 秒 —— 够跑完
#     （2026-10-01 07:05:35 的签到就是这么完成的：07:05:20 系统自己醒 45 秒
#      → 07:05:32 脚本被拉起 → 07:05:35 签到成功 + 微信送达 → 07:06:05 回睡）
#   · `rtc/SleepService` 型  常只有 2 秒 —— 起不来
#     （2026-10-02 早上 07:00–10:28 十余次全是这种，一次都没跑成，
#      最后靠用户 10:28 开盖把机器唤醒才补跑）
#
# 加这一条等于把「碰运气」换成「保底」：每天固定时刻把机器叫醒一次，
# launchd 立刻补跑主脚本，跑完系统按自己的空闲计时睡回去。
#
# ── 代价与边界（如实说明）────────────────────────────────────
# · 每天多一次唤醒 → 电池下有微量耗电；唤醒本身只持续到系统空闲计时结束。
# · 需要 sudo，且**必须由你手动执行**。本项目的 Python 代码至今不碰任何
#   pmset 写操作（见 README），这个脚本是唯一出口，且只在你显式调用时才动手。
# · `pmset repeat` 同一类型**只能存在一个**，再设会覆盖前一个。
# · 它**不替代**系统的维护唤醒（Power Nap）—— 那是系统行为，本项目既设不了
#   也取消不了；两者并存，本脚本只是额外加一条保底。
#
# ── 已知坑：zsh 不把 `#` 当注释（2026-10-02 实测）─────────────────
# 命令后面跟一段 `# 说明` 是再自然不过的写法，但 zsh 交互式默认**没有**开
# `interactive_comments`，所以：
#     sudo bash daily_wake.sh enable     # 设成每天 07:05
# 会被拆成 `enable` `#` `设成每天` `07:05` 四个参数传进来，$1 成了 `#`，
# 于是报「时刻格式应为 HH:MM（收到：#）」。这不是脚本的错，但脚本可以兜住：
# 下面 cmd_enable / cmd_test 会**丢弃第一个以 `#` 开头的参数及其后全部参数**，
# 并打一行提示。想彻底避免，就写 `setopt interactive_comments`，或不带注释执行。
# ============================================================
set -uo pipefail

DAYS="MTWRFSU"          # 每天。pmset 的星期记法：M=周一 … S=周六 U=周日
DEFAULT_AT="07:05"      # 签到闸门 07:00 之后 5 分钟
LABEL="com.workbuddy.wb-reward-catchup"    # 被保底唤醒触发的那个 LaunchAgent

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DIR="$(cd "$(dirname "$0")" && pwd)"

die() { echo "✗ $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "这个操作需要 root。请改用：sudo bash \"$SELF\" $1"
}

# 若第一个参数是 zsh 漏传进来的「注释残留」（见文件头说明），打印提示并返回 0 ——
# 调用方据此执行 `set --`。清参数的动作必须留在调用方：`set --` 清的是**当前函数自己**
# 的 positional parameters，写在辅助函数里对调用方无效（实现时踩过）。
comment_arg_pending() {
  [ "$#" -gt 0 ] && [ "${1#\#}" != "$1" ] || return 1
  echo "▸ 提示：zsh 默认不把 \`#\` 当注释，命令末尾那段说明被当成参数了，已忽略。"
  echo "  下次去掉注释即可，或先执行 setopt interactive_comments。"
  return 0
}

# 打印 pmset 认为的重复事件；没有则输出空
repeat_block() {
  pmset -g sched 2>/dev/null | sed -n '/[Rr]epeating power events/,/^[A-Z]/p' \
    | sed '$d'
}

cmd_status() {
  echo "▸ 电源事件一览（pmset -g sched）"
  pmset -g sched 2>/dev/null | sed 's/^/    /'
  echo
  if repeat_block | /usr/bin/grep -qiE "wake|poweron"; then
    echo "✓ 已设置重复唤醒 —— 每天有一次保底窗口。"
  else
    echo "✗ 未设置重复唤醒 —— 合盖期间能否签到完全取决于系统当次给的维护唤醒窗口。"
    echo "  实测那是「碰运气」：45 秒的窗口能成，2 秒的不行。想变成「保底」就执行："
    echo "      sudo bash \"$SELF\" enable"
  fi
  # status 只报告，永远成功退出 —— 别让「没设置」看起来像命令出错。
  return 0
}

cmd_enable() {
  need_root enable
  if comment_arg_pending "$@"; then set --; fi
  local at="${1:-$DEFAULT_AT}"
  # 用 BASH_REMATCH 取时分，**不要**用 ${at%%:*} 拆 —— 那样在 "abc" 这种输入下
  # 会把非数字传进 $((10#$hh))，bash 在算术上下文里把它当变量名，配合 set -u
  # 直接报 "unbound variable"（实测踩过）。走正则捕获组则保证拿到的一定是数字。
  # 另：**任何**输出文案里的变量都要写成 ${at} 这种带花括号的形式 —— `$at）`「变量紧跟全角
  # 字符」在 bash 3.2（macOS 自带的 /bin/bash 就是）下会被解析成变量名 `at）`，直接报
  # unbound variable。2026-10-02 补记：这条当时只改了 die 文案，`echo` 里的 `$LABEL）` 漏了，
  # 结果是唤醒**设置成功之后**脚本崩在最后那段说明上、退出码 1，白让用户以为失败。
  # 教训：这类修法必须扫全部输出语句，不能只看报错路径。
  if [[ ! "$at" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
    die "时刻格式应为 HH:MM，例如 07:05（收到：${at}）"
  fi
  local hh="${BASH_REMATCH[1]}" mm="${BASH_REMATCH[2]}"
  # 10# 强制十进制，否则 08/09 会被当成非法八进制
  if (( 10#$hh >= 24 || 10#$mm >= 60 )); then
    die "时刻超出范围：${at}（应在 00:00–23:59 之内）"
  fi

  pmset repeat wake "$DAYS" "$(printf '%02d:%02d:00' "$((10#$hh))" "$((10#$mm))")" \
    || die "pmset repeat 设置失败"
  echo "✓ 已设置：每天 ${at} 自动唤醒"
  echo
  cmd_status
  echo
  echo "说明："
  echo "  · 唤醒后 launchd 会立刻补跑主脚本（${LABEL}），跑完系统自己睡回去。"
  echo "  · 明早核对是否真的醒了："
  echo "      pmset -g log | grep -E 'Wake |DarkWake' | tail -20"
  echo "      tail -5 \"$DIR/runtime/logs/catchup.log\""
  echo "  · 想立刻验证唤醒是否生效（要合盖才有意义）："
  echo "      sudo bash \"$SELF\" test"
}

cmd_disable() {
  need_root disable
  pmset repeat cancel || die "pmset repeat cancel 失败"
  echo "✓ 已取消重复电源事件"
  echo
  cmd_status
}

cmd_test() {
  need_root test
  if comment_arg_pending "$@"; then set --; fi
  local mins="${1:-3}"
  [[ "$mins" =~ ^[0-9]+$ ]] || die "分钟数应为整数（收到：${mins}）"
  local when
  when="$(date -v+"${mins}"M '+%m/%d/%y %H:%M:%S')" || die "计算唤醒时刻失败"
  pmset schedule wake "$when" || die "pmset schedule 设置失败"
  echo "✓ 已设一次性唤醒：${when}（约 ${mins} 分钟后）"
  echo
  echo "验证步骤（必须合盖才有意义）："
  echo "  1. 现在合上盖子，等 $mins 分钟以上；"
  echo "  2. 开盖后立刻看这两条："
  echo "       pmset -g log | grep -E 'Wake |DarkWake' | tail -10"
  echo "       tail -5 \"$DIR/runtime/logs/catchup.log\""
  echo "     若看到「Wake from」且紧接着主脚本被拉起，就说明唤醒路径可用。"
  echo "  3. 这条一次性事件执行完会自动消失，不用清理。"
}

case "${1:-status}" in
  status)          cmd_status ;;
  enable|on)       shift; cmd_enable "$@" ;;
  disable|off)     cmd_disable ;;
  test)            shift; cmd_test "$@" ;;
  *)
    cat <<HINT
用法：
  bash  $0 status              # 查看当前设置（无需 sudo）
  sudo bash $0 enable [HH:MM]  # 设置每日唤醒，默认 $DEFAULT_AT
  sudo bash $0 disable         # 取消
  sudo bash $0 test [分钟]     # 设一次性唤醒用于验证，默认 3 分钟后
HINT
    exit 2
    ;;
esac
