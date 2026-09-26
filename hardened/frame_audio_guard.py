#!/usr/bin/env python3
"""Unofficial Frame audio guard. Local review candidate, NOT deployed.

Upstream purpose retained; persistence/identity/verified recovery are new.
Only user-owned audio threads, user files, and local RealtimeKit D-Bus.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

NAME = "frame-stream-audio-fix"
RESET = 0x40000000
RR = 2 | RESET
NORMAL = RESET
SERVICES = {"pipewire": "/usr/bin/pipewire", "pipewire-pulse": "/usr/bin/pipewire",
            "wireplumber": "/usr/bin/wireplumber"}
EXPECTED = {"policy", "rtprio", "nice"}
IDENTITY = {"boot_id", "uid", "pid", "tid", "pid_start", "tid_start", "exe",
            "exe_dev", "exe_ino", "process_name", "thread_name", "service"}


class GuardError(Exception):
    pass


class RpcUncertain(GuardError):
    """A D-Bus client error does not prove the server cancelled its request."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def emit(event, **fields):
    print(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
                      **fields}, sort_keys=True), flush=True)


def safe_path(path: Path, directory=False, create=False, private=False):
    """Reject symlink traversal and writable-by-other paths in our HOME subtree."""
    path = path.absolute()
    home = Path.home().absolute()
    if path != home and home not in path.parents:
        raise GuardError("PATH_OUTSIDE_HOME")
    relative = path.relative_to(home)
    current = home
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            st = current.lstat()
        except FileNotFoundError:
            if not create or (current == path and not directory):
                if current == path:
                    return path
                raise GuardError("MISSING_PARENT")
            current.mkdir(mode=0o700)
            sync_dir(current.parent)
            st = current.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise GuardError(f"UNSAFE_PATH: {current}")
        if current == path and private and st.st_mode & 0o077:
            raise GuardError(f"MANAGED_PATH_NOT_PRIVATE: {current}")
        if current != path or directory:
            if not stat.S_ISDIR(st.st_mode):
                raise GuardError("NOT_DIRECTORY")
        elif not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise GuardError("NOT_PRIVATE_REGULAR_FILE")
    return path


def sync_dir(directory):
    # Windows only hosts the fault-injection tests. Runtime refuses non-Linux.
    if sys.platform != "win32":
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, data, mode=0o600):
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        if sys.platform != "win32":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class StateStore:
    """State never silently resets on read/checksum/schema failure."""
    def __init__(self, root, validate_paths=True):
        self.root = Path(root)
        self.path = self.root / "state.json"
        self.anchor = self.root / "state-required"
        self.validate_paths = validate_paths

    def check(self):
        if self.validate_paths:
            safe_path(self.root, directory=True, create=True, private=True)
            safe_path(self.path, private=True)
            safe_path(self.anchor, private=True)

    def load(self):
        self.check()
        if not self.path.exists():
            if self.anchor.exists():
                raise GuardError("STATE_MISSING_FAIL_CLOSED")
            return []
        if self.path.stat().st_size > 1_000_000:
            raise GuardError("STATE_CORRUPT_OVERSIZE")
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            body = obj["body"]
            if set(obj) != {"body", "sha256"} or hashlib.sha256(canonical(body)).hexdigest() != obj["sha256"]:
                raise ValueError("checksum")
            if set(body) != {"version", "records"} or body["version"] != 1:
                raise ValueError("version")
            records = body["records"]
            if not isinstance(records, list) or len(records) > 128:
                raise ValueError("records")
            keys = set()
            for rec in records:
                validate_record(rec)
                key = (rec["identity"]["pid"], rec["identity"]["tid"])
                if key in keys:
                    raise ValueError("duplicate")
                keys.add(key)
            return records
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise GuardError(f"STATE_CORRUPT: {exc}") from exc

    def save(self, records):
        self.check()
        for rec in records:
            validate_record(rec)
        if not self.anchor.exists():
            atomic_bytes(self.anchor, b"state-required-v1\n")
        body = {"version": 1, "records": records}
        atomic_bytes(self.path, canonical({"body": body, "sha256": hashlib.sha256(canonical(body)).hexdigest()}) + b"\n")


