"""求解器测试：不可达结论、状态数公开、无探测上限、字典序路径、哈希防篡改。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from authplanner.model import Manifest  # noqa: E402
from authplanner.solver import (  # noqa: E402
    enumerate_reachable,
    solve,
    verify_plan_hash,
    verify_report_hash,
)
from authplanner.util import load_json_file, sha256_hex  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_manifest(name):
    raw = load_json_file(os.path.join(FIXTURE_DIR, name))
    return Manifest.from_dict(raw), raw


class SolveReachableTests(unittest.TestCase):
    def test_atomic_replace_only_path(self):
        manifest, raw = load_manifest("atomic_replace_only.json")
        doc = solve(manifest, sha256_hex(raw))
        self.assertEqual(doc["report_kind"], "plan")
        self.assertEqual(doc["target_state"], ["new"])
        self.assertEqual(
            [s["operation_key"] for s in doc["steps"]],
            ["replace:old->new"],
        )
        self.assertEqual(doc["step_count"], 1)
        self.assertTrue(verify_plan_hash(doc))

    def test_lexicographic_path_prefers_disable_first(self):
        """tiny 夹具中 enable:e 与 disable:old 同为最短路径首步候选，
        但 enable:e 非法（依赖 c？否——真正非法原因由约束决定）；
        规范结果为 disable:* 字典序优先的两步路径。"""
        manifest, raw = load_manifest("tiny_reachable.json")
        doc = solve(manifest, sha256_hex(raw))
        self.assertEqual(
            [s["operation_key"] for s in doc["steps"]],
            ["disable:old", "enable:e"],
        )

    def test_every_step_carries_verified_hashes(self):
        manifest, raw = load_manifest("or_dependencies.json")
        doc = solve(manifest, sha256_hex(raw))
        # 每步前后包集合与状态哈希自洽，且首尾与旧态/目标态衔接
        first, last = doc["steps"][0], doc["steps"][-1]
        self.assertEqual(first["before_packages"], doc["old_state"])
        self.assertEqual(last["after_packages"], doc["target_state"])
        for step in doc["steps"]:
            self.assertNotEqual(step["before_hash"], step["after_hash"])
            self.assertTrue(step["operation_key"])
        # 相邻步骤首尾相接
        for prev, nxt in zip(doc["steps"], doc["steps"][1:]):
            self.assertEqual(prev["after_hash"], nxt["before_hash"])


class UnreachableTests(unittest.TestCase):
    def test_unreachable_report_is_independently_reviewable(self):
        manifest, raw = load_manifest("unreachable_optimal.json")
        doc = solve(manifest, sha256_hex(raw))
        self.assertEqual(doc["report_kind"], "unreachable")
        self.assertEqual(doc["conclusion"], "no_reachable_target")
        # 公开求解状态数，且明确声明未使用探测上限
        stats = doc["search_stats"]
        self.assertGreater(stats["states_discovered"], 0)
        self.assertFalse(stats["search_capped"])
        self.assertEqual(stats["feasible_final_states"], 0)
        self.assertTrue(verify_report_hash(doc))

    def test_no_probe_cap_means_full_state_space_when_unconstrained(self):
        """无冲突/依赖/必保/禁用时，24 包以下小例可达集即全部 2^n 状态。"""
        data = {
            "packages": [
                {"id": f"p{i:02d}", "project": "P1",
                 "capabilities": [["s", f"c{i}"]], "cost": 1,
                 "conflicts": [], "requires": []}
                for i in range(6)
            ],
            "projects": [{"id": "P1", "required": [["s", "c0"]], "forbidden": []}],
            "old_state": [],
        }
        manifest = Manifest.from_dict(data)
        search = enumerate_reachable(manifest)
        self.assertEqual(len(search["parents"]), 2 ** 6)
        self.assertFalse(solve(manifest, sha256_hex(data))["search_stats"]["search_capped"])

    def test_tampered_report_hash_fails(self):
        manifest, raw = load_manifest("unreachable_optimal.json")
        doc = solve(manifest, sha256_hex(raw))
        doc["search_stats"]["states_discovered"] += 1
        self.assertFalse(verify_report_hash(doc))

    def test_tampered_plan_hash_fails(self):
        manifest, raw = load_manifest("tiny_reachable.json")
        doc = solve(manifest, sha256_hex(raw))
        doc["target_state"].append("ghost")
        self.assertFalse(verify_plan_hash(doc))


class GenerationDeterminismTests(unittest.TestCase):
    def test_solve_is_deterministic_across_runs(self):
        manifest, raw = load_manifest("legacy_cycle.json")
        first = solve(manifest, sha256_hex(raw))
        second = solve(manifest, sha256_hex(raw))
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["generation"], second["generation"])


if __name__ == "__main__":
    unittest.main()
