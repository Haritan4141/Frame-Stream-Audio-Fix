"""Local fault-injection harness. No real scheduler, systemctl, D-Bus or SSH calls."""
import contextlib
import copy
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frame_audio_guard as g

NEW_BOOT = "11111111-2222-3333-4444-555555555555"


def snapshot(tid=11, policy=g.RR, nice=0):
    return {"identity": {"boot_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "uid": 1000, "pid": 10, "tid": tid, "pid_start": 100, "tid_start": 101 + tid,
        "exe": "/usr/bin/pipewire", "exe_dev": 1, "exe_ino": 99,
        "process_name": "pipewire", "thread_name": "data-loop.0", "service": "pipewire"},
        "original": {"policy": policy, "rtprio": 80 if policy & ~g.RESET in (1, 2) else 0, "nice": nice}}


class FakeBackend:
    def __init__(self, snaps=None, world=None):
        self.world = Path(world) if world else None
        self.threads = {str(s["identity"]["tid"]): copy.deepcopy(s) for s in (snaps or [snapshot()])}
        if self.world and self.world.exists():
            self.threads = json.loads(self.world.read_text())
        self.calls = []
        self.fail_restore = set()
        self.false_success = set()
        self.partial_demote = False
        self.active = True
        self.enumerations = 0
        self.persist()

    def persist(self):
        if self.world:
            g.atomic_bytes(self.world, g.canonical(self.threads))

    def snapshot(self, ident):
        if str(ident["tid"]) not in self.threads:
            raise ProcessLookupError("mock thread exited")
        return copy.deepcopy(self.threads[str(ident["tid"])])

    def candidates(self):
        self.enumerations += 1
        return copy.deepcopy(list(self.threads.values()))

    def require(self, rec, expected):
        current = self.snapshot(rec["identity"])
        if not g.same(rec, current) or current["original"] != expected:
            raise g.GuardError("mock identity/current changed")

    def demote(self, rec, expected):
        self.require(rec, expected)
        tid = rec["identity"]["tid"]
        self.calls.append(("demote", tid))
        self.threads[str(tid)]["original"] = {"policy": g.NORMAL, "rtprio": 0,
                                              "nice": expected["nice"] if self.partial_demote else -11}
        self.persist()
        if self.partial_demote:
            raise g.GuardError("mock RTKit failed after scheduler changed")

    def set_nice(self, rec, expected, value):
        self.require(rec, expected)
        tid = rec["identity"]["tid"]
        self.calls.append(("nice", tid, value))
        self.threads[str(tid)]["original"]["nice"] = value
        self.persist()

    def restore_rr(self, rec, expected):
        self.require(rec, expected)
        tid = rec["identity"]["tid"]
        self.calls.append(("restore", tid))
        if tid in self.fail_restore:
            raise g.GuardError("mock temporary restore failure")
        if tid not in self.false_success:
            self.threads[str(tid)]["original"]["policy"] = rec["original"]["policy"]
            self.threads[str(tid)]["original"]["rtprio"] = rec["original"]["rtprio"]
        self.persist()

    def streaming(self):
        return self.active


class FakeService:
    def __init__(self):
        self.events = []
        self.is_inactive = True
        self.fail = None

    def _action(self, name):
        self.events.append(name)
        if self.fail == name:
            raise g.GuardError("mock service " + name + " failed")

    def stop(self): self._action("stop")
    def inactive(self):
        self._action("inactive")
        return self.is_inactive
    def disable(self): self._action("disable")
    def reload(self): self._action("reload")
    def reload_and_verify_removed(self):
        self.reload()
        self._action("removed_verify")


