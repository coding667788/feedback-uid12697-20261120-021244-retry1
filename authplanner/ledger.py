"""rollout 的模拟台账：SQLite 持久化、代次管理、幂等键与重启接续。

台账只记录“模拟授权变更”，不触碰任何真实系统。
每个步骤在同一个事务内写入并提交；故障注入严格发生在持久化之前，
因此进程异常退出后台账仍保持“已提交步全部完整、未提交步无痕”的状态。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from .model import LedgerError, Manifest
from .util import canonical_json, sha256_hex

SCHEMA_VERSION = "1"


class ControlledFault(RuntimeError):
    """持久化前主动触发的受控异常（故障注入的第二种形态）。"""


_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    seq INTEGER PRIMARY KEY,
    operation_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    before_summary TEXT NOT NULL,
    after_summary TEXT NOT NULL,
    chain_prev TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
"""


def idempotency_key(
    plan_id: str, generation: str, seq: int, operation_key: str,
    before_hash: str, after_hash: str,
) -> str:
    payload = {
        "plan_id": plan_id,
        "generation": generation,
        "seq": seq,
        "operation_key": operation_key,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    return "idem-" + sha256_hex(payload)[:24]


def chain_hash(
    prev: str, seq: int, idem_key: str, operation_key: str,
    before_hash: str, after_hash: str,
) -> str:
    payload = {
        "prev": prev,
        "seq": seq,
        "idempotency_key": idem_key,
        "operation_key": operation_key,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    return "chain-" + sha256_hex(payload)[:24]


class Ledger:
    def __init__(self, path: str, conn: sqlite3.Connection, meta: dict[str, str]):
        self.path = path
        self.conn = conn
        self.meta = meta

    # ------------------------------------------------------------------ 打开/创建
    @classmethod
    def open(cls, path: str, manifest: Manifest, plan: dict[str, Any]) -> "Ledger":
        """打开既有台账并校验代次与计划一致性；不存在则按当前计划创建。"""
        if plan.get("report_kind") != "plan":
            raise LedgerError("只有 report_kind=plan 的文档可以 rollout，不可达报告无步骤可执行")

        offered = _plan_identity(plan)
        if os.path.exists(path):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                stored = _read_meta(conn)
                _reject_if_stale(stored, offered)
                ledger = cls(path, conn, stored)
                ledger._verify_chain(manifest, plan)
            except Exception:
                # 拒绝路径上确保连接关闭，避免 Windows 下文件句柄残留
                conn.close()
                raise
            return ledger

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_DDL)
        initial_meta = {
            "schema_version": SCHEMA_VERSION,
            "status": "running",
            "committed_steps": "0",
            **offered,
        }
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES(:key, :value)",
            [{"key": k, "value": v} for k, v in initial_meta.items()],
        )
        conn.commit()
        return cls(path, conn, initial_meta)

    # ------------------------------------------------------------------ 链校验
    def _verify_chain(self, manifest: Manifest, plan: dict[str, Any]) -> None:
        """重放台账已提交步：序号连续、幂等键唯一且与计划一致、链哈希可验、
        每步前后状态与计划声明一致。任何偏差均拒绝继续。"""
        rows = self.conn.execute(
            "SELECT * FROM steps ORDER BY seq"
        ).fetchall()
        prev = ""
        state = manifest.old_state
        for expected_seq, row in enumerate(rows):
            if row["seq"] != expected_seq:
                raise LedgerError(f"台账步骤序号不连续：期望 {expected_seq}，实际 {row['seq']}")
            try:
                step = plan["steps"][expected_seq]
            except (KeyError, IndexError):
                raise LedgerError(f"台账第 {expected_seq} 步在当前计划中不存在（计划内容可能已更换）")
            operation = step["operation"]
            op_key = manifest.operation_key(operation)
            if row["operation_key"] != op_key:
                raise LedgerError(
                    f"台账第 {expected_seq} 步操作 {row['operation_key']} 与计划 {op_key} 不一致"
                )
            before = state
            after = manifest.apply_operation(before, operation)
            if manifest.transition_violations(before, operation, after):
                raise LedgerError(f"台账第 {expected_seq} 步重放时违反过渡约束")
            if row["before_hash"] != manifest.state_hash(before):
                raise LedgerError(f"台账第 {expected_seq} 步 before_hash 与重放不符")
            if row["after_hash"] != manifest.state_hash(after):
                raise LedgerError(f"台账第 {expected_seq} 步 after_hash 与重放不符")
            idem = idempotency_key(
                self.meta["plan_id"], self.meta["generation"], expected_seq,
                op_key, row["before_hash"], row["after_hash"],
            )
            if row["idempotency_key"] != idem:
                raise LedgerError(f"台账第 {expected_seq} 步幂等键不符，台账可能被外部篡改")
            chash = chain_hash(
                prev, expected_seq, idem, op_key, row["before_hash"], row["after_hash"]
            )
            if row["chain_hash"] != chash:
                raise LedgerError(f"台账第 {expected_seq} 步链哈希断裂")
            if row["chain_prev"] != prev:
                raise LedgerError(f"台账第 {expected_seq} 步链前驱断裂")
            prev = chash
            state = after

        committed = len(rows)
        if int(self.meta.get("committed_steps", "0")) != committed:
            raise LedgerError("台账 meta.committed_steps 与实际步数不符")
        self._replay_state = state

    # ------------------------------------------------------------------ 执行
    def rollout(
        self,
        manifest: Manifest,
        plan: dict[str, Any],
        faults: dict[int, str] | None = None,
    ) -> dict[str, Any]:
        """逐步骤验证代次与预期状态后写入台账，支持重启后按已提交步接续。

        faults: {步骤序号: "exit" | "exception"}，仅用于测试故障注入。
        """
        faults = faults or {}
        steps = plan["steps"]
        total = len(steps)
        rows = self.conn.execute("SELECT seq, idempotency_key FROM steps ORDER BY seq").fetchall()
        committed = len(rows)

        # 已完成台账的幂等重放：不重复写入，直接返回
        if self.meta.get("status") == "completed":
            if committed != total:
                raise LedgerError("台账标记 completed 但步数与计划不一致，拒绝继续")
            return self._result(
                manifest, plan, committed_before=committed, total=total,
                resumed=committed, applied=0, skipped=committed,
            )

        # 重启接续：先重放已提交步，得到当前模拟状态
        self._verify_chain(manifest, plan)
        cursor = self._replay_state
        resumed = committed
        applied = 0
        skipped = 0

        for seq in range(total):
            step = steps[seq]
            operation = step["operation"]
            op_key = manifest.operation_key(operation)

            if seq < committed:
                # 已提交步：台账与计划已在 _verify_chain 中逐一核对，幂等跳过
                skipped += 1
                continue

            # 新步：先验证当前代次与预期状态（意外状态拒绝）
            if step["before_hash"] != manifest.state_hash(cursor):
                raise LedgerError(
                    f"第 {seq} 步预期状态 {step['before_hash']} "
                    f"与台账推导的当前状态 {manifest.state_hash(cursor)} 不符，拒绝继续"
                )
            before = cursor
            after = manifest.apply_operation(before, operation)
            violations = manifest.transition_violations(before, operation, after)
            if violations:
                raise LedgerError(f"第 {seq} 步非法，拒绝写入: {'; '.join(violations)}")
            if step["after_hash"] != manifest.state_hash(after):
                raise LedgerError(f"第 {seq} 步计划 after_hash 与重放不符，拒绝写入")

            idem = idempotency_key(
                self.meta["plan_id"], self.meta["generation"], seq,
                op_key, manifest.state_hash(before), manifest.state_hash(after),
            )
            prev_chain = ""
            if seq > 0:
                prev_row = self.conn.execute(
                    "SELECT chain_hash FROM steps WHERE seq = ?", (seq - 1,)
                ).fetchone()
                if prev_row is None:
                    raise LedgerError(f"缺少第 {seq - 1} 步链记录，拒绝写入")
                prev_chain = prev_row["chain_hash"]
            chash = chain_hash(
                prev_chain, seq, idem, op_key,
                manifest.state_hash(before), manifest.state_hash(after),
            )

            # —— 故障注入点：严格位于任何持久化动作之前 ——
            mode = faults.get(seq)
            if mode == "exception":
                raise ControlledFault(
                    f"受控异常：第 {seq} 步（{op_key}）持久化前主动中止，台账未写入该步"
                )
            if mode == "exit":
                # 自有进程正常退出；当前步尚未 INSERT/COMMIT，重启后可原样接续
                os._exit(0)

            self.conn.execute(
                "INSERT INTO steps(seq, operation_key, idempotency_key, before_hash, "
                "after_hash, before_summary, after_summary, chain_prev, chain_hash) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    op_key,
                    idem,
                    manifest.state_hash(before),
                    manifest.state_hash(after),
                    canonical_json(manifest.state_summary(before)),
                    canonical_json(manifest.state_summary(after)),
                    prev_chain,
                    chash,
                ),
            )
            self.conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'committed_steps'", (str(seq + 1),)
            )
            self.conn.commit()
            self.meta["committed_steps"] = str(seq + 1)
            cursor = after
            applied += 1

        self.conn.execute("UPDATE meta SET value = 'completed' WHERE key = 'status'")
        self.conn.commit()
        self.meta["status"] = "completed"
        return self._result(manifest, plan, committed, total, resumed=resumed, applied=applied, skipped=skipped)

    def _result(
        self, manifest: Manifest, plan: dict[str, Any], committed_before: int, total: int,
        resumed: int, applied: int, skipped: int,
    ) -> dict[str, Any]:
        state = self.replay_state(manifest, plan)
        return {
            "ledger_path": self.path,
            "plan_id": self.meta["plan_id"],
            "generation": self.meta["generation"],
            "status": self.meta.get("status", "running"),
            "total_steps": total,
            "resumed_from_step": resumed,
            "steps_applied": applied,
            "steps_skipped_committed": skipped,
            "current_state": sorted(manifest.active_ids(state)),
            "current_summary": manifest.state_summary(state),
        }

    def replay_state(self, manifest: Manifest, plan: dict[str, Any]) -> int:
        state = manifest.old_state
        rows = self.conn.execute("SELECT seq FROM steps ORDER BY seq").fetchall()
        for row in rows:
            state = manifest.apply_operation(state, plan["steps"][row["seq"]]["operation"])
        return state

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 只读查询
    @classmethod
    def read_status(cls, path: str) -> dict[str, Any]:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            meta = _read_meta(conn)
            rows = conn.execute(
                "SELECT seq, operation_key, idempotency_key, before_hash, after_hash, "
                "chain_prev, chain_hash FROM steps ORDER BY seq"
            ).fetchall()
            return {
                "ledger_path": path,
                "meta": meta,
                "committed_steps": len(rows),
                "steps": [dict(row) for row in rows],
            }
        finally:
            conn.close()


