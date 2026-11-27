"""NA-06.7.5: bounded outputs from a trusted, admitted Local Runner.

A dedicated sibling of the fresh worktree is the only output namespace.
Never scan the worktree, accept an arbitrary path or follow links. Retained
payloads are mutable local files; a later delivery must verify the digest again.
This is not an OS security sandbox for repository code.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from local_workspace import validate_root, workspace_record, verify_prepared_workspace

MAX_LOCAL_ARTIFACT_BYTES = 10 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
_EXTENSIONS = frozenset((".txt",".md",".csv",".json",".xml",".html",".pdf",".docx",
                         ".xlsx",".pptx",".png",".jpg",".jpeg",".webp",".zip",
                         ".apk",".wav",".ogg",".mp3",".log"))


def local_filename(value: Any) -> bool:
    return (isinstance(value,str) and bool(_NAME.fullmatch(value))
            and not _RESERVED.match(value) and not value.endswith(".")
            and Path(value).suffix.lower() in _EXTENSIONS
            and not value.casefold().startswith(("auth_data","auth-data")))


def _ordinary(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return kind(info.st_mode) and not (
        getattr(info,"st_file_attributes",0) & getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400))


def _directory(root: Path, identity: str) -> Path:
    directory = root / (identity + ".artifacts")
    if directory.resolve(strict=True) != directory or not _ordinary(directory.lstat(),directory=True):
        raise ValueError("unsafe artifact directory")
    return directory


def prepare_output_dir(source, root, sha, workspace, names, *, permitted):
    """Create exactly one fresh output directory before the child is launched."""
    record = workspace_record(workspace,sha)
    if (not record or record["status"]!="prepared" or not names or len(names)>5
            or any(not local_filename(name) for name in names)
            or len({name.casefold() for name in names})!=len(names)):
        raise ValueError("invalid local artifact output scope")
    root=validate_root(str(root),(source,))
    if not permitted():
        return None
    directory=root/(record["workspace_id"]+".artifacts")
    directory.mkdir(exist_ok=False)
    return _directory(root,record["workspace_id"]) if permitted() else None


def observe_file(directory: Path, name: str, *, permitted):
    """Hash one declared regular file; no payload or absolute path in evidence."""
    if not local_filename(name):
        return {"reason":"unsafe_local_output"}
    path=directory/name
    try:
        if not permitted():
            return None
        if directory.resolve(strict=True)!=directory or not _ordinary(directory.lstat(),directory=True):
            return {"reason":"unsafe_local_output"}
        before=path.lstat()
        if (path.resolve(strict=True)!=path or not _ordinary(before)
                or before.st_nlink!=1):
            return {"reason":"unsafe_local_output"}
        if not 0<before.st_size<=MAX_LOCAL_ARTIFACT_BYTES:
            return {"reason":"local_output_size"}
        fd=os.open(path,os.O_RDONLY | getattr(os,"O_BINARY",0) | getattr(os,"O_NOFOLLOW",0)
                   | getattr(os,"O_NONBLOCK",0))
        with os.fdopen(fd,"rb") as stream:
            opened=os.fstat(stream.fileno())
            if not _ordinary(opened) or opened.st_nlink!=1 or not os.path.samestat(before,opened):
                return {"reason":"unsafe_local_output"}
            digest=hashlib.sha256()
            size=0
            while True:
                if not permitted():
                    return None
                chunk=stream.read(65536)
                if not chunk:
                    break
                size+=len(chunk)
                if size>MAX_LOCAL_ARTIFACT_BYTES:
                    return {"reason":"local_output_size"}
                digest.update(chunk)
            after=os.fstat(stream.fileno())
        latest=path.lstat()
        if (not _ordinary(latest) or latest.st_nlink!=1 or not os.path.samestat(before,latest)
                or directory.resolve(strict=True)!=directory or path.resolve(strict=True)!=path
                or size!=before.st_size or after.st_size!=size or latest.st_size!=size
                or before.st_mtime_ns!=after.st_mtime_ns or after.st_mtime_ns!=latest.st_mtime_ns):
            return {"reason":"local_output_changed"}
        return {"reason":"","size":size,"sha256":digest.hexdigest()} if permitted() else None
    except FileNotFoundError:
        return {"reason":"not_found"}
    except (OSError,ValueError,RuntimeError):
        return {"reason":"unsafe_local_output"}


def collect_local_outputs(source,root,repo,sha,workspace,names,*,permitted):
    record=workspace_record(workspace,sha)
    if not record or record["status"]!="prepared" or not permitted():
        return None
    # Tests may not silently alter the verified checkout; outputs belong outside it.
    clean=verify_prepared_workspace(source,root,repo,sha,record["workspace_id"],permitted=permitted)
    if not permitted():
        return None
    if clean is None:
        return {name:{"reason":"local_workspace_changed"} for name in names}
    try:
        root=validate_root(str(root),(source,))
        directory=_directory(root,record["workspace_id"])
    except (OSError,ValueError,RuntimeError):
        return {name:{"reason":"unsafe_local_output"} for name in names}
    outputs={}
    for name in names:
        value=observe_file(directory,name,permitted=permitted)
        if value is None or not permitted():
            return None
        outputs[name]=value
    return outputs
