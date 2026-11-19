"""verify 命令的核心：独立重放计划并核对全部约束。

不直接信任求解产物：从输入清单重新构造 Manifest，逐步应用操作并
独立检查每一步的结构、过渡不变量与依赖环规则，最后判断终态全部约束。
"""
from __future__ import annotations

from typing import Any

from .model import Manifest
from .solver import (
    enumerate_reachable,
    reconstruct_path,
    verify_plan_hash,
    verify_report_hash,
)


def verify_document(
    manifest: Manifest,
    document: dict[str, Any],
    *,
    check_optimality: bool = False,
) -> dict[str, Any]:
    """核对一份 plan 或 unreachable 报告，返回结构化核对结论。"""
    kind = document.get("report_kind")
    if kind == "unreachable":
        return _verify_unreachable(manifest, document, check_optimality)
    if kind == "plan":
        return _verify_plan(manifest, document, check_optimality)
    return {
        "ok": False,
        "report_kind": str(kind),
        "errors": [f"无法识别的 report_kind: {kind!r}"],
    }


def _verify_plan(
    manifest: Manifest, plan: dict[str, Any], check_optimality: bool
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if not verify_plan_hash(plan):
        errors.append("plan_hash 不匹配，文档内容可能被篡改")

    if plan.get("old_state") != sorted(manifest.active_ids(manifest.old_state)):
        errors.append(
            f"plan.old_state={plan.get('old_state')} 与输入清单旧状态 "
            f"{sorted(manifest.active_ids(manifest.old_state))} 不一致"
        )

    steps = plan.get("steps")
    if not isinstance(steps, list):
        errors.append("plan.steps 必须是列表")
        steps = []

    expected_seq = 0
    cursor = manifest.old_state
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            errors.append(f"第 {expected_seq} 步不是对象")
            break
        seq = raw_step.get("seq")
        if seq != expected_seq:
            errors.append(f"步骤序号不连续：期望 {expected_seq}，实际 {seq}")
        operation = raw_step.get("operation")
        if not isinstance(operation, dict):
            errors.append(f"第 {expected_seq} 步缺少 operation 对象")
            break

        before = cursor
        after = manifest.apply_operation(before, operation)
        step_errors = manifest.transition_violations(before, operation, after)
        for msg in step_errors:
            errors.append(f"第 {seq} 步非法（{manifest.operation_key(operation)}）：{msg}")

        # 与计划自带的前后摘要逐一核对
        if raw_step.get("before_hash") != manifest.state_hash(before):
            errors.append(f"第 {seq} 步 before_hash 与重放结果不符")
        if raw_step.get("after_hash") != manifest.state_hash(after):
            errors.append(f"第 {seq} 步 after_hash 与重放结果不符")
        if raw_step.get("before_packages") != sorted(manifest.active_ids(before)):
            errors.append(f"第 {seq} 步 before_packages 与重放结果不符")
        if raw_step.get("after_packages") != sorted(manifest.active_ids(after)):
            errors.append(f"第 {seq} 步 after_packages 与重放结果不符")
        if raw_step.get("operation_key") != manifest.operation_key(operation):
            errors.append(f"第 {seq} 步 operation_key 不规范")

        cursor = after
        expected_seq += 1

    # 终态全部约束
    final_violations = manifest.invariant_violations(cursor)
    for msg in final_violations:
        errors.append(f"终态违反约束：{msg}")
    for pi, proj in enumerate(manifest.projects):
        granted = manifest.project_granted_mask(cursor, pi)
        missing = manifest.required_mask[pi] & ~granted
        if missing:
            errors.append(
                f"终态项目 {proj.project_id} 必需能力未覆盖: "
                f"{manifest._mask_to_pairs(pi, missing)}"
            )

    if plan.get("target_state") != sorted(manifest.active_ids(cursor)):
        errors.append("plan.target_state 与逐步重放到达的状态不一致")

    objective = manifest.objective(cursor)
    claimed_obj = plan.get("objective") or {}
    if claimed_obj.get("unrequested_count") != objective[0]:
        errors.append("objective.unrequested_count 与重放计算不符")
    if claimed_obj.get("total_cost") != objective[1]:
        errors.append("objective.total_cost 与重放计算不符")
    if claimed_obj.get("active_package_ids") != list(objective[2]):
        errors.append("objective.active_package_ids 与重放计算不符")
    if plan.get("step_count") != len(steps):
        errors.append("step_count 与实际步骤数不符")

    # 可选：独立重跑完整穷举，确认最优性与最少步数/字典序路径
    optimality: dict[str, Any] | None = None
    if check_optimality:
        search = enumerate_reachable(manifest)
        finals = search["feasible_finals"]
        if not finals:
            errors.append("最优性复核：重跑穷举未发现任何可达终态，与计划存在矛盾")
        else:
            best = min(finals, key=manifest.objective)
            if best != cursor:
                errors.append(
                    "最优性复核：存在更优可达终态 "
                    f"{sorted(manifest.active_ids(best))}，目标值 {manifest.objective(best)}"
                )
            canonical_path = reconstruct_path(manifest, search, best)
            claimed_keys = [
                s.get("operation_key") for s in steps if isinstance(s, dict)
            ]
            canonical_keys = [manifest.operation_key(op) for op in canonical_path]
            if claimed_keys != canonical_keys:
                errors.append(
                    "最优性复核：路径不是同最优解下的最少步数/字典序首选路径；"
                    f"规范路径为 {canonical_keys}"
                )
            optimality = {
                "rechecked": True,
                "states_discovered": len(search["parents"]),
                "canonical_operation_keys": canonical_keys,
            }
    else:
        warnings.append("未启用 --check-optimality，仅核对计划合法性，未独立复核最优性")

    return {
        "ok": not errors,
        "report_kind": "plan",
        "plan_id": plan.get("plan_id"),
        "generation": plan.get("generation"),
        "replayed_steps": len(steps),
        "final_state": sorted(manifest.active_ids(cursor)),
        "final_summary": manifest.state_summary(cursor),
        "optimality": optimality,
        "warnings": warnings,
        "errors": errors,
    }


def _verify_unreachable(
    manifest: Manifest, report: dict[str, Any], check_optimality: bool
) -> dict[str, Any]:
    errors: list[str] = []
    if not verify_report_hash(report):
        errors.append("report_hash 不匹配，文档内容可能被篡改")
    if report.get("conclusion") != "no_reachable_target":
        errors.append("unreachable 报告缺少标准结论 no_reachable_target")

    # 不可达结论必须可独立复核：重跑完整穷举
    search = enumerate_reachable(manifest)
    if search["feasible_finals"]:
        errors.append(
            "不可达结论不成立：重跑穷举发现可达终态 "
            f"{[sorted(manifest.active_ids(s)) for s in search['feasible_finals']]}"
        )
    claimed_stats = report.get("search_stats") or {}
    if claimed_stats.get("states_discovered") != len(search["parents"]):
        errors.append(
            f"状态数声明 {claimed_stats.get('states_discovered')} "
            f"与独立重跑 {len(search['parents'])} 不符"
        )

    return {
        "ok": not errors,
        "report_kind": "unreachable",
        "rechecked_states": len(search["parents"]),
        "reachable_feasible_finals": len(search["feasible_finals"]),
        "errors": errors,
    }
