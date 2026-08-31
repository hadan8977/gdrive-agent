"""安全护栏。本模块的每个函数都是"拒绝优先"的。

三类风险:
  1. 路径穿越到 restic 备份仓库 (gdrive:vps-backup) → 备份历史不可逆损毁
  2. 下载写爆本地根分区 → quantcheck 等线上业务被拖死
  3. 误递归删除 → 数据丢失
"""
from __future__ import annotations

import posixpath
import shutil
from pathlib import Path

from . import config


class GuardError(PermissionError):
    """操作被护栏拒绝。消息面向 Agent,应说明原因与可行替代。"""


def resolve(path: str | None) -> str:
    """把用户路径规范化为服务根内的相对路径。越界或命中 deny 列表则拒绝。

    接受 "datasets/x"、"/datasets/x"、""。拒绝 "../"、"gdrive:..." 等越界写法。
    """
    cfg = config.load()
    raw = (path or "").strip()

    if ":" in raw.split("/")[0] and raw.split("/")[0]:
        raise GuardError(
            f"不接受带 remote 前缀的路径 {raw!r}。请传相对于服务根的路径,"
            f"例如 'datasets/foo'。服务根固定为 gdrive:{cfg['remote']['root']}/"
        )

    # 显式拒绝任何 .. 分量。不能依赖 normpath:它对绝对路径会静默吞掉开头的 ..,
    # 把 "../vps-backup" 悄悄重解释为根内的 "vps-backup",属于静默改写语义,不可接受。
    if any(part == ".." for part in raw.replace("\\", "/").split("/")):
        raise GuardError(
            f"路径 {raw!r} 含 '..' 分量,拒绝。所有路径必须是相对于服务根 "
            f"gdrive:{cfg['remote']['root']}/ 的下行路径。"
        )

    rel = posixpath.normpath(posixpath.join("/", raw)).lstrip("/")
    if rel == ".":
        rel = ""

    full = posixpath.join(cfg["remote"]["root"], rel) if rel else cfg["remote"]["root"]
    for denied in cfg["remote"]["deny"]:
        if full == denied or full.startswith(denied + "/"):
            raise GuardError(
                f"路径 {raw!r} 命中禁止区 {denied!r}。该目录是 restic 加密备份仓库,"
                f"本服务永久禁止访问,以防损毁备份历史。"
            )
    return rel


def remote_fs() -> str:
    """所有操作的 fs 根。限定在此可确保 rclone 侧也无法越界。"""
    cfg = config.load()
    return f"{cfg['remote']['backend']}{cfg['remote']['root']}"


def free_bytes(path: str | Path) -> int:
    """path 所在文件系统的可用字节数。path 不存在时上溯到最近的已存在父目录。"""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def check_disk(need_bytes: int, dest: str | Path) -> dict:
    """下载前置磁盘预检。空间不足则抛错,并给出可行替代方案。

    这是"数据可能比硬盘大"场景下最重要的一道闸:宁可不传,也不能把 / 写满。
    """
    cfg = config.load()
    reserve = int(cfg["disk"]["reserve_gb"]) * 1024**3
    free = free_bytes(dest)
    usable = free - reserve

    if need_bytes > usable:
        raise GuardError(
            f"磁盘空间不足,拒绝下载。需要 {human(need_bytes)},"
            f"可用 {human(free)} 减去保留水位 {human(reserve)} 后仅剩 {human(max(0, usable))}。\n"
            f"可行替代:\n"
            f"  1. 只取子集:drive_download 传 subset 参数(glob),分批下载\n"
            f"  2. 挂载读取:用 /mnt/gdrive 只读挂载,pandas 可直接读,本地不落全量\n"
            f"  3. 流式窥探:drive_cat 看文件头,先确认是否真的需要\n"
            f"  4. 清暂存区:staging clean 释放空间后重试"
        )
    return {"need": need_bytes, "free": free, "reserve": reserve, "usable": usable}


def require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise GuardError(
            f"{what} 是破坏性操作,需显式传 confirm=true 才会执行。"
            f"请先用 drive_ls / drive_stat 确认目标无误。"
        )


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PiB"