def _plan_identity(plan: dict[str, Any]) -> dict[str, str]:
    return {
        "plan_id": str(plan["plan_id"]),
        "generation": str(plan["generation"]),
        "plan_hash": str(plan["plan_hash"]),
        "input_hash": str(plan["input_hash"]),
        "total_steps": str(plan["step_count"]),
    }


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    meta = {row["key"]: row["value"] for row in rows}
    required = {"plan_id", "generation", "plan_hash", "input_hash", "total_steps", "status"}
    missing = required - set(meta)
    if missing:
        raise LedgerError(f"台账 meta 缺少字段: {sorted(missing)}，台账可能已损坏")
    return meta


def _reject_if_stale(stored: dict[str, str], offered: dict[str, str]) -> None:
    """代次冲突三类拒绝：旧代计划、同 ID 异内容、外来输入。"""
    if stored["generation"] != offered["generation"]:
        raise LedgerError(
            f"代次冲突：台账属于 {stored['generation']}，"
            f"当前计划属于 {offered['generation']}，拒绝继续"
        )
    if stored["plan_id"] == offered["plan_id"] and stored["plan_hash"] != offered["plan_hash"]:
        raise LedgerError(
            f"同 ID 异内容：plan_id={stored['plan_id']} 的计划哈希与台账记录不一致，拒绝继续"
        )
    if stored["input_hash"] != offered["input_hash"]:
        raise LedgerError("输入清单哈希与台账记录不一致，拒绝继续")
    if stored["total_steps"] != offered["total_steps"]:
        raise LedgerError("计划总步数与台账记录不一致，拒绝继续")
