"""对外操作语义层:所有 CLI / MCP 调用最终都落到这里。

约定:
  - 快操作(ls/stat/df/mkdir/mv/rm)同步返回结果
  - 传输类(upload/download)提交异步 job,立刻返回 job_id,由调用方轮询
    —— 这样 3 小时的传输不会撑爆 MCP 的响应超时
  - 所有变更类操作写审计日志
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config, profile, rc, staging
from .guards import GuardError, check_disk, human, remote_fs, require_confirm, resolve

AUDIT = Path("/var/log/gdrive-agent/audit.log")


def audit(action: str, **detail) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action, **detail}
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- 只读 ----------

def df() -> dict:
    """一次看全:Drive 配额 + 本地磁盘 + 暂存区用量。Agent 决策前应先看这个。"""
    about = rc.call("operations/about", fs=config.load()["remote"]["backend"])
    st = staging.listing()
    import shutil as _sh
    local = _sh.disk_usage("/")
    reserve = int(config.load()["disk"]["reserve_gb"]) * 1024**3
    return {
        "drive": {
            "total": human(about["total"]), "used": human(about["used"]),
            "free": human(about["free"]), "free_bytes": about["free"],
        },
        "local": {
            "total": human(local.total), "free": human(local.free), "free_bytes": local.free,
            "reserve": human(reserve),
            "usable_for_download": human(max(0, local.free - reserve)),
            "usable_bytes": max(0, local.free - reserve),
        },
        "staging": {"used": st["used"], "cap": st["cap"], "items": len(st["items"])},
    }


def ls(path: str = "", recursive: bool = False, limit: int = 1000) -> dict:
    rel = resolve(path)
    opt = {"recurse": bool(recursive)}
    out = rc.call("operations/list", fs=remote_fs(), remote=rel, opt=opt)
    items = out.get("list", [])
    truncated = len(items) > limit
    return {
        "path": rel or "/", "count": len(items), "truncated": truncated,
        "items": [
            {"name": i["Name"], "path": i["Path"], "is_dir": i["IsDir"],
             "size": human(i["Size"]) if not i["IsDir"] else "-",
             "bytes": i["Size"], "modified": i.get("ModTime")}
            for i in items[:limit]
        ],
    }


def stat(path: str) -> dict:
    rel = resolve(path)
    out = rc.call("operations/stat", fs=remote_fs(), remote=rel)
    item = out.get("item")
    if item is None:
        return {"path": rel, "exists": False}
    return {
        "path": rel, "exists": True, "is_dir": item["IsDir"],
        "bytes": item["Size"], "size": human(item["Size"]),
        "modified": item.get("ModTime"), "mime": item.get("MimeType"),
    }


def size(path: str) -> dict:
    """递归统计远端体量。下载预检依赖它。"""
    rel = resolve(path)
    out = rc.call("operations/size", fs=remote_fs(), remote=rel, timeout=600)
    return {"path": rel, "bytes": out["bytes"], "size": human(out["bytes"]), "files": out["count"]}


def cat(path: str, offset: int = 0, limit: int = 4096) -> dict:
    """流式窥探远端文件的一段,不落盘。用来先看 schema/文件头再决定是否下载。"""
    rel = resolve(path)
    cfg = config.load()["remote"]
    import subprocess
    cmd = ["/usr/local/bin/rclone", "cat", f"{cfg['backend']}{cfg['root']}/{rel}",
           "--offset", str(offset), "--count", str(limit)]
    res = subprocess.run(cmd, capture_output=True, timeout=120,
                         env={**os.environ, "RCLONE_CONFIG": "/root/.config/rclone/rclone.conf"})
    if res.returncode != 0:
        raise RuntimeError(f"cat 失败: {res.stderr.decode(errors='replace')[:400]}")
    raw = res.stdout
    try:
        text, is_text = raw.decode("utf-8"), True
    except UnicodeDecodeError:
        text, is_text = raw[:256].hex(), False
    return {"path": rel, "offset": offset, "bytes_read": len(raw),
            "is_text": is_text, "content": text}


# ---------- 传输(异步) ----------

def upload(local: str, remote: str = "", strategy: str | None = None,
           mirror: bool = False) -> dict:
    """本地 → Drive。自动 profile 选策略,返回 job_id。

    mirror=True 时用 sync/sync 做单向镜像(远端多余文件会被删除),用于周期性备份类任务。
    默认 sync/copy 只增不删,更安全。
    """
    src = Path(local)
    if not src.exists():
        raise FileNotFoundError(f"本地路径不存在: {local}")
    rel = resolve(remote or src.name)
    prof = profile.profile(src)
    chosen = strategy or prof["strategy"]
    opts = profile.rclone_opts(chosen)

    if src.is_file():
        if mirror:
            raise GuardError("mirror 仅适用于目录,单文件请用默认模式。")
        parent, name = str(src.parent), src.name
        jobid = rc.call_async("operations/copyfile",
                              srcFs=parent, srcRemote=name,
                              dstFs=remote_fs(), dstRemote=rel or name, _config=opts)
    else:
        method = "sync/sync" if mirror else "sync/copy"
        jobid = rc.call_async(method, srcFs=str(src),
                              dstFs=f"{remote_fs()}/{rel}" if rel else remote_fs(), _config=opts)

    audit("upload", local=str(src), remote=rel, strategy=chosen, job=jobid,
          mirror=mirror, files=prof["files"])
    return {"job_id": jobid, "local": str(src), "remote": rel,
            "strategy": chosen, "reason": prof["reason"], "mirror": mirror,
            "files": prof["files"], "size": prof.get("bytes_human"),
            "next": f"轮询 drive_job(action='status', job_id={jobid}) 查看进度"}


def download(remote: str, local: str | None = None, subset: str | None = None,
             skip_disk_check: bool = False) -> dict:
    """Drive → 本地。**下载前强制磁盘预检**,空间不足直接拒绝并给替代方案。"""
    rel = resolve(remote)
    dest = Path(local) if local else staging.path_for(Path(rel).name or "download")
    dest.parent.mkdir(parents=True, exist_ok=True)

    info = size(rel)
    disk = None
    if not skip_disk_check:
        disk = check_disk(info["bytes"], dest.parent)

    opts = {"Transfers": 8, "Checkers": 16}
    if subset:
        opts["IncludeRule"] = [subset]
    jobid = rc.call_async("sync/copy", srcFs=f"{remote_fs()}/{rel}" if rel else remote_fs(),
                          dstFs=str(dest), _config=opts)
    audit("download", remote=rel, local=str(dest), bytes=info["bytes"], job=jobid, subset=subset)
    return {"job_id": jobid, "remote": rel, "local": str(dest),
            "size": info["size"], "files": info["files"], "subset": subset,
            "disk_check": {k: human(v) for k, v in disk.items()} if disk else "skipped",
            "next": f"轮询 drive_job(action='status', job_id={jobid}) 查看进度"}


# ---------- 任务 ----------

def job(action: str = "status", job_id: int | None = None) -> dict:
    if action == "list":
        return rc.call("job/list")
    if action == "cancel":
        rc.call("job/stop", jobid=job_id)
        audit("job_cancel", job=job_id)
        return {"job_id": job_id, "cancelled": True}
    st = rc.call("job/status", jobid=job_id)
    stats = rc.call("core/stats", group=f"job/{job_id}")
    return {
        "job_id": job_id, "finished": st["finished"], "success": st["success"],
        "error": st["error"] or None, "duration_sec": round(st.get("duration", 0), 1),
        "output": st.get("output"),
        "progress": {
            "transferred": human(stats.get("bytes", 0)),
            "total": human(stats.get("totalBytes", 0)),
            "speed": f"{human(stats.get('speed', 0))}/s",
            "eta_sec": stats.get("eta"),
            "files_done": stats.get("transfers", 0),
            "errors": stats.get("errors", 0),
        },
    }


# ---------- 变更(带护栏) ----------

def mkdir(path: str) -> dict:
    rel = resolve(path)
    rc.call("operations/mkdir", fs=remote_fs(), remote=rel)
    audit("mkdir", remote=rel)
    return {"path": rel, "created": True}


def move(src: str, dst: str) -> dict:
    s, d = resolve(src), resolve(dst)
    st = stat(s)
    if not st["exists"]:
        raise FileNotFoundError(f"源不存在: {s}")
    method = "sync/move" if st["is_dir"] else "operations/movefile"
    if st["is_dir"]:
        jobid = rc.call_async(method, srcFs=f"{remote_fs()}/{s}", dstFs=f"{remote_fs()}/{d}")
        audit("move", src=s, dst=d, job=jobid)
        return {"job_id": jobid, "src": s, "dst": d}
    rc.call(method, srcFs=remote_fs(), srcRemote=s, dstFs=remote_fs(), dstRemote=d)
    audit("move", src=s, dst=d)
    return {"src": s, "dst": d, "moved": True}


def delete(path: str, recursive: bool = False, confirm: bool = False,
           permanent: bool = False) -> dict:
    """删除。递归删除需 confirm;默认进 Drive 回收站(可恢复),permanent 才彻底删。"""
    rel = resolve(path)
    if not rel:
        raise GuardError("拒绝删除服务根目录。")
    st = stat(rel)
    if not st["exists"]:
        return {"path": rel, "deleted": False, "reason": "不存在"}

    if st["is_dir"]:
        require_confirm(confirm, f"递归删除目录 {rel!r}")
        info = size(rel)
        cfg = {"DriveUseTrash": not permanent}
        rc.call("operations/purge", fs=remote_fs(), remote=rel, _config=cfg)
        audit("delete", remote=rel, recursive=True, bytes=info["bytes"], permanent=permanent)
        return {"path": rel, "deleted": True, "was_dir": True, "freed": info["size"],
                "files": info["files"], "recoverable": not permanent}

    rc.call("operations/deletefile", fs=remote_fs(), remote=rel,
            _config={"DriveUseTrash": not permanent})
    audit("delete", remote=rel, recursive=False, permanent=permanent)
    return {"path": rel, "deleted": True, "was_dir": False, "recoverable": not permanent}


def link(path: str) -> dict:
    rel = resolve(path)
    out = rc.call("operations/publiclink", fs=remote_fs(), remote=rel)
    audit("link", remote=rel)
    return {"path": rel, "url": out.get("url")}


def verify(path: str) -> dict:
    """取远端 md5 清单,用于与本地比对完整性。"""
    rel = resolve(path)
    out = rc.call("operations/hashsum", fs=remote_fs(), remote=rel,
                  hashType="md5", download=False, timeout=600)
    return {"path": rel, "hashes": out.get("hashsum", []), "type": "md5"}
