"""海量小文件的 tar 分卷打包 + 可检索索引。

存在理由:Google Drive 每个文件都有固定 API 开销,列目录约每秒几个文件。
裸传 2 万个小文件可能要几小时;打成几个 2GB 的包只要几分钟。

索引保证打包不是单向操作——仍可定位并只取回单个文件,不必下整包。
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

from . import config
from .guards import human


def build(src: str | Path, out_dir: str | Path, volume_bytes: int | None = None) -> dict:
    """把 src 目录打成若干 tar 分卷,同时写 index.json 记录每个文件落在哪一卷。"""
    src = Path(src).resolve()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if volume_bytes is None:
        volume_bytes = int(config.load()["transfer"]["pack_volume_gb"]) * 1024**3

    index: dict[str, dict] = {}
    volumes: list[dict] = []
    vol_no = 0
    tar = None
    written = 0

    def open_vol():
        nonlocal tar, vol_no, written
        vol_no += 1
        written = 0
        path = out / f"vol-{vol_no:04d}.tar"
        tar = tarfile.open(path, "w")
        volumes.append({"volume": vol_no, "file": path.name, "bytes": 0, "files": 0})
        return path

    def close_vol():
        nonlocal tar
        if tar is not None:
            tar.close()
            volumes[-1]["bytes"] = (out / volumes[-1]["file"]).stat().st_size
            tar = None

    open_vol()
    for fp in sorted(p for p in src.rglob("*") if p.is_file()):
        size = fp.stat().st_size
        if written + size > volume_bytes and written > 0:
            close_vol()
            open_vol()
        rel = str(fp.relative_to(src))
        tar.add(fp, arcname=rel)
        index[rel] = {"volume": vol_no, "bytes": size}
        written += size
        volumes[-1]["files"] += 1
    close_vol()

    meta = {
        "source": str(src),
        "files": len(index),
        "volumes": volumes,
        "volume_bytes": volume_bytes,
        "index": index,
    }
    (out / "index.json").write_text(json.dumps(meta, ensure_ascii=False))
    return {
        "volumes": len(volumes), "files": len(index),
        "total": human(sum(v["bytes"] for v in volumes)),
        "index_path": str(out / "index.json"),
    }


def locate(index_path: str | Path, member: str) -> dict:
    """查某个文件在哪一卷。取回单文件时只需下那一卷。"""
    meta = json.loads(Path(index_path).read_text())
    entry = meta["index"].get(member)
    if entry is None:
        raise KeyError(f"索引中无此文件: {member}")
    vol = next(v for v in meta["volumes"] if v["volume"] == entry["volume"])
    return {"member": member, "volume_file": vol["file"],
            "volume_bytes": vol["bytes"], "member_bytes": entry["bytes"]}


def extract(volume_path: str | Path, member: str, dest: str | Path) -> dict:
    """从指定分卷里只解出一个文件。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(volume_path, "r") as tar:
        tar.extract(tar.getmember(member), path=dest, filter="data")
    return {"member": member, "extracted_to": str(dest / member)}
