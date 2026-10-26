"""Behavioral tests for task file retention. Every mutation uses a temporary fixture."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "task_files.py"
SPEC = importlib.util.spec_from_file_location("task_files_under_test", SCRIPT)
CORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CORE)
SEVEN_DAYS = 7 * 24 * 60 * 60


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="task-files-test-")
        self.addCleanup(self.fixture.cleanup)
        self.base = Path(self.fixture.name)
        self.root = self.base / "enrolled"
        self.workspace = self.root / "project"
        self.workspace.mkdir(parents=True)
        self.state = self.base / "manager-state"
        self.call("init", "--root", self.root, "--mode", "auto")

    def command(self, *args):
        return [sys.executable, str(SCRIPT), "--state-dir", str(self.state),
                *(str(arg) for arg in args)]

    def call(self, *args, ok=True, timeout=15):
        completed = subprocess.run(self.command(*args), capture_output=True,
                                   text=True, encoding="utf-8", timeout=timeout)
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI did not return JSON: {completed.stdout!r}; "
                      f"stderr={completed.stderr!r}; error={exc}")
        self.assertIsInstance(data, dict)
        if ok:
            self.assertEqual(completed.returncode, 0, data)
            self.assertTrue(data.get("ok"), data)
        else:
            self.assertNotEqual(completed.returncode, 0, data)
            self.assertFalse(data.get("ok"), data)
        return data

    def entity(self, result, kind):
        entity = result.get(kind, result)
        self.assertIn("id", entity, result)
        return entity

    def start(self):
        return self.entity(self.call("start", "--workspace", self.workspace,
                                     "--label", "Test task"), "task")

    def reserve(self, task, kind="scratch", name="preview"):
        resource = self.entity(self.call("reserve", "--task", task["id"],
                                        "--name", name, "--kind", kind), "resource")
        self.assertTrue(Path(resource["path"]).is_dir())
        return resource

    def create_payload(self, resource, content="temporary preview"):
        path = Path(resource["path"]) / "payload.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def publish(self, task):
        delivery = self.workspace / "outputs" / f"{task['id']}.txt"
        delivery.parent.mkdir(exist_ok=True)
        delivery.write_text("User's final document", encoding="utf-8")
        self.call("publish", "--task", task["id"], "--path", delivery)
        return delivery

    def retired(self, content="temporary preview"):
        task = self.start()
        resource = self.reserve(task)
        payload = self.create_payload(resource, content)
        self.call("seal", "--resource", resource["id"])
        delivery = self.publish(task)
        self.call("finish", "--task", task["id"])
        self.call("retire", "--resource", resource["id"], "--reason", "Preview verified")
        return task, resource, payload, delivery

    def read_state(self):
        return json.loads((self.state / "state.json").read_text(encoding="utf-8"))

    def write_state(self, state):
        # Only the isolated fixture is modified, never the installed manager.
        (self.state / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def expire(self, resource):
        state = self.read_state()
        record = state["resources"][resource["id"]]
        record["retired_at"] = time.time() - SEVEN_DAYS - 3600
        record["delete_after"] = record["retired_at"] + SEVEN_DAYS
        self.write_state(state)

    def sweep_at(self, epoch):
        instant = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
        with mock.patch.object(CORE, "utc_now", return_value=instant):
            with CORE.Manager(self.state).transaction() as manager:
                return manager.sweep()

    def assert_preserved(self, resource, payload):
        self.assertTrue(Path(resource["path"]).is_dir())
        self.assertTrue(payload.is_file())

    def test_creation_does_not_adopt_unrelated_existing_files(self):
        original = self.workspace / "original.txt"
        original.write_text("Keep this input", encoding="utf-8")
        unrelated = self.workspace / "work" / "legacy"
        unrelated.mkdir(parents=True)
        legacy = unrelated / "old.tmp"
        legacy.write_text("Not registered", encoding="utf-8")
        self.retired()
        self.call("sweep")
        self.assertEqual(original.read_text(encoding="utf-8"), "Keep this input")
        self.assertEqual(legacy.read_text(encoding="utf-8"), "Not registered")

    def test_reserve_refuses_path_traversal_name(self):
        task = self.start()
        self.call("reserve", "--task", task["id"], "--name", "../../outside",
                  "--kind", "scratch", ok=False)
        self.assertFalse((self.base / "outside").exists())

    def test_working_material_cannot_be_retired_as_scratch(self):
        task = self.start()
        resource = self.reserve(task, kind="working")
        payload = self.create_payload(resource, "Unfinished editable document")
        self.call("seal", "--resource", resource["id"])
        self.publish(task)
        self.call("finish", "--task", task["id"])
        self.call("retire", "--resource", resource["id"], "--reason", "Attempt", ok=False)
        self.assertEqual(payload.read_text(encoding="utf-8"), "Unfinished editable document")

    def test_retirement_requires_actual_delivery(self):
        task = self.start()
        resource = self.reserve(task)
        payload = self.create_payload(resource)
        self.call("seal", "--resource", resource["id"])
        self.call("finish", "--task", task["id"])
        self.call("retire", "--resource", resource["id"], "--reason", "Attempt", ok=False)
        self.assertTrue(payload.exists())

    def test_new_retirement_is_not_deleted_immediately(self):
        _, resource, payload, delivery = self.retired()
        self.call("sweep")
        self.assertTrue(Path(resource["path"]).exists())
        self.assertTrue(payload.exists())
        self.assertEqual(delivery.read_text(encoding="utf-8"), "User's final document")

    def test_publish_refuses_nonexistent_delivery(self):
        task = self.start()
        self.call("publish", "--task", task["id"], "--path",
                  self.workspace / "does-not-exist.txt", ok=False)

    def test_publish_can_protect_external_delivery_without_writing_it(self):
        task = self.start()
        outside = self.base / "other-user-file.txt"
        outside.write_text("Other data", encoding="utf-8")
        before = outside.stat()
        self.call("publish", "--task", task["id"], "--path", outside)
        self.assertEqual(outside.read_text(encoding="utf-8"), "Other data")
        self.assertEqual(outside.stat().st_mtime_ns, before.st_mtime_ns)

    def test_unregistered_workspace_refused(self):
        outside = self.base / "not-enrolled"
        outside.mkdir()
        self.call("start", "--workspace", outside, "--label", "Outside", ok=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_seal_refuses_link_to_external_directory(self):
        task = self.start()
        resource = self.reserve(task)
        outside = self.base / "external-target"
        outside.mkdir()
        sentinel = outside / "important.txt"
        sentinel.write_text("Do not follow links", encoding="utf-8")
        link = Path(resource["path"]) / "external-link"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
            self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.call("seal", "--resource", resource["id"], ok=False)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "Do not follow links")

    def test_linked_git_worktree_is_not_automatically_managed(self):
        (self.workspace / ".git").write_text("gitdir: /some/shared/git/worktrees/test\n",
                                            encoding="utf-8")
        self.call("start", "--workspace", self.workspace, "--label", "Worktree", ok=False)

    def test_changed_content_before_retirement_is_preserved(self):
        task = self.start()
        resource = self.reserve(task)
        payload = self.create_payload(resource, "Original")
        self.call("seal", "--resource", resource["id"])
        original_stat = payload.stat()
        payload.write_text("Modified", encoding="utf-8")
        os.utime(payload, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.publish(task)
        self.call("finish", "--task", task["id"])
        self.call("retire", "--resource", resource["id"], "--reason", "Attempt", ok=False)
        self.assertEqual(payload.read_text(encoding="utf-8"), "Modified")

    @unittest.skipUnless(os.name == "nt", "Production purge is Windows-only")
    def test_full_seven_days_then_only_registered_scratch_is_removed(self):
        source = self.workspace / "source.txt"
        source.write_text("User input", encoding="utf-8")
        _, resource, payload, delivery = self.retired()
        unregistered = Path(resource["path"]).parent / "unregistered-old-preview"
        unregistered.mkdir()
        unknown = unregistered / "keep.txt"
        unknown.write_text("Unknown ownership", encoding="utf-8")
        record = self.read_state()["resources"][resource["id"]]
        self.assertGreaterEqual(record["delete_after"] - record["retired_at"], SEVEN_DAYS)
        early = self.sweep_at(record["retired_at"] + SEVEN_DAYS - 0.01)
        self.assertEqual(early["purged"], [])
        self.assert_preserved(resource, payload)
        final = self.sweep_at(record["retired_at"] + SEVEN_DAYS + 0.01)
        self.assertIn(resource["id"], final["purged"], final)
        self.assertFalse(Path(resource["path"]).exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "User input")
        self.assertEqual(unknown.read_text(encoding="utf-8"), "Unknown ownership")
        self.assertEqual(delivery.read_text(encoding="utf-8"), "User's final document")
        self.assertEqual(self.call("sweep")["purged"], [])

    def test_modified_content_with_original_size_and_mtime_survives_expiry(self):
        _, resource, payload, _ = self.retired("Original")
        self.expire(resource)
        original = payload.stat()
        payload.write_text("Modified", encoding="utf-8")
        os.utime(payload, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.call("sweep")
        self.assert_preserved(resource, payload)
        self.assertEqual(payload.read_text(encoding="utf-8"), "Modified")

    def test_new_unrecorded_file_in_retired_directory_blocks_deletion(self):
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        added = Path(resource["path"]) / "new-user-work.txt"
        added.write_text("Keep new work", encoding="utf-8")
        self.call("sweep")
        self.assert_preserved(resource, payload)
        self.assertEqual(added.read_text(encoding="utf-8"), "Keep new work")

    def test_dry_run_preserves_exact_state_and_content(self):
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        before = (self.state / "state.json").read_bytes()
        report = self.call("sweep", "--dry-run")
        self.assertEqual(report["purged"], [])
        self.assertTrue(report["eligible"], report)
        self.assertEqual((self.state / "state.json").read_bytes(), before)
        self.assert_preserved(resource, payload)

    def test_restore_and_keep_prevent_later_purge(self):
        for operation in ("restore", "keep"):
            with self.subTest(operation=operation):
                _, resource, payload, _ = self.retired()
                self.expire(resource)
                self.call(operation, "--resource", resource["id"])
                self.call("sweep")
                self.assert_preserved(resource, payload)

    def test_resume_cancels_retirement_for_all_task_resources(self):
        task, resource, payload, _ = self.retired()
        self.expire(resource)
        self.call("resume", "--task", task["id"])
        self.call("sweep")
        self.assert_preserved(resource, payload)
        record = self.read_state()["resources"][resource["id"]]
        self.assertIsNone(record["delete_after"])

    def test_audit_and_recycle_modes_do_not_permanently_delete(self):
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        for mode in ("audit", "recycle"):
            with self.subTest(mode=mode):
                self.call("set-mode", "--mode", mode)
                self.call("sweep")
                self.assert_preserved(resource, payload)

    def test_disabled_enrollment_blocks_purge(self):
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        self.call("disable", "--root", self.root)
        self.call("sweep")
        self.assert_preserved(resource, payload)

    def test_delivery_changed_after_retirement_blocks_purge(self):
        _, resource, payload, delivery = self.retired()
        self.expire(resource)
        delivery.write_text("Unverified changed delivery", encoding="utf-8")
        self.call("sweep")
        self.assert_preserved(resource, payload)
        self.assertEqual(delivery.read_text(encoding="utf-8"), "Unverified changed delivery")

    def test_required_source_dependency_cannot_be_retired(self):
        task = self.start()
        resource = self.reserve(task)
        payload = self.create_payload(resource)
        self.call("seal", "--resource", resource["id"])
        delivery = self.publish(task)
        self.call("publish", "--task", task["id"], "--path", delivery,
                  "--requires", resource["id"])
        self.call("finish", "--task", task["id"])
        self.call("retire", "--resource", resource["id"], "--reason", "Attempt", ok=False)
        self.assert_preserved(resource, payload)

    @unittest.skipUnless(os.name == "nt", "Production purge is Windows-only")
    def test_independent_task_can_purge_while_other_task_is_leased(self):
        task, resource, payload, _ = self.retired()
        _, other, other_payload, _ = self.retired()
        self.expire(resource)
        self.expire(other)
        with CORE.Manager(self.state).lease(task["id"]):
            report = self.call("sweep")
        self.assert_preserved(resource, payload)
        self.assertFalse(other_payload.exists())
        self.assertIn(other["id"], report["purged"], report)

    def test_producer_lease_prevents_finishing_while_process_is_running(self):
        task = self.start()
        ready, release = self.base / "ready", self.base / "release"
        child = ("import pathlib,time,sys; "
                 "pathlib.Path(sys.argv[1]).write_text('ready'); "
                 "end=time.monotonic()+15\n"
                 "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<end: time.sleep(.05)")
        process = subprocess.Popen(self.command("run", "--task", task["id"], "--",
                                                 sys.executable, "-c", child, ready, release),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(ready.exists(), "Producer did not start")
            self.call("finish", "--task", task["id"], ok=False)
        finally:
            release.write_text("release", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.call("finish", "--task", task["id"])

    def test_corrupt_state_json_fails_without_touching_scratch(self):
        _, resource, payload, _ = self.retired()
        (self.state / "state.json").write_text("{broken", encoding="utf-8")
        self.call("sweep", ok=False)
        self.assert_preserved(resource, payload)

    def test_nonobject_state_is_rejected_with_json_error(self):
        _, resource, payload, _ = self.retired()
        for malformed in (None, [], "unexpected"):
            with self.subTest(value=malformed):
                self.write_state(malformed)
                self.call("sweep", ok=False)
                self.assert_preserved(resource, payload)

    def test_hardlinked_material_cannot_be_sealed_or_published(self):
        task = self.start()
        resource = self.reserve(task)
        outside = self.base / "shared-source.txt"
        outside.write_text("Linked user source", encoding="utf-8")
        link = Path(resource["path"]) / "hardlink.txt"
        os.link(outside, link)
        self.call("seal", "--resource", resource["id"], ok=False)
        self.call("publish", "--task", task["id"], "--path", link, ok=False)
        self.assertEqual(outside.read_text(encoding="utf-8"), "Linked user source")
        self.assertEqual(link.read_text(encoding="utf-8"), "Linked user source")

    def test_replaced_resource_directory_is_not_treated_as_owned(self):
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        original = Path(resource["path"])
        moved = original.with_name("saved-original")
        original.rename(moved)
        original.mkdir()
        replacement = original / "user-work.txt"
        replacement.write_text("Unowned replacement", encoding="utf-8")
        self.call("sweep")
        self.assertEqual(replacement.read_text(encoding="utf-8"), "Unowned replacement")
        self.assertTrue((moved / payload.name).is_file())

    def test_corrupt_snapshot_fails_without_touching_scratch(self):
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        state = self.read_state()
        state["resources"][resource["id"]]["snapshot"]["files"]["../../outside"] = {}
        self.write_state(state)
        self.call("sweep", ok=False)
        self.assert_preserved(resource, payload)

    def test_shortened_retention_record_cannot_delete_early(self):
        _, resource, payload, _ = self.retired()
        state = self.read_state()
        record = state["resources"][resource["id"]]
        record["retired_at"] = time.time() - 60
        record["delete_after"] = time.time() - 1
        self.write_state(state)
        self.call("sweep")
        self.assert_preserved(resource, payload)

    def test_nonfinite_retention_timestamps_fail_closed(self):
        _, resource, payload, _ = self.retired()
        original = self.read_state()
        for field in ("retired_at", "delete_after"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    state = json.loads(json.dumps(original))
                    record = state["resources"][resource["id"]]
                    record["retired_at"] = time.time() - SEVEN_DAYS - 3600
                    record["delete_after"] = record["retired_at"] + SEVEN_DAYS
                    record[field] = value
                    self.write_state(state)
                    result = subprocess.run(self.command("sweep"), capture_output=True,
                                            text=True, encoding="utf-8", timeout=15)
                    self.assertIsInstance(json.loads(result.stdout), dict)
                    self.assert_preserved(resource, payload)

    def test_delivery_inside_scratch_makes_directory_ineligible(self):
        task = self.start()
        resource = self.reserve(task)
        payload = self.create_payload(resource, "Published final artifact")
        self.call("seal", "--resource", resource["id"])
        self.call("publish", "--task", task["id"], "--path", payload)
        self.call("finish", "--task", task["id"])
        self.call("retire", "--resource", resource["id"], "--reason", "Attempt", ok=False)
        self.assert_preserved(resource, payload)

    def test_resource_path_tampering_cannot_target_unowned_directory(self):
        _, resource, payload, delivery = self.retired()
        self.expire(resource)
        state = self.read_state()
        state["resources"][resource["id"]]["path"] = str(delivery.parent)
        self.write_state(state)
        self.call("sweep")
        self.assert_preserved(resource, payload)
        self.assertEqual(delivery.read_text(encoding="utf-8"), "User's final document")

    @unittest.skipUnless(os.name == "nt", "Windows directory junction test")
    def test_junction_added_after_retirement_is_never_followed(self):
        import _winapi
        _, resource, payload, _ = self.retired()
        self.expire(resource)
        target = self.base / "protected-target"
        target.mkdir()
        sentinel = target / "important.txt"
        sentinel.write_text("Preserve target", encoding="utf-8")
        link = Path(resource["path"]) / "redirect"
        _winapi.CreateJunction(str(target), str(link))
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        self.call("sweep")
        self.assert_preserved(resource, payload)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "Preserve target")

    @unittest.skipUnless(os.name == "nt", "Production purge is Windows-only")
    def test_interrupted_purge_resumes_from_saved_journal(self):
        _, resource, payload, delivery = self.retired()
        self.expire(resource)
        original = Path(resource["path"])
        tomb = original.with_name(".purging-" + resource["id"])

        def interrupted_delete(path, snapshot):
            self.assertEqual(path, tomb)
            # Simulate interruption after one guarded file deletion.
            (path / payload.name).unlink()
            raise OSError("Simulated interruption")

        with mock.patch.object(CORE, "guarded_delete", side_effect=interrupted_delete):
            with CORE.Manager(self.state).transaction() as manager:
                report = manager.sweep()
        self.assertEqual(report["purged"], [])
        self.assertFalse(original.exists())
        self.assertTrue(tomb.exists())
        self.assertEqual(self.read_state()["resources"][resource["id"]]["state"], "purging")
        resumed = self.call("sweep")
        self.assertIn(resource["id"], resumed["purged"], resumed)
        self.assertFalse(tomb.exists())
        self.assertEqual(delivery.read_text(encoding="utf-8"), "User's final document")
        self.assertEqual(self.call("sweep")["purged"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
