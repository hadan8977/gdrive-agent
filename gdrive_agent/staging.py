"""本地暂存区:把服务器硬盘当 Drive 的临时中转。

设计目标是"用完自动消失,且永远填不满盘":
  - 每个条目有 TTL,到期回收
  - 总量超过上限时按 LRU 淘汰
  - Agent 正在使用的条目可以 lease 钉住,GC 不碰
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config
from .guards import human


def _root() -> Path:
    return Path(config.load()["disk"]["staging"])


def _leases() -> Path:
    p = Path("/var/lib/gdrive-agent/leases")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _dir_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def path_for(name: str) -> Path:
    """暂存区内的安全路径。name 不得逃出暂存根。"""
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / name.lstrip("/")).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise PermissionError(f"暂存路径 {name!r} 越出暂存区,拒绝。")
    return target


def lease_file(name: str) -> Path:
    return _leases() / (name.strip("/").replace("/", "__") + ".lease")


def acquire(name: str, hours: float = 24) -> dict:
    """钉住一个暂存条目,GC 期间不回收。"""
    lf = lease_file(name)
    expires = time.time() + hours * 3600
    lf.write_text(json.dumps({"name": name, "expires": expires}))
    return {"name": name, "expires": expires, "hours": hours}


def release(name: str) -> dict:
    lf = lease_file(name)
    existed = lf.exists()
    lf.unlink(missing_ok=True)
    return {"name": name, "released": existed}


def is_leased(name: str) -> bool:
    lf = lease_file(name)
    if not lf.exists():
        return False
    try:
        if json.loads(lf.read_text())["expires"] > time.time():
            return True
    except (json.JSONDecodeError, KeyError):
        pass
    lf.unlink(missing_ok=True)  # 过期租约自动失效
    return False


def listing() -> dict:
    """暂存区现状:条目、用量、配额、本地磁盘余量。"""
    cfg = config.load()["disk"]
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    items = []
    total = 0
    for child in sorted(root.iterdir()):
        size = _dir_size(child)
        total += size
        items.append({
            "name": child.name,
            "bytes": size,
            "size": human(size),
            "age_hours": round((time.time() - child.stat().st_mtime) / 3600, 1),
            "leased": is_leased(child.name),
        })
    cap = int(cfg["staging_cap_gb"]) * 1024**3
    return {
        "items": items,
        "used_bytes": total,
        "used": human(total),
        "cap": human(cap),
        "cap_bytes": cap,
        "over_cap": total > cap,
        "disk_free": human(shutil.disk_usage(root).free),
    }


def clean(older_than_hours: float | None = None, force: bool = False) -> dict:
    """回收暂存区。先按 TTL 清,仍超配额则按 LRU 继续淘汰。leased 条目除非 force 否则跳过。"""
    cfg = config.load()["disk"]
    ttl = cfg["ttl_hours"] if older_than_hours is None else older_than_hours
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    removed, kept, freed = [], [], 0

    entries = []
    for child in root.iterdir():
        entries.append((child, child.stat().st_mtime, _dir_size(child)))

    def drop(child: Path, size: int, why: str):
        nonlocal freed
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        release(child.name)
        freed += size
        removed.append({"name": child.name, "size": human(size), "reason": why})

    # 第一轮:TTL
    for child, mtime, size in list(entries):
        if (now - mtime) / 3600 < ttl:
            kept.append(child.name)
            continue
        if is_leased(child.name) and not force:
            kept.append(child.name)
            removed.append({"name": child.name, "size": human(size), "reason": "skipped: leased"})
            continue
        drop(child, size, f"older than {ttl}h")
        entries = [e for e in entries if e[0] != child]

    # 第二轮:超配额则 LRU
    cap = int(cfg["staging_cap_gb"]) * 1024**3
    remaining = [e for e in entries if e[0].exists()]
    used = sum(s for _c, _m, s in remaining)
    for child, _mtime, size in sorted(remaining, key=lambda e: e[1]):
        if used <= cap:
            break
        if is_leased(child.name) and not force:
            continue
        drop(child, size, "LRU: over staging cap")
        used -= size

    return {"removed": removed, "freed": human(freed), "freed_bytes": freed, "kept": kept}
