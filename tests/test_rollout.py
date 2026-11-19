"""rollout/台账测试：代次校验、幂等键、重启接续、故障注入与拒绝路径。"""
import copy
import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from authplanner.ledger import (  # noqa: E402
    ControlledFault,
    Ledger,
    LedgerError,
    idempotency_key,
)
from authplanner.model import Manifest  # noqa: E402
from authplanner.solver import _hash_payload, solve  # noqa: E402
from authplanner.util import load_json_file, sha256_hex  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cleanup_tmp(tmp: tempfile.TemporaryDirectory) -> None:
    """Windows 下 SQLite 句柄释放可能滞后于 GC，先强制回收再容错删除。"""
    gc.collect()
    shutil.rmtree(tmp.name, ignore_errors=True)


def solved(name):
    raw = load_json_file(os.path.join(FIXTURE_DIR, name))
    manifest = Manifest.from_dict(raw)
    return manifest, raw, solve(manifest, sha256_hex(raw))


class RolloutHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp.name, "rollout.db")
        self.manifest, self.raw, self.plan = solved("or_dependencies.json")

    def tearDown(self):
        _cleanup_tmp(self.tmp)

    def test_full_rollout_writes_every_step_with_summaries_and_idem(self):
        ledger = Ledger.open(self.ledger_path, self.manifest, self.plan)
        result = ledger.rollout(self.manifest, self.plan)
        ledger.close()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps_applied"], self.plan["step_count"])
        self.assertEqual(result["current_state"], self.plan["target_state"])

        status = Ledger.read_status(self.ledger_path)
        self.assertEqual(status["committed_steps"], self.plan["step_count"])
        self.assertEqual(status["meta"]["generation"], self.plan["generation"])
        for row, step in zip(status["steps"], self.plan["steps"]):
            self.assertEqual(row["operation_key"], step["operation_key"])
            self.assertEqual(row["before_hash"], step["before_hash"])
            self.assertEqual(row["after_hash"], step["after_hash"])
            # 每步保存前后摘要
            conn = sqlite3.connect(self.ledger_path)
            row_full = conn.execute(
                "SELECT before_summary, after_summary FROM steps WHERE seq=?",
                (row["seq"],),
            ).fetchone()
            conn.close()
            before_summary = json.loads(row_full[0])
            after_summary = json.loads(row_full[1])
            self.assertEqual(before_summary["state_hash"], step["before_hash"])
            self.assertEqual(after_summary["state_hash"], step["after_hash"])
            expected_idem = idempotency_key(
                self.plan["plan_id"], self.plan["generation"], row["seq"],
                row["operation_key"], row["before_hash"], row["after_hash"],
            )
            self.assertEqual(row["idempotency_key"], expected_idem)

    def test_resume_after_exception_fault_commits_remaining_steps(self):
        ledger = Ledger.open(self.ledger_path, self.manifest, self.plan)
        with self.assertRaises(ControlledFault):
            ledger.rollout(self.manifest, self.plan, faults={0: "exception"})
        ledger.close()

        mid = Ledger.read_status(self.ledger_path)
        self.assertEqual(mid["committed_steps"], 0)  # 持久化前异常，零写入
        self.assertEqual(mid["meta"]["status"], "running")

        # 重启：以已提交步进度接续
        ledger2 = Ledger.open(self.ledger_path, self.manifest, self.plan)
        result2 = ledger2.rollout(self.manifest, self.plan)
        ledger2.close()
        self.assertEqual(result2["status"], "completed")
        self.assertEqual(result2["resumed_from_step"], 0)
        self.assertEqual(result2["steps_applied"], 2)

    def test_resume_midway_after_fault_at_second_step(self):
        ledger = Ledger.open(self.ledger_path, self.manifest, self.plan)
        with self.assertRaises(ControlledFault):
            ledger.rollout(self.manifest, self.plan, faults={1: "exception"})
        ledger.close()
        self.assertEqual(Ledger.read_status(self.ledger_path)["committed_steps"], 1)

        ledger2 = Ledger.open(self.ledger_path, self.manifest, self.plan)
        result = ledger2.rollout(self.manifest, self.plan)
        ledger2.close()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["resumed_from_step"], 1)
        self.assertEqual(result["steps_skipped_committed"], 1)
        self.assertEqual(result["steps_applied"], 1)

    def test_idempotent_rerun_after_completion(self):
        ledger = Ledger.open(self.ledger_path, self.manifest, self.plan)
        first = ledger.rollout(self.manifest, self.plan)
        ledger.close()

        ledger2 = Ledger.open(self.ledger_path, self.manifest, self.plan)
        second = ledger2.rollout(self.manifest, self.plan)
        ledger2.close()
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["steps_applied"], 0)
        self.assertEqual(second["steps_skipped_committed"], first["total_steps"])
        # 台账行数不增加
        self.assertEqual(
            Ledger.read_status(self.ledger_path)["committed_steps"],
            first["total_steps"],
        )

    def test_exit_fault_in_subprocess_then_resume(self):
        """exit 故障注入：子进程 os._exit(0) 正常退出，台账停在故障前，可接续。"""
        plan_path = os.path.join(self.tmp.name, "plan.json")
        input_path = os.path.join(self.tmp.name, "input.json")
        with open(plan_path, "w", encoding="utf-8") as handle:
            json.dump(self.plan, handle)
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(self.raw, handle)

        env = dict(os.environ)
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "planner.py"),
                "rollout",
                input_path,
                plan_path,
                "--ledger",
                self.ledger_path,
                "--fault",
                "0:exit",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(Ledger.read_status(self.ledger_path)["committed_steps"], 0)

        proc2 = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "planner.py"),
                "rollout",
                input_path,
                plan_path,
                "--ledger",
                self.ledger_path,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        status = Ledger.read_status(self.ledger_path)
        self.assertEqual(status["meta"]["status"], "completed")
        self.assertEqual(status["committed_steps"], 2)


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp.name, "rollout.db")
        self.manifest, self.raw, self.plan = solved("or_dependencies.json")
        ledger = Ledger.open(self.ledger_path, self.manifest, self.plan)
        ledger.rollout(self.manifest, self.plan)
        ledger.close()

    def tearDown(self):
        _cleanup_tmp(self.tmp)

    def test_stale_generation_rejected(self):
        stale = copy.deepcopy(self.plan)
        stale["generation"] = "gen-deadbeefdeadbe"
        with self.assertRaises(LedgerError) as ctx:
            Ledger.open(self.ledger_path, self.manifest, stale)
        self.assertIn("代次冲突", str(ctx.exception))

    def test_same_id_changed_content_rejected(self):
        changed = copy.deepcopy(self.plan)
        changed["steps"][0]["after_hash"] = "0" * 16  # 同 plan_id，内容被换
        changed["plan_hash"] = sha256_hex(_hash_payload(changed))
        with self.assertRaises(LedgerError) as ctx:
            Ledger.open(self.ledger_path, self.manifest, changed)
        self.assertIn("同 ID 异内容", str(ctx.exception))

    def test_foreign_input_rejected(self):
        other_manifest, _other_raw, other_plan = solved("tiny_reachable.json")
        with self.assertRaises(LedgerError):
            Ledger.open(self.ledger_path, other_manifest, other_plan)

    def test_unreachable_document_cannot_rollout(self):
        manifest, raw, report = solved("unreachable_optimal.json")
        with self.assertRaises(LedgerError):
            Ledger.open(os.path.join(self.tmp.name, "x.db"), manifest, report)

    def test_externally_tampered_ledger_rejected(self):
        conn = sqlite3.connect(self.ledger_path)
        conn.execute("UPDATE steps SET operation_key = ? WHERE seq = 0", ("enable:hacked",))
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            Ledger.open(self.ledger_path, self.manifest, self.plan)

    def test_unexpected_drift_state_rejected(self):
        """删除最后一步台账记录但保留 meta 计数，模拟服务外状态漂移。"""
        conn = sqlite3.connect(self.ledger_path)
        conn.execute("DELETE FROM steps WHERE seq = 1")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            Ledger.open(self.ledger_path, self.manifest, self.plan)


class StatusTests(unittest.TestCase):
    def test_status_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            Ledger.read_status(os.path.join(tempfile.mkdtemp(), "nope.db"))

    def test_status_reports_chain_and_derived_state(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "s.db")
            manifest, raw, plan = solved("atomic_replace_only.json")
            ledger = Ledger.open(path, manifest, plan)
            ledger.rollout(manifest, plan)
            ledger.close()

            status = Ledger.read_status(path)
            self.assertEqual(status["committed_steps"], 1)
            self.assertEqual(status["steps"][0]["chain_prev"], "")
            self.assertTrue(status["steps"][0]["chain_hash"].startswith("chain-"))

            reopen = Ledger.open(path, manifest, plan)
            state = reopen.replay_state(manifest, plan)
            reopen.close()
            self.assertEqual(sorted(manifest.active_ids(state)), plan["target_state"])
        finally:
            _cleanup_tmp(tmp)


if __name__ == "__main__":
    unittest.main()