def validate_record(rec):
    if set(rec) != {"identity", "original", "phase"} or rec["phase"] not in {
            "WRITE_AHEAD", "TRACKED", "DEMOTE_IN_FLIGHT", "RESTORE_IN_FLIGHT"}:
        raise ValueError("record schema")
    ident, orig = rec["identity"], rec["original"]
    if set(ident) != IDENTITY or set(orig) != EXPECTED:
        raise ValueError("identity/state schema")
    if ident["service"] not in SERVICES or ident["exe"] != SERVICES[ident["service"]]:
        raise ValueError("non-audio executable")
    if not re.fullmatch(r"[a-f0-9-]{36}", ident["boot_id"]):
        raise ValueError("boot identity")
    for key in ("uid", "pid", "tid", "pid_start", "tid_start", "exe_dev", "exe_ino"):
        if type(ident[key]) is not int or ident[key] < (0 if key in {"uid", "exe_dev"} else 1):
            raise ValueError("numeric identity")
    for key in ("process_name", "thread_name"):
        if not isinstance(ident[key], str) or not 1 <= len(ident[key]) <= 64:
            raise ValueError("name identity")
    if any(type(orig[k]) is not int for k in EXPECTED):
        raise ValueError("scheduler values")
    if orig["policy"] != RR or not 1 <= orig["rtprio"] <= 80 or not 0 <= orig["nice"] <= 19:
        raise ValueError("unsupported original state")


def same(a, b):
    return a["identity"] == b["identity"]


class Engine:
    def __init__(self, store, backend, logger=emit):
        self.store, self.backend, self.log = store, backend, logger

    def rpc(self, rec, records, phase, action):
        # A crash after dispatch but before acknowledgement is NOT equivalent
        # to a completed request. Never erase this uncertainty by observing one
        # apparently restored snapshot; a delayed RPC could still change it.
        rec["phase"] = phase
        self.store.save(records)
        try:
            action()
        except RpcUncertain:
            raise
        except (GuardError, OSError) as exc:
            # Backend contract: these errors occurred before dispatch or came
            # from a synchronous syscall, not from an ambiguous D-Bus request.
            rec["phase"] = "TRACKED"
            self.store.save(records)
            raise exc
        rec["phase"] = "TRACKED"
        self.store.save(records)

    def restore_all(self):
        records = self.store.load()
        for rec in list(records):
            ident, orig = rec["identity"], rec["original"]
            try:
                actual = self.backend.snapshot(ident)
                if not same(rec, actual):
                    raise GuardError("IDENTITY_MISMATCH_NO_TOUCH")
                if rec["phase"].endswith("_IN_FLIGHT"):
                    raise GuardError("RPC_COMPLETION_UNKNOWN_MANUAL_REVIEW_NO_TOUCH")
                current = actual["original"]
                if current != orig:
                    # Do not overwrite a scheduler changed by an unrelated actor.
                    if current["policy"] not in (NORMAL, RR) or current["nice"] not in (orig["nice"], -11):
                        raise GuardError("UNEXPECTED_STATE_NO_TOUCH")
                    if current["rtprio"] != (orig["rtprio"] if current["policy"] == RR else 0):
                        raise GuardError("UNEXPECTED_PRIORITY_NO_TOUCH")
                    if current["nice"] != orig["nice"]:
                        self.backend.set_nice(rec, current, orig["nice"])
                    actual = self.backend.snapshot(ident)
                    if not same(rec, actual) or actual["original"]["nice"] != orig["nice"]:
                        raise GuardError("NICE_RESTORE_VERIFY_FAILED")
                    if actual["original"]["policy"] != orig["policy"]:
                        self.rpc(rec, records, "RESTORE_IN_FLIGHT",
                                 lambda: self.backend.restore_rr(rec, actual["original"]))
                verified = self.backend.snapshot(ident)
                if not same(rec, verified) or verified["original"] != orig:
                    raise GuardError("RESTORE_VERIFY_FAILED")
                # A write failure leaves the durable prior record available.
                remaining = [r for r in records if r != rec]
                self.store.save(remaining)
                records = remaining
                self.log("RESTORED_VERIFIED", pid=ident["pid"], tid=ident["tid"])
            except (OSError, GuardError, subprocess.SubprocessError) as exc:
                self.log("WARNING_RESTORE_UNRESOLVED", pid=ident["pid"], tid=ident["tid"], reason=str(exc))
        return not self.store.load()

    def demote_all(self, continuing=False):
        tracked = self.store.load()
        if tracked and not continuing:
            self.log("NEW_DEMOTE_BLOCKED_UNRESOLVED_STATE")
            return False
        for rec in tracked:
            try:
                if rec["phase"] != "TRACKED":
                    raise GuardError("TRACKED_OPERATION_INCOMPLETE")
                current = self.backend.snapshot(rec["identity"])
                if not same(rec, current) or current["original"] != {"policy": NORMAL, "rtprio": 0, "nice": -11}:
                    raise GuardError("TRACKED_THREAD_CHANGED")
            except (OSError, GuardError) as exc:
                self.log("WARNING_ACTIVE_STATE_UNRESOLVED", reason=str(exc))
                return False
        for snap in self.backend.candidates():
            orig = snap["original"]
            if orig["policy"] & ~RESET == 0:
                continue  # untouched normal audio worker threads
            try:
                rec = copy.deepcopy(snap)
                rec["phase"] = "WRITE_AHEAD"
                validate_record(rec)
            except (ValueError, TypeError, KeyError):
                self.log("WARNING_UNSUPPORTED_SCHEDULER_NO_TOUCH", snapshot=snap)
                continue
            records = self.store.load()
            records.append(rec)
            self.store.save(records)  # MUST complete before any scheduler mutation
            try:
                self.rpc(rec, records, "DEMOTE_IN_FLIGHT", lambda: self.backend.demote(rec, orig))
                actual = self.backend.snapshot(rec["identity"])
                if not same(rec, actual) or actual["original"] != {"policy": NORMAL, "rtprio": 0, "nice": -11}:
                    raise GuardError("DEMOTE_VERIFY_FAILED")
                self.log("DEMOTED_VERIFIED", pid=rec["identity"]["pid"], tid=rec["identity"]["tid"])
            except (OSError, GuardError, subprocess.SubprocessError) as exc:
                self.log("WARNING_DEMOTE_FAILED", reason=str(exc))
                self.restore_all()
                return False
        return True

    def startup_recovery(self):
        return self.restore_all()  # never enumerate RR-only candidates before this


