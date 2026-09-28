#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
credentials.py — WorkBuddy 积分助手 · 统一登录态读取（Phase 1）

设计要点：
  - 新版明文登录态优先，多路径候选 + 统一安全规则
  - 旧版 state.vscdb + Electron safeStorage 回退已于 2026-09-28 移除
    （理由见下方 ⚠️ 段落）
  - 登录态结构使用 account.uid + auth.accessToken

登录态只有一个来源：`workbuddy-desktop.info`（纯 Python 读取，无需任何外部运行时）。
       macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       Windows: %LOCALAPPDATA%（优先）/ %APPDATA% 下的 CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       Linux:   ~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       兜底：   ~/.workbuddy/auth/workbuddy-desktop.info（便携版 / 未知布局）
     结构：{ account: { uid, ... }, auth: { accessToken, refreshToken, expiresAt, ... }, ... }

同一个文件里的 `auth.accessToken` 有两种形态，本模块都要能读：
  ① **明文 JWT** —— v5.5.x 及更早，或客户端关闭了字段加密。
  ② **at-rest 加密信封** `{"$wbEncrypted":1,"envelope":"<base64>"}` —— **v5.6.2 起默认开启**，
     委托同目录的 `atrest.py` 解密（AES-256-GCM；密钥自环境变量 / DPAPI / 运行中客户端
     进程内存取得）。本机实测解密正常。
  ⟹ 两种形态都试；解不开时报**准确原因**（见下），不要笼统地说「未找到登录态」。

统一返回结构（load_credentials()）：
  {
    "access_token": "...",   # 真实 token。等同账号密码，调用方负责保密：勿打印 / 勿写日志 / 勿落盘
    "uid": "...",            # 可能为空字符串
    "source": "workbuddy-desktop.info"
  }

⚠️ 2026-09-28 移除：旧版 `state.vscdb` + Electron safeStorage 回退链路。
  实测（Windows 11 / 客户端 5.6.2）该链路**已明确失效**，证据：
    · `{WorkBuddy,CodeBuddy}/User/globalStorage/state.vscdb` 四个候选**全部不存在**；
    · `CodeBuddyExtension` 下已无 User/globalStorage 布局（Local 与 Roaming 都没有）；
    · 全盘仅存的 state.vscdb 属于 **CodeBuddy CN / Trae CN**（另外两个应用），其密钥是
      `secret://…tencent-cloud.coding-copilot…`，与 WorkBuddy 桌面端登录态无关；
    · `_find_electron()` 在本机返回空 —— 该回退**不可能成功**，只会拖长失败路径，
      并把报错引向「请先安装并登录」这个错误方向（真实原因是字段加密）。
  故整链（sqlite3 读取 + 子进程调 Electron safeStorage + 临时文件）一并移除，
  同时省掉 `sqlite3`/`subprocess`/`tempfile`/`shutil` 四个依赖。
  副作用：`source` 字段不再可能是 `"state.vscdb"`。

安全规则（务必遵守）：
  - access_token 等同账号密码：仅在内存中使用，禁止输出到 stdout/日志、禁止保存副本、禁止提交仓库、禁止上传任何第三方
  - 只读：不修改 WorkBuddy 客户端的任何文件
  - 本模块不做任何网络请求
  - 解密得到的明文只在内存中流转，用完即弃，绝不落盘
  - atRestSecretKey 由 atrest.py 从进程内存读取，**同样不落盘**
