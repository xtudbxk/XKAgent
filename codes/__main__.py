"""codes/__main__.py — 应用入口委托（兼容 python3 -m codes）。

所有初始化逻辑移至 codes/main.py，此文件仅作为委托层。
"""
import os
import sys

# ── 项目根手动注入 sys.path（与 main.py 保持一致，保证
#    `python3 codes/__main__.py` 与 `python3 -m codes` 两种方式均可导入 codes） ──
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codes.main import main

main()
