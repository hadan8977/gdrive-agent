#!/opt/gdrive-agent/.venv/bin/python
"""gdrive-agent 的 MCP 服务器 — Agent 操作 Google Drive 的主接口。

设计要点:
  - 传输类操作立刻返回 job_id 而非阻塞,因此 3 小时的传输不会撑爆 MCP 响应超时
  - 工具描述写明"何时该调用",而不只是"做什么" —— 这是 Agent 选对工具的主要依据
  - 所有护栏(命名空间隔离/磁盘预检/删除确认)在 ops 层,MCP 层只做转发
"""
from __future__ import annotations

import sys
from typing import Literal

sys.path.insert(0, "/opt/gdrive-agent")

from mcp.server.mcpserver import MCPServer

from gdrive_agent import ops, pack, staging, tasks
from gdrive_agent.guards import GuardError
from gdrive_agent import profile as profile_mod

server = MCPServer(
    name="gdrive",
    instructions=(
        "Google Drive 存储服务,用于把数据存到用户自己的 Drive(5TiB)并按需取回。\n"
        "本地磁盘仅约 150G 而 Drive 有 5TiB,所以下载前务必先看 drive_df / drive_stat 的体量;"
        "服务会在空间不足时拒绝下载并给出替代方案(取子集 / 只读挂载 /mnt/gdrive / 流式 drive_cat)。\n"
        "上传下载是异步的:提交后拿 job_id,再用 drive_job 轮询。\n"
        "所有路径都相对于服务根 gdrive:agent-data/,不接受 '..' 和 remote 前缀。\n"
        "完整文档: /opt/gdrive-agent/README.md"
    ),
)


def _guard(fn, *a, **kw):
    """把护栏异常转成 Agent 可读的结果,而不是让 MCP 抛栈。"""
    try:
        return fn(*a, **kw)
    except (GuardError, FileNotFoundError, ValueError, KeyError, RuntimeError) as exc:
        return {"error": str(exc), "error_type": type(exc).__name__}


# ---------------- 只读 / 勘察 ----------------

@server.tool(
    description=(
        "一次查看三处空间用量:Google Drive 配额、本机磁盘余量、本地暂存区。"
        "在计划任何下载之前先调用它 —— 返回的 local.usable_for_download 是本次能安全下载的上限,"
        "超过这个数的下载会被服务拒绝。上传前一般不需要调用(Drive 空间通常充裕)。"
    )
)
def drive_df() -> dict:
    return _guard(ops.df)


@server.tool(
    description=(
        "列出 Drive 上某个目录的内容。用于探查有哪些数据集、确认上传结果、"
        "或在下载前查看目录结构。recursive=True 会递归展开(大目录会慢,先用 drive_stat 看规模)。"
    )
)
def drive_ls(path: str = "", recursive: bool = False, limit: int = 1000) -> dict:
    """列目录。

    Args:
        path: 相对服务根的路径,如 'datasets/hs300'。留空表示根目录。
        recursive: 是否递归列出子目录内容。海量小文件目录慎用。
        limit: 最多返回多少条,防止塞爆上下文。
    """
    return _guard(ops.ls, path, recursive, limit)


@server.tool(
    description=(
        "查看单个文件或目录的元信息(是否存在、是文件还是目录、大小、修改时间)。"
        "比 drive_ls 便宜,适合在操作前确认目标存在。要知道一个目录的递归总体量请用 drive_size。"
    )
)
def drive_stat(path: str) -> dict:
    """Args: path: 相对服务根的路径。"""
    return _guard(ops.stat, path)


@server.tool(
    description=(
        "递归统计一个远端目录的总字节数和文件数。**下载大目录前必须先调用它**,"
        "把结果和 drive_df 的 local.usable_for_download 对比,判断本地放不放得下。"
        "大目录上可能耗时数十秒。"
    )
)
def drive_size(path: str) -> dict:
    """Args: path: 相对服务根的路径。"""
    return _guard(ops.size, path)


@server.tool(
    description=(
        "不落盘读取远端文件的一个片段。用于在决定是否下载一个大文件前先看它的头部 —— "
        "确认 CSV 表头、parquet magic bytes、JSON 结构是否符合预期。"
        "这是处理'文件比硬盘大'场景的关键工具:先窥探再决定。"
    )
)
def drive_cat(path: str, offset: int = 0, limit: int = 4096) -> dict:
    """Args:
        path: 相对服务根的文件路径。
        offset: 从第几字节开始读。
        limit: 最多读多少字节。默认 4KB,足够看文件头。
    """
    return _guard(ops.cat, path, offset, limit)


