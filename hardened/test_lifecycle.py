"""Synthetic lifecycle fault injection. No real scheduler, RPC, service or SSH."""
import copy
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import frame_audio_guard as g
from test_guard import FakeBackend, snapshot


class LifecycleBackend(FakeBackend):
    def lifetime_proof(self, ident):
        now = self.threads.get(str(ident["tid"]))
        if now is None:
            observation = {"exists": False}
        else:
            current = now["identity"]
            if current["boot_id"] != ident["boot_id"]:
                raise g.GuardError("DIFFERENT_BOOT_MANUAL_REVIEW")
            if (current["pid_start"], current["tid_start"]) == (ident["pid_start"], ident["tid_start"]):
                return None
            observation = {"exists": True, "pid_start": current["pid_start"], "tid_start": current["tid_start"]}
        proof = {"boot_id": ident["boot_id"], "observations": [observation, copy.deepcopy(observation)]}
        g.retirement_reason({"phase": "TRACKED", "identity": ident}, proof)
        return proof


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = g.StateStore(self.root, False)
        self.backend = LifecycleBackend()
        self.logs = []
        self.engine = g.Engine(self.store, self.backend, lambda event, **kw: self.logs.append((event, kw)))

    def tearDown(self):
        self.temp.cleanup()

    def demote_exit(self):
        self.assertTrue(self.engine.demote_all())
        rec = self.store.load()[0]
        del self.backend.threads["11"]
        self.backend.calls.clear()
        return rec

    def archives(self):
        return list((self.root / "retired").glob("*.json"))

    def test_tracked_disappearance_archive_before_remove(self):
        rec = self.demote_exit()
        previous = self.store.path.read_bytes()
        self.assertTrue(self.engine.startup_recovery())
        self.assertEqual(self.store.load(), [])
        self.assertEqual(self.backend.calls, [])
        archive = self.store.retirement(rec)
        self.assertEqual(archive["record"], rec)
        self.assertEqual(archive["previous_state_sha256"], hashlib.sha256(previous).hexdigest())
        self.assertEqual(archive["previous_body_sha256"], json.loads(previous)["sha256"])
        self.assertEqual(archive["reason"], "RETIRED_EXITED_TRACKED_THREAD")

    def test_tid_reused_no_old_restore_new_identity_not_touched(self):
        self.engine.demote_all()
        self.backend.threads["11"]["identity"]["tid_start"] += 50
        new = copy.deepcopy(self.backend.threads["11"])
        self.backend.calls.clear()
        self.assertTrue(self.engine.restore_all())
        self.assertEqual(self.backend.threads["11"], new)
        self.assertEqual(self.backend.calls, [])
        self.assertIn("RETIRED_TID_REUSED_TRACKED_THREAD", [e[0] for e in self.logs])

    def test_pid_reused_no_old_restore(self):
        self.engine.demote_all()
        self.backend.threads["11"]["identity"]["pid_start"] += 50
        self.backend.calls.clear()
        self.assertTrue(self.engine.restore_all())
        self.assertEqual(self.backend.calls, [])
        self.assertIn("RETIRED_PID_REUSED_TRACKED_THREAD", [e[0] for e in self.logs])

    def test_same_identity_unexpected_scheduler_block(self):
        self.engine.demote_all()
        self.backend.threads["11"]["original"]["rtprio"] = 99
        self.backend.calls.clear()
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.archives(), [])

    def test_third_party_partial_looking_states_block_no_touch(self):
        self.engine.demote_all()
        for current in ({"policy": g.NORMAL, "rtprio": 0, "nice": 0},
                        {"policy": g.RR, "rtprio": 80, "nice": -11}):
            with self.subTest(current=current):
                self.backend.threads["11"]["original"] = current.copy()
                self.backend.calls.clear()
                self.assertFalse(self.engine.restore_all())
                self.assertEqual(self.backend.calls, [])
                self.assertEqual(len(self.store.load()), 1)
                self.assertEqual(self.archives(), [])

    def test_restore_order_rr_then_nice_with_inflight_until_verify(self):
        self.engine.demote_all()
        self.backend.calls.clear()
        real = self.backend.set_nice
        phases = []
        def set_nice(rec, expected, value):
            phases.append(self.store.load()[0]["phase"])
            real(rec, expected, value)
        with patch.object(self.backend, "set_nice", side_effect=set_nice):
            self.assertTrue(self.engine.restore_all())
        self.assertEqual(self.backend.calls, [("restore", 11), ("nice", 11, 0)])
        self.assertEqual(phases, ["RESTORE_IN_FLIGHT"])

    def test_retirement_first_record_preserves_next_rpc_write_ahead(self):
        self.backend.threads["12"] = snapshot(12)
        self.engine.demote_all()
        del self.backend.threads["11"]
        real = self.backend.restore_rr
        phases = []
        def restore(rec, expected):
            phases.append(self.store.load()[0]["phase"])
            real(rec, expected)
            raise g.RpcUncertain("lost restore reply after earlier retirement")
        with patch.object(self.backend, "restore_rr", side_effect=restore):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(phases, ["RESTORE_IN_FLIGHT"])
        self.assertEqual(self.store.load()[0]["phase"], "RESTORE_IN_FLIGHT")
        self.assertEqual(self.store.load()[0]["identity"]["tid"], 12)
        self.assertEqual(len(self.archives()), 1)

    def test_nice_restore_failure_stays_inflight_no_false_retry(self):
        self.engine.demote_all()
        with patch.object(self.backend, "set_nice", side_effect=PermissionError("failed nice")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load()[0]["phase"], "RESTORE_IN_FLIGHT")
        before = list(self.backend.calls)
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.backend.calls, before)

    def test_same_ticks_name_exe_uid_changes_block_not_reuse(self):
        self.engine.demote_all()
        original = copy.deepcopy(self.backend.threads["11"])
        for field, value in (("thread_name", "renamed"), ("exe_ino", 777), ("uid", 2000)):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["identity"][field] = value
                self.backend.threads["11"] = changed
                self.backend.calls.clear()
                self.assertFalse(self.engine.restore_all())
                self.assertEqual(self.backend.calls, [])
                self.assertEqual(self.archives(), [])

    def test_demote_inflight_disappears_block(self):
        rec = self.demote_exit()
        rec["phase"] = "DEMOTE_IN_FLIGHT"
        self.store.save([rec])
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load(), [rec])
        self.assertEqual(self.archives(), [])

    def test_restore_inflight_disappears_block(self):
        rec = self.demote_exit()
        rec["phase"] = "RESTORE_IN_FLIGHT"
        self.store.save([rec])
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load(), [rec])
        self.assertEqual(self.archives(), [])

    def test_inflight_reused_also_block(self):
        self.engine.demote_all()
        rec = self.store.load()[0]
        self.backend.threads["11"]["identity"]["tid_start"] += 1
        self.backend.calls.clear()
        for phase in ("DEMOTE_IN_FLIGHT", "RESTORE_IN_FLIGHT"):
            rec["phase"] = phase
            self.store.save([rec])
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.archives(), [])

    def test_write_ahead_disappears_pre_dispatch_retire(self):
        rec = snapshot()
        rec["phase"] = "WRITE_AHEAD"
        self.store.save([rec])
        self.backend.threads.clear()
        self.assertTrue(self.engine.restore_all())
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.store.retirement(rec)["reason"], "RETIRED_EXITED_WRITE_AHEAD_THREAD")

    def test_write_ahead_live_changed_scheduler_block(self):
        rec = snapshot()
        rec["phase"] = "WRITE_AHEAD"
        self.store.save([rec])
        self.backend.threads["11"]["original"] = {"policy": g.NORMAL, "rtprio": 0, "nice": -11}
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.backend.calls, [])

    def test_rpc_phase_durable_before_dispatch_and_after_verify(self):
        phases = []
        real = self.backend.demote
        def demote(rec, expected):
            phases.append(self.store.load()[0]["phase"])
            real(rec, expected)
        real_verify = self.engine.verify
        def verify(rec, expected):
            phases.append(self.store.load()[0]["phase"])
            real_verify(rec, expected)
        with patch.object(self.backend, "demote", side_effect=demote), patch.object(self.engine, "verify", side_effect=verify):
            self.assertTrue(self.engine.demote_all())
        self.assertEqual(phases, ["DEMOTE_IN_FLIGHT", "DEMOTE_IN_FLIGHT"])
        self.assertEqual(self.store.load()[0]["phase"], "TRACKED")

    def test_rpc_completed_but_postverify_disappears_retains_inflight(self):
        real = self.backend.demote
        def demote(rec, expected):
            real(rec, expected)
            del self.backend.threads["11"]
        with patch.object(self.backend, "demote", side_effect=demote):
            self.assertFalse(self.engine.demote_all())
        self.assertEqual(self.store.load()[0]["phase"], "DEMOTE_IN_FLIGHT")
        self.assertEqual(self.archives(), [])

    def test_archive_write_failure_retains_active_bytes(self):
        self.demote_exit()
        previous = self.store.path.read_bytes()
        with patch.object(g, "atomic_bytes", side_effect=OSError("archive full")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertEqual(self.archives(), [])

    def test_archive_dir_fsync_failure_retains_active_bytes(self):
        self.demote_exit()
        previous = self.store.path.read_bytes()
        with patch.object(g, "sync_dir", side_effect=OSError("directory fsync failed")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_crash_archive_to_removal_idempotent_retry(self):
        rec = self.demote_exit()
        with patch.object(self.store, "save", side_effect=KeyboardInterrupt("untrappable crash model")):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.restore_all()
        self.assertEqual(self.store.load(), [rec])
        archive = self.archives()[0].read_bytes()
        fresh = g.Engine(g.StateStore(self.root, False), self.backend, lambda *a, **k: None)
        # Already committed proof remains valid, even if a later probe fails.
        with patch.object(self.backend, "lifetime_proof", side_effect=PermissionError("later unreadable")):
            self.assertTrue(fresh.startup_recovery())
        for _ in range(3):
            self.assertTrue(fresh.startup_recovery())
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.archives()[0].read_bytes(), archive)
        self.assertEqual(self.backend.calls, [])

    def test_archive_replace_failed_keeps_state_and_no_partial_archive(self):
        self.demote_exit()
        previous = self.store.path.read_bytes()
        with patch.object(g.os, "replace", side_effect=OSError("replace failed")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertEqual(self.archives(), [])
        self.assertTrue(self.engine.restore_all())

    def test_corrupt_existing_archive_blocks_no_rewrite(self):
        rec = self.demote_exit()
        path = self.store.retirement_path(rec)
        path.write_bytes(b"broken archive")
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load(), [rec])
        self.assertEqual(path.read_bytes(), b"broken archive")

    def test_permission_io_generic_errors_never_retired(self):
        self.engine.demote_all()
        for error in (PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "IO"), OSError("generic"),
                      g.GuardError("PROC_STAT_READ_INVALID")):
            with self.subTest(error=repr(error)), patch.object(self.backend, "lifetime_proof", side_effect=error):
                self.assertFalse(self.engine.restore_all())
                self.assertEqual(len(self.store.load()), 1)
                self.assertEqual(self.archives(), [])

    def test_lower_ticks_and_other_boot_block(self):
        self.engine.demote_all()
        self.backend.threads["11"]["identity"]["tid_start"] -= 1
        self.assertFalse(self.engine.restore_all())
        self.backend.threads["11"]["identity"]["boot_id"] = "11111111-2222-3333-4444-555555555555"
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.archives(), [])

    def test_dynamic_rr60_appears_demotes_exits_service_continues(self):
        self.engine.demote_all()
        transient = snapshot(12)
        transient["original"]["rtprio"] = 60
        transient["identity"]["thread_name"] = "AudioFilterModu"
        self.backend.threads["12"] = transient
        self.assertTrue(self.engine.demote_all(continuing=True))
        self.assertIn(("demote", 12), self.backend.calls)
        del self.backend.threads["12"]
        self.assertTrue(self.engine.demote_all(continuing=True))
        self.assertEqual([r["identity"]["tid"] for r in self.store.load()], [11])
        self.assertEqual(len(self.archives()), 1)

    def test_stable3_transient1_exit_stream_end_restores_three(self):
        for tid in (12, 13):
            self.backend.threads[str(tid)] = snapshot(tid)
        originals = copy.deepcopy(self.backend.threads)
        class Steps:
            index = 0
            def is_set(this): return this.index >= 4
            def wait(this, _):
                this.index += 1
                if this.index == 1:
                    transient = snapshot(14)
                    transient["original"]["rtprio"] = 60
                    transient["identity"]["thread_name"] = "AudioFilterModu"
                    self.backend.threads["14"] = transient
                elif this.index == 2:
                    del self.backend.threads["14"]
                elif this.index == 3:
                    self.backend.active = False
        self.assertTrue(g.run(self.engine, Steps()))
        self.assertEqual(self.store.load(), [])
        self.assertEqual(self.backend.threads, originals)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual({c[1] for c in self.backend.calls if c[0] == "restore"}, {11, 12, 13})

    def test_sanitized_v1_transient_record_compatibility(self):
        rec = snapshot(12)
        rec["original"]["rtprio"] = 60
        rec["identity"]["thread_name"] = "AudioFilterModu"
        rec["phase"] = "TRACKED"
        self.store.save([rec])
        self.backend.threads.clear()
        self.assertTrue(self.engine.startup_recovery())
        self.assertEqual(self.store.retirement(rec)["record"], rec)
        self.assertEqual(self.store.load(), [])
        self.assertEqual(self.backend.calls, [])

    def test_linux_lifetime_probe_absence_and_reuse(self):
        backend = g.LinuxBackend()
        ident = snapshot()["identity"]
        for observation in ({"exists": False}, {"exists": True, "pid_start": 100, "tid_start": 999}):
            with patch.object(Path, "read_text", return_value=ident["boot_id"]), \
                    patch.object(backend, "task_lifetime", return_value=observation):
                proof = backend.lifetime_proof(ident)
            self.assertTrue(g.retirement_reason({"phase": "TRACKED", "identity": ident}, proof).startswith("RETIRED_"))

    def test_linux_probe_unstable_read_or_boot_blocks(self):
        backend = g.LinuxBackend()
        ident = snapshot()["identity"]
        with patch.object(Path, "read_text", return_value=ident["boot_id"]), \
                patch.object(backend, "task_lifetime", side_effect=[{"exists": False}, {"exists": True}]):
            with self.assertRaisesRegex(g.GuardError, "UNSTABLE"):
                backend.lifetime_proof(ident)

    def test_missing_proc_stat_but_present_task_not_exit(self):
        with patch.object(g, "proc_stat", side_effect=FileNotFoundError()), patch.object(Path, "stat"):
            with self.assertRaisesRegex(g.GuardError, "TASK_PRESENT"):
                g.LinuxBackend().task_lifetime(10, 11)

    def test_missing_task_path_is_explicit_exit_evidence(self):
        with patch.object(g, "proc_stat", side_effect=FileNotFoundError()), \
                patch.object(Path, "stat", side_effect=FileNotFoundError()):
            self.assertEqual(g.LinuxBackend().task_lifetime(10, 11), {"exists": False})

    def test_missing_task_but_direct_tid_exists_blocks_dead_leader_case(self):
        def probe(path):
            if path == Path("/proc/10/task/11"):
                raise FileNotFoundError()
            return object()
        with patch.object(g, "proc_stat", side_effect=FileNotFoundError()), patch.object(Path, "stat", probe):
            with self.assertRaisesRegex(g.GuardError, "DIRECT_TID_PRESENT"):
                g.LinuxBackend().task_lifetime(10, 11)

    def test_task_path_permission_error_not_exit(self):
        with patch.object(g, "proc_stat", side_effect=FileNotFoundError()), \
                patch.object(Path, "stat", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                g.LinuxBackend().task_lifetime(10, 11)

    def test_task_stat_permission_io_esrch_not_inferred_exit(self):
        for error in (PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "IO"),
                      ProcessLookupError(errno.ESRCH, "no such process")):
            with self.subTest(error=repr(error)), patch.object(g, "proc_stat", side_effect=error):
                with self.assertRaises(type(error)):
                    g.LinuxBackend().task_lifetime(10, 11)

    def test_exit_between_probe_and_snapshot_reprobed(self):
        self.engine.demote_all()
        actual_snapshot = self.backend.snapshot
        def exits(ident):
            self.backend.threads.clear()
            return actual_snapshot(ident)
        with patch.object(self.backend, "snapshot", side_effect=exits):
            self.assertTrue(self.engine.restore_all())
        self.assertEqual(len(self.archives()), 1)

    def test_snapshot_permission_not_reprobed_or_retired(self):
        self.engine.demote_all()
        with patch.object(self.backend, "snapshot", side_effect=PermissionError("permission")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.archives(), [])

    def test_candidate_disappears_before_snapshot_safe_skip(self):
        backend = g.LinuxBackend()
        with patch.object(backend, "command", return_value="10"), \
                patch.object(Path, "iterdir", return_value=[Path("11")]), \
                patch.object(backend, "snapshot", side_effect=ProcessLookupError()), \
                patch.object(backend, "task_lifetime", return_value={"exists": False}):
            self.assertEqual(backend.candidates(), [])

    def test_candidate_generic_error_or_reuse_not_skipped(self):
        backend = g.LinuxBackend()
        with patch.object(backend, "command", return_value="10"), patch.object(Path, "iterdir", return_value=[Path("11")]):
            with patch.object(backend, "snapshot", side_effect=PermissionError()):
                with self.assertRaises(PermissionError):
                    backend.candidates()
            with patch.object(backend, "snapshot", side_effect=ProcessLookupError()), \
                    patch.object(backend, "task_lifetime", return_value={"exists": True, "pid_start": 100, "tid_start": 500}):
                with self.assertRaises(ProcessLookupError):
                    backend.candidates()

    def test_retry_seals_archive_fsync_before_active_removal(self):
        rec = self.demote_exit()
        with patch.object(self.store, "save", side_effect=OSError("state write failed")):
            self.assertFalse(self.engine.restore_all())
        archive = self.archives()[0].read_bytes()
        with patch.object(g.os, "fsync", side_effect=OSError("archive reseal failed")):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load(), [rec])
        self.assertEqual(self.archives()[0].read_bytes(), archive)
        self.assertTrue(self.engine.restore_all())
        self.assertEqual(self.archives()[0].read_bytes(), archive)

    def test_conflicting_archive_full_record_never_reused(self):
        rec = self.demote_exit()
        with patch.object(self.store, "save", side_effect=OSError("state write failed")):
            self.engine.restore_all()
        path = self.archives()[0]
        obj = json.loads(path.read_bytes())
        obj["body"]["record"]["original"]["rtprio"] = 79
        obj["sha256"] = hashlib.sha256(g.canonical(obj["body"])).hexdigest()
        bad = g.canonical(obj)
        path.write_bytes(bad)
        self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load(), [rec])
        self.assertEqual(path.read_bytes(), bad)

    def test_archive_and_state_corruption_never_auto_cleared(self):
        self.demote_exit()
        with patch.object(self.store, "save", side_effect=OSError("state write failed")):
            self.engine.restore_all()
        self.store.path.write_bytes(b"broken active state")
        with self.assertRaises(g.GuardError):
            self.engine.restore_all()
        self.assertEqual(self.store.path.read_bytes(), b"broken active state")

    def test_write_ahead_is_pre_dispatch_after_crash(self):
        save = self.store.save
        def fail_flight(records):
            if records[0]["phase"] == "DEMOTE_IN_FLIGHT":
                raise KeyboardInterrupt("crash before flight is durable")
            save(records)
        with patch.object(self.store, "save", side_effect=fail_flight):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.demote_all()
        self.assertEqual(self.store.load()[0]["phase"], "WRITE_AHEAD")
        self.assertEqual(self.backend.calls, [])
        self.backend.threads.clear()
        self.assertTrue(self.engine.restore_all())

    def test_restore_reply_then_exit_before_verify_stays_inflight(self):
        self.engine.demote_all()
        real = self.backend.restore_rr
        def exits(rec, expected):
            real(rec, expected)
            self.backend.threads.clear()
        with patch.object(self.backend, "restore_rr", side_effect=exits):
            self.assertFalse(self.engine.restore_all())
        self.assertEqual(self.store.load()[0]["phase"], "RESTORE_IN_FLIGHT")
        self.assertEqual(self.archives(), [])

    def test_durable_archive_private_posix_modes(self):
        self.demote_exit()
        self.assertTrue(self.engine.restore_all())
        if sys.platform != "win32":
            self.assertEqual((self.root / "retired").stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.archives()[0].stat().st_mode & 0o777, 0o600)

    def test_malformed_proc_stat_block(self):
        with patch.object(Path, "read_text", return_value="bad stat"):
            with self.assertRaisesRegex(g.GuardError, "PROC_STAT_READ_INVALID"):
                g.proc_stat(Path("ignored"))

    def test_real_child_killed_after_archive_before_removal(self):
        child = subprocess.Popen([sys.executable, "-B", __file__, "--archive-child", str(self.root)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 8
            while not (self.root / "archived.marker").exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.03)
            self.assertTrue((self.root / "archived.marker").exists())
            child.kill()
            child.wait(timeout=5)
            rec = self.store.load()[0]
            self.backend.threads.clear()
            self.assertTrue(self.engine.startup_recovery())
            self.assertEqual(self.store.retirement(rec)["reason"], "RETIRED_EXITED_TRACKED_THREAD")
            self.assertEqual(self.backend.calls, [])
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def archive_child(root):
    root = Path(root)
    store = g.StateStore(root, False)
    backend = LifecycleBackend()
    engine = g.Engine(store, backend, lambda *a, **k: None)
    assert engine.demote_all()
    backend.threads.clear()
    def crash_window(_):
        (root / "archived.marker").write_text("durable archive before removal")
        while True:
            time.sleep(1)
    store.save = crash_window
    engine.restore_all()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--archive-child":
        archive_child(sys.argv[2])
    else:
        unittest.main()
