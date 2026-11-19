"""领域模型单元测试：输入校验、四类不变量、依赖环规则与单步转移。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from authplanner.model import Manifest, ModelError  # noqa: E402
from authplanner.util import load_json_file  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    return Manifest.from_dict(load_json_file(os.path.join(FIXTURE_DIR, name)))


def state_of(manifest, *ids):
    state = 0
    for pkg_id in ids:
        state |= 1 << manifest.index[pkg_id]
    return state


class InputValidationTests(unittest.TestCase):
    def test_rejects_unknown_references(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        data["packages"][0]["conflicts"] = ["ghost"]
        with self.assertRaises(ModelError):
            Manifest.from_dict(data)

    def test_rejects_self_dependency(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        data["packages"][0]["requires"] = [["a"]]
        with self.assertRaises(ModelError):
            Manifest.from_dict(data)

    def test_rejects_too_many_packages(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        base = data["packages"][0]
        data["packages"] = [
            dict(base, id=f"p{i}", project="P1", conflicts=[], requires=[])
            for i in range(25)
        ]
        with self.assertRaises(ModelError):
            Manifest.from_dict(data)

    def test_rejects_duplicate_package_id(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        data["packages"][1]["id"] = data["packages"][0]["id"]
        with self.assertRaises(ModelError):
            Manifest.from_dict(data)

    def test_rejects_undeclared_replacement_pair(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "atomic_replace_only.json"))
        # 包对合法（old 启用、extra 停用）但未在 replacements 中声明
        data["replacements"] = []
        manifest = Manifest.from_dict(data)
        op = {"type": "replace", "remove": "old", "add": "extra"}
        after = manifest.apply_operation(manifest.old_state, op)
        violations = manifest.transition_violations(manifest.old_state, op, after)
        self.assertTrue(any("未在输入 replacements 中声明" in v for v in violations))

    def test_rejects_old_state_violating_invariant(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        data["old_state"] = ["a", "f"]  # f 与 c 无关但 f 依赖 a|b（a 满足），改为冲突对
        data["packages"][5]["conflicts"] = ["a"]
        with self.assertRaises(ModelError):
            Manifest.from_dict(data)

    def test_rejects_required_forbidden_overlap(self):
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        data["projects"][0]["forbidden"] = [["repo", "read"]]
        with self.assertRaises(ModelError):
            Manifest.from_dict(data)

    def test_conflict_normalized_bidirectionally(self):
        manifest = load("tiny_reachable.json")
        i_f = manifest.index["f"]
        i_c = manifest.index["c"]
        self.assertTrue(manifest.conflict_mask[i_f] & (1 << i_c))
        self.assertTrue(manifest.conflict_mask[i_c] & (1 << i_f))


class InvariantTests(unittest.TestCase):
    def test_forbidden_package_pair_is_unsplittable(self):
        """覆盖禁用对的包不能通过任何方式保留：整包禁用。"""
        data = load_json_file(os.path.join(FIXTURE_DIR, "tiny_reachable.json"))
        data["projects"][0]["forbidden"] = [["repo", "write"]]  # 仅 b 覆盖
        manifest = Manifest.from_dict(data)
        bad = state_of(manifest, "a", "b", "c")
        violations = manifest.invariant_violations(bad)
        self.assertTrue(any("绝对禁止" in v for v in violations))
        self.assertFalse(manifest.is_feasible_final(bad))

    def test_mustkeep_gap_rejected_in_transition(self):
        manifest = load("atomic_replace_only.json")
        # 停用 old 而不补偿必保能力 r.k
        after = state_of(manifest)  # 空状态
        violations = manifest.invariant_violations(after)
        self.assertTrue(any("必保能力缺失" in v for v in violations))

    def test_or_dependency_any_precondition_satisfies(self):
        manifest = load("or_dependencies.json")
        # pre1 满足第一个 OR 组，pre3 满足第二个 AND 组
        ok = state_of(manifest, "pre1", "pre3", "target")
        self.assertEqual(manifest.invariant_violations(ok), [])
        # pre2 同样满足第一个 OR 组
        ok2 = state_of(manifest, "pre2", "pre3", "target")
        self.assertEqual(manifest.invariant_violations(ok2), [])
        # 两个前置都不在 -> 依赖失败
        bad = state_of(manifest, "pre3", "target")
        self.assertTrue(any("依赖组未满足" in v for v in manifest.invariant_violations(bad)))
        # 只满足第一组、缺 pre3 -> AND 语义下仍失败
        bad2 = state_of(manifest, "pre1", "target")
        self.assertTrue(manifest.invariant_violations(bad2))

    def test_conflict_pair_rejected(self):
        manifest = load("tiny_reachable.json")
        bad = state_of(manifest, "c", "f")
        self.assertTrue(any("冲突" in v for v in manifest.invariant_violations(bad)))


class CycleTests(unittest.TestCase):
    def test_legacy_cycle_loads_and_is_legal(self):
        manifest = load("legacy_cycle.json")
        # 旧状态中的环属于历史包袱，不变量校验不查环，加载成功
        self.assertEqual(manifest.invariant_violations(manifest.old_state), [])

    def test_cannot_create_new_cycle_in_single_step(self):
        manifest = load("legacy_cycle.json")
        idx = manifest.index
        # 停用 cycleA：cycleB 立即失去前置 -> 该转移本身非法
        without_a = manifest.old_state & ~(1 << idx["cycleA"])
        op_disable = {"type": "disable", "package": "cycleA"}
        self.assertTrue(
            manifest.transition_violations(manifest.old_state, op_disable, without_a)
        )

    def test_new_cycle_detection_on_synthetic_mutual_dependency(self):
        """合成最小双包互依场景：x 已启用，再启用依赖 x 且被 x 依赖的 y 即新环。"""
        data = {
            "packages": [
                {"id": "x", "project": "P1", "capabilities": [["s", "x"]],
                 "cost": 1, "conflicts": [], "requires": [["y"]]},
                {"id": "y", "project": "P1", "capabilities": [["s", "y"]],
                 "cost": 1, "conflicts": [], "requires": [["x"]]},
            ],
            "projects": [{"id": "P1", "required": [], "forbidden": []}],
            "old_state": [],
        }
        manifest = Manifest.from_dict(data)
        before = state_of(manifest, "x")
        self.assertTrue(manifest.introduces_new_cycle(before, 1 << manifest.index["y"]))
        # 无环依赖（y -> x，x 不依赖 y）不算新环
        data["packages"][0]["requires"] = []
        manifest2 = Manifest.from_dict(data)
        before2 = state_of(manifest2, "x")
        self.assertFalse(
            manifest2.introduces_new_cycle(before2, 1 << manifest2.index["y"])
        )

    def test_reenabling_into_legacy_cycle_is_allowed(self):
        """回到历史环中的包不构成“新建”环。"""
        manifest = load("legacy_cycle.json")
        idx = manifest.index
        # 旧态 cycleA/cycleB 环已存在；模拟 solo 停用再启用，不涉及新环
        bit = 1 << idx["solo"]
        before = manifest.old_state & ~bit
        self.assertFalse(manifest.introduces_new_cycle(before, bit))
        # newbie 依赖 cycleA，在旧态直接启用不产生环（无回到 newbie 的路径）
        self.assertFalse(
            manifest.introduces_new_cycle(manifest.old_state, 1 << idx["newbie"])
        )

    def test_solver_never_emits_new_cycle(self):
        from authplanner.solver import enumerate_reachable

        manifest = load("legacy_cycle.json")
        search = enumerate_reachable(manifest)
        idx = manifest.index
        # 任何可达状态中，newbie 与 cycleA 同时启用时 cycleB 必在（无新环）
        for state in search["parents"]:
            if state & (1 << idx["newbie"]):
                self.assertTrue(state & (1 << idx["cycleA"]))
                self.assertTrue(state & (1 << idx["cycleB"]))


class TransitionTests(unittest.TestCase):
    def test_operation_key_lexicographic_order(self):
        manifest = load("tiny_reachable.json")
        keys = [manifest.operation_key(op) for op in manifest.legal_operations(manifest.old_state)]
        self.assertEqual(keys, sorted(keys))
        # disable:* < enable:* < replace:* 的公开序
        self.assertTrue(all(k.startswith(("disable", "enable")) for k in keys))

    def test_undeclared_replace_rejected_at_transition(self):
        manifest = load("tiny_reachable.json")  # 该夹具无 replacements
        op = {"type": "replace", "remove": "old", "add": "e"}
        after = manifest.apply_operation(manifest.old_state, op)
        violations = manifest.transition_violations(manifest.old_state, op, after)
        self.assertTrue(any("未在输入 replacements 中声明" in v for v in violations))

    def test_atomic_replace_is_single_step(self):
        manifest = load("atomic_replace_only.json")
        op = {"type": "replace", "remove": "old", "add": "new"}
        after = manifest.apply_operation(manifest.old_state, op)
        self.assertEqual(manifest.transition_violations(manifest.old_state, op, after), [])
        self.assertEqual(sorted(manifest.active_ids(after)), ["new"])

    def test_enable_twice_is_rejected(self):
        manifest = load("tiny_reachable.json")
        op = {"type": "enable", "package": "a"}
        after = manifest.apply_operation(manifest.old_state, op)
        self.assertTrue(manifest.transition_violations(manifest.old_state, op, after))


class ObjectiveTests(unittest.TestCase):
    def test_dedup_counts_per_project_triples(self):
        manifest = load("dedup_ranking.json")
        both_legs = state_of(manifest, "leg", "leg2")
        # 同一能力标识 shared.secret 出现在两个项目 -> 按三元组去重计 2
        self.assertEqual(manifest.unrequested_count(both_legs), 2)
        clean = state_of(manifest, "a", "b")
        self.assertEqual(manifest.unrequested_count(clean), 0)
        # 第一级目标优先：干净方案虽贵但胜出
        self.assertLess(manifest.objective(clean), manifest.objective(both_legs))

    def test_tie_break_by_cost_then_id_sequence(self):
        manifest = load("or_dependencies.json")
        cheap = state_of(manifest, "pre1", "pre3", "target")
        pricey = state_of(manifest, "pre2", "pre3", "target")
        # 两者未申报数相同，成本低的 pre1 方案优先
        self.assertLess(manifest.objective(cheap), manifest.objective(pricey))


if __name__ == "__main__":
    unittest.main()
