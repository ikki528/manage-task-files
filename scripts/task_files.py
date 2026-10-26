#!/usr/bin/env python3
"""Manage explicitly owned scratch directories. Standard library only; no model calls.

Nothing outside reserve-created directories is ever deleted. Recovery is in-place
until expiry; permanent purge uses a journal and guarded, individual file deletes.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

VERSION = 1
RETENTION_SECONDS = 7 * 24 * 3600
OWNER = ".task-files-owner.json"
TASK_OWNER = ".task-files-task.json"


class SafetyError(Exception):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def timestamp():
    return utc_now().timestamp()


def inside(path, root):
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


def is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def checked_path(raw, exists=True):
    p = Path(os.path.abspath(os.fspath(raw)))
    for part in reversed([p, *p.parents]):
        try:
            s = part.lstat()
        except FileNotFoundError:
            continue
        if is_link(s):
            raise SafetyError(f"Symbolic link/reparse point refused: {part}")
    if exists and not p.exists():
        raise SafetyError(f"Path does not exist: {p}")
    return p


def identity(p):
    s = Path(p).lstat()
    if is_link(s):
        raise SafetyError(f"Linked path refused: {p}")
    return [s.st_dev, s.st_ino]


def atomic_json(path, obj):
    path = Path(path)
    tmp = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class FileLock:
    def __init__(self, path, timeout=5):
        self.path, self.timeout, self.file = Path(path), timeout, None

    def __enter__(self):
        checked_path(self.path, exists=False)
        self.file = open(self.path, "a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        until = time.monotonic() + self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= until:
                    self.file.close()
                    self.file = None
                    raise SafetyError(f"Resource is busy: {self.path.name}")
                time.sleep(0.05)

    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


def fingerprint_file(p):
    checked_path(p)
    before = p.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SafetyError(f"Only ordinary, single-link files are supported: {p}")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        opened = os.fstat(f.fileno())
        if [opened.st_dev, opened.st_ino] != [before.st_dev, before.st_ino]:
            raise SafetyError(f"File replaced during inspection: {p}")
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
        after = os.fstat(f.fileno())
    checked_path(p)
    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SafetyError(f"File changed during inspection: {p}")
    if identity(p) != [opened.st_dev, opened.st_ino]:
        raise SafetyError(f"File replaced during inspection: {p}")
    return {"id": [after.st_dev, after.st_ino], "size": after.st_size,
            "mtime_ns": after.st_mtime_ns, "sha256": h.hexdigest()}


def inventory(root):
    root = checked_path(root)
    if not root.is_dir():
        raise SafetyError("Resource must be a directory")
    files, dirs = {}, {".": identity(root)}
    for base, names, leaves in os.walk(root, followlinks=False):
        for name in names:
            p = checked_path(Path(base) / name)
            if not p.is_dir():
                raise SafetyError(f"Unexpected directory entry: {p}")
            dirs[p.relative_to(root).as_posix()] = identity(p)
        for name in leaves:
            p = Path(base) / name
            files[p.relative_to(root).as_posix()] = fingerprint_file(p)
    return {"files": files, "dirs": dirs}


def validate_inventory_shape(inv):
    if not isinstance(inv, dict) or set(inv) != {"files", "dirs"}:
        raise SafetyError("Invalid inventory")
    for collection in (inv["files"], inv["dirs"]):
        if not isinstance(collection, dict):
            raise SafetyError("Invalid inventory entries")
        for name in collection:
            if name != "." and (not name or "\\" in name or ":" in name or name.startswith("/") or ".." in Path(name).parts):
                raise SafetyError("Unsafe path in inventory")
    if "." not in inv["dirs"] or OWNER not in inv["files"]:
        raise SafetyError("Missing ownership data")


def check_inventory(current, saved, partial=False):
    validate_inventory_shape(saved)
    for key in ("files", "dirs"):
        if not partial and set(current[key]) != set(saved[key]):
            raise SafetyError("Resource contents changed since sealing")
        for rel, entry in current[key].items():
            if saved[key].get(rel) != entry:
                raise SafetyError(f"Resource changed or contains an unknown entry: {rel}")


class Manager:
    def __init__(self, state_dir):
        self.home = checked_path(state_dir, exists=False)
        self.path = self.home / "state.json"
        self.data = None

    @contextlib.contextmanager
    def transaction(self, create=False):
        if create:
            self.home.mkdir(parents=True, exist_ok=True)
            checked_path(self.home)
        if not self.home.exists():
            raise SafetyError("Manager is not initialized")
        with FileLock(self.home / "state.lock"):
            if self.path.exists():
                checked_path(self.path)
                try:
                    self.data = json.loads(self.path.read_text(encoding="utf-8"))
                    self.validate_state()
                except (ValueError, KeyError, TypeError) as e:
                    raise SafetyError(f"State is invalid; no files changed: {e}") from e
            elif create:
                self.data = {"version": VERSION, "manager_id": uuid.uuid4().hex,
                             "mode": "audit", "retention_seconds": RETENTION_SECONDS,
                             "roots": {}, "tasks": {}, "resources": {}, "events": []}
            else:
                raise SafetyError("Missing state; no files changed")
            yield self

    def validate_state(self):
        d = self.data
        if not isinstance(d, dict):
            raise SafetyError("State must be an object")
        if d.get("version") != VERSION or d.get("retention_seconds") != RETENTION_SECONDS:
            raise SafetyError("Unsupported state or unsafe retention policy")
        if not re.fullmatch(r"[0-9a-f]{32}", d["manager_id"]):
            raise SafetyError("Invalid manager identity")
        if d["mode"] not in ("audit", "recycle", "auto"):
            raise SafetyError("Invalid mode")
        for section in ("roots", "tasks", "resources"):
            if not isinstance(d[section], dict):
                raise SafetyError("Invalid state collection")
        if not isinstance(d.get("events"), list):
            raise SafetyError("Invalid event collection")
        for tid, t in d["tasks"].items():
            if tid != t["id"] or not re.fullmatch(r"[0-9a-f]{32}", tid):
                raise SafetyError("Invalid task identity")
            if t["state"] not in ("active", "idle") or not isinstance(t["deliveries"], list):
                raise SafetyError("Invalid task state")
        for rid, r in d["resources"].items():
            if rid != r["id"] or not re.fullmatch(r"[0-9a-f]{32}", rid):
                raise SafetyError("Invalid resource identity")
            if r["kind"] not in ("scratch", "working") or r["task_id"] not in d["tasks"]:
                raise SafetyError("Invalid resource record")
            if r["state"] not in ("active", "sealed", "retired", "purging", "purged") or type(r["pinned"]) is not bool:
                raise SafetyError("Invalid resource state")
            for field in ("created_at", "retired_at", "delete_after"):
                value = r.get(field)
                if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or value < 0):
                    raise SafetyError("Invalid resource timestamp")
            if r["state"] in ("sealed", "retired", "purging") and r.get("snapshot") is None:
                raise SafetyError("Sealed resource has no inventory")
            if r.get("snapshot") is not None:
                validate_inventory_shape(r["snapshot"])

    def save(self):
        self.validate_state()
        atomic_json(self.path, self.data)

    def event(self, action, **fields):
        self.data["events"].append({"at": timestamp(), "action": action, **fields})
        self.data["events"] = self.data["events"][-200:]

    def protect_root(self, p):
        p = checked_path(p)
        user = Path.home()
        protected = [user / ".codex", user / ".cache" / "codex-runtimes", self.home]
        for env in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(env):
                protected.append(Path(os.environ[env]))
        if p == Path(p.anchor) or p == user or any(inside(p, x) or inside(x, p) for x in protected):
            raise SafetyError("Root overlaps a protected application/system/state directory")
        return p

    def enabled_root(self, workspace):
        p = self.protect_root(workspace)
        roots = [(Path(k), v) for k, v in self.data["roots"].items() if inside(p, k)]
        if not roots:
            raise SafetyError("Workspace is not enrolled")
        root, rec = max(roots, key=lambda kv: len(str(kv[0])))
        if not rec["enabled"] or identity(checked_path(root)) != rec["identity"]:
            raise SafetyError("Root disabled or its identity changed")
        for directory in [p, *p.parents]:
            if (directory / ".git").is_file():
                raise SafetyError("Linked/managed Git worktrees are delegated to Codex")
            if directory == root:
                break
        return root

    def init(self, root, mode):
        root = self.protect_root(root)
        if not root.is_dir():
            raise SafetyError("Root must be a directory")
        key = str(root)
        if key in self.data["roots"] and self.data["roots"][key]["identity"] != identity(root):
            raise SafetyError("Existing root identity changed")
        self.data["roots"][key] = {"enabled": True, "identity": identity(root)}
        self.data["mode"] = mode
        self.event("enroll", root=key, mode=mode)
        self.save()
        return {"root": key, "mode": mode, "retention_days": 7}

    def start(self, workspace, label, session=None):
        ws = checked_path(workspace)
        root = self.enabled_root(ws)
        tid = uuid.uuid4().hex
        base = checked_path(ws / "work" / "managed-task-files", exists=False)
        base.mkdir(parents=True, exist_ok=True)
        checked_path(base)
        path = base / tid
        path.mkdir()
        owner = {"manager": self.data["manager_id"], "task": tid}
        atomic_json(path / TASK_OWNER, owner)
        t = {"id": tid, "workspace": str(ws), "root": str(root), "path": str(path),
             "identity": identity(path), "label": label, "session": session,
             "state": "active", "created_at": timestamp(), "deliveries": []}
        self.data["tasks"][tid] = t
        self.event("start", task=tid)
        self.save()
        return {"task": t}

    def task(self, tid):
        try:
            t = self.data["tasks"][tid]
        except KeyError:
            raise SafetyError("Unknown task")
        if not re.fullmatch(r"[0-9a-f]{32}", tid) or t["id"] != tid:
            raise SafetyError("Invalid task identity")
        self.enabled_root(t["workspace"])
        expected = Path(t["workspace"]) / "work" / "managed-task-files" / tid
        p = checked_path(t["path"])
        if p != expected or identity(p) != t["identity"]:
            raise SafetyError("Task path/identity changed")
        marker = checked_path(p / TASK_OWNER)
        if json.loads(marker.read_text(encoding="utf-8")) != {"manager": self.data["manager_id"], "task": tid}:
            raise SafetyError("Task ownership mismatch")
        return t

    def lease(self, tid, timeout=0):
        if not re.fullmatch(r"[0-9a-f]{32}", tid):
            raise SafetyError("Invalid task ID")
        return FileLock(self.home / ("task-" + tid + ".lock"), timeout)

    def resource(self, rid):
        try:
            r = self.data["resources"][rid]
        except KeyError:
            raise SafetyError("Unknown resource")
        t = self.task(r["task_id"])
        expected = Path(t["path"]) / (rid + "-" + r["name"])
        if Path(r["path"]) != expected or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", r["name"]):
            raise SafetyError("Resource path mismatch")
        return r, t

    def resource_path(self, r, partial=False):
        path = Path(r["path"])
        tomb = path.with_name(".purging-" + r["id"])
        if partial and tomb.exists():
            if path.exists():
                raise SafetyError("Both original and purge paths exist")
            path = tomb
        path = checked_path(path)
        if identity(path) != r["identity"]:
            raise SafetyError("Resource directory was replaced")
        marker = path / OWNER
        if marker.exists():
            checked_path(marker)
            expected = {"manager": self.data["manager_id"], "task": r["task_id"], "resource": r["id"]}
            if json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise SafetyError("Resource ownership mismatch")
        elif not (partial and not any(path.iterdir())):
            raise SafetyError("Ownership marker missing")
        return path

    def reserve(self, tid, name, kind):
        t = self.task(tid)
        if t["state"] != "active":
            raise SafetyError("Resume the task before creating new resources")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", name):
            raise SafetyError("Name must be 1-48 ASCII letters/digits/underscore/hyphen")
        with self.lease(tid):
            rid = uuid.uuid4().hex
            path = Path(t["path"]) / (rid + "-" + name)
            path.mkdir()
            atomic_json(path / OWNER, {"manager": self.data["manager_id"], "task": tid, "resource": rid})
            r = {"id": rid, "task_id": tid, "name": name, "path": str(path), "identity": identity(path),
                 "kind": kind, "state": "active", "pinned": False, "created_at": timestamp(),
                 "snapshot": None, "retired_at": None, "delete_after": None, "reason": None}
            self.data["resources"][rid] = r
            self.event("reserve", resource=rid, kind=kind)
            self.save()
        return {"resource": r}

    def seal(self, rid):
        r, t = self.resource(rid)
        if r["state"] in ("retired", "purging", "purged"):
            raise SafetyError("Restore/resume before resealing")
        with self.lease(t["id"]):
            p = self.resource_path(r)
            r["snapshot"] = inventory(p)
            r["state"] = "sealed"
            r["sealed_at"] = timestamp()
            self.event("seal", resource=rid)
            self.save()
        return {"resource": r}

    def publish(self, tid, path, requires=None):
        t = self.task(tid)
        with self.lease(tid):
            p = checked_path(path)
            fp = fingerprint_file(p)
            required = requires or []
            for rid in required:
                r, _ = self.resource(rid)
                if r["state"] in ("retired", "purging", "purged"):
                    raise SafetyError("Restore dependency before publishing")
            d = {"path": str(p), "fingerprint": fp, "requires": required, "at": timestamp()}
            t["deliveries"].append(d)
            for rid in required:
                self.data["resources"][rid]["pinned"] = True
            self.event("publish", task=tid, path=str(p))
            self.save()
        return {"delivery": d}

    def verify_deliveries(self, t, r):
        if not t["deliveries"]:
            raise SafetyError("No verified delivery registered; unfinished work is retained")
        for task in self.data["tasks"].values():
            for d in task["deliveries"]:
                if r["id"] in d.get("requires", []) or inside(d["path"], r["path"]):
                    raise SafetyError("A delivery depends on this resource")
        for d in t["deliveries"]:
            if fingerprint_file(checked_path(d["path"])) != d["fingerprint"]:
                raise SafetyError("A delivery changed/moved; verify it before cleanup")

    def retire(self, rid, reason):
        r, t = self.resource(rid)
        if self.data["mode"] == "audit":
            raise SafetyError("Audit mode does not arm deletion; choose recycle/auto explicitly")
        with self.lease(t["id"]):
            if t["state"] != "idle":
                raise SafetyError("Finish the task stage before retiring resources")
            if r["kind"] != "scratch" or r["pinned"] or r["state"] != "sealed":
                raise SafetyError("Only unpinned, sealed scratch resources may be retired")
            self.verify_deliveries(t, r)
            check_inventory(inventory(self.resource_path(r)), r["snapshot"])
            now = timestamp()
            r.update(state="retired", retired_at=now, delete_after=now + RETENTION_SECONDS, reason=reason)
            self.event("retire", resource=rid, delete_after=r["delete_after"])
            self.save()
        return {"resource": r, "recovery": "in-place; cancel with restore before purge"}

    def finish(self, tid):
        t = self.task(tid)
        with self.lease(tid):
            t["state"] = "idle"
            t["finished_at"] = timestamp()
            self.event("finish", task=tid)
            self.save()
        return {"task": t}

    def cancel_retirement(self, r, pin=False):
        if r["state"] == "purged":
            raise SafetyError("Already permanently deleted; recovery window has ended")
        if r["state"] == "purging":
            raise SafetyError("Purge was interrupted; inspect partial remnants before manual recovery")
        self.resource_path(r)
        r.update(state="active", snapshot=None, retired_at=None, delete_after=None)
        if pin:
            r["pinned"] = True

    def restore(self, rid, pin=False):
        r, t = self.resource(rid)
        with self.lease(t["id"]):
            self.cancel_retirement(r, pin)
            self.event("keep" if pin else "restore", resource=rid)
            self.save()
        return {"resource": r, "restored_path": r["path"], "permanent_deletion_undone": False}

    def resume(self, tid):
        t = self.task(tid)
        resources = [r for r in self.data["resources"].values() if r["task_id"] == tid and r["state"] != "purged"]
        with self.lease(tid):
            if any(r["state"] == "purging" for r in resources):
                raise SafetyError("Task has interrupted purge remnants; inspect them first")
            for r in resources:
                self.cancel_retirement(r)
            t["state"] = "active"
            self.event("resume", task=tid)
            self.save()
        return {"task": t}

    def eligible(self, r, t, now):
        if r["kind"] != "scratch" or r["pinned"] or t["state"] != "idle":
            raise SafetyError("Resource is protected or task is active")
        start, end = r.get("retired_at"), r.get("delete_after")
        if any(type(value) not in (float, int) or not math.isfinite(value) or value < 0 for value in (start, end)) or end < start + RETENTION_SECONDS:
            raise SafetyError("Missing/invalid retention timestamps")
        if now < end:
            raise SafetyError("Recovery period has not elapsed")
        self.verify_deliveries(t, r)

    def sweep(self, dry_run=False):
        report = {"mode": self.data["mode"], "dry_run": dry_run, "checked": 0,
                  "eligible": [], "purged": [], "skipped": [], "released_bytes": 0}
        for rid in list(self.data["resources"]):
            r = self.data["resources"][rid]
            if r["state"] not in ("retired", "purging"):
                continue
            report["checked"] += 1
            try:
                r, t = self.resource(rid)
                with self.lease(t["id"]):
                    self.eligible(r, t, timestamp())
                    original = Path(r["path"])
                    tomb = original.with_name(".purging-" + rid)
                    if r["state"] == "purging" and not original.exists() and not tomb.exists():
                        if not dry_run and self.data["mode"] == "auto":
                            r.update(state="purged", snapshot=None, purged_at=timestamp())
                            self.event("purge-reconciled", resource=rid)
                            self.save()
                        report["skipped"].append({"id": rid, "reason": "Purge already removed directory; no additional bytes credited"})
                        continue
                    p = self.resource_path(r, partial=r["state"] == "purging")
                    current = inventory(p)
                    check_inventory(current, r["snapshot"], partial=r["state"] == "purging")
                    report["eligible"].append({"id": rid, "bytes": sum(x["size"] for x in current["files"].values())})
                    if dry_run or self.data["mode"] != "auto":
                        continue
                    if r["state"] != "purging":
                        r["state"] = "purging"
                        self.event("purge-start", resource=rid)
                        self.save()
                    if p == original:
                        if tomb.exists():
                            raise SafetyError("Purge staging path occupied")
                        checked_path(original)
                        os.rename(original, tomb)
                        p = tomb
                    removed = guarded_delete(p, r["snapshot"])
                    r.update(state="purged", snapshot=None, purged_at=timestamp(), released_bytes=removed)
                    self.event("purge-complete", resource=rid, released_bytes=removed)
                    self.save()
                    report["purged"].append(rid)
                    report["released_bytes"] += removed
            except (SafetyError, OSError, ValueError, KeyError, TypeError) as e:
                report["skipped"].append({"id": rid, "reason": str(e)})
        if not dry_run:
            self.data["last_sweep"] = {"at": timestamp(), **report}
            self.event("sweep", checked=report["checked"], purged=len(report["purged"]), released_bytes=report["released_bytes"])
            self.save()
        return report

    def status(self):
        resources = []
        for r in self.data["resources"].values():
            item = {k: r.get(k) for k in ("id", "task_id", "path", "kind", "state", "pinned", "delete_after", "reason")}
            item["sealed_bytes"] = sum(x["size"] for x in (r.get("snapshot") or {}).get("files", {}).values())
            resources.append(item)
        return {"state_dir": str(self.home), "mode": self.data["mode"], "retention_days": 7,
                "roots": self.data["roots"], "tasks": list(self.data["tasks"].values()),
                "resources": resources, "last_sweep": self.data.get("last_sweep"),
                "recent_events": self.data["events"][-20:]}


def guarded_delete(root, snapshot):
    """Delete only expected remaining entries, with no blanket recursive removal.

    Windows holds each directory against rename, and opens files denying write and
    delete sharing before validating and deleting through that same handle.
    """
    validate_inventory_shape(snapshot)
    if os.name != "nt":
        raise SafetyError("Permanent deletion currently supported on Windows only")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    dispose = kernel.SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value

    class Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    def open_handle(path, directory=False):
        checked_path(path)
        access = 0x80000000 | 0x10000  # GENERIC_READ | DELETE
        sharing = 3 if directory else 1  # directories allow child activity, never rename
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        h = create(str(path), access, sharing, None, 3, flags, None)
        if h == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def delete_handle(h):
        info = Disposition(True)
        if not dispose(h, 4, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    removed = 0

    def walk(path, rel):
        nonlocal removed
        expected_dir = snapshot["dirs"].get(rel)
        if expected_dir != identity(checked_path(path)):
            raise SafetyError("Directory changed before deletion")
        hdir = open_handle(path, directory=True)
        try:
            # Re-check after acquiring rename protection.
            if identity(path) != expected_dir:
                raise SafetyError("Directory identity changed")
            entries = list(path.iterdir())
            entries.sort(key=lambda p: (p.name == OWNER, p.name))
            for child in entries:
                sub = child.name if rel == "." else rel + "/" + child.name
                s = child.lstat()
                if is_link(s):
                    raise SafetyError("Linked entry encountered during purge")
                if stat.S_ISDIR(s.st_mode):
                    walk(child, sub)
                    continue
                expected = snapshot["files"].get(sub)
                if not expected:
                    raise SafetyError(f"Unknown file encountered during purge: {sub}")
                hf = open_handle(child)
                fd = None
                try:
                    fd = msvcrt.open_osfhandle(int(hf), os.O_RDONLY | os.O_BINARY)
                    with os.fdopen(fd, "rb") as f:
                        fd = -1  # file object owns the Win32 handle
                        info = os.fstat(f.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise SafetyError("Nonordinary or hard-linked file encountered")
                        digest = hashlib.sha256()
                        for block in iter(lambda: f.read(1024 * 1024), b""):
                            digest.update(block)
                        actual = {"id": [info.st_dev, info.st_ino], "size": info.st_size,
                                  "mtime_ns": info.st_mtime_ns, "sha256": digest.hexdigest()}
                        if actual != expected:
                            raise SafetyError(f"File changed before deletion: {sub}")
                        # Keep the ownership marker until every other entry is gone.
                        if child.name == OWNER and rel == "." and any(x.name != OWNER for x in path.iterdir()):
                            raise SafetyError("New entries appeared during purge")
                        delete_handle(hf)
                        removed += info.st_size
                finally:
                    if fd is None:
                        close(hf)
            if any(path.iterdir()):
                raise SafetyError("Directory not empty after guarded deletion")
            delete_handle(hdir)
        finally:
            close(hdir)
    walk(root, ".")
    return removed


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    # MSIX-packaged desktop applications may virtualize LOCALAPPDATA for child
    # processes. A Windows scheduled task must see the same physical state path.
    default = Path.home() / ".codex" / "task-files-state"
    p.add_argument("--state-dir", default=str(default))
    commands = p.add_subparsers(dest="command", required=True)
    q = commands.add_parser("init"); q.add_argument("--root", required=True); q.add_argument("--mode", choices=["audit", "recycle", "auto"], default="audit")
    q = commands.add_parser("start"); q.add_argument("--workspace", required=True); q.add_argument("--label", required=True); q.add_argument("--session")
    q = commands.add_parser("reserve"); q.add_argument("--task", required=True); q.add_argument("--name", required=True); q.add_argument("--kind", choices=["scratch", "working"], required=True)
    for name in ("seal", "keep", "restore"):
        q = commands.add_parser(name); q.add_argument("--resource", required=True)
    q = commands.add_parser("retire"); q.add_argument("--resource", required=True); q.add_argument("--reason", required=True)
    q = commands.add_parser("publish"); q.add_argument("--task", required=True); q.add_argument("--path", required=True); q.add_argument("--requires", action="append", default=[])
    for name in ("finish", "resume"):
        q = commands.add_parser(name); q.add_argument("--task", required=True)
    q = commands.add_parser("run"); q.add_argument("--task", required=True); q.add_argument("argv", nargs=argparse.REMAINDER)
    q = commands.add_parser("sweep"); q.add_argument("--dry-run", action="store_true")
    for name in ("enable", "disable"):
        q = commands.add_parser(name); q.add_argument("--root", required=True)
    q = commands.add_parser("set-mode"); q.add_argument("--mode", choices=["audit", "recycle", "auto"], required=True)
    commands.add_parser("status")
    commands.add_parser("hook")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    mgr = Manager(args.state_dir)
    try:
        if args.command == "run":
            command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            if not command:
                raise SafetyError("Missing producer command")
            with mgr.transaction():
                t = mgr.task(args.task)
                if t["state"] != "active":
                    raise SafetyError("Resume the task before running a producer")
                cwd = t["workspace"]
            with mgr.lease(args.task):
                # Re-read after locking, closing a race with finish/retire.
                with mgr.transaction():
                    if mgr.task(args.task)["state"] != "active":
                        raise SafetyError("Task is no longer active")
                proc = subprocess.run(command, cwd=cwd, shell=False)
            result = {"ok": proc.returncode == 0, "producer_exit_code": proc.returncode}
            print(json.dumps(result, ensure_ascii=False))
            return proc.returncode
        if args.command == "hook":
            # Hooks only remind agents of the contract; they never delete or infer completion.
            event = json.load(sys.stdin)
            if event.get("hook_event_name") == "SessionStart":
                result = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "For enrolled local tasks that create temporary files, use $manage-task-files. Keep unfinished work and final deliverables protected. Cleanup applies only to explicitly retired, registered scratch groups."}}
            else:
                result = {}
            print(json.dumps(result))
            return 0
        with mgr.transaction(create=args.command == "init"):
            c = args.command
            if c == "init": result = mgr.init(args.root, args.mode)
            elif c == "start": result = mgr.start(args.workspace, args.label, args.session)
            elif c == "reserve": result = mgr.reserve(args.task, args.name, args.kind)
            elif c == "seal": result = mgr.seal(args.resource)
            elif c == "publish": result = mgr.publish(args.task, args.path, args.requires)
            elif c == "retire": result = mgr.retire(args.resource, args.reason)
            elif c == "finish": result = mgr.finish(args.task)
            elif c == "resume": result = mgr.resume(args.task)
            elif c in ("restore", "keep"): result = mgr.restore(args.resource, pin=c == "keep")
            elif c == "sweep": result = mgr.sweep(args.dry_run)
            elif c == "status": result = mgr.status()
            elif c == "set-mode":
                mgr.data["mode"] = args.mode; mgr.event("set-mode", mode=args.mode); mgr.save(); result = {"mode": args.mode}
            elif c in ("enable", "disable"):
                key = str(checked_path(args.root))
                if key not in mgr.data["roots"]:
                    raise SafetyError("Unknown root")
                mgr.data["roots"][key]["enabled"] = c == "enable"
                mgr.event(c, root=key); mgr.save(); result = {"root": key, "enabled": c == "enable"}
            else: raise SafetyError("Unsupported operation")
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except (SafetyError, OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
