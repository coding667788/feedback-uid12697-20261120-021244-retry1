"""领域模型：输入校验、位掩码预计算、状态不变量与单步转移。

状态用一个整数位掩码表示：第 i 位为 1 表示第 i 个候选授权包处于启用状态。
所有“虚构能力标识”只作为字符串数据参与集合运算，不触碰任何真实系统。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_PACKAGES = 24
MAX_PROJECTS = 64


class ModelError(ValueError):
    """输入清单结构或语义非法。"""


class LedgerError(RuntimeError):
    """台账、代次或状态校验失败（供 ledger 模块复用同一异常体系）。"""


@dataclass(frozen=True)
class Package:
    pkg_id: str
    project_id: str
    capabilities: frozenset[tuple[str, str]]  # (scope, capability)
    cost: int
    conflicts: tuple[str, ...]
    # 依赖：外层列表为 AND，内层候选列表为 OR（任一候选前置包满足即可）
    requires: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Project:
    project_id: str
    required: frozenset[tuple[str, str]]
    forbidden: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class Replacement:
    """输入显式允许的一对原子替换：停用 remove、同时启用 add。"""

    remove: str
    add: str
    key: str  # 公开操作字典序所用的规范键


def _as_str_list(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ModelError(f"{context} 必须是字符串列表")
    return list(value)


def _as_cap_pairs(value: Any, context: str) -> frozenset[tuple[str, str]]:
    if not isinstance(value, list):
        raise ModelError(f"{context} 必须是 (scope, capability) 对的列表")
    pairs: set[tuple[str, str]] = set()
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(x, str) for x in item)
        ):
            raise ModelError(f"{context} 中的每一项必须是形如 [scope, capability] 的两字符串列表")
        pairs.add((item[0], item[1]))
    return frozenset(pairs)


@dataclass
class Manifest:
    packages: tuple[Package, ...]
    projects: tuple[Project, ...]
    old_state: int
    replacements: tuple[Replacement, ...]
    notes: tuple[str, ...] = field(default=())

    # —— 预计算索引 ——
    index: dict[str, int] = field(default_factory=dict)
    project_index: dict[str, int] = field(default_factory=dict)
    conflict_mask: tuple[int, ...] = ()
    require_masks: tuple[tuple[int, ...], ...] = ()
    # 包在“启用集”中的依赖出边：所有 OR 组里候选包的并集
    dep_neighbor_mask: tuple[int, ...] = ()
    cover_mask: tuple[int, ...] = ()          # 每个包在所属项目的覆盖位
    forbidden_mask: tuple[int, ...] = ()      # 每个包是否触碰所属项目禁用对
    required_mask: tuple[int, ...] = ()       # 每个项目的必需覆盖位
    mustkeep_mask: tuple[int, ...] = ()       # 每个项目的必保覆盖位
    pkg_project: tuple[int, ...] = ()
    # 每个项目 (scope,capability) -> 能力位编号（预计算阶段固定，禁止重建改序）
    cap_lookups: tuple[dict[tuple[str, str], int], ...] = ()

    # ------------------------------------------------------------------ 构造
    @staticmethod
    def from_dict(data: Any) -> "Manifest":
        if not isinstance(data, dict):
            raise ModelError("输入根节点必须是 JSON 对象")

        raw_packages = data.get("packages")
        if not isinstance(raw_packages, list):
            raise ModelError("packages 必须是列表")
        if not (1 <= len(raw_packages) <= MAX_PACKAGES):
            raise ModelError(f"候选授权包数量必须在 1..{MAX_PACKAGES} 之间，实际 {len(raw_packages) if isinstance(raw_packages, list) else 'N/A'}")

        errors: list[str] = []
        packages: list[Package] = []
        seen_ids: set[str] = set()
        for pos, raw in enumerate(raw_packages):
            ctx = f"packages[{pos}]"
            if not isinstance(raw, dict):
                errors.append(f"{ctx} 必须是对象")
                continue
            pkg_id = raw.get("id")
            if not isinstance(pkg_id, str) or not pkg_id:
                errors.append(f"{ctx}.id 必须是非空字符串")
                continue
            if pkg_id in seen_ids:
                errors.append(f"包 ID 重复: {pkg_id}")
                continue
            seen_ids.add(pkg_id)
            project_id = raw.get("project")
            if not isinstance(project_id, str) or not project_id:
                errors.append(f"{ctx}.project 必须是非空字符串")
                project_id = project_id if isinstance(project_id, str) else f"__invalid_{pos}"
            caps = _safe_caps(raw.get("capabilities", []), f"{ctx}.capabilities", errors)
            cost = raw.get("cost", 0)
            if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
                errors.append(f"{ctx}.cost 必须是非负整数")
                cost = cost if isinstance(cost, int) and not isinstance(cost, bool) else 0
            conflicts = tuple(_as_str_list(raw.get("conflicts", []), f"{ctx}.conflicts"))
            requires = _safe_requires(raw.get("requires", []), f"{ctx}.requires", errors)
            packages.append(
                Package(pkg_id, project_id, caps, cost, conflicts, requires)
            )

        raw_projects = data.get("projects")
        if not isinstance(raw_projects, list) or not raw_projects:
            raise ModelError("projects 必须是非空列表")
        projects: list[Project] = []
        seen_projects: set[str] = set()
        for pos, raw in enumerate(raw_projects):
            ctx = f"projects[{pos}]"
            if not isinstance(raw, dict):
                errors.append(f"{ctx} 必须是对象")
                continue
            project_id = raw.get("id")
            if not isinstance(project_id, str) or not project_id:
                errors.append(f"{ctx}.id 必须是非空字符串")
                continue
            if project_id in seen_projects:
                errors.append(f"项目 ID 重复: {project_id}")
                continue
            seen_projects.add(project_id)
            required = _safe_caps(raw.get("required", []), f"{ctx}.required", errors)
            forbidden = _safe_caps(raw.get("forbidden", []), f"{ctx}.forbidden", errors)
            inter = required & forbidden
            if inter:
                errors.append(f"项目 {project_id} 的必需能力与禁用能力重叠: {sorted(inter)}")
            projects.append(Project(project_id, required, forbidden))

        if errors:
            raise ModelError("; ".join(errors))

        index = {pkg.pkg_id: i for i, pkg in enumerate(packages)}
        project_index = {proj.project_id: i for i, proj in enumerate(projects)}
        n = len(packages)
        pcount = len(projects)

        # 跨引用校验
        ref_errors: list[str] = []
        for pkg in packages:
            if pkg.project_id not in project_index:
                ref_errors.append(f"包 {pkg.pkg_id} 引用了未知项目 {pkg.project_id}")
            for other in pkg.conflicts:
                if other not in index:
                    ref_errors.append(f"包 {pkg.pkg_id} 冲突引用未知包 {other}")
                if other == pkg.pkg_id:
                    ref_errors.append(f"包 {pkg.pkg_id} 不能与自身冲突")
            for group in pkg.requires:
                for candidate in group:
                    if candidate not in index:
                        ref_errors.append(f"包 {pkg.pkg_id} 依赖引用未知包 {candidate}")
                    if candidate == pkg.pkg_id:
                        ref_errors.append(f"包 {pkg.pkg_id} 不能依赖自身")
                if not group:
                    ref_errors.append(f"包 {pkg.pkg_id} 存在空的依赖候选组")

        # 原子替换
        replacements: list[Replacement] = []
        seen_repl: set[tuple[str, str]] = set()
        for pos, raw in enumerate(data.get("replacements", [])):
            ctx = f"replacements[{pos}]"
            if not isinstance(raw, dict):
                ref_errors.append(f"{ctx} 必须是对象")
                continue
            remove, add = raw.get("remove"), raw.get("add")
            if not isinstance(remove, str) or not isinstance(add, str):
                ref_errors.append(f"{ctx} 的 remove/add 必须是字符串")
                continue
            if remove not in index or add not in index:
                ref_errors.append(f"{ctx} 引用了未知包: {remove} -> {add}")
                continue
            if remove == add:
                ref_errors.append(f"{ctx} 不能替换为自身")
                continue
            pair = (remove, add)
            if pair in seen_repl:
                ref_errors.append(f"原子替换重复声明: {remove} -> {add}")
                continue
            seen_repl.add(pair)
            replacements.append(
                Replacement(remove, add, f"replace:{remove}->{add}")
            )

        # 旧状态与必保能力
        old_ids = _as_str_list(data.get("old_state", []), "old_state")
        old_state = 0
        for pkg_id in old_ids:
            if pkg_id not in index:
                ref_errors.append(f"old_state 引用未知包 {pkg_id}")
            else:
                bit = 1 << index[pkg_id]
                if old_state & bit:
                    ref_errors.append(f"old_state 中包 {pkg_id} 重复")
                old_state |= bit

        mustkeep: list[frozenset[tuple[str, str]]] = [frozenset() for _ in projects]
        for pos, raw in enumerate(data.get("must_keep", [])):
            ctx = f"must_keep[{pos}]"
            if not isinstance(raw, dict):
                ref_errors.append(f"{ctx} 必须是对象")
                continue
            project_id = raw.get("project")
            if project_id not in project_index:
                ref_errors.append(f"{ctx} 引用未知项目 {project_id}")
                continue
            caps = _safe_caps(raw.get("capabilities", []), f"{ctx}.capabilities", ref_errors)
            mustkeep[project_index[project_id]] |= caps

        if ref_errors:
            raise ModelError("; ".join(ref_errors))

        manifest = Manifest(
            packages=tuple(packages),
            projects=tuple(projects),
            old_state=old_state,
            replacements=tuple(replacements),
            notes=tuple(data.get("notes", [])),
        )
        manifest.index = index
        manifest.project_index = project_index
        manifest._precompute(mustkeep)
        manifest._validate_old_state()
        return manifest

    # ------------------------------------------------------------------ 预计算
    def _precompute(self, mustkeep: list[frozenset[tuple[str, str]]]) -> None:
        n = len(self.packages)
        pcount = len(self.projects)

        # 冲突位掩码（双向归一化）
        conflict_mask = [0] * n
        for i, pkg in enumerate(self.packages):
            for other in pkg.conflicts:
                conflict_mask[i] |= 1 << self.index[other]
        for i in range(n):
            bits = conflict_mask[i]
            while bits:
                b = bits & -bits
                j = b.bit_length() - 1
                conflict_mask[j] |= 1 << i
                bits -= b
        self.conflict_mask = tuple(conflict_mask)

        # 依赖
        require_masks: list[tuple[int, ...]] = []
        dep_neighbor_mask = [0] * n
        for i, pkg in enumerate(self.packages):
            groups: list[int] = []
            for group in pkg.requires:
                mask = 0
                for candidate in group:
                    j = self.index[candidate]
                    mask |= 1 << j
                    dep_neighbor_mask[i] |= 1 << j
                groups.append(mask)
            require_masks.append(tuple(groups))
        self.require_masks = tuple(require_masks)
        self.dep_neighbor_mask = tuple(dep_neighbor_mask)
        self.pkg_project = tuple(self.project_index[pkg.project_id] for pkg in self.packages)

        # 每个项目内部的能力位编号（顺序固定：required ∪ forbidden ∪ mustkeep，
        # 再并入各包覆盖的能力；frozenset 迭代顺序在同一进程内确定，
        # 但位编号只在本次加载内使用，不跨进程持久化，故无兼容性问题）
        cap_lookups: list[dict[tuple[str, str], int]] = [dict() for _ in self.projects]
        for pi, proj in enumerate(self.projects):
            for cap in proj.required | proj.forbidden | mustkeep[pi]:
                cap_lookups[pi].setdefault(cap, len(cap_lookups[pi]))
        for pkg in self.packages:
            pi = self.project_index[pkg.project_id]
            for cap in pkg.capabilities:
                cap_lookups[pi].setdefault(cap, len(cap_lookups[pi]))
        self.cap_lookups = tuple(cap_lookups)

        cover_mask = [0] * n
        forbidden_mask = [0] * n
        for i, pkg in enumerate(self.packages):
            pi = self.project_index[pkg.project_id]
            proj = self.projects[pi]
            mask = 0
            fmask = 0
            for cap in pkg.capabilities:
                bit = 1 << cap_lookups[pi][cap]
                mask |= bit
                if cap in proj.forbidden:
                    fmask |= bit
            cover_mask[i] = mask
            forbidden_mask[i] = fmask
        self.cover_mask = tuple(cover_mask)
        self.forbidden_mask = tuple(forbidden_mask)

        required_mask = [0] * pcount
        mustkeep_mask = [0] * pcount
        for pi, proj in enumerate(self.projects):
            for cap in proj.required:
                required_mask[pi] |= 1 << cap_lookups[pi][cap]
            for cap in mustkeep[pi]:
                mustkeep_mask[pi] |= 1 << cap_lookups[pi][cap]
        self.required_mask = tuple(required_mask)
        self.mustkeep_mask = tuple(mustkeep_mask)

        # 每个项目“全部启用包可能触碰的禁用位”并集（热路径预计算）
        forbidden_bits = [0] * pcount
        for i, pkg in enumerate(self.packages):
            forbidden_bits[self.pkg_project[i]] |= forbidden_mask[i]
        self._forbidden_bits = tuple(forbidden_bits)

        # 项目 -> 属于该项目的包位掩码
        project_pkg_bits = [0] * pcount
        for i in range(n):
            project_pkg_bits[self.pkg_project[i]] |= 1 << i
        self._project_pkg_bits = tuple(project_pkg_bits)

        # 原子替换包对集合（热路径 O(1) 判定）
        self._replacement_pairs = frozenset(
            (repl.remove, repl.add) for repl in self.replacements
        )
        # 按包 ID 字典序排列的包索引（保证 BFS 出边序与操作键字典序一致）
        self._id_order = tuple(
            sorted(range(n), key=lambda i: self.packages[i].pkg_id)
        )
        # 预计算操作键，BFS 存父操作时复用同一字符串对象（驻留，节省内存）
        self._enable_keys = tuple(
            "enable:" + self.packages[i].pkg_id for i in range(n)
        )
        self._disable_keys = tuple(
            "disable:" + self.packages[i].pkg_id for i in range(n)
        )
        # 原子替换按操作键排序，预解析为包索引
        self._repl_sorted = tuple(
            sorted(
                (
                    (
                        self.index[repl.remove],
                        self.index[repl.add],
                        repl.key,
                    )
                    for repl in self.replacements
                ),
                key=lambda item: item[2],
            )
        )

    def _validate_old_state(self) -> None:
        """旧状态是“当前正在运行”的真实状态，必须满足全部过渡不变量。

        依赖环允许在旧状态中已经存在（历史包袱），因此环检测不用于旧状态校验。
        """
        violations = self.invariant_violations(self.old_state)
        if violations:
            raise ModelError("旧状态不满足过渡不变量: " + "; ".join(violations))

    # ------------------------------------------------------------------ 状态查询
    def active_ids(self, state: int) -> list[str]:
        return [self.packages[i].pkg_id for i in range(len(self.packages)) if state & (1 << i)]

    def project_active_mask(self, state: int, project: int) -> int:
        return state & self._project_pkg_bits[project]

    def project_granted_mask(self, state: int, project: int) -> int:
        granted = 0
        bits = state & self._project_pkg_bits[project]
        while bits:
            b = bits & -bits
            granted |= self.cover_mask[b.bit_length() - 1]
            bits -= b
        return granted

    def granted_triples(self, state: int) -> set[tuple[str, str, str]]:
        """当前状态授予的全部 (project, scope, capability) 三元组（已去重）。"""
        out: set[tuple[str, str, str]] = set()
        bits = state
        while bits:
            b = bits & -bits
            i = b.bit_length() - 1
            pkg = self.packages[i]
            for scope, cap in pkg.capabilities:
                out.add((pkg.project_id, scope, cap))
            bits -= b
        return out

    def requested_triples(self) -> set[tuple[str, str, str]]:
        """各项目申报（required）的全部能力三元组。"""
        out: set[tuple[str, str, str]] = set()
        for proj in self.projects:
            for scope, cap in proj.required:
                out.add((proj.project_id, scope, cap))
        return out

    def unrequested_count(self, state: int) -> int:
        """目标函数第一项：授予但未申报的能力数，按三元组去重。"""
        return len(self.granted_triples(state) - self.requested_triples())

    def total_cost(self, state: int) -> int:
        total = 0
        bits = state
        while bits:
            b = bits & -bits
            total += self.packages[b.bit_length() - 1].cost
            bits -= b
        return total

    def objective(self, state: int) -> tuple[int, int, tuple[str, ...]]:
        """三级目标：未申报能力数 → 总成本 → 排序后包 ID 序列。"""
        return (
            self.unrequested_count(state),
            self.total_cost(state),
            tuple(sorted(self.active_ids(state))),
        )

    def state_summary(self, state: int) -> dict[str, Any]:
        """台账与输出使用的前后摘要（确定性、无时间戳）。"""
        per_project: dict[str, dict[str, Any]] = {}
        for pi, proj in enumerate(self.projects):
            granted_mask = self.project_granted_mask(state, pi)
            per_project[proj.project_id] = {
                "active_packages": sorted(
                    self.packages[i].pkg_id
                    for i in range(len(self.packages))
                    if (state & (1 << i)) and self.pkg_project[i] == pi
                ),
                "granted_pairs": sorted(self._mask_to_pairs(pi, granted_mask)),
                "required_covered": (granted_mask & self.required_mask[pi]) == self.required_mask[pi],
                "mustkeep_covered": (granted_mask & self.mustkeep_mask[pi]) == self.mustkeep_mask[pi],
                "forbidden_violated": bool(granted_mask & self._forbidden_bits[pi]),
            }
        return {
            "state_hash": self.state_hash(state),
            "active_packages": sorted(self.active_ids(state)),
            "unrequested_count": self.unrequested_count(state),
            "total_cost": self.total_cost(state),
            "projects": per_project,
        }

    def _mask_to_pairs(self, project: int, mask: int) -> list[tuple[str, str]]:
        inv = {v: k for k, v in self.cap_lookups[project].items()}
        out: list[tuple[str, str]] = []
        while mask:
            b = mask & -mask
            bit_index = b.bit_length() - 1
            if bit_index in inv:
                out.append(inv[bit_index])
            mask -= b
        return sorted(out)

    def state_hash(self, state: int) -> str:
        from .util import sha256_hex

        return sha256_hex({"active_packages": sorted(self.active_ids(state))})[:16]

    # ------------------------------------------------------------------ 不变量
    def invariants_hold(self, state: int) -> bool:
        """BFS 热路径：四类过渡不变量的纯位掩码布尔判定，不构造任何字符串。"""
        # 1. 冲突：任一启用包的冲突位与启用集相交
        bits = state
        while bits:
            b = bits & -bits
            if state & self.conflict_mask[b.bit_length() - 1]:
                return False
            bits -= b
        # 2. 依赖：每个启用包的每个 OR 组至少有一个前置启用
        bits = state
        while bits:
            b = bits & -bits
            for group_mask in self.require_masks[b.bit_length() - 1]:
                if not (state & group_mask):
                    return False
            bits -= b
        # 3+4. 逐项目：禁用规则与必保能力
        for pi in range(len(self.projects)):
            granted = self.project_granted_mask(state, pi)
            if granted & self._forbidden_bits[pi]:
                return False
            if self.mustkeep_mask[pi] & ~granted:
                return False
        return True

    def invariant_violations(self, state: int) -> list[str]:
        """过渡状态必须始终满足的四类不变量（不含必需能力，那是终态要求）。

        1. 包冲突；2. 依赖（OR 组至少一个前置启用）；
        3. 禁用规则（包覆盖的禁用对不可分割剔除，整包禁用）；
        4. 必保能力（每个项目过渡期持续覆盖）。
        """
        violations: list[str] = []

        # 1. 冲突
        bits = state
        while bits:
            b = bits & -bits
            i = b.bit_length() - 1
            if state & self.conflict_mask[i]:
                others = [
                    self.packages[j].pkg_id
                    for j in range(len(self.packages))
                    if (state & (1 << j)) and (self.conflict_mask[i] & (1 << j))
                ]
                violations.append(f"包 {self.packages[i].pkg_id} 与启用中的包冲突: {sorted(others)}")
            bits -= b

        # 2. 依赖
        bits = state
        while bits:
            b = bits & -bits
            i = b.bit_length() - 1
            for group_mask in self.require_masks[i]:
                if not (state & group_mask):
                    candidates = [
                        self.packages[j].pkg_id
                        for j in range(len(self.packages))
                        if group_mask & (1 << j)
                    ]
                    violations.append(
                        f"包 {self.packages[i].pkg_id} 的依赖组未满足，需前置之一: {sorted(candidates)}"
                    )
            bits -= b

        # 3. 禁用规则
        for pi, proj in enumerate(self.projects):
            granted = self.project_granted_mask(state, pi)
            if granted & self._forbidden_bits[pi]:
                bad_pairs = self._mask_to_pairs(pi, granted & self._forbidden_bits[pi])
                violations.append(f"项目 {proj.project_id} 授予了绝对禁止的能力对: {bad_pairs}")

        # 4. 必保能力
        for pi, proj in enumerate(self.projects):
            granted = self.project_granted_mask(state, pi)
            missing = self.mustkeep_mask[pi] & ~granted
            if missing:
                violations.append(
                    f"项目 {proj.project_id} 过渡期必保能力缺失: {self._mask_to_pairs(pi, missing)}"
                )

        # 去重（冲突会双向报告）
        return sorted(set(violations))

    def is_feasible_final(self, state: int) -> bool:
        """终态判定：四类过渡不变量 + 每个项目必需能力全覆盖。"""
        if not self.invariants_hold(state):
            return False
        for pi in range(len(self.projects)):
            granted = self.project_granted_mask(state, pi)
            if (granted & self.required_mask[pi]) != self.required_mask[pi]:
                return False
        return True

    # ------------------------------------------------------------------ 依赖环
    def introduces_new_cycle(self, before: int, added_bit: int) -> bool:
        """判断在 before 上新增 added_bit（单个包）是否会产生“新建”的依赖环。

        规则（对应夹具“依赖循环可在旧态已存在但无法单步新建”）：
        - 只统计包含新增包的环；
        - 环上的其他包必须全部在 before 中已启用，即该环无法在新增之前就存在；
        - 已存在于旧状态的环属于历史包袱，永远不算新建。
        """
        start = added_bit.bit_length() - 1
        start_mask = 1 << start
        # 从新增包沿依赖出边 DFS，寻找回到新增包的路径，途经节点限启用集 ∪ {start}
        allowed = before | start_mask
        stack = [start]
        visited = 1 << start
        while stack:
            node = stack.pop()
            neighbors = self.dep_neighbor_mask[node] & allowed
            if neighbors & start_mask and node != start:
                return True
            unseen = neighbors & ~visited
            while unseen:
                b = unseen & -unseen
                visited |= b
                stack.append(b.bit_length() - 1)
                unseen -= b
        return False

    # ------------------------------------------------------------------ 单步转移
    def apply_operation(self, state: int, operation: dict[str, str]) -> int:
        """按操作字典应用单步变更，不做合法性校验（校验在 transition_violations）。"""
        kind = operation["type"]
        if kind == "enable":
            return state | (1 << self.index[operation["package"]])
        if kind == "disable":
            return state & ~(1 << self.index[operation["package"]])
        if kind == "replace":
            out = state & ~(1 << self.index[operation["remove"]])
            return out | (1 << self.index[operation["add"]])
        raise ModelError(f"未知操作类型: {kind}")

    def operation_key(self, operation: dict[str, str]) -> str:
        """公开规定的操作规范键，同时作为字典序比较与幂等键的基础。"""
        kind = operation["type"]
        if kind == "enable":
            return f"enable:{operation['package']}"
        if kind == "disable":
            return f"disable:{operation['package']}"
        if kind == "replace":
            return f"replace:{operation['remove']}->{operation['add']}"
        raise ModelError(f"未知操作类型: {kind}")

    def legal_operations(self, state: int) -> list[dict[str, str]]:
        """按公开字典序枚举当前状态下结构上可应用的全部单步操作。

        排序规则（README 公开）：先按操作键字符串字典序，
        即 disable:* < enable:* < replace:*，同类型内按包 ID 排序。
        操作的“合法性”（不变量、新建环）由 transition_violations 判定。
        """
        ops: list[dict[str, str]] = []
        n = len(self.packages)
        for i in range(n):
            bit = 1 << i
            pkg_id = self.packages[i].pkg_id
            if state & bit:
                ops.append({"type": "disable", "package": pkg_id})
            else:
                ops.append({"type": "enable", "package": pkg_id})
        for repl in self.replacements:
            remove_bit = 1 << self.index[repl.remove]
            add_bit = 1 << self.index[repl.add]
            if (state & remove_bit) and not (state & add_bit):
                ops.append({"type": "replace", "remove": repl.remove, "add": repl.add})
        ops.sort(key=self.operation_key)
        return ops

    def transition_violations(
        self, before: int, operation: dict[str, str], after: int
    ) -> list[str]:
        """校验单步转移：结构合法性、过渡不变量、禁止单步新建依赖环。"""
        kind = operation["type"]
        violations: list[str] = []
        n = len(self.packages)

        if kind == "enable":
            pkg_id = operation.get("package", "")
            if pkg_id not in self.index:
                return [f"enable 引用未知包 {pkg_id}"]
            bit = 1 << self.index[pkg_id]
            if before & bit:
                violations.append(f"enable 的包 {pkg_id} 已处于启用状态")
            if after != (before | bit):
                violations.append("enable 操作前后状态不一致")
            if not violations and self.introduces_new_cycle(before, bit):
                violations.append(f"启用 {pkg_id} 将单步新建依赖环（历史已存在的环除外）")
        elif kind == "disable":
            pkg_id = operation.get("package", "")
            if pkg_id not in self.index:
                return [f"disable 引用未知包 {pkg_id}"]
            bit = 1 << self.index[pkg_id]
            if not (before & bit):
                violations.append(f"disable 的包 {pkg_id} 已处于停用状态")
            if after != (before & ~bit):
                violations.append("disable 操作前后状态不一致")
        elif kind == "replace":
            remove, add = operation.get("remove", ""), operation.get("add", "")
            if remove not in self.index or add not in self.index:
                return [f"replace 引用未知包: {remove} -> {add}"]
            allowed = self._replacement_pairs
            if (remove, add) not in allowed:
                violations.append(f"原子替换未在输入 replacements 中声明: {remove} -> {add}")
            rb, ab = 1 << self.index[remove], 1 << self.index[add]
            if not (before & rb):
                violations.append(f"原子替换要求 {remove} 处于启用状态")
            if before & ab:
                violations.append(f"原子替换要求 {add} 处于停用状态")
            expected = (before & ~rb) | ab
            if after != expected:
                violations.append("replace 操作前后状态不一致")
            if not violations and self.introduces_new_cycle(before & ~rb, ab):
                violations.append(f"原子替换启用 {add} 将单步新建依赖环（历史已存在的环除外）")
        else:
            return [f"未知操作类型: {kind}"]

        if not violations:
            inv = self.invariant_violations(after)
            violations.extend(inv)
        return violations


def _safe_caps(value: Any, context: str, errors: list[str]) -> frozenset[tuple[str, str]]:
    try:
        return _as_cap_pairs(value, context)
    except ModelError as exc:
        errors.append(str(exc))
        return frozenset()


def _safe_requires(value: Any, context: str, errors: list[str]) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        errors.append(f"{context} 必须是候选前置包列表的列表")
        return ()
    groups: list[tuple[str, ...]] = []
    for pos, group in enumerate(value):
        try:
            groups.append(tuple(_as_str_list(group, f"{context}[{pos}]")))
        except ModelError as exc:
            errors.append(str(exc))
    return tuple(groups)
