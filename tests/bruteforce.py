"""独立枚举交叉校验器（测试专用，刻意不导入 authplanner.solver）。

与生产求解器采用不同的数据结构（frozenset 状态、集合运算）和独立的
遍历实现，用于在小实例上交叉校验：
  1. 可达状态集合与可达终态集合；
  2. 最优终态的三级目标排序结果；
  3. 最短路径长度与字典序首选操作序列。
"""
from __future__ import annotations

from typing import Any


def _load(path: str) -> dict[str, Any]:
    import json

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class BruteForcePlanner:
    def __init__(self, data: dict[str, Any]):
        self.packages = {p["id"]: p for p in data["packages"]}
        self.projects = {p["id"]: p for p in data["projects"]}
        self.replacements = [(r["remove"], r["add"]) for r in data.get("replacements", [])]
        self.old = frozenset(data.get("old_state", []))
        mk: dict[str, set[tuple[str, str]]] = {pid: set() for pid in self.projects}
        for item in data.get("must_keep", []):
            mk[item["project"]].update(tuple(x) for x in item["capabilities"])
        self.must_keep = {k: frozenset(v) for k, v in mk.items()}

    # ------------------------------------------------------------ 约束（独立实现）
    def _granted(self, active: frozenset[str]) -> dict[str, set[tuple[str, str]]]:
        out: dict[str, set[tuple[str, str]]] = {pid: set() for pid in self.projects}
        for pkg_id in active:
            out[self.packages[pkg_id]["project"]].update(
                tuple(c) for c in self.packages[pkg_id]["capabilities"]
            )
        return out

    def invariant_ok(self, active: frozenset[str]) -> bool:
        for pkg_id in active:
            pkg = self.packages[pkg_id]
            if set(pkg.get("conflicts", [])) & active:
                return False
            for group in pkg.get("requires", []):
                if not (set(group) & active):
                    return False
        granted = self._granted(active)
        for pid, proj in self.projects.items():
            forbidden = {tuple(c) for c in proj.get("forbidden", [])}
            if granted[pid] & forbidden:
                return False
            if not set(self.must_keep[pid]).issubset(granted[pid]):
                return False
        return True

    def is_final(self, active: frozenset[str]) -> bool:
        if not self.invariant_ok(active):
            return False
        granted = self._granted(active)
        return all(
            {tuple(c) for c in proj["required"]}.issubset(granted[pid])
            for pid, proj in self.projects.items()
        )

    def _new_cycle(self, before: frozenset[str], added: str) -> bool:
        """仅判定包含 added 的新环（与生产代码同语义的独立实现）。"""
        active = before | {added}

        def neighbors(node: str) -> set[str]:
            nbs: set[str] = set()
            for group in self.packages[node].get("requires", []):
                nbs.update(group)
            return nbs & active

        stack = [added]
        seen = {added}
        while stack:
            node = stack.pop()
            for nb in neighbors(node):
                if nb == added and node != added:
                    return True
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        return False

    def moves(self, active: frozenset[str]) -> list[tuple[str, dict[str, str]]]:
        ops: list[tuple[str, dict[str, str]]] = []
        for pkg_id in sorted(self.packages):
            if pkg_id in active:
                ops.append((f"disable:{pkg_id}", {"type": "disable", "package": pkg_id}))
            else:
                ops.append((f"enable:{pkg_id}", {"type": "enable", "package": pkg_id}))
        for remove, add in sorted(self.replacements):
            if remove in active and add not in active:
                key = f"replace:{remove}->{add}"
                ops.append((key, {"type": "replace", "remove": remove, "add": add}))
        ops.sort(key=lambda item: item[0])
        return ops

    def _apply(self, active: frozenset[str], op: dict[str, str]):
        """返回 (后继状态, 是否单步新建依赖环)。"""
        if op["type"] == "enable":
            nxt = active | {op["package"]}
            added, before = op["package"], active
        elif op["type"] == "disable":
            return active - {op["package"]}, False
        else:
            nxt = (active - {op["remove"]}) | {op["add"]}
            added, before = op["add"], active - {op["remove"]}
        return nxt, self._new_cycle(before, added)

    # ------------------------------------------------------------ 穷举
    def explore(self) -> dict[str, Any]:
        """标准 FIFO BFS：父状态按发现顺序出队，出边按操作键排序，首见认领。"""
        from collections import deque

        parents: dict[frozenset[str], frozenset[str] | None] = {self.old: None}
        parent_key: dict[frozenset[str], str] = {}
        depth = {self.old: 0}
        queue = deque([self.old])
        while queue:
            state = queue.popleft()
            for key, op in self.moves(state):
                nxt, cycle = self._apply(state, op)
                if cycle or nxt in parents:
                    continue
                if not self.invariant_ok(nxt):
                    continue
                parents[nxt] = state
                parent_key[nxt] = key
                depth[nxt] = depth[state] + 1
                queue.append(nxt)

        finals = {s for s in parents if self.is_final(s)}
        return {
            "reachable": set(parents),
            "finals": finals,
            "parents": parents,
            "parent_key": parent_key,
            "depth": depth,
        }

    def objective(self, active: frozenset[str]) -> tuple[int, int, tuple[str, ...]]:
        granted_triples: set[tuple[str, str, str]] = set()
        requested: set[tuple[str, str, str]] = set()
        cost = 0
        for pkg_id in active:
            pkg = self.packages[pkg_id]
            cost += pkg["cost"]
            for scope, cap in pkg["capabilities"]:
                granted_triples.add((pkg["project"], scope, cap))
        for pid, proj in self.projects.items():
            for scope, cap in proj["required"]:
                requested.add((pid, scope, cap))
        return (
            len(granted_triples - requested),
            cost,
            tuple(sorted(active)),
        )

    def best_plan(self) -> dict[str, Any]:
        info = self.explore()
        if not info["finals"]:
            return {
                "reachable_count": len(info["reachable"]),
                "finals": set(),
                "best": None,
                "path_keys": None,
            }
        best = min(info["finals"], key=self.objective)
        keys: list[str] = []
        cursor = best
        while cursor != self.old:
            keys.append(info["parent_key"][cursor])
            cursor = info["parents"][cursor]
        keys.reverse()
        return {
            "reachable_count": len(info["reachable"]),
            "finals": info["finals"],
            "best": best,
            "objective": self.objective(best),
            "path_keys": keys,
        }


def brute_force_file(path: str) -> dict[str, Any]:
    return BruteForcePlanner(_load(path)).best_plan()