@server.tool(
    description=(
        "探测一个**本地**目录的形态(文件数、总量、尺寸分布),返回上传时会自动选用的策略及理由。"
        "上传前想预知服务会怎么处理(直传还是打包)时调用。drive_upload 内部会自动做这件事,"
        "所以只在需要提前判断耗时或策略时才单独调用。"
    )
)
def drive_profile(local_path: str) -> dict:
    """Args: local_path: 本机上的目录或文件的绝对路径。"""
    return _guard(profile_mod.profile, local_path)


# ---------------- 传输(异步) ----------------

@server.tool(
    description=(
        "把本地文件或目录上传到 Drive。**立刻返回 job_id,不等待传输完成** —— "
        "随后用 drive_job 轮询进度。服务会自动探测目录形态并选择传输策略"
        "(少量大文件直传 / 海量小文件自动 tar 分卷打包),返回值里的 reason 说明选了什么以及为什么。"
        "mirror=True 会让远端与本地完全一致(**删除远端多余文件**),仅用于镜像备份场景,默认只增不删。"
    )
)
def drive_upload(local: str, remote: str = "", mirror: bool = False,
                 strategy: str | None = None) -> dict:
    """Args:
        local: 本机绝对路径,文件或目录皆可。
        remote: Drive 上的目标路径(相对服务根)。留空则用本地文件名。
        mirror: True=单向镜像(会删远端多余文件),False=增量复制(只增不删,默认)。
        strategy: 覆盖自动策略,可选 direct-large / direct-small / pack / split。通常不用传。
    """
    return _guard(ops.upload, local, remote, strategy, mirror)


@server.tool(
    description=(
        "从 Drive 下载到本地。**立刻返回 job_id**,用 drive_job 轮询。"
        "**下载前会强制磁盘预检**:若远端体量超过本地可用空间(减去 20G 保留水位),"
        "会直接拒绝并在错误信息里列出替代方案,而不是传到一半把根分区写满。"
        "空间不够时优先考虑:用 subset 参数取子集,或改用只读挂载 /mnt/gdrive 直接读。"
        "不传 local 时会落到暂存区(24 小时后自动回收)。"
    )
)
def drive_download(remote: str, local: str | None = None,
                   subset: str | None = None) -> dict:
    """Args:
        remote: Drive 上的源路径(相对服务根)。
        local: 本机目标路径。留空则放进暂存区 /var/lib/gdrive-agent/staging/(会被 GC 回收)。
        subset: glob 过滤,只下载匹配的文件,如 '*2024*.parquet'。用于分批取回超大数据集。
    """
    return _guard(ops.download, remote, local, subset)


@server.tool(
    description=(
        "查询/列出/取消传输任务。提交 drive_upload 或 drive_download 后用它轮询,"
        "返回 finished、success、已传字节、速度、ETA、错误数。"
        "大传输可能耗时数小时,轮询间隔建议 5-30 秒,不要忙等。"
    )
)
def drive_job(action: Literal["status", "list", "cancel"] = "status",
              job_id: int | None = None) -> dict:
    """Args:
        action: status=查单个任务 / list=列出所有任务 / cancel=取消任务。
        job_id: status 和 cancel 时必填,由 drive_upload/drive_download 返回。
    """
    return _guard(ops.job, action, job_id)


# ---------------- 变更(带护栏) ----------------

@server.tool(
    description=(
        "在 Drive 上创建空目录,父目录会自动一并创建。"
        "多数情况下**不需要调用** —— drive_upload 会自动建好目标路径上的所有目录。"
        "仅在需要预先规划目录结构、或要创建一个暂时还没有内容的占位目录时才用它"
        "(例如为一批将陆续上传的数据集先划好 datasets/xxx/ 的分区)。"
        "对已存在的目录调用是安全的,不会报错也不会清空内容。"
    )
)
def drive_mkdir(path: str) -> dict:
    """Args: path: 相对服务根的路径,如 'datasets/hs300/2024'。"""
    return _guard(ops.mkdir, path)


@server.tool(
    description=(
        "在 Drive 内移动或重命名文件/目录。整理数据集时使用,比下载再上传快得多(服务端操作)。"
        "移动目录是异步的,会返回 job_id。"
    )
)
def drive_move(src: str, dst: str) -> dict:
    """Args:
        src: 源路径(相对服务根)。
        dst: 目标路径(相对服务根)。
    """
    return _guard(ops.move, src, dst)


@server.tool(
    description=(
        "删除 Drive 上的文件或目录。**删除目录必须显式传 confirm=True**,否则会被拒绝。"
        "默认进 Drive 回收站(可在网页端恢复);permanent=True 才彻底删除,不可恢复。"
        "删除前建议先 drive_stat 或 drive_ls 确认目标。备份仓库路径被永久禁止,无法误删。"
    )
)
def drive_delete(path: str, recursive: bool = False, confirm: bool = False,
                 permanent: bool = False) -> dict:
    """Args:
        path: 要删除的路径(相对服务根)。
        recursive: 目标是目录时需要 True。
        confirm: 删除目录的安全确认,必须显式传 True。
        permanent: True=彻底删除不可恢复,False=进回收站(默认)。
    """
    return _guard(ops.delete, path, recursive, confirm, permanent)


