"""序列化与哈希辅助函数（仅使用标准库）。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """稳定的规范 JSON 文本：键排序、无空格、保留非 ASCII 字符。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(data: Any) -> str:
    """对字节串或任意 JSON 可序列化对象计算 SHA-256 十六进制摘要。"""
    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        payload = canonical_json(data).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def dumps_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
