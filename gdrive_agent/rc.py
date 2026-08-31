"""rclone rcd 的 RPC 客户端。

传输、重试、进度、限速全部由 rclone 负责,本模块只做 HTTP 封装与错误归一化。
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from . import config

RC_USER = "gdrive-agent"


class RcError(RuntimeError):
    """rclone rcd 返回的错误,或无法连接到 rcd。"""


def call(method: str, timeout: int | None = None, **params) -> dict:
    """调用一个 rc 方法。params 直接作为 JSON body 传给 rclone。"""
    cfg = config.load()
    url = f"{config.rc_url()}/{method}"
    body = json.dumps(params).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if cfg["rc"]["token"]:
        # rclone rcd 只支持 HTTP Basic(--rc-user/--rc-pass),不支持 Bearer
        cred = base64.b64encode(f"{RC_USER}:{cfg['rc']['token']}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg["rc"]["timeout_sec"]) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            pass
        raise RcError(f"{method} 失败: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RcError(
            f"无法连接 rclone rcd ({config.rc_url()}): {exc.reason}。"
            f"检查 `systemctl status gdrive-rcd.service`"
        ) from exc


def call_async(method: str, **params) -> int:
    """提交异步任务,立刻返回 jobid。用于传输类长耗时操作。"""
    return call(method, _async=True, **params)["jobid"]


def alive() -> bool:
    try:
        call("rc/noop", timeout=5)
        return True
    except RcError:
        return False