@server.tool(
    description=(
        "为 Drive 上的文件生成公开分享链接。需要把产出物分享给他人、或让外部服务直接下载时使用。"
        "注意:生成的链接是公开可访问的,不要对敏感数据使用。"
    )
)
def drive_link(path: str) -> dict:
    """Args: path: 相对服务根的文件路径。"""
    return _guard(ops.link, path)


@server.tool(
    description=(
        "获取远端文件的 md5 校验清单,用于核对下载完整性或确认上传成功。"
        "rclone 传输时已自动校验,所以通常不必调用;仅在怀疑数据损坏时使用。"
    )
)
def drive_verify(path: str) -> dict:
    """Args: path: 相对服务根的路径。"""
    return _guard(ops.verify, path)


# ---------------- 本地暂存区 ----------------

@server.tool(
    description=(
        "管理本地暂存区(把服务器硬盘当 Drive 的临时中转)。暂存条目默认 24 小时后被自动回收,"
        "总量超 100G 时按最久未用淘汰。\n"
        "  list  — 看当前暂存了什么、占了多少、本地还剩多少\n"
        "  lease — 钉住某个条目防止被回收(正在用一份数据跑分析时必须调用,否则可能中途被清掉)\n"
        "  release — 用完后解除钉住\n"
        "  clean — 立刻回收过期条目,腾出磁盘空间(下载报空间不足时先试这个)"
    )
)
def staging_manage(action: Literal["list", "lease", "release", "clean"] = "list",
                   name: str | None = None, hours: float = 24) -> dict:
    """Args:
        action: 要执行的操作。
        name: lease/release 时指定暂存条目名(即 staging 目录下的条目名)。
        hours: lease 的时长,超时后租约自动失效。
    """
    if action == "list":
        return _guard(staging.listing)
    if action == "lease":
        return _guard(staging.acquire, name, hours)
    if action == "release":
        return _guard(staging.release, name)
    return _guard(staging.clean)


# ---------------- 周期任务 ----------------

@server.tool(
    description=(
        "管理周期性任务(定时把数据同步到 Drive 或从 Drive 拉回)。任务会生成 systemd timer 常驻,"
        "服务器重启后自动恢复。用于'每天定时备份某目录到 Drive'这类持续性需求。\n"
        "  create — 建任务,需要 name/ttype/schedule,以及 local+remote(或 script 类型的 command)\n"
        "  list/status/logs — 查看任务、下次执行时间、上次结果、日志\n"
        "  run — 立刻手动跑一次(不影响定时)\n"
        "  delete — 删除任务并清理 systemd unit"
    )
)
def task_manage(
    action: Literal["create", "list", "status", "logs", "run", "delete"] = "list",
    name: str | None = None,
    ttype: Literal["sync", "push", "pull", "script"] | None = None,
    schedule: str | None = None,
    local: str | None = None,
    remote: str | None = None,
    command: str | None = None,
    lines: int = 50,
) -> dict:
    """Args:
        action: 要执行的操作。
        name: 任务名,小写字母/数字/连字符。
        ttype: sync=本地→Drive镜像(删远端多余) / push=只增不删 / pull=Drive→本地 / script=任意命令。
        schedule: systemd OnCalendar 语法,如 'daily'、'*-*-* 03:00:00'、'Mon *-*-* 06:00:00'。
        local: sync/push/pull 的本地路径。
        remote: sync/push/pull 的 Drive 路径(相对服务根)。
        command: script 类型要执行的命令。
        logs: 返回最后多少行日志。
    """
    if action == "list":
        return _guard(tasks.listing)
    if action == "status":
        return _guard(tasks.status, name)
    if action == "logs":
        return _guard(tasks.logs, name, lines)
    if action == "run":
        return _guard(tasks.run_now, name)
    if action == "delete":
        return _guard(tasks.delete, name)
    spec = {"command": command} if ttype == "script" else {"local": local, "remote": remote}
    return _guard(tasks.create, name, ttype, schedule, spec)


if __name__ == "__main__":
    # stdio(默认):供本机 agent 以子进程方式接入,零配置、无端口。
    # streamable-http:供任意框架 / 远程 agent 接入,绑 127.0.0.1,远程走 SSH 隧道。
    #   用法: mcp_server.py --http [port]
    if "--http" in sys.argv:
        idx = sys.argv.index("--http")
        port = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 5573
        # 只绑回环:远程 agent 走 SSH 隧道接入,不直接暴露到公网。
        server.run(transport="streamable-http", host="127.0.0.1", port=port,
                   stateless_http=True)
    else:
        server.run(transport="stdio")
