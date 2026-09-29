"""变异测试执行器：把 alphafox_live.py 在内存里改成"bug 版"后跑指定测试脚本。

不修改任何磁盘文件（源码在内存中 replace 后 exec 成模块，注入 sys.modules）。
用法: python extra/_mut_runner.py '<json spec>'
"""
import json
import os
import runpy
import sys
import types

spec = json.loads(sys.argv[1])
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

mod_path = os.path.join(root, spec["module"] + ".py")
src = open(mod_path, encoding="utf-8").read()
if spec["old"] not in src:
    print("MUTATION-NOT-APPLIED: 未匹配到目标代码")
    sys.exit(2)
mutated = src.replace(spec["old"], spec["new"], 1)

mod = types.ModuleType(spec["module"])
mod.__file__ = mod_path
# 变异后的源码挂在模块上：源码抽取型检查（_check_* 读 web.py 原文 exec 真码块）
# 必须读它，否则它们读磁盘原文 → 变异对它们不存在（历史 bug：web.py 的变异
# 因为 exec 撞 fastapi 未安装而崩，rc=1 被当成"被抓住"，18 条全是假阳性）
mod.__source__ = mutated
sys.modules[spec["module"]] = mod
try:
    exec(compile(mutated, mod_path, "exec"), mod.__dict__)
except Exception as e:      # 如 web.py：本地没装 fastapi / 模块级起线程
    print(f"[warn] {spec['module']} 变异模块 exec 失败"
          f"（源码抽取型检查不受影响，继续）: {type(e).__name__}: {e}",
          file=sys.stderr)
    # 但要**大声标记**：模块级 exec 失败时，import 型测试会因为"模块里啥都没有"
    # 直接崩 → rc=1 → 被记成"被测试抓住" = 假阳性（变异本身写错了/语法不对）。
    # 打一个可识别的标记，让检查器按"变异未生效"处理（[ERR]）而不是"抓住"。
    print(f"[MUT-EXEC-FAIL] {spec['module']}: {type(e).__name__}: {e}")

sys.argv = [spec["script"]]
runpy.run_path(os.path.join(root, spec["script"]), run_name="__main__")