"""

from __future__ import annotations

import json
import os
import sys

# 允许在同目录找到 atrest（WorkBuddy 5.6.2+ 登录态加密信封解密模块）。
# checkin.py / travel.py 会先把自己目录插入 sys.path，这里再兜底一次，
# 保证 credentials.py 无论被直接运行还是被上层模块 import 都能 import atrest。
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atrest  # noqa: E402

SOURCE_DESKTOP_INFO = "workbuddy-desktop.info"

# 候选登录态文件「存在但被系统拒读」（如 macOS 隐私保护/TCC）的记录，
# 仅用于生成准确的错误信息，不含任何敏感内容。
_PERM_DENIED: list[str] = []

# 候选登录态文件「存在但 accessToken 是加密信封且解密失败」的记录，
# 仅用于生成准确的错误信息，不含任何敏感内容。
_ENCRYPTED_FOUND: list[str] = []


class CredentialError(RuntimeError):
    """登录态读取失败的统一异常。"""


# ---------------------------------------------------------------------------
# 平台基础目录
# ---------------------------------------------------------------------------
def _home() -> str:
    return os.path.expanduser("~")


def _appdata() -> str:
    return os.environ.get("APPDATA", "")


def _localappdata() -> str:
    return os.environ.get("LOCALAPPDATA", "")


def _xdg_config() -> str:
    return os.environ.get("XDG_CONFIG_HOME", os.path.join(_home(), ".config"))


# ---------------------------------------------------------------------------
# 候选路径
# ---------------------------------------------------------------------------
# ★ Windows 上必须**同时**列 %APPDATA%(Roaming) 与 %LOCALAPPDATA%(Local)。
#   2026-09-20 实测：桌面端把新版明文登录态写进了
#     %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
#   而旧实现只找 %APPDATA%（Roaming）→ 于是**明明已登录却报「读不到登录态」**，
#   签到与领取全部空转，报错文案还会把人引向「请先登录」这个错误方向。
#   Electron 应用把用户数据放 Roaming 还是 Local 取决于打包方，两个都列、
#   逐个探测、哪边有就用哪边 —— 与 winenv 里 asar 候选的做法一致。
def desktop_info_candidates() -> list[str]:
    """登录态候选路径（按平台，**顺序即优先级**）。

    ★ Windows 上必须**同时**列 %LOCALAPPDATA%(Local) 与 %APPDATA%(Roaming)。
      2026-09-20 实测：桌面端把登录态写进了
        %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
      而当时只找 %APPDATA%（Roaming）→ **明明已登录却报「读不到登录态」**，
      签到与领取全部空转，报错还会把人引向「请先登录」这个错误方向。
      Electron 应用把用户数据放 Roaming 还是 Local 取决于打包方，两个都列、
      逐个探测、哪边有就用哪边 —— 与 winenv 里 asar 候选的做法一致。
      （2026-09-28 复核：本机仍是 **Local** 命中，Roaming 那条不存在。）

    ★ 末尾追加一条 `~/.workbuddy/auth/workbuddy-desktop.info` 兜底
      （社区实现常用的候选表）：便携版或将来
      客户端改布局时，这条能兜住而不用改代码。本机当前不存在，属无害候选。
    """
    rel = os.path.join(
        "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
    )
    portable = os.path.join(_home(), ".workbuddy", "auth", "workbuddy-desktop.info")
    if sys.platform == "darwin":
        out = [os.path.join(_home(), "Library", "Application Support", rel)]
    elif sys.platform == "win32":
        out = []
        # Local 优先（实测命中目录），再退 Roaming
        for base in (_localappdata(), _appdata()):
            if base:
                p = os.path.join(base, rel)
                if p not in out:
                    out.append(p)
        # 两个环境变量都取不到时保留一条，维持原有行为（不去猜其它路径）
        if not out:
            out = [os.path.join(_appdata(), rel)]
    else:
        out = [os.path.join(_xdg_config(), rel)]
    if portable not in out:
        out.append(portable)
    return out


# ---------------------------------------------------------------------------
# 域标准化（登录态里的 auth.domain 才是官方认定的接口 host）
# ---------------------------------------------------------------------------
def normalize_domain(raw) -> str:
    """把登录态里的 domain 标准化为 https://host（去尾斜杠）。非法值返回空串。"""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    d = raw.strip()
    if not d.startswith(("http://", "https://")):
        d = "https://" + d
    return d.rstrip("/")


# ---------------------------------------------------------------------------
# A. 新版明文（纯 Python）
# ---------------------------------------------------------------------------
def _load_plaintext() -> dict | None:
    """返回登录态 dict；找不到返回 None；若候选路径存在但被系统拒读，
    通过 _PERM_DENIED 记录路径（供 load_credentials 生成准确的错误信息），
    避免把「权限被拒」误报成「文件不存在」。"""
    for path in desktop_info_candidates():
        try:
            os.stat(path)
        except FileNotFoundError:
            continue
        except PermissionError:
            _PERM_DENIED.append(path)
            continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            # 文件损坏 / 写入中 / 权限问题：忽略，尝试下一个候选 / 落入旧版分支
            continue
        account = data.get("account") or {}
        auth = data.get("auth") or {}
        token = auth.get("accessToken")
        # 明文 JWT（5.5.x 及更早 / 加密未开启）
        if isinstance(token, str) and token:
            uid = account.get("uid") or auth.get("uid") or ""
            return {
                "access_token": token,
                "uid": uid,
                "domain": normalize_domain(auth.get("domain") or data.get("domain")),
                "source": SOURCE_DESKTOP_INFO,
            }
        # 5.6.2+ 加密信封：AES-256-GCM 解密后取明文 JWT
        if atrest.is_envelope(token):
            try:
                token = atrest.decrypt_token(token)
            except atrest.AtRestError:
                _ENCRYPTED_FOUND.append(path)
                continue
            if isinstance(token, str) and token:
                uid = account.get("uid") or auth.get("uid") or ""
                return {
                    "access_token": token,
                    "uid": uid,
                    "domain": normalize_domain(auth.get("domain") or data.get("domain")),
                    "source": SOURCE_DESKTOP_INFO,
                }
    return None