def proc_stat(path):
    raw = path.read_text()
    stop = raw.rfind(")")
    fields = raw[stop + 2:].split()
    return raw[raw.find("(") + 1:stop], int(fields[19])


class LinuxBackend:
    @staticmethod
    def command(args):
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=8).strip()

    def snapshot(self, ident):
        pid, tid, service = ident["pid"], ident["tid"], ident["service"]
        proc = Path("/proc") / str(pid)
        task = proc / "task" / str(tid)
        first = proc_stat(task / "stat")
        process_name, pid_start = proc_stat(proc / "stat")
        exe = os.readlink(proc / "exe")
        exe_stat = (proc / "exe").stat()
        uid = task.stat().st_uid
        if uid != os.getuid() or exe != SERVICES[service]:
            raise GuardError("TARGET_NOT_OWN_AUDIO_PROCESS")
        policy = os.sched_getscheduler(tid)
        rtprio = os.sched_getparam(tid).sched_priority
        nice = os.getpriority(os.PRIO_PROCESS, tid)
        last_exe = (proc / "exe").stat()
        if (first != proc_stat(task / "stat") or
                (process_name, pid_start) != proc_stat(proc / "stat") or
                uid != task.stat().st_uid or exe != os.readlink(proc / "exe") or
                (exe_stat.st_dev, exe_stat.st_ino) != (last_exe.st_dev, last_exe.st_ino) or
                (policy, rtprio, nice) != (os.sched_getscheduler(tid),
                    os.sched_getparam(tid).sched_priority, os.getpriority(os.PRIO_PROCESS, tid))):
            raise GuardError("IDENTITY_CHANGED_DURING_READ")
        return {"identity": {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "uid": uid, "pid": pid, "tid": tid, "pid_start": pid_start, "tid_start": first[1],
            "exe": exe, "exe_dev": exe_stat.st_dev, "exe_ino": exe_stat.st_ino,
            "process_name": process_name, "thread_name": first[0], "service": service},
            "original": {"policy": policy, "rtprio": rtprio, "nice": nice}}

    def candidates(self):
        out = []
        for service in SERVICES:
            pid = int(self.command(["systemctl", "--user", "show", service, "-p", "MainPID", "--value"]))
            if pid == 0:
                continue
            for task in (Path("/proc") / str(pid) / "task").iterdir():
                out.append(self.snapshot({"pid": pid, "tid": int(task.name), "service": service}))
        return out

    def require(self, rec, expected):
        current = self.snapshot(rec["identity"])
        if not same(rec, current) or current["original"] != expected:
            raise GuardError("PRE_OPERATION_IDENTITY_OR_STATE_CHANGED")

    def demote(self, rec, expected):
        self.require(rec, expected)
        # RealtimeKit applies OTHER|RESET and nice. A failed call may be partial.
        self.rtkit("MakeThreadHighPriorityWithPID", "tti", rec, -11)

    def set_nice(self, rec, expected, value):
        self.require(rec, expected)
        # Only originals 0..19 are supported: restoration lowers nice priority.
        if not 0 <= value <= 19:
            raise GuardError("UNSUPPORTED_NICE_RESTORE")
        os.setpriority(os.PRIO_PROCESS, rec["identity"]["tid"], value)

    def restore_rr(self, rec, expected):
        self.require(rec, expected)
        self.rtkit("MakeThreadRealtimeWithPID", "ttu", rec, rec["original"]["rtprio"])

    def rtkit(self, method, signature, rec, value):
        ident = rec["identity"]
        try:
            self.command(["busctl", "call", "org.freedesktop.RealtimeKit1", "/org/freedesktop/RealtimeKit1",
                "org.freedesktop.RealtimeKit1", method, signature, "--", str(ident["pid"]), str(ident["tid"]), str(value)])
        except subprocess.SubprocessError as exc:
            # Includes timeout and nonzero busctl exit. Do not assume the server
            # did nothing, or that killing a client cancels a queued method.
            raise RpcUncertain("RTKIT_REPLY_UNCERTAIN_RETAIN_STATE") from exc

    def streaming(self):
        # Scan executable links, not arbitrary command-line/credential contents.
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                if p.stat().st_uid == os.getuid() and Path(os.readlink(p / "exe")).name == "vrlink":
                    return True
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
        return False


