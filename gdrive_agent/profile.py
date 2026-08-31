"""目录形态探测与传输策略自动选择。

文件形态不可预设(大文件/海量小文件/混合都可能),所以不让调用方声明,
而是扫描后自动决策——并把选择理由回传,让 Agent 知道发生了什么。

Drive 的关键特性:每文件都有固定 API 开销。裸传 1 万个小文件可能耗时数小时,
而打成几个大包只需几分钟。这是 pack 策略存在的唯一理由。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import config
from .guards import human

DIRECT_LARGE = "direct-large"
DIRECT_SMALL = "direct-small"
PACK = "pack"
SPLIT = "split"


def scan_local(path: str | Path, sample_limit: int = 200_000) -> dict:
    """扫描本地目录,返回文件数、总字节、尺寸分布。单文件也支持。"""
    p = Path(path)
    if p.is_file():
        size = p.stat().st_size
        return {"files": 1, "bytes": size, "sizes": [size], "truncated": False, "is_file": True}

    files = 0
    total = 0
    sizes: list[int] = []
    truncated = False
    for root, _dirs, names in os.walk(p, followlinks=False):
        for name in names:
            fp = Path(root) / name
            try:
                size = fp.lstat().st_size
            except OSError:
                continue
            files += 1
            total += size
            if len(sizes) < sample_limit:
                sizes.append(size)
            else:
                truncated = True
    return {"files": files, "bytes": total, "sizes": sizes, "truncated": truncated, "is_file": False}


def choose_strategy(stats: dict) -> dict:
    """依据扫描结果选传输策略,并给出人类可读的理由。"""
    cfg = config.load()["transfer"]
    files = stats["files"]
    total = stats["bytes"]
    avg = (total / files) if files else 0
    avg_mb = avg / 1024**2

    if files == 0:
        return {"strategy": DIRECT_SMALL, "reason": "空目录", "files": 0, "bytes": 0}

    many_small = files > cfg["pack_min_files"] and avg_mb < cfg["pack_max_avg_mb"]
    # 混合判定:文件很多,但存在少量大文件占据了绝大部分体量
    big_files = sum(1 for s in stats["sizes"] if s >= 64 * 1024**2)
    mixed = many_small and big_files > 0

    if mixed:
        strategy, reason = SPLIT, (
            f"{files} 个文件(均值 {avg_mb:.2f}MB)中含 {big_files} 个 ≥64MB 的大文件 → "
            f"分治:大文件直传,小文件子树打包"
        )
    elif many_small:
        strategy, reason = PACK, (
            f"{files} 个文件、均值仅 {avg_mb:.2f}MB,超过打包阈值"
            f"({cfg['pack_min_files']} 个 / {cfg['pack_max_avg_mb']}MB)。"
            f"裸传会被 Drive 每文件 API 开销拖死 → tar 分卷上传,保留可检索索引"
        )
    elif avg_mb >= 64:
        strategy, reason = DIRECT_LARGE, (
            f"{files} 个文件、均值 {human(avg)},属大文件 → "
            f"直传,大分块({cfg['large_chunk']})/低并发({cfg['large_transfers']})"
        )
    else:
        strategy, reason = DIRECT_SMALL, (
            f"{files} 个文件、均值 {human(avg)} → 直传,高并发({cfg['small_transfers']})"
        )

    return {
        "strategy": strategy,
        "reason": reason,
        "files": files,
        "bytes": total,
        "bytes_human": human(total),
        "avg_bytes": int(avg),
    }


def rclone_opts(strategy: str) -> dict:
    """把策略翻译成 rclone 的 _config 覆盖参数。"""
    cfg = config.load()["transfer"]
    if strategy in (DIRECT_LARGE, PACK, SPLIT):
        return {"Transfers": cfg["large_transfers"], "Checkers": cfg["large_transfers"] * 2}
    return {"Transfers": cfg["small_transfers"], "Checkers": cfg["small_checkers"]}


def profile(path: str | Path) -> dict:
    stats = scan_local(path)
    out = choose_strategy(stats)
    out["truncated_sample"] = stats["truncated"]
    return out
