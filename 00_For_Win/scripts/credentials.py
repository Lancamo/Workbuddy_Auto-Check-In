#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
credentials.py — WorkBuddy 积分助手 · 统一登录态读取（Phase 1）

设计要点：
  - 新版明文登录态优先，旧版 state.vscdb 作为回退
  - 提供跨平台路径候选和统一安全规则
  - 登录态结构使用 account.uid + auth.accessToken

支持两类登录态：
  A. 新版明文（WorkBuddy 桌面端 v5.3.8+，纯 Python 读取，主路径）
     文件：workbuddy-desktop.info
       macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       Windows: %APPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       Linux:   ~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
     结构：{ account: { uid, ... }, auth: { accessToken, refreshToken, expiresAt, ... }, ... }
  B. 旧版加密（回退路径，需要 Electron safeStorage 解密）
     文件：state.vscdb
       macOS:   ~/Library/Application Support/{WorkBuddy,CodeBuddy}/User/globalStorage/state.vscdb
       Windows: %APPDATA%/{WorkBuddy,CodeBuddy}/User/globalStorage/state.vscdb
       Linux:   ~/.config/{WorkBuddy,CodeBuddy}/User/globalStorage/state.vscdb
     说明：本模块仅用标准库 sqlite3 只读取出加密会话，再委托 Electron 的
           safeStorage.decryptString() 解密（macOS 命中钥匙串 / Windows DPAPI / Linux keyring）。
           纯 Python 无法解 Electron safeStorage，因此旧版分支必须能找到 Electron 二进制；
           找不到时抛出带指引的 CredentialError。

统一返回结构（load_credentials()）：
  {
    "access_token": "...",   # 真实 token。等同账号密码，调用方负责保密：勿打印 / 勿写日志 / 勿落盘
    "uid": "...",            # 可能为空字符串（旧版会话若无 uid 字段）
    "source": "workbuddy-desktop.info" | "state.vscdb"
  }

安全规则（务必遵守）：
  - access_token 等同账号密码：仅在内存中使用，禁止输出到 stdout/日志、禁止保存副本、禁止提交仓库、禁止上传任何第三方
  - 只读：不修改 WorkBuddy 客户端的任何文件（state.vscdb 以只读模式打开）
  - 本模块不做任何网络请求
  - 解密得到的明文只在内存中流转，用完即弃，临时文件仅存放"加密"会话并立即删除
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

APP_NAMES = ("WorkBuddy", "CodeBuddy")

# 旧版 vscdb 中的会话 key
LEGACY_SESSION_KEYS = (
    'secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}',
)

SOURCE_DESKTOP_INFO = "workbuddy-desktop.info"
SOURCE_VSCDB = "state.vscdb"

# 候选登录态文件「存在但被系统拒读」（如 macOS 隐私保护/TCC）的记录，
# 仅用于生成准确的错误信息，不含任何敏感内容。
_PERM_DENIED: list[str] = []


class CredentialError(RuntimeError):
    """登录态读取失败的统一异常。"""


# ---------------------------------------------------------------------------
# 平台基础目录
# ---------------------------------------------------------------------------
def _home() -> str:
    return os.path.expanduser("~")


def _appdata() -> str:
    return os.environ.get("APPDATA", "")


def _xdg_config() -> str:
    return os.environ.get("XDG_CONFIG_HOME", os.path.join(_home(), ".config"))


# ---------------------------------------------------------------------------
# 候选路径
# ---------------------------------------------------------------------------
def desktop_info_candidates() -> list[str]:
    """新版明文登录态候选路径（按平台）。"""
    rel = os.path.join(
        "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
    )
    if sys.platform == "darwin":
        return [os.path.join(_home(), "Library", "Application Support", rel)]
    if sys.platform == "win32":
        return [os.path.join(_appdata(), rel)]
    return [os.path.join(_xdg_config(), rel)]


