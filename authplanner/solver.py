"""求解器：从旧状态出发的完整 BFS 穷举 + 三级目标择优。

关键承诺（对应需求）：
- 缓存全部已见状态，不设任何探测上限；24 个包的状态空间上限 2^24 可达，
  实际可达集通常远小，内存以整数集合承担。
- 绝不因“探测上限”返回最优；只有完整遍历后才下结论。
- 同一最优目标的多条路径中，取步数最少者；同步数按公开操作字典序择先。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from .model import Manifest, ModelError
from .util import canonical_json, sha256_hex

PLAN_SCHEMA_VERSION = 1


class _RecordView:
    """对 visited 紧凑记录某一字段的只读映射视图（避免复制成并行大字典）。"""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: dict[int, tuple[int, str, int]], pos: int):
        self._data = data
        self._pos = pos

    def __getitem__(self, key: int):
        return self._data[key][self._pos]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: int, default=None):
        record = self._data.get(key)
        return default if record is None else record[self._pos]


def enumerate_reachable(manifest: Manifest) -> dict[str, Any]:
    """BFS 穷举旧状态可达的全部合法状态。

    路径择优规则（README 公开）：同一目标的全部最短路径中，按操作键序列的
    字典序取首选。标准 FIFO BFS 中，父状态按发现顺序出队（同一层内即按
    字典序前缀排序），每个父状态的出边再按操作键字典序展开，首次发现即认领，
    因此每个状态记录的父指针同时满足“步数最少”与“完整操作序列字典序最小”。
    已见状态全部缓存，不设探测上限。

    热路径实现要点：状态是整数位掩码；visited 为单个字典，值是紧凑三元组
    (父状态, 操作键, 深度)；出边直接遍历预计算的包索引/替换表，
    操作键复用预计算字符串，不分配任何操作字典或临时集合。
    """
    start = manifest.old_state
    visited: dict[int, tuple[int, str, int]] = {start: (-1, "", 0)}
    queue: deque[int] = deque([start])
    feasible_finals: list[int] = []
    id_order = manifest._id_order
    enable_keys = manifest._enable_keys
    disable_keys = manifest._disable_keys
    replacements = manifest._repl_sorted

    while queue:
        state = queue.popleft()
        if manifest.is_feasible_final(state):
            feasible_finals.append(state)
        depth_here = visited[state][2] + 1

        # 单包操作按全局操作键字典序展开：先全部 disable:*，再全部 enable:*，
        # 同类型内按包 ID 字典序；replace:* 整体排在最后
        for i in id_order:
            bit = 1 << i
            if not (state & bit):
                continue
            nxt = state & ~bit
            if nxt in visited:
                continue
            if not manifest.invariants_hold(nxt):
                continue
            visited[nxt] = (state, disable_keys[i], depth_here)
            queue.append(nxt)

        for i in id_order:
            bit = 1 << i
            if state & bit:
                continue
            nxt = state | bit
            if nxt in visited:
                continue
            if manifest.introduces_new_cycle(state, bit):
                continue
            if not manifest.invariants_hold(nxt):
                continue
            visited[nxt] = (state, enable_keys[i], depth_here)
            queue.append(nxt)

        # 声明的原子替换（按操作键预排序），排在全部单包操作之后
        for remove_idx, add_idx, key in replacements:
            remove_bit, add_bit = 1 << remove_idx, 1 << add_idx
            if not (state & remove_bit) or (state & add_bit):
                continue
            cycle_base = state & ~remove_bit
            nxt = cycle_base | add_bit
            if nxt in visited:
                continue
            if manifest.introduces_new_cycle(cycle_base, add_bit):
                continue
            if not manifest.invariants_hold(nxt):
                continue
            visited[nxt] = (state, key, depth_here)
            queue.append(nxt)

    return {
        "start": start,
        "visited": visited,
        "parents": _RecordView(visited, 0),
        "parent_key": _RecordView(visited, 1),
        "depth": _RecordView(visited, 2),
        "feasible_finals": feasible_finals,
    }


def reconstruct_path(
    manifest: Manifest, search: dict[str, Any], target: int
) -> list[dict[str, str]]:
    visited = search["visited"]
    if target not in visited:
        raise KeyError("目标状态不在可达集中")
    keys: list[str] = []
    state = target
    while state != search["start"]:
        keys.append(visited[state][1])
        state = visited[state][0]
    keys.reverse()
    return [_operation_from_key(manifest, key) for key in keys]


def _operation_from_key(manifest: Manifest, key: str) -> dict[str, str]:
    if key.startswith("enable:"):
        return {"type": "enable", "package": key[len("enable:"):]}
    if key.startswith("disable:"):
        return {"type": "disable", "package": key[len("disable:"):]}
    if key.startswith("replace:"):
        payload = key[len("replace:"):]
        remove, add = payload.split("->", 1)
        return {"type": "replace", "remove": remove, "add": add}
    raise ModelError(f"无法解析的操作键: {key}")


def solve(manifest: Manifest, input_hash: str) -> dict[str, Any]:
    """完整求解，返回计划（reachable）或不可达结论报告（unreachable）。"""
    search = enumerate_reachable(manifest)
    visited_count = len(search["parents"])
    finals = search["feasible_finals"]

    stats = {
        "states_discovered": visited_count,
        "feasible_final_states": len(finals),
        "package_count": len(manifest.packages),
        "state_space_upper_bound": 1 << len(manifest.packages),
        "search_capped": False,  # 永远为 False：无探测上限
    }

    if not finals:
        report = {
            "report_kind": "unreachable",
            "schema_version": PLAN_SCHEMA_VERSION,
            "input_hash": input_hash,
            "old_state": sorted(manifest.active_ids(manifest.old_state)),
            "conclusion": "no_reachable_target",
            "explanation": (
                "从旧状态出发的全部合法状态均无法同时满足依赖、冲突、禁用、"
                "必保能力与各项目必需能力；已完整穷举可达状态空间，未使用探测上限。"
            ),
            "search_stats": stats,
        }
        report["report_hash"] = sha256_hex(_hash_payload(report))
        return report

    # 三级目标择优：(未申报能力数, 总成本, 排序包 ID 序列)
    best = min(finals, key=manifest.objective)
    path = reconstruct_path(manifest, search, best)

    # 重放路径，记录每一步前后状态哈希，供 verify/rollout 独立复核
    steps: list[dict[str, Any]] = []
    cursor = manifest.old_state
    for seq, operation in enumerate(path):
        before = cursor
        after = manifest.apply_operation(before, operation)
        steps.append(
            {
                "seq": seq,
                "operation": operation,
                "operation_key": manifest.operation_key(operation),
                "before_hash": manifest.state_hash(before),
                "after_hash": manifest.state_hash(after),
                "before_packages": sorted(manifest.active_ids(before)),
                "after_packages": sorted(manifest.active_ids(after)),
            }
        )
        cursor = after

    objective = manifest.objective(best)
    plan = {
        "report_kind": "plan",
        "schema_version": PLAN_SCHEMA_VERSION,
        "input_hash": input_hash,
        "plan_id": input_hash[:12],
        "generation": _generation_id(input_hash),
        "old_state": sorted(manifest.active_ids(manifest.old_state)),
        "target_state": sorted(manifest.active_ids(best)),
        "objective": {
            "unrequested_count": objective[0],
            "total_cost": objective[1],
            "active_package_ids": list(objective[2]),
        },
        "step_count": len(steps),
        "steps": steps,
        "search_stats": stats,
        "final_summary": manifest.state_summary(best),
    }
    plan["plan_hash"] = sha256_hex(_hash_payload(plan))
    return plan


def _generation_id(input_hash: str) -> str:
    """代次标识：同一输入哈希派生，同 ID 异内容会被台账拒绝。"""
    return "gen-" + input_hash[:16]


def _hash_payload(doc: dict[str, Any]) -> str:
    """哈希时剔除摘要字段自身（plan_hash / report_hash）。"""
    clone = {k: v for k, v in doc.items() if k not in ("plan_hash", "report_hash")}
    return canonical_json(clone)


def verify_plan_hash(plan: dict[str, Any]) -> bool:
    """独立复核计划文档自身的防篡改哈希。"""
    digest = plan.get("plan_hash")
    if not isinstance(digest, str):
        return False
    return sha256_hex(_hash_payload(plan)) == digest


def verify_report_hash(report: dict[str, Any]) -> bool:
    digest = report.get("report_hash")
    if not isinstance(digest, str):
        return False
    return sha256_hex(_hash_payload(report)) == digest
