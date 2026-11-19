"""根目录启动器：支持在完全隔离模式 `python -I planner.py ...` 下运行。

常规用法 `python -m authplanner ...` 同样可用。
本文件只做路径引导，不含业务逻辑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from authplanner.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
