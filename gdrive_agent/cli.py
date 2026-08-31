"""gdrive CLI。与 MCP 工具面一一对应,供 systemd 任务、脚本和人使用。

所有子命令支持 --json 输出机器可读结果。传输类默认异步返回 job_id,
加 --wait 则阻塞到完成(周期任务用这个)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import ops, pack, staging, tasks
from .guards import GuardError


def _emit(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict) and "items" in data:
        print(f"{data.get('path','')}  ({data['count']} 项)")
        for i in data["items"]:
            kind = "d" if i["is_dir"] else "-"
            print(f"  {kind} {i['size']:>10}  {i['name']}")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _wait(job_id: int, quiet: bool = False) -> dict:
    """阻塞轮询到任务结束。返回最终状态。"""
    while True:
        st = ops.job("status", job_id)
        if st["finished"]:
            if not quiet:
                p = st["progress"]
                print(f"[job {job_id}] {'成功' if st['success'] else '失败'} "
                      f"耗时 {st['duration_sec']}s 传输 {p['transferred']} 错误 {p['errors']}",
                      file=sys.stderr)
            return st
        if not quiet:
            p = st["progress"]
            print(f"[job {job_id}] {p['transferred']}/{p['total']} @ {p['speed']} "
                  f"ETA {p['eta_sec']}s", file=sys.stderr)
        time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gdrive",
        description="面向 Agent 的 Google Drive 存储服务。完整文档: /opt/gdrive-agent/README.md")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    # --json 需要在子命令前后都能用(Agent 更习惯写在后面)。
    # SUPPRESS 默认值确保子命令未传 --json 时不会覆盖掉顶层已设的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="机器可读输出")
    _real_add = ap.add_subparsers(dest="cmd", required=True)

    class _Sub:
        def add_parser(self, name, **kw):
            kw.setdefault("parents", []).append(common)
            return _real_add.add_parser(name, **kw)

    sub = _Sub()

    sub.add_parser("df", help="Drive 配额 + 本地磁盘 + 暂存区,一次看全")

    p = sub.add_parser("ls", help="列目录"); p.add_argument("path", nargs="?", default="")
    p.add_argument("-r", "--recursive", action="store_true"); p.add_argument("--limit", type=int, default=1000)

    p = sub.add_parser("stat", help="查看条目详情"); p.add_argument("path")
    p = sub.add_parser("size", help="递归统计体量"); p.add_argument("path")

    p = sub.add_parser("cat", help="流式读取一段,不落盘"); p.add_argument("path")
    p.add_argument("--offset", type=int, default=0); p.add_argument("--limit", type=int, default=4096)

    p = sub.add_parser("profile", help="探测本地目录形态并给出传输策略"); p.add_argument("path")

    p = sub.add_parser("upload", help="本地 → Drive"); p.add_argument("local")
    p.add_argument("--remote", default=""); p.add_argument("--strategy")
    p.add_argument("--mirror", action="store_true", help="单向镜像,删除远端多余文件")
    p.add_argument("--wait", action="store_true")

    p = sub.add_parser("download", help="Drive → 本地(含磁盘预检)"); p.add_argument("remote")
    p.add_argument("--local"); p.add_argument("--subset", help="glob 过滤,只取子集")
    p.add_argument("--wait", action="store_true")

    p = sub.add_parser("job", help="任务状态"); p.add_argument("action", choices=["status","list","cancel"])
    p.add_argument("job_id", nargs="?", type=int)

    p = sub.add_parser("mkdir"); p.add_argument("path")
    p = sub.add_parser("mv"); p.add_argument("src"); p.add_argument("dst")
    p = sub.add_parser("rm", help="删除(默认进回收站)"); p.add_argument("path")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("--confirm", action="store_true"); p.add_argument("--permanent", action="store_true")
    p = sub.add_parser("link", help="生成分享链接"); p.add_argument("path")
    p = sub.add_parser("verify", help="取远端 md5 清单"); p.add_argument("path")

    p = sub.add_parser("staging", help="本地暂存区")
    p.add_argument("action", choices=["list","lease","release","clean"])
    p.add_argument("name", nargs="?"); p.add_argument("--hours", type=float, default=24)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("task", help="周期任务")
    p.add_argument("action", choices=["create","list","status","logs","run","delete"])
    p.add_argument("name", nargs="?")
    p.add_argument("--type", dest="ttype", choices=list(tasks.TYPES))
    p.add_argument("--schedule"); p.add_argument("--local"); p.add_argument("--remote")
    p.add_argument("--command"); p.add_argument("--desc", default=""); p.add_argument("--lines", type=int, default=50)

    p = sub.add_parser("pack", help="小文件分卷打包"); p.add_argument("src"); p.add_argument("out")

    a = ap.parse_args(argv)
    try:
        r = _dispatch(a)
    except (GuardError, ValueError, FileNotFoundError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False)
              if a.json else f"错误: {exc}", file=sys.stderr)
        return 2
    if r is not None:
        _emit(r, a.json)
    return 0


def _dispatch(a):
    c = a.cmd
    if c == "df": return ops.df()
    if c == "ls": return ops.ls(a.path, a.recursive, a.limit)
    if c == "stat": return ops.stat(a.path)
    if c == "size": return ops.size(a.path)
    if c == "cat": return ops.cat(a.path, a.offset, a.limit)
    if c == "profile":
        from . import profile as pf
        return pf.profile(a.path)
    if c == "upload":
        r = ops.upload(a.local, a.remote, a.strategy, mirror=a.mirror)
        if a.wait:
            st = _wait(r["job_id"])
            if not st["success"]:
                raise RuntimeError(f"上传失败: {st['error']}")
            r["final"] = st
        return r
    if c == "download":
        r = ops.download(a.remote, a.local, a.subset)
        if a.wait:
            st = _wait(r["job_id"])
            if not st["success"]:
                raise RuntimeError(f"下载失败: {st['error']}")
            r["final"] = st
        return r
    if c == "job": return ops.job(a.action, a.job_id)
    if c == "mkdir": return ops.mkdir(a.path)
    if c == "mv": return ops.move(a.src, a.dst)
    if c == "rm": return ops.delete(a.path, a.recursive, a.confirm, a.permanent)
    if c == "link": return ops.link(a.path)
    if c == "verify": return ops.verify(a.path)
    if c == "staging":
        if a.action == "list": return staging.listing()
        if a.action == "lease": return staging.acquire(a.name, a.hours)
        if a.action == "release": return staging.release(a.name)
        return staging.clean(force=a.force)
    if c == "task":
        if a.action == "list": return tasks.listing()
        if a.action == "status": return tasks.status(a.name)
        if a.action == "logs": return tasks.logs(a.name, a.lines)
        if a.action == "run": return tasks.run_now(a.name)
        if a.action == "delete": return tasks.delete(a.name)
        spec = {"command": a.command} if a.ttype == "script" else {"local": a.local, "remote": a.remote}
        return tasks.create(a.name, a.ttype, a.schedule, spec, a.desc)
    if c == "pack": return pack.build(a.src, a.out)
    raise ValueError(f"未实现的子命令: {c}")


if __name__ == "__main__":
    sys.exit(main())
