"""交叉校验：生产求解器 vs 独立枚举器（不同数据结构、独立实现）。

小实例上逐夹具比对：可达状态数、可达终态集合、最优目标值、
最短路径长度与字典序操作序列，以及不可达结论。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from authplanner.model import Manifest  # noqa: E402
from authplanner.solver import enumerate_reachable, reconstruct_path, solve  # noqa: E402
from authplanner.util import load_json_file, sha256_hex  # noqa: E402
from bruteforce import BruteForcePlanner  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# 所有包数足够小、可完整独立枚举的夹具
ENUMERABLE = [
    "tiny_reachable.json",
    "unreachable_optimal.json",
    "atomic_replace_only.json",
    "dedup_ranking.json",
    "legacy_cycle.json",
    "or_dependencies.json",
]


class CrossCheckTests(unittest.TestCase):
    def _pair(self, name):
        path = os.path.join(FIXTURE_DIR, name)
        raw = load_json_file(path)
        manifest = Manifest.from_dict(raw)
        brute = BruteForcePlanner(raw)
        return manifest, brute, raw

    def test_reachable_sets_match(self):
        for name in ENUMERABLE:
            with self.subTest(fixture=name):
                manifest, brute, _ = self._pair(name)
                search = enumerate_reachable(manifest)
                brute_info = brute.explore()

                prod_states = {
                    frozenset(manifest.active_ids(state))
                    for state in search["parents"]
                }
                self.assertEqual(prod_states, brute_info["reachable"])

                prod_finals = {
                    frozenset(manifest.active_ids(state))
                    for state in search["feasible_finals"]
                }
                self.assertEqual(prod_finals, brute_info["finals"])

    def test_best_objective_and_path_match(self):
        for name in ENUMERABLE:
            with self.subTest(fixture=name):
                manifest, brute, raw = self._pair(name)
                document = solve(manifest, sha256_hex(raw))
                brute_plan = brute.best_plan()

                if document["report_kind"] == "unreachable":
                    self.assertIsNone(brute_plan["best"])
                    self.assertEqual(
                        document["search_stats"]["states_discovered"],
                        brute_plan["reachable_count"],
                    )
                    continue

                self.assertEqual(
                    tuple(document["target_state"]),
                    tuple(sorted(brute_plan["best"])),
                )
                self.assertEqual(
                    document["objective"]["unrequested_count"],
                    brute_plan["objective"][0],
                )
                self.assertEqual(
                    document["objective"]["total_cost"],
                    brute_plan["objective"][1],
                )
                prod_keys = [step["operation_key"] for step in document["steps"]]
                self.assertEqual(prod_keys, brute_plan["path_keys"])
                self.assertEqual(document["step_count"], len(brute_plan["path_keys"]))

                # 最短路径性质：BFS 深度等于计划步数
                search = enumerate_reachable(manifest)
                target_bits = 0
                for pkg_id in document["target_state"]:
                    target_bits |= 1 << manifest.index[pkg_id]
                self.assertEqual(search["depth"][target_bits], document["step_count"])

    def test_tiny_fixture_explicit_values(self):
        """夹具1 的手工推导值：目标 {a,c,e}，两步 disable:old -> enable:e。"""
        manifest, brute, raw = self._pair("tiny_reachable.json")
        document = solve(manifest, sha256_hex(raw))
        self.assertEqual(document["report_kind"], "plan")
        self.assertEqual(document["target_state"], ["a", "c", "e"])
        self.assertEqual(document["objective"]["unrequested_count"], 0)
        self.assertEqual(document["objective"]["total_cost"], 3)
        self.assertEqual(
            [s["operation_key"] for s in document["steps"]],
            ["disable:old", "enable:e"],
        )
        self.assertEqual(document["step_count"], 2)
        # 独立枚举器给出同一结论
        self.assertEqual(brute.best_plan()["path_keys"], ["disable:old", "enable:e"])


if __name__ == "__main__":
    unittest.main()
