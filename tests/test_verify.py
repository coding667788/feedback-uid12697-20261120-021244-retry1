"""verify 命令测试：逐步重放、终态全约束、最优性复核、不可达报告复核、篡改拒绝。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from authplanner.model import Manifest  # noqa: E402
from authplanner.solver import solve  # noqa: E402
from authplanner.util import load_json_file, sha256_hex  # noqa: E402
from authplanner.verify import verify_document  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def solved(name):
    raw = load_json_file(os.path.join(FIXTURE_DIR, name))
    manifest = Manifest.from_dict(raw)
    return manifest, solve(manifest, sha256_hex(raw))


class VerifyPlanTests(unittest.TestCase):
    def test_valid_plan_passes(self):
        for name in [
            "tiny_reachable.json",
            "atomic_replace_only.json",
            "dedup_ranking.json",
            "legacy_cycle.json",
            "or_dependencies.json",
        ]:
            with self.subTest(fixture=name):
                manifest, plan = solved(name)
                result = verify_document(manifest, plan, check_optimality=True)
                self.assertTrue(result["ok"], result["errors"])
                self.assertEqual(result["final_state"], plan["target_state"])
                self.assertIsNotNone(result["optimality"])

    def test_tampered_step_rejected(self):
        manifest, plan = solved("tiny_reachable.json")
        plan["steps"][0]["operation"]["package"] = "f"  # 篡改操作但保留旧哈希
        result = verify_document(manifest, plan)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_dropped_step_rejected(self):
        manifest, plan = solved("or_dependencies.json")
        plan["steps"].pop()
        result = verify_document(manifest, plan)
        self.assertFalse(result["ok"])

    def test_wrong_target_state_rejected(self):
        manifest, plan = solved("tiny_reachable.json")
        plan["target_state"] = ["a"]
        # 同步重算哈希会通过哈希校验，但重放比对仍应拒绝
        from authplanner.solver import _hash_payload
        from authplanner.util import sha256_hex as _h
        plan["plan_hash"] = _h(_hash_payload(plan))
        result = verify_document(manifest, plan)
        self.assertFalse(result["ok"])
        self.assertTrue(any("target_state" in e for e in result["errors"]))

    def test_optimality_recheck_catches_suboptimal_plan(self):
        """把一条可达但次优的合法路径包装成计划，--check-optimality 必须识破。

        or_dependencies 中 pre2 方案比 pre1 方案贵；手工构造
        {pre2,pre3,target} 的合法两步计划，其每步与终态均合法，
        但三级目标排序下不是最优，普通 verify 通过而最优性复核失败。
        """
        raw = load_json_file(os.path.join(FIXTURE_DIR, "or_dependencies.json"))
        manifest = Manifest.from_dict(raw)

        def bits(ids):
            state = 0
            for pkg_id in ids:
                state |= 1 << manifest.index[pkg_id]
            return state

        s0 = bits(["pre3"])
        s1 = bits(["pre2", "pre3"])
        s2 = bits(["pre2", "pre3", "target"])
        ops = [
            {"type": "enable", "package": "pre2"},
            {"type": "enable", "package": "target"},
        ]
        steps = []
        for seq, (before, op, after) in enumerate(zip([s0, s1], ops, [s1, s2])):
            steps.append({
                "seq": seq,
                "operation": op,
                "operation_key": manifest.operation_key(op),
                "before_hash": manifest.state_hash(before),
                "after_hash": manifest.state_hash(after),
                "before_packages": sorted(manifest.active_ids(before)),
                "after_packages": sorted(manifest.active_ids(after)),
            })
        suboptimal = {
            "report_kind": "plan",
            "schema_version": 1,
            "input_hash": sha256_hex(raw),
            "plan_id": sha256_hex(raw)[:12],
            "generation": "gen-" + sha256_hex(raw)[:16],
            "old_state": sorted(manifest.active_ids(s0)),
            "target_state": sorted(manifest.active_ids(s2)),
            "objective": {
                "unrequested_count": manifest.unrequested_count(s2),
                "total_cost": manifest.total_cost(s2),
                "active_package_ids": sorted(manifest.active_ids(s2)),
            },
            "step_count": 2,
            "steps": steps,
            "search_stats": {"states_discovered": 0, "feasible_final_states": 0,
                             "package_count": 4, "state_space_upper_bound": 16,
                             "search_capped": False},
            "final_summary": manifest.state_summary(s2),
        }
        from authplanner.solver import _hash_payload
        suboptimal["plan_hash"] = sha256_hex(_hash_payload(suboptimal))

        plain = verify_document(manifest, suboptimal, check_optimality=False)
        self.assertTrue(plain["ok"], plain["errors"])
        strict = verify_document(manifest, suboptimal, check_optimality=True)
        self.assertFalse(strict["ok"])
        self.assertTrue(any("更优可达终态" in e for e in strict["errors"]))


class VerifyUnreachableTests(unittest.TestCase):
    def test_unreachable_report_reconfirmed(self):
        manifest, report = solved("unreachable_optimal.json")
        result = verify_document(manifest, report)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["reachable_feasible_finals"], 0)
        self.assertEqual(
            result["rechecked_states"],
            report["search_stats"]["states_discovered"],
        )

    def test_false_unreachable_report_rejected(self):
        # 用可达夹具求解后把文档改写成不可达结论
        manifest, plan = solved("tiny_reachable.json")
        fake_report = {
            "report_kind": "unreachable",
            "schema_version": 1,
            "input_hash": plan["input_hash"],
            "old_state": plan["old_state"],
            "conclusion": "no_reachable_target",
            "search_stats": {"states_discovered": 1, "feasible_final_states": 0,
                             "package_count": 6, "state_space_upper_bound": 64,
                             "search_capped": False},
        }
        from authplanner.solver import _hash_payload
        from authplanner.util import sha256_hex as _h
        fake_report["report_hash"] = _h(_hash_payload(fake_report))
        result = verify_document(manifest, fake_report)
        self.assertFalse(result["ok"])
        self.assertTrue(any("不可达结论不成立" in e for e in result["errors"]))

    def test_state_count_mismatch_rejected(self):
        manifest, report = solved("unreachable_optimal.json")
        report["search_stats"]["states_discovered"] = 999
        from authplanner.solver import _hash_payload
        from authplanner.util import sha256_hex as _h
        report["report_hash"] = _h(_hash_payload(report))
        result = verify_document(manifest, report)
        self.assertFalse(result["ok"])
        self.assertTrue(any("状态数" in e for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