@contextlib.contextmanager
def locked(store):
    import fcntl  # Linux production only; core tests inject a single owner
    store.check()
    path = store.root / "guard.lock"
    safe_path(path, private=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise GuardError("LOCK_BUSY_NO_ACTION") from exc
    finally:
        os.close(fd)  # flock is released even after SIGKILL/process exit


class Files:
    def __init__(self, home):
        self.home = Path(home)
        self.bin = self.home / ".local/bin" / NAME
        self.helper = self.home / ".local/bin/frame_audio_guard.py"
        self.unit = self.home / ".config/systemd/user" / (NAME + ".service")
        self.paths = (self.unit, self.bin, self.helper)

    def precheck(self):
        for p in self.paths:
            safe_path(p, private=True)

    def remove_transaction(self, reload_callback, rollback_reload_callback):
        self.precheck()
        archive = self.home / ".local/state" / NAME / "uninstall-archive"
        safe_path(archive, directory=True, create=True, private=True)
        if any(archive.iterdir()):
            raise GuardError("UNINSTALL_ARCHIVE_EXISTS_REVIEW_REQUIRED")
        moved = []
        try:
            for p in self.paths:
                if not p.is_file():
                    raise GuardError("INSTALL_FILE_MISSING")
            for p in self.paths:
                target = archive / p.name
                os.rename(p, target)
                moved.append((p, target))
                sync_dir(p.parent)
                sync_dir(archive)
            reload_callback()
            atomic_bytes(archive / "REMOVAL_COMPLETED", b"inactive; restored; unit unloaded; archives retained\n")
        except BaseException:
            # A normal failure restores control files. Hard-kill archive is kept
            # for explicit local recovery; never overwrite a concurrent file.
            for p, target in reversed(moved):
                if p.exists():
                    raise GuardError("UNINSTALL_ROLLBACK_PATH_CONFLICT_ARCHIVE_RETAINED")
                os.rename(target, p)
                sync_dir(p.parent)
            # The prior reload may already have unloaded the unit. Restoring
            # files alone is not a complete rollback of the manager's view.
            (archive / "REMOVAL_COMPLETED").unlink(missing_ok=True)
            sync_dir(archive)
            if moved:
                try:
                    rollback_reload_callback()
                except (OSError, GuardError, subprocess.SubprocessError) as exc:
                    raise GuardError("UNINSTALL_FILES_RESTORED_BUT_ROLLBACK_RELOAD_FAILED") from exc
            raise


class UserService:
    def call(self, *args):
        subprocess.run(["systemctl", "--user", *args, NAME + ".service"], check=True, timeout=60)

    def stop(self):
        self.call("stop")

    def inactive(self):
        state = LinuxBackend.command(["systemctl", "--user", "show", NAME + ".service", "-p", "ActiveState", "--value"])
        return state == "inactive"  # failed/activating/unknown is NOT a green light

    def disable(self):
        self.call("disable")

    def reload(self):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)

    def reload_and_verify_removed(self):
        self.reload()
        raw = LinuxBackend.command(["systemctl", "--user", "show", NAME + ".service",
            "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", "-p", "FragmentPath"])
        props = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if (props.get("LoadState") != "not-found" or props.get("ActiveState") != "inactive" or
                props.get("FragmentPath") or props.get("UnitFileState") not in ("", "not-found")):
            raise GuardError("REMOVAL_SERVICE_VERIFY_FAILED")


