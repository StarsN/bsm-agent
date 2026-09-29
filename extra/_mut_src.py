"""读取模块源码：**变异注入的源码优先**，否则读磁盘原文。

变异测试跑时 `_mut_runner.py` 会把改过的源码挂到 `sys.modules[mod].__source__`
上（模块本身可能 exec 不起来，比如 web.py 需要 fastapi / 模块级起线程）。
检查脚本必须走这个函数读源码，否则读到的永远是磁盘上的原文 —— 变异对
"源码抽取型检查"（_check_* 抽真码块 exec 的那类）完全不可见。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_module_source(name: str) -> str:
    mod = sys.modules.get(name)
    src = getattr(mod, "__source__", None) if mod is not None else None
    if isinstance(src, str) and src:
        return src
    with open(os.path.join(ROOT, f"{name}.py"), encoding="utf-8") as f:
        return f.read()
