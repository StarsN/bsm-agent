"""死代码扫描：页面脚本里"定义了但没有任何引用"的函数（含逐个证据）。

判据（比"调用计数"严谨 —— 调用计数会漏掉传引用 `addEventListener('click', fn)`、
字符串调用、动态拼 onclick 等写法）：
  1. 函数名在**整个页面常量**里的出现次数 == 定义处次数（多一处引用就不算死）；
  2. 再枚举页面的**全部可触发入口**（内联 on*= 属性、addEventListener 注册、
     .onclick= 赋值、setAttribute('on…')、setTimeout/setInterval 回调），确认没有
     任何入口引用它；
  3. 页面不能有外部 <script src>、iframe、eval / new Function / window[ 这类
     绕过静态检查的调用路径（有的话本工具会直接报"无法判定"）。

用法：
  python extra/_check_dead_code.py            # 扫所有页面
  python extra/_check_dead_code.py HTML       # 只看某个页面常量
"""
import ast
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page_constants(src: str) -> dict:
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) \
                and node.value.value.rstrip().endswith("</html>"):
            out[getattr(node.targets[0], "id", "?")] = (node.value.value, node.lineno)
    return out


def scripts(html: str) -> str:
    return "\n;\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def entry_points(html: str) -> list:
    """页面上所有"用户能触发"的入口里的处理器表达式"""
    markup = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    js = scripts(html)
    ents = re.findall(r'on\w+="([^"]*)"', markup)                    # 内联属性
    ents += [m.group(1) for m in re.finditer(
        r"addEventListener\(\s*['\"][^'\"]+['\"]\s*,\s*([^,)]+)", js)]  # 注册
    ents += re.findall(r"\.on\w+\s*=\s*([^;\n]+)", js)                # 属性赋值
    ents += re.findall(r"setAttribute\(\s*['\"]on\w+['\"]\s*,\s*([^)]+)\)", js)
    ents += re.findall(r"set(?:Timeout|Interval)\(\s*([^,)]+)", js)   # 定时器回调
    return [e.strip() for e in ents]


def bypass_paths(html: str) -> list:
    out = []
    if re.search(r"<script[^>]*src=", html):
        out.append("外部 <script src>")
    if re.search(r"<iframe", html):
        out.append("iframe")
    if re.search(r"\beval\(", html) or re.search(r"new Function", html) \
            or re.search(r"window\[", html):
        out.append("eval/new Function/window[]")
    return out


def page_report(name: str, html: str, lineno: int) -> list:
    js = scripts(html)
    names = set(re.findall(r"(?:async\s+)?function\s+(\w+)\s*\(", js))
    names |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\()", js))
    bypass = bypass_paths(html)
    ents = entry_points(html)
    print(f"\n=== {name}（web.py L{lineno} 起）：函数 {len(names)} 个 / "
          f"可触发入口 {len(ents)} 个"
          + (f" / ⚠ 存在绕过路径 {bypass} → 无法判定" if bypass else ""))
    dead = []
    for fn in sorted(names):
        occ = len(re.findall(r"\b" + re.escape(fn) + r"\b", html))
        defs = len(re.findall(r"function\s+" + re.escape(fn) + r"\s*\(", html))
        defs += len(re.findall(r"(?:const|let|var)\s+" + re.escape(fn) + r"\s*=", html))
        if occ <= defs:
            used_in_entry = any(fn in e for e in ents)
            dead.append(fn)
            print(f"   ✗ {fn}: 全页出现 {occ} 次（定义 {defs} 次）"
                  f" / 入口引用 {used_in_entry}")
            if used_in_entry:      # 理论上不会走到（出现次数会 > 定义数）
                print("       ⚠ 入口里有引用 → 判定不可靠，人工看")
    if not dead:
        print("   （没有零引用的函数）")
    return dead


def main() -> int:
    src = open(os.path.join(ROOT, "web.py"), encoding="utf-8").read()
    consts = page_constants(src)
    want = [a for a in sys.argv[1:] if a.strip()]
    total = 0
    for name, (html, lineno) in sorted(consts.items()):
        if want and name not in want:
            continue
        total += len(page_report(name, html, lineno))
    print(f"\n合计零引用函数 {total} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
