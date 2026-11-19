"""端到端 CLI 测试：四个子命令、退出码约定、隔离模式与文件落盘。"""
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PLANNER = os.path.join(REPO_ROOT, "planner.py")


def run(args, isolated=False, timeout=60):
    cmd = [sys.executable]
    if isolated:
        cmd.append("-I")
    cmd += [PLANNER] + args
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout
    )


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = lambda name: os.path.join(FIXTURE_DIR, name)
        self.out = lambda name: os.path.join(self.tmp.name, name)

    def tearDown(self):
        gc.collect()
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def test_version(self):
        proc = run(["--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("authplanner", proc.stdout)

    def test_solve_verify_rollout_status_pipeline(self):
        plan_path = self.out("plan.json")
        ledger_path = self.out("ledger.db")

        proc = run(["solve", self.fx("tiny_reachable.json"), "-o", plan_path])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.load(open(plan_path, encoding="utf-8"))
        self.assertEqual(plan["report_kind"], "plan")

        proc = run([
            "verify", self.fx("tiny_reachable.json"), plan_path,
            "--check-optimality", "-o", self.out("verify.json"),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verification = json.load(open(self.out("verify.json"), encoding="utf-8"))
        self.assertTrue(verification["ok"])

        proc = run([
            "rollout", self.fx("tiny_reachable.json"), plan_path,
            "--ledger", ledger_path,
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rollout = json.loads(proc.stdout)
        self.assertEqual(rollout["status"], "completed")

        proc = run([
            "status", ledger_path,
            "--input", self.fx("tiny_reachable.json"), "--plan", plan_path,
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = json.loads(proc.stdout)
        self.assertEqual(status["committed_steps"], plan["step_count"])
        self.assertEqual(status["derived_state"], plan["target_state"])
        self.assertTrue(status["matches_plan_target"])

    def test_unreachable_exit_code_3_and_verify_exit_0(self):
        report_path = self.out("report.json")
        proc = run(["solve", self.fx("unreachable_optimal.json"), "-o", report_path])
        self.assertEqual(proc.returncode, 3)
        report = json.load(open(report_path, encoding="utf-8"))
        self.assertEqual(report["conclusion"], "no_reachable_target")
        self.assertGreater(report["search_stats"]["states_discovered"], 0)

        proc = run(["verify", self.fx("unreachable_optimal.json"), report_path])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_verify_failure_exit_code_4(self):
        plan_path = self.out("plan.json")
        run(["solve", self.fx("tiny_reachable.json"), "-o", plan_path])
        plan = json.load(open(plan_path, encoding="utf-8"))
        plan["steps"][0]["after_hash"] = "f" * 16
        json.dump(plan, open(plan_path, "w", encoding="utf-8"))
        proc = run(["verify", self.fx("tiny_reachable.json"), plan_path])
        self.assertEqual(proc.returncode, 4)

    def test_generation_conflict_exit_code_5(self):
        plan_path = self.out("plan.json")
        ledger_path = self.out("ledger.db")
        run(["solve", self.fx("or_dependencies.json"), "-o", plan_path])
        run(["rollout", self.fx("or_dependencies.json"), plan_path,
             "--ledger", ledger_path])

        stale_plan = json.load(open(plan_path, encoding="utf-8"))
        stale_plan["generation"] = "gen-0000000000000000"
        stale_path = self.out("stale.json")
        json.dump(stale_plan, open(stale_path, "w", encoding="utf-8"))
        proc = run(["rollout", self.fx("or_dependencies.json"), stale_path,
                    "--ledger", ledger_path])
        self.assertEqual(proc.returncode, 5)
        self.assertIn("代次冲突", proc.stderr)

    def test_status_missing_ledger_exit_code_2(self):
        proc = run(["status", self.out("missing.db")])
        self.assertEqual(proc.returncode, 2)

    def test_bad_input_exit_code_1(self):
        bad = self.out("bad.json")
        json.dump({"packages": []}, open(bad, "w", encoding="utf-8"))
        proc = run(["solve", bad])
        self.assertEqual(proc.returncode, 1)

    def test_dump_reachable_for_independent_review(self):
        dump_path = self.out("reachable.json")
        proc = run([
            "solve", self.fx("atomic_replace_only.json"),
            "--dump-reachable", dump_path, "-o", self.out("p.json"),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dump = json.load(open(dump_path, encoding="utf-8"))
        states = dump["reachable_states"]
        self.assertTrue(all("is_feasible_final" in s and "depth" in s for s in states))
        # 旧状态深度为 0
        self.assertEqual(next(s for s in states if s["depth"] == 0)["depth"], 0)

    def test_runs_under_isolated_mode(self):
        proc = run(["--version"], isolated=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run(
            ["solve", self.fx("or_dependencies.json"), "-o", self.out("iso.json")],
            isolated=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.load(open(self.out("iso.json"), encoding="utf-8"))["report_kind"],
            "plan",
        )


if __name__ == "__main__":
    unittest.main()
