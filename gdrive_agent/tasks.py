"""持续性任务:声明式定义 → systemd service + timer。

沿用 vps-backup.timer 已验证的模式(OnCalendar + Persistent=true),
不自造调度器。任务定义存 /etc/gdrive-agent/tasks/<name>.json,是唯一事实来源;
unit 文件由定义渲染而来,删除任务时一并清理。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

TASK_DIR = Path("/etc/gdrive-agent/tasks")
UNIT_DIR = Path("/etc/systemd/system")
CLI = "/usr/local/bin/gdrive"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

TYPES = {
    "sync": "本地目录 → Drive 单向镜像(远端多余文件会被删除,保持一致)",
    "push": "本地目录 → Drive 增量复制(只增不删)",
    "pull": "Drive → 本地增量复制",
    "script": "执行任意命令(环境已预置 rclone 配置)",
}


def _unit(name: str) -> tuple[Path, Path]:
    return UNIT_DIR / f"gdrive-task-{name}.service", UNIT_DIR / f"gdrive-task-{name}.timer"


def _validate(name: str, ttype: str, schedule: str, spec: dict) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"任务名 {name!r} 非法。只允许小写字母/数字/连字符,≤41 字符。")
    if ttype not in TYPES:
        raise ValueError(f"未知任务类型 {ttype!r}。可选: {', '.join(TYPES)}")
    if ttype == "script":
        if not spec.get("command"):
            raise ValueError("script 类型需要 spec.command")
    else:
        for key in ("local", "remote"):
            if not spec.get(key):
                raise ValueError(f"{ttype} 类型需要 spec.{key}")
    if not schedule.strip():
        raise ValueError("需要 schedule(systemd OnCalendar 语法,如 'daily'、'*-*-* 03:00:00')")


def _exec_line(ttype: str, spec: dict) -> str:
    if ttype == "script":
        return spec["command"]
    local, remote = spec["local"], spec["remote"]
    if ttype == "pull":
        return f"{CLI} download {remote!r} --local {local!r} --wait"
    flag = " --mirror" if ttype == "sync" else ""
    return f"{CLI} upload {local!r} --remote {remote!r}{flag} --wait"


def create(name: str, ttype: str, schedule: str, spec: dict,
           description: str = "") -> dict:
    _validate(name, ttype, schedule, spec)
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    definition = {"name": name, "type": ttype, "schedule": schedule,
                  "spec": spec, "description": description or TYPES[ttype]}
    (TASK_DIR / f"{name}.json").write_text(json.dumps(definition, ensure_ascii=False, indent=2))

    svc, timer = _unit(name)
    svc.write_text(f"""[Unit]
Description=gdrive-agent task: {name} ({ttype})
Documentation=file:///opt/gdrive-agent/README.md
After=network-online.target gdrive-rcd.service
Wants=network-online.target
Requires=gdrive-rcd.service

[Service]
Type=oneshot
Environment=HOME=/root
Environment=RCLONE_CONFIG=/root/.config/rclone/rclone.conf
ExecStart={_exec_line(ttype, spec)}
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=0
StandardOutput=append:/var/log/gdrive-agent/task-{name}.log
StandardError=append:/var/log/gdrive-agent/task-{name}.log
""")
    timer.write_text(f"""[Unit]
Description=Schedule for gdrive-agent task: {name}

[Timer]
OnCalendar={schedule}
Persistent=true

[Install]
WantedBy=timers.target
""")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", timer.name)
    return {"name": name, "type": ttype, "schedule": schedule,
            "units": [svc.name, timer.name], "enabled": True,
            "next_run": _next_run(name)}


def _systemctl(*args: str) -> str:
    res = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    if res.returncode != 0 and "--quiet" not in args:
        raise RuntimeError(f"systemctl {' '.join(args)} 失败: {res.stderr.strip()}")
    return res.stdout


def _next_run(name: str) -> str | None:
    out = subprocess.run(
        ["systemctl", "list-timers", f"gdrive-task-{name}.timer", "--no-legend", "--no-pager"],
        capture_output=True, text=True).stdout.strip()
    return out.split("  ")[0] if out else None


def listing() -> dict:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(TASK_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        name = d["name"]
        d["active"] = _systemctl("is-enabled", f"gdrive-task-{name}.timer", "--quiet") is not None
        d["next_run"] = _next_run(name)
        d["last_result"] = subprocess.run(
            ["systemctl", "show", f"gdrive-task-{name}.service", "-p", "Result", "--value"],
            capture_output=True, text=True).stdout.strip()
        out.append(d)
    return {"tasks": out, "count": len(out)}


def status(name: str) -> dict:
    f = TASK_DIR / f"{name}.json"
    if not f.exists():
        raise FileNotFoundError(f"无此任务: {name}")
    d = json.loads(f.read_text())
    d["next_run"] = _next_run(name)
    d["unit_state"] = subprocess.run(
        ["systemctl", "show", f"gdrive-task-{name}.service",
         "-p", "Result", "-p", "ActiveState", "-p", "ExecMainStatus"],
        capture_output=True, text=True).stdout.strip().splitlines()
    return d


def logs(name: str, lines: int = 50) -> dict:
    log = Path(f"/var/log/gdrive-agent/task-{name}.log")
    journal = subprocess.run(
        ["journalctl", "-u", f"gdrive-task-{name}.service", "-n", str(lines), "--no-pager"],
        capture_output=True, text=True).stdout
    return {"name": name,
            "log_tail": log.read_text().splitlines()[-lines:] if log.exists() else [],
            "journal": journal.splitlines()[-lines:]}


def run_now(name: str) -> dict:
    if not (TASK_DIR / f"{name}.json").exists():
        raise FileNotFoundError(f"无此任务: {name}")
    _systemctl("start", f"gdrive-task-{name}.service", "--no-block")
    return {"name": name, "started": True,
            "note": f"异步执行中。用 task(action='logs', name='{name}') 看结果"}


def delete(name: str) -> dict:
    f = TASK_DIR / f"{name}.json"
    if not f.exists():
        raise FileNotFoundError(f"无此任务: {name}")
    svc, timer = _unit(name)
    subprocess.run(["systemctl", "disable", "--now", timer.name], capture_output=True)
    for u in (svc, timer):
        u.unlink(missing_ok=True)
    f.unlink()
    _systemctl("daemon-reload")
    _systemctl("reset-failed", f"gdrive-task-{name}.service", "--quiet")
    return {"name": name, "deleted": True, "units_removed": [svc.name, timer.name]}
