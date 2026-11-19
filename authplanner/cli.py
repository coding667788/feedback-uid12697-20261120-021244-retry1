"""命令行入口：solve / verify / rollout / status 四个子命令。

退出码约定（README 公开）：
  0 成功；1 用法或输入模型错误；2 文件不存在；
  3 求解完成但无可达目标；4 verify 核对失败；
  5 台账/代次/状态拒绝；6 受控异常故障注入触发。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .ledger import ControlledFault, Ledger
from .model import LedgerError, Manifest, ModelError
from .solver import solve
from .util import dumps_pretty, load_json_file, sha256_hex
from .verify import verify_document

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_MISSING = 2
EXIT_UNREACHABLE = 3
EXIT_VERIFY_FAILED = 4
EXIT_LEDGER_REJECTED = 5
EXIT_CONTROLLED_FAULT = 6


def _load_manifest(input_path: str) -> tuple[Manifest, str]:
    raw = load_json_file(input_path)
    input_hash = sha256_hex(raw)
    manifest = Manifest.from_dict(raw)
    return manifest, input_hash


def _write_output(path: str | None, text: str) -> None:
    if path:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def cmd_solve(args: argparse.Namespace) -> int:
    manifest, input_hash = _load_manifest(args.input)
    document = solve(manifest, input_hash)

    if args.dump_reachable:
        from .solver import enumerate_reachable

        search = enumerate_reachable(manifest)
        dump = {
            "input_hash": input_hash,
            "reachable_states": [
                {
                    "active_packages": sorted(manifest.active_ids(state)),
                    "is_feasible_final": manifest.is_feasible_final(state),
                    "depth": search["depth"][state],
                }
                for state in sorted(search["parents"])
            ],
        }
        with open(args.dump_reachable, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps_pretty(dump))

    _write_output(args.output, dumps_pretty(document))
    if document["report_kind"] == "unreachable":
        return EXIT_UNREACHABLE
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    manifest, input_hash = _load_manifest(args.input)
    document = load_json_file(args.document)
    if document.get("input_hash") != input_hash:
        result = {
            "ok": False,
            "errors": [
                f"文档 input_hash={document.get('input_hash')} 与输入清单哈希 {input_hash} 不一致"
            ],
        }
        _write_output(args.output, dumps_pretty(result))
        return EXIT_VERIFY_FAILED

    result = verify_document(
        manifest, document, check_optimality=args.check_optimality
    )
    _write_output(args.output, dumps_pretty(result))
    return EXIT_OK if result["ok"] else EXIT_VERIFY_FAILED


def _parse_faults(values: list[str]) -> dict[int, str]:
    faults: dict[int, str] = {}
    for value in values:
        if ":" not in value:
            raise SystemExit(f"--fault 格式应为 序号:exit|exception，收到 {value!r}")
        seq_text, mode = value.split(":", 1)
        if mode not in ("exit", "exception"):
            raise SystemExit(f"--fault 模式仅支持 exit/exception，收到 {mode!r}")
        try:
            seq = int(seq_text)
        except ValueError as exc:
            raise SystemExit(f"--fault 步骤序号必须是整数，收到 {seq_text!r}") from exc
        if seq < 0:
            raise SystemExit("--fault 步骤序号不能为负")
        faults[seq] = mode
    return faults


def cmd_rollout(args: argparse.Namespace) -> int:
    manifest, input_hash = _load_manifest(args.input)
    plan = load_json_file(args.plan)
    if plan.get("input_hash") != input_hash:
        raise LedgerError(
            f"计划 input_hash={plan.get('input_hash')} 与输入清单哈希 {input_hash} 不一致，拒绝继续"
        )
    faults = _parse_faults(args.fault)
    ledger = Ledger.open(args.ledger, manifest, plan)
    try:
        result = ledger.rollout(manifest, plan, faults)
    finally:
        ledger.close()
    _write_output(args.output, dumps_pretty(result))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    try:
        status = Ledger.read_status(args.ledger)
    except FileNotFoundError:
        msg = {"ok": False, "error": f"台账不存在: {args.ledger}"}
        sys.stdout.write(dumps_pretty(msg))
        return EXIT_MISSING

    if args.plan and args.input:
        manifest, input_hash = _load_manifest(args.input)
        plan = load_json_file(args.plan)
        if plan.get("input_hash") != input_hash:
            raise LedgerError("status 复核失败：计划与输入清单哈希不一致")
        conn_ledger = Ledger.open(args.ledger, manifest, plan)
        try:
            state = conn_ledger.replay_state(manifest, plan)
            status["derived_state"] = sorted(manifest.active_ids(state))
            status["derived_summary"] = manifest.state_summary(state)
            expected = (
                plan["target_state"]
                if status["meta"].get("status") == "completed"
                else None
            )
            status["matches_plan_target"] = (
                expected == sorted(manifest.active_ids(state)) if expected is not None else None
            )
        finally:
            conn_ledger.close()

    status["ok"] = True
    sys.stdout.write(dumps_pretty(status))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="authplanner",
        description="完全离线的构建代理权限清单规划器（虚构数据，无真实系统变更）",
    )
    parser.add_argument("--version", action="version", version=f"authplanner {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_solve = sub.add_parser("solve", help="完整穷举求解最优授权清单与受控切换路径")
    p_solve.add_argument("input", help="输入清单 JSON 路径")
    p_solve.add_argument("-o", "--output", help="计划/报告输出路径（默认 stdout）")
    p_solve.add_argument(
        "--dump-reachable",
        help="附加输出：全部可达状态及其终态可行性、深度，供独立复核",
    )
    p_solve.set_defaults(func=cmd_solve)

    p_verify = sub.add_parser("verify", help="逐步重放并核对计划或不可达报告")
    p_verify.add_argument("input", help="输入清单 JSON 路径")
    p_verify.add_argument("document", help="solve 产出的 plan 或 unreachable 报告路径")
    p_verify.add_argument(
        "--check-optimality",
        action="store_true",
        help="独立重跑完整穷举，复核三级目标最优性与字典序最短路径",
    )
    p_verify.add_argument("-o", "--output", help="核对结论输出路径（默认 stdout）")
    p_verify.set_defaults(func=cmd_verify)

    p_rollout = sub.add_parser("rollout", help="按计划逐步骤写入模拟台账，支持重启接续")
    p_rollout.add_argument("input", help="输入清单 JSON 路径")
    p_rollout.add_argument("plan", help="solve 产出的 plan JSON 路径")
    p_rollout.add_argument("--ledger", required=True, help="SQLite 台账文件路径")
    p_rollout.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="SEQ:MODE",
        help="故障注入：SEQ 步持久化前 exit（正常退出）或 exception（受控异常），可重复",
    )
    p_rollout.add_argument("-o", "--output", help="执行结果输出路径（默认 stdout）")
    p_rollout.set_defaults(func=cmd_rollout)

    p_status = sub.add_parser("status", help="读取台账代次、已提交步与链式哈希")
    p_status.add_argument("ledger", help="SQLite 台账文件路径")
    p_status.add_argument("--input", help="可选：配合 --plan 复核台账推导状态")
    p_status.add_argument("--plan", help="可选：配合 --input 复核台账推导状态")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        sys.stderr.write(f"文件不存在: {exc}\n")
        return EXIT_MISSING
    except ModelError as exc:
        sys.stderr.write(f"输入清单错误: {exc}\n")
        return EXIT_USAGE
    except LedgerError as exc:
        sys.stderr.write(f"台账拒绝: {exc}\n")
        return EXIT_LEDGER_REJECTED
    except ControlledFault as exc:
        sys.stderr.write(f"故障注入（受控异常）: {exc}\n")
        return EXIT_CONTROLLED_FAULT


if __name__ == "__main__":
    raise SystemExit(main())