class FakeFiles:
    def __init__(self):
        self.removed = False
        self.prechecked = False

    def precheck(self): self.prechecked = True
    def remove_transaction(self, reload_callback, rollback_reload_callback):
        reload_callback()
        self.removed = True


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = g.StateStore(self.root, validate_paths=False)
        self.backend = FakeBackend()
        self.logs = []
        self.engine = g.Engine(self.store, self.backend, lambda event, **kw: self.logs.append((event, kw)))

    def tearDown(self):
        self.temp.cleanup()

    def test_normal_demote_restore_exact_nice_and_reset_flag(self):
        original = self.backend.snapshot({"tid": 11})
        self.assertTrue(self.engine.demote_all())
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.backend.threads["11"]["original"], {"policy": g.NORMAL, "rtprio": 0, "nice": -11})
        self.assertTrue(self.engine.restore_all())
        self.assertEqual(self.store.load(), [])
        self.assertEqual(self.backend.threads["11"], original)

    def test_restore_temporary_failure_retains_state_then_retry(self):
        self.engine.demote_all()
        self.backend.fail_restore.add(11)
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(len(self.store.load()), 1)
        self.backend.fail_restore.clear()
        self.assertTrue(self.engine.restore_all())

    def test_ambiguous_dbus_timeout_retains_inflight_no_false_recovery(self):
        with patch.object(self.backend, "demote", side_effect=g.RpcUncertain("mock delayed request")):
            self.assertFalse(self.engine.demote_all())
        self.assertEqual(self.store.load()[0]["phase"], "DEMOTE_IN_FLIGHT")
        # Even an unchanged original snapshot cannot prove a queued call is gone.
        before = len(self.backend.calls)
        self.assertFalse(self.engine.startup_recovery())
        self.assertEqual(len(self.backend.calls), before)
        self.assertEqual(len(self.store.load()), 1)

    def test_crash_in_restore_rpc_is_not_silently_cleared(self):
        self.engine.demote_all()
        with patch.object(self.backend, "restore_rr", side_effect=g.RpcUncertain("mock crash before reply")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load()[0]["phase"], "RESTORE_IN_FLIGHT")
        self.backend.threads["11"]["original"] = snapshot()["original"]
        self.assertFalse(self.engine.startup_recovery())
        self.assertEqual(len(self.store.load()), 1)

    def test_only_successful_thread_removed_from_state(self):
        self.backend.threads["12"] = snapshot(12)
        self.engine.demote_all()
        self.backend.fail_restore.add(12)
        self.assertFalse(self.engine.restore_all())
        self.assertEqual([r["identity"]["tid"] for r in self.store.load()], [12])
        self.backend.fail_restore.clear()
        self.assertTrue(self.engine.restore_all())

    def test_restore_exit_success_without_effect_fails_verify(self):
        self.engine.demote_all()
        self.backend.false_success.add(11)
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(len(self.store.load()), 1)

    def test_crash_restart_loads_state_before_rr_enumeration(self):
        self.engine.demote_all()
        fresh = g.Engine(g.StateStore(self.root, False), self.backend, lambda *a, **k: None)
        self.assertTrue(fresh.startup_recovery())
        self.assertEqual(self.backend.enumerations, 1)
        self.assertEqual(self.backend.threads["11"]["original"], snapshot()["original"])

    def test_sigkill_equivalent_child_no_cleanup_then_recovery(self):
        child = subprocess.Popen([sys.executable, "-B", __file__, "--crash-child", str(self.root)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 8
            while not (self.root / "demoted.marker").exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.03)
            self.assertTrue((self.root / "demoted.marker").exists(), "child failed before write-ahead demote")
            child.kill()  # TerminateProcess on Windows / SIGKILL on POSIX; no finally/trap
            child.wait(timeout=5)
            backend = FakeBackend(world=self.root / "world.json")
            self.assertEqual(backend.threads["11"]["original"]["policy"], g.NORMAL)
            fresh = g.Engine(g.StateStore(self.root, False), backend, lambda *a, **k: None)
            self.assertEqual(len(fresh.store.load()), 1)
            self.assertTrue(fresh.startup_recovery())
            self.assertEqual(backend.threads["11"]["original"], snapshot()["original"])
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_stale_identity_no_touch_boot_pid_tid_exe_name(self):
        self.engine.demote_all()
        original = copy.deepcopy(self.backend.threads["11"])
        for field in ("boot_id", "uid", "pid_start", "tid_start", "exe_ino", "process_name", "thread_name"):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                value = changed["identity"][field]
                changed["identity"][field] = value + 1 if isinstance(value, int) else "changed"
                self.backend.threads["11"] = changed
                before = len(self.backend.calls)
                self.assertFalse(self.engine.restore_all())
                self.assertEqual(len(self.backend.calls), before)
                self.assertEqual(len(self.store.load()), 1)

    def test_exited_thread_retained_for_manual_review(self):
        self.engine.demote_all()
        del self.backend.threads["11"]
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(len(self.store.load()), 1)

    def test_unknown_external_scheduler_change_no_touch(self):
        self.engine.demote_all()
        self.backend.threads["11"]["original"] = {"policy": 5, "rtprio": 0, "nice": 0}
        before = len(self.backend.calls)
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(before, len(self.backend.calls))

    def test_recovery_failure_blocks_loop_and_new_candidates(self):
        self.engine.demote_all()
        self.backend.fail_restore.add(11)
        self.assertFalse(g.run(self.engine, __import__("threading").Event()))
        self.assertEqual(self.backend.enumerations, 1)

    def test_unresolved_state_blocks_new_demote(self):
        self.engine.demote_all()
        before = len(self.backend.calls)
        self.assertFalse(self.engine.demote_all())
        self.assertEqual(len(self.backend.calls), before)

    def test_continuing_stream_finds_new_rt_threads_without_duplicate(self):
        self.engine.demote_all()
        self.backend.threads["12"] = snapshot(12)
        self.assertTrue(self.engine.demote_all(continuing=True))
        self.assertEqual(len(self.store.load()), 2)
        self.assertEqual(self.backend.calls.count(("demote", 11)), 1)

    def test_partial_demote_error_immediately_recovers(self):
        self.backend.partial_demote = True
        self.assertFalse(self.engine.demote_all())
        self.assertEqual(self.store.load(), [])
        self.assertEqual(self.backend.threads["11"]["original"], snapshot()["original"])

    def test_unsupported_policy_and_nice_never_demoted(self):
        for policy, nice in ((g.RESET | 1, 0), (1, 0), (2, 0), (3, 0), (5, 0), (6, 0), (12345, 0), (g.RR, -11)):
            with self.subTest(policy=policy, nice=nice):
                self.backend.threads["11"] = snapshot(policy=policy, nice=nice)
                self.assertTrue(self.engine.demote_all())
                self.assertEqual(self.backend.calls, [])
                self.assertEqual(self.store.load(), [])
        self.assertTrue(any(e[0] == "WARNING_UNSUPPORTED_SCHEDULER_NO_TOUCH" for e in self.logs))

    def test_normal_threads_not_changed(self):
        for policy in (0, g.NORMAL):
            self.backend.threads["11"] = snapshot(policy=policy, nice=-11)
            self.assertTrue(self.engine.demote_all())
            self.assertEqual(self.backend.calls, [])

    def test_state_corruption_blocks_without_overwrite(self):
        self.engine.demote_all()
        for value in (b"{broken", b"{}", b"[]", b"\xff", b'{"body":{},"sha256":"bad"}'):
            with self.subTest(value=value):
                self.store.path.write_bytes(value)
                with self.assertRaises(g.GuardError):
                    self.engine.startup_recovery()
                self.assertEqual(self.store.path.read_bytes(), value)

    def test_missing_state_after_anchor_is_not_assumed_empty(self):
        self.engine.demote_all()
        self.store.path.unlink()
        with self.assertRaisesRegex(g.GuardError, "STATE_MISSING"):
            self.engine.startup_recovery()

    def test_state_write_failure_prevents_scheduler_mutation(self):
        with patch.object(self.store, "save", side_effect=OSError("mock disk full")):
            with self.assertRaises(OSError):
                self.engine.demote_all()
        self.assertEqual(self.backend.calls, [])

    def test_atomic_replace_interruption_keeps_old_readable_state(self):
        self.engine.demote_all()
        old = self.store.path.read_bytes()
        with patch.object(g.os, "replace", side_effect=OSError("mock interrupted replacement")):
            with self.assertRaises(OSError):
                self.store.save([])
        self.assertEqual(self.store.path.read_bytes(), old)
        self.assertEqual(len(self.store.load()), 1)

    def test_uninstall_restore_failure_preserves_files_and_state(self):
        self.engine.demote_all()
        self.backend.fail_restore.add(11)
        service, files = FakeService(), FakeFiles()
        self.assertFalse(g.uninstall(self.engine, service, files, contextlib.nullcontext))
        self.assertFalse(files.removed)
        self.assertNotIn("disable", service.events)
        self.assertEqual(len(self.store.load()), 1)

    def test_uninstall_success_order_restores_then_removes(self):
        self.engine.demote_all()
        service, files = FakeService(), FakeFiles()
        self.assertTrue(g.uninstall(self.engine, service, files, contextlib.nullcontext))
        self.assertTrue(files.removed)
        self.assertEqual(self.store.load(), [])
        self.assertEqual(service.events, ["stop", "inactive", "disable", "inactive", "reload", "removed_verify"])

    def test_uninstall_stop_disable_reload_failure_keeps_files(self):
        for failure in ("stop", "disable", "reload", "removed_verify"):
            with self.subTest(failure=failure):
                service, files = FakeService(), FakeFiles()
                service.fail = failure
                self.assertFalse(g.uninstall(self.engine, service, files, contextlib.nullcontext))
                self.assertFalse(files.removed)

    def test_uninstall_active_service_keeps_files(self):
        service, files = FakeService(), FakeFiles()
        service.is_inactive = False
        self.assertFalse(g.uninstall(self.engine, service, files, contextlib.nullcontext))
        self.assertFalse(files.removed)

    def test_disk_removal_transaction_rolls_back_reload_failure(self):
        files = g.Files(self.root)
        for p in files.paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"mock owned install artifact")
        def safe_fixture(path, directory=False, create=False, **kwargs):
            if directory and create:
                path.mkdir(parents=True, exist_ok=True)
            return path
        with patch.object(g, "safe_path", side_effect=safe_fixture):
            events = []
            def failed_removal_reload():
                events.append("first-reload-then-verification-failed")
                raise g.GuardError("reload verify failed")
            with self.assertRaisesRegex(g.GuardError, "reload"):
                files.remove_transaction(failed_removal_reload, lambda: events.append("rollback-reload"))
            self.assertEqual(events, ["first-reload-then-verification-failed", "rollback-reload"])
            self.assertTrue(all(p.read_bytes() == b"mock owned install artifact" for p in files.paths))
            with self.assertRaisesRegex(g.GuardError, "FILES_RESTORED_BUT_ROLLBACK_RELOAD_FAILED"):
                files.remove_transaction(failed_removal_reload, lambda: (_ for _ in ()).throw(g.GuardError("second reload failed")))
            self.assertTrue(all(p.read_bytes() == b"mock owned install artifact" for p in files.paths))
            files.remove_transaction(lambda: None, lambda: None)
        self.assertTrue(all(not p.exists() for p in files.paths))
        self.assertEqual(len(list((self.root / ".local/state" / g.NAME / "uninstall-archive").iterdir())), 4)

    def test_proc_stat_handles_spaces_and_closing_parenthesis(self):
        path = self.root / "stat"
        fields = ["S"] + ["0"] * 18 + ["98765"] + ["0"] * 5
        path.write_text("123 (audio worker) name) " + " ".join(fields))
        self.assertEqual(g.proc_stat(path), ("audio worker) name", 98765))

    def test_manual_ack_requires_different_boot_hash_and_inactive(self):
        self.engine.demote_all()
        digest = g.hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        service = FakeService()
        with self.assertRaisesRegex(g.GuardError, "SAME_BOOT"):
            g.acknowledge_previous_boot(self.store, snapshot()["identity"]["boot_id"], digest, service)
        with self.assertRaisesRegex(g.GuardError, "HASH_MISMATCH"):
            g.acknowledge_previous_boot(self.store, NEW_BOOT, "wrong", service)
        service.is_inactive = False
        with self.assertRaisesRegex(g.GuardError, "INACTIVE"):
            g.acknowledge_previous_boot(self.store, NEW_BOOT, digest, service)
        self.assertEqual(len(self.store.load()), 1)

    def test_manual_ack_rejects_invalid_current_boot(self):
        self.engine.demote_all()
        with self.assertRaisesRegex(g.GuardError, "INVALID_CURRENT_BOOT"):
            g.acknowledge_previous_boot(self.store, "", "", FakeService())

    def test_private_leaf_permissions_symlink_hardlink_and_parent_checks(self):
        leaf = self.root / "state.json"
        meta = {"mode": g.stat.S_IFREG | 0o600, "uid": 1000, "links": 1, "parent_mode": 0o755}
        def synthetic_stat(path):
            return SimpleNamespace(st_mode=meta["mode"] if path == leaf else g.stat.S_IFDIR | meta["parent_mode"],
                                   st_uid=meta["uid"], st_nlink=meta["links"] if path == leaf else 1)
        with patch.object(Path, "home", return_value=self.root), patch.object(Path, "lstat", synthetic_stat), \
                patch.object(g.os, "getuid", return_value=1000, create=True):
            self.assertEqual(g.safe_path(leaf, private=True), leaf)
            for mode, links in ((g.stat.S_IFREG | 0o644, 1), (g.stat.S_IFLNK | 0o600, 1), (g.stat.S_IFREG | 0o600, 2)):
                meta.update(mode=mode, links=links)
                with self.assertRaises(g.GuardError):
                    g.safe_path(leaf, private=True)
            meta.update(mode=g.stat.S_IFREG | 0o600, links=1, parent_mode=0o777)
            with self.assertRaises(g.GuardError):
                g.safe_path(leaf, private=True)
            meta.update(parent_mode=0o755, uid=1001)
            with self.assertRaises(g.GuardError):
                g.safe_path(leaf, private=True)

    def test_unit_blocks_guard_exit4_restart_storm(self):
        unit = (Path(__file__).parent / "frame-stream-audio-fix.service").read_text()
        self.assertIn("RestartPreventExitStatus=4", unit)
        self.assertIn("ExecStopPost=-%h/.local/bin/frame-stream-audio-fix recover", unit)
        self.assertIn("StartLimitBurst=3", unit)

    def test_manual_ack_archives_old_state_not_restore_claim(self):
        self.engine.demote_all()
        data = self.store.path.read_bytes()
        digest = g.hashlib.sha256(data).hexdigest()
        with patch.object(g, "emit") as log:
            g.acknowledge_previous_boot(self.store, NEW_BOOT, digest, FakeService())
        self.assertEqual(self.store.load(), [])
        self.assertEqual((self.root / ("operator-ack-previous-boot-" + digest + ".json")).read_bytes(), data)
        self.assertEqual(log.call_args.args[0], "OPERATOR_ACK_PREVIOUS_BOOT_NOT_RESTORED")

    def test_manual_ack_archive_write_failure_keeps_state(self):
        self.engine.demote_all()
        data = self.store.path.read_bytes()
        digest = g.hashlib.sha256(data).hexdigest()
        with patch.object(g, "atomic_bytes", side_effect=OSError("mock archive full")):
            with self.assertRaises(OSError):
                g.acknowledge_previous_boot(self.store, NEW_BOOT, digest, FakeService())
        self.assertEqual(self.store.path.read_bytes(), data)

    def test_upstream_memory_loss_reproduction_model(self):
        source = (Path(__file__).parent.parent / "frame-stream-audio-fix.sh").read_text()
        self.assertIn("declare -A demoted", source)
        self.assertIn('$2=="RR"||$2=="FF"', source)
        self.assertIn("Restart=always", source)
        scheduler = {11: "RR"}
        in_memory = {11: 80}
        scheduler[11] = "OTHER"
        in_memory = {}  # process died; new process has a fresh associative array
        rediscovered = {tid: 80 for tid, policy in scheduler.items() if policy in ("RR", "FF")}
        self.assertEqual(rediscovered, {})
        self.assertEqual(in_memory, {})
        self.assertEqual(scheduler[11], "OTHER")


def crash_child(root):
    root = Path(root)
    backend = FakeBackend(world=root / "world.json")
    engine = g.Engine(g.StateStore(root, False), backend, lambda *a, **k: None)
    assert engine.demote_all()
    (root / "demoted.marker").write_text("mock demotion complete; waiting for untrappable termination")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
        crash_child(sys.argv[2])
    else:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GuardTests))
        summary = {"verified_at": datetime.datetime.now().astimezone().isoformat(),
            "platform": sys.platform, "python": sys.version, "tests": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "success": result.wasSuccessful(), "scope": "LOCAL MOCKS ONLY; Frame unchanged",
            "failure_details": [(str(test), trace) for test, trace in result.failures + result.errors]}
        (Path(__file__).parent / "test-results.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        sys.exit(not result.wasSuccessful())