def legacy_vscdb_candidates() -> list[str]:
    """旧版 state.vscdb 会话库候选路径（按平台）。"""
    if sys.platform == "darwin":
        roots = [os.path.join(_home(), "Library", "Application Support", a) for a in APP_NAMES]
    elif sys.platform == "win32":
        roots = [os.path.join(_appdata(), a) for a in APP_NAMES]
    else:
        roots = [os.path.join(_xdg_config(), a) for a in APP_NAMES]
    return [os.path.join(r, "User", "globalStorage", "state.vscdb") for r in roots]


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
# B. 旧版 state.vscdb（sqlite3 读加密会话 + Electron safeStorage 解密）
# ---------------------------------------------------------------------------
def _read_legacy_blob(db_path: str) -> str | None:
    """只读打开 state.vscdb，取出加密会话原始字符串（未解密）。绝不写库。"""
    uri = "file:{}?mode=ro".format(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise CredentialError("无法只读打开 state.vscdb: {}".format(e))
    try:
        cur = conn.cursor()
        for key in LEGACY_SESSION_KEYS:
            try:
                row = cur.execute(
                    "SELECT value FROM ItemTable WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error:
                continue
            if row and row[0]:
                return row[0]
    finally:
        conn.close()
    return None


def _find_electron() -> str:
    """定位 Electron 二进制（仅旧版分支需要）。可用 WB_REWARD_ELECTRON 显式指定。"""
    candidates = []
    env_path = os.environ.get("WB_REWARD_ELECTRON")
    if env_path:
        candidates.append(env_path)
    candidates += [
        os.path.join(
            _home(), ".workbuddy", "tools", "electron",
            "Electron.app", "Contents", "MacOS", "Electron",
        ),
        os.path.join(_home(), ".workbuddy", "tools", "electron", "electron.exe"),
        shutil.which("electron") or "",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


# 委托 Electron 解密的内嵌脚本：读取"加密"会话文件 → safeStorage 解密 →
# 仅把明文经 stdout 单行输出（DECRYPT_RESULT:<json>），不落盘。
# 解密脚本只处理本项目识别到的旧版会话格式。
_ELECTRON_DECRYPT_JS = r"""
"use strict";
const fs = require("fs");
const { app, safeStorage } = require("electron");
const APP_NAME = process.env.WB_REWARD_APP_NAME || "WorkBuddy";
app.setName(APP_NAME); // 必须在 ready 之前，保证钥匙串/DAPPI 绑定名正确
function emit(line) {
  process.stdout.write(line + "\n");
  setTimeout(() => app.exit(0), 150); // 延迟退出，确保 stdout flush
}
const blobPath = process.argv[process.argv.length - 1];
app.whenReady().then(() => {
  if (!safeStorage.isEncryptionAvailable()) {
    emit("DECRYPT_RESULT:ERR 系统加密不可用");
    return;
  }
  try {
    const raw = fs.readFileSync(blobPath, "utf8");
    const parsed = JSON.parse(raw);
    let buf = null;
    if (parsed && parsed.type === "Buffer" && Array.isArray(parsed.data)) {
      buf = Buffer.from(parsed.data);
    } else if (typeof parsed === "string") {
      buf = Buffer.from(parsed, "base64");
    } else if (Buffer.isBuffer(parsed)) {
      buf = parsed;
    }
    if (!buf) throw new Error("未知的存储格式");
    const decrypted = safeStorage.decryptString(buf);
    emit("DECRYPT_RESULT:" + decrypted);
  } catch (e) {
    emit("DECRYPT_RESULT:ERR " + e.message);
  }
});
"""


def _decrypt_legacy_blob(blob: str) -> str:
    """用 Electron safeStorage 解密加密会话，返回解密后的明文字符串（不落盘）。"""
    electron = _find_electron()
    if not electron:
        raise CredentialError(
            "旧版 state.vscdb 需要 Electron 运行时解密（safeStorage），但未找到 Electron。"
            "请安装 Electron 或设置环境变量 WB_REWARD_ELECTRON 指向可用的 Electron 二进制；"
            "若你是 v5.3.8+ 新版账户，应走明文分支（请确认 workbuddy-desktop.info 存在）。"
        )
    js_path = blob_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as jf:
            jf.write(_ELECTRON_DECRYPT_JS)
            js_path = jf.name
        with tempfile.NamedTemporaryFile("w", delete=False) as bf:
            bf.write(blob)  # 注意：临时文件存的是"加密"会话，不是明文 token
            blob_path = bf.name

        env = dict(os.environ)
        env.pop("ELECTRON_RUN_AS_NODE", None)  # Agent 沙箱常设此变量，需显式去除
        proc = subprocess.run(
            [electron, js_path, blob_path],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        payload = ""
        for line in proc.stdout.splitlines():
            if line.startswith("DECRYPT_RESULT:"):
                payload = line[len("DECRYPT_RESULT:"):]
                break
        if not payload:
            raise CredentialError(
                "Electron 解密无有效输出（可能并非以 Electron 运行，或脚本输出被截断）。"
            )
        if payload.startswith("ERR"):
            raise CredentialError("Electron 解密失败：" + payload[3:].strip())
        return payload
    finally:
        for p in (js_path, blob_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _load_legacy() -> dict | None:
    for db_path in legacy_vscdb_candidates():
        try:
            os.stat(db_path)
        except FileNotFoundError:
            continue
        except PermissionError:
            _PERM_DENIED.append(db_path)
            continue
        except OSError:
            continue
        blob = _read_legacy_blob(db_path)
        if not blob:
            continue
        decrypted = _decrypt_legacy_blob(blob)
        try:
            session = json.loads(decrypted)
        except json.JSONDecodeError:
            continue
        auth = session.get("auth") or {}
        account = session.get("account") or {}
        token = auth.get("accessToken")
        if isinstance(token, str) and token:
            uid = account.get("uid") or auth.get("uid") or ""
            return {
                "access_token": token,
                "uid": uid,
                "domain": normalize_domain(auth.get("domain") or session.get("domain")),
                "source": SOURCE_VSCDB,
            }
    return None


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def load_credentials() -> dict:
    """
    读取本地登录态，返回统一结构 {"access_token","uid","source"}。
    优先新版明文，缺失时回退旧版 state.vscdb。
    失败时抛出 CredentialError（信息中不含任何 token）。
    """
    cred = _load_plaintext()
    if cred:
        return cred
    cred = _load_legacy()
    if cred:
        return cred
    if _PERM_DENIED:
        raise CredentialError(
            "登录态文件存在但被系统拒绝读取（权限/隐私保护，如 macOS TCC）："
            + "；".join(_PERM_DENIED)
            + "。请给运行终端授权「完全磁盘访问」，或改在 WorkBuddy 自动化/桌面端上下文运行。"
        )
    raise CredentialError(
        "未找到可用登录态：新版明文文件与旧版 state.vscdb 均未命中。"
        "请先安装并登录 WorkBuddy 桌面端。"
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