# ---------------------------------------------------------------------------
# B. 【已移除】旧版 state.vscdb + Electron safeStorage 回退链路
# ---------------------------------------------------------------------------
# 2026-09-28 删除。该链路在客户端 5.6.2 上已**明确失效**，证据见文件头 ⚠️ 段落：
#   · {WorkBuddy,CodeBuddy}\User\globalStorage\state.vscdb 四个候选全不存在；
#   · CodeBuddyExtension 下已无 User\globalStorage 布局；
#   · 现存 state.vscdb 属于 CodeBuddy CN / Trae CN，与 WorkBuddy 桌面端无关；
#   · _find_electron() 返回空 —— 该路径**不可能成功**。
# 删掉的是：sqlite3 只读取加密会话 + 子进程调 Electron safeStorage.decryptString()
# + 两个临时文件的生命周期管理。留下的唯一影响：source 不再可能是 "state.vscdb"。


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def load_credentials() -> dict:
    """
    读取本地登录态，返回统一结构 {"access_token","uid","domain","source"}。
    唯一来源是 workbuddy-desktop.info（accessToken 明文或 5.6.2+ 加密信封都支持）。
    失败时抛出 CredentialError（信息中不含任何 token）。

    ★ 错误信息必须指向**真实原因**（2026-09-28）：以前无论哪种失败都报
      「新版明文文件与旧版 state.vscdb 均未命中，请先安装并登录 WorkBuddy」——
      客户端 5.6.2 引入字段加密后，这句话把人引向「重新登录」，
      而真正该做的是「让客户端跑起来以便取到解密密钥」。故分三档报错。
    """
    cred = _load_plaintext()
    if cred:
        return cred
    if _PERM_DENIED:
        raise CredentialError(
            "登录态文件存在但被系统拒绝读取（权限/隐私保护，如 macOS TCC）："
            + "；".join(_PERM_DENIED)
            + "。请给运行终端授权「完全磁盘访问」，或改在 WorkBuddy 自动化/桌面端上下文运行。"
        )
    if _ENCRYPTED_FOUND:
        raise CredentialError(
            "登录态存在但 accessToken 已被 5.6.2+ 客户端加密，且未能解出密钥："
            + "；".join(_ENCRYPTED_FOUND)
            + "。请确保 WorkBuddy 客户端**正在运行**并已登录、脚本与客户端同一用户；"
            "或设置环境变量 WORKBUDDY_ATREST_KEY / WORKBUDDY_ATREST_KEY_FILE 显式指定密钥。"
        )
    raise CredentialError(
        "未找到登录态文件（workbuddy-desktop.info）。请先安装并登录 WorkBuddy 桌面端，"
        "并确认脚本与客户端以**同一用户**运行。已探测："
        + "；".join(desktop_info_candidates())
    )


# ---------------------------------------------------------------------------
# 脱敏工具（用于安全打印/日志，绝不输出 token 本体）
# ---------------------------------------------------------------------------
def mask_secret(secret: str) -> str:
    """对 access_token 这类高敏感串：不展示任何字符，仅说明已读取及长度。"""
    if not secret:
        return "<空>"
    return "<已读取（不展示）, 长度 {}>".format(len(secret))


def mask_uid(uid: str) -> str:
    """对 uid：保留首尾少量字符，中间打码。"""
    if not uid:
        return "<空>"
    if len(uid) <= 6:
        return "*" * len(uid)
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


def describe(cred: dict) -> dict:
    """把 load_credentials() 的结果转成可安全打印的脱敏描述（不含真实 token）。"""
    return {
        "source": cred.get("source", ""),
        "access_token": mask_secret(cred.get("access_token", "")),
        "uid": mask_uid(cred.get("uid", "")),
    }


if __name__ == "__main__":
    # 直接运行时仅打印脱敏结果，绝不打印真实 token。
    try:
        c = load_credentials()
    except CredentialError as e:
        print("❌ 读取登录态失败：{}".format(e))
        sys.exit(1)
    d = describe(c)
    print("✅ 找到登录态来源：{}".format(d["source"]))
    print("✅ access_token：{}".format(d["access_token"]))
    print("✅ uid：{}".format(d["uid"]))