def uninstall(engine, service, files, lock_factory):
    """No deletion until stop, explicit restore, verify, inactive, zero-state pass."""
    try:
        service.stop()
        with lock_factory():
            if not engine.restore_all() or not service.inactive() or engine.store.load():
                raise GuardError("UNRESOLVED_OR_NOT_INACTIVE")
            files.precheck()
            service.disable()
            if not service.inactive() or engine.store.load():
                raise GuardError("PRE_DELETE_RECHECK_FAILED")
            files.remove_transaction(service.reload_and_verify_removed, service.reload)
    except (GuardError, OSError, subprocess.SubprocessError) as exc:
        engine.log("UNINSTALL_BLOCKED_RESTORE_FAILED", reason=str(exc))
        return False
    engine.log("UNINSTALLED_VERIFIED", note="state/audit retained; no system audio restart")
    return True


def run(engine, stop_event):
    if not engine.startup_recovery():
        return False
    was_streaming = False
    try:
        while not stop_event.is_set():
            active = engine.backend.streaming()
            if active:
                if not engine.demote_all(continuing=was_streaming):
                    return False
            elif not active and engine.store.load():
                if not engine.restore_all():
                    return False
            was_streaming = active
            stop_event.wait(2)
    finally:
        restored = engine.restore_all()
    return restored


def acknowledge_previous_boot(store, current_boot, expected_hash, service):
    """Explicit operator resolution, NOT successful restoration of old threads.

    A reboot ends old tasks and outstanding D-Bus requests. Same-boot ambiguity
    cannot be acknowledged by this command. Caller MUST hold the writer lock.
    """
    if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", current_boot):
        raise GuardError("INVALID_CURRENT_BOOT_ID")
    records = store.load()
    if not records or not service.inactive():
        raise GuardError("ACK_REQUIRES_RECORDS_AND_INACTIVE_SERVICE")
    if any(r["identity"]["boot_id"] == current_boot for r in records):
        raise GuardError("SAME_BOOT_ACK_REFUSED")
    data = store.path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected_hash != digest:
        raise GuardError("ACK_HASH_MISMATCH")
    archive = store.root / ("operator-ack-previous-boot-" + digest + ".json")
    if store.validate_paths:
        safe_path(archive, private=True)
    if archive.exists() and archive.read_bytes() != data:
        raise GuardError("ACK_ARCHIVE_CONFLICT")
    if not archive.exists():
        atomic_bytes(archive, data)
    if not service.inactive():
        raise GuardError("ACK_SERVICE_CHANGED")
    store.save([])
    emit("OPERATOR_ACK_PREVIOUS_BOOT_NOT_RESTORED", archived_sha256=digest,
         note="Old thread lifetime ended by separately approved reboot; original state preserved in archive")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "recover", "status", "uninstall", "acknowledge-previous-boot"))
    parser.add_argument("--confirm-state-sha256")
    parser.add_argument("--acknowledge-not-restored", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() == 0 or socket.gethostname() != "frame":
        raise GuardError("WRONG_HOST_OR_ROOT_REFUSED")
    import pwd
    if pwd.getpwuid(os.getuid()).pw_name != "steamos":
        raise GuardError("WRONG_USER")
    os.umask(0o077)
    store = StateStore(Path.home() / ".local/state" / NAME)
    engine = Engine(store, LinuxBackend())
    if args.command == "uninstall":
        return 0 if uninstall(engine, UserService(), Files(Path.home()), lambda: locked(store)) else 4
    if args.command == "status":
        # Atomic snapshots are readable while run holds the single-writer lock.
        # Tracked state during an active stream is not itself a restore failure.
        records = store.load()
        emit("STATUS", pending_restore=len(records), streaming=engine.backend.streaming(),
             records=records, note="snapshot only; use recover for verified restoration")
        return 0
    with locked(store):
        if args.command == "acknowledge-previous-boot":
            if not args.acknowledge_not_restored or not args.confirm_state_sha256:
                raise GuardError("EXPLICIT_OPERATOR_ACK_REQUIRED")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            acknowledge_previous_boot(store, boot, args.confirm_state_sha256, UserService())
            return 0
        if args.command == "recover":
            return 0 if engine.restore_all() else 4
        event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: event.set())
        signal.signal(signal.SIGINT, lambda *_: event.set())
        return 0 if run(engine, event) else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GuardError, OSError, subprocess.SubprocessError, ValueError) as exc:
        emit("GUARD_BLOCKED", reason=str(exc))
        sys.exit(4)
