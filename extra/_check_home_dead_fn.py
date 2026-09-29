"""复核：首页脚本里的 `saveTradingSettings` / `toggleDataSourceOpts` 是不是死代码。

方法：把首页**所有"用户能触发的入口"**枚举出来（静态标记里的内联 on*= 处理器、
JS 里 addEventListener 注册的处理器、.onclick= 赋值、setTimeout/setInterval 回调），
再看这两个名字有没有出现在任何入口里。

注意：不要用"桩 DOM + node 执行"来判——桩的 querySelector 每次返回新对象，
页面真实写入的 innerHTML 抓不到（实测只抓到 4 个字符、0 个监听器 = 假结论）。

用法：python extra/_check_home_dead_fn.py
"""
import ast
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = ("saveTradingSettings", "toggleDataSourceOpts")


def page_constants(src):
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) \
                and node.value.value.rstrip().endswith("</html>"):
            out[getattr(node.targets[0], "id", "?")] = node.value.value
    return out


def bindings(html):
    """页面里所有"用户可触发入口"的处理器表达式"""
    markup = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    inline = re.findall(r'on\w+="([^"]*)"', markup)          # 内联属性
    js = "\n;\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    regs = [m.group(2).strip() for m in re.finditer(
        r"addEventListener\(\s*['\"][^'\"]+['\"]\s*,\s*([^,)]+)", js)]
    assigns = [x.strip() for x in re.findall(r"\.on\w+\s*=\s*([^;\n]+)", js)]
    timers = [x.strip() for x in re.findall(r"set(?:Timeout|Interval)\(\s*([^,)]+)", js)]
    return inline + regs + assigns + timers, markup


def main() -> int:
    src = open(os.path.join(ROOT, "web.py"), encoding="utf-8").read()
    consts = page_constants(src)
    home = consts.get("HTML", "")
    if not home:
        print("[FAIL] 没找到首页常量 HTML")
        return 1
    ents, markup = bindings(home)
    print(f"首页：静态标记 {len(markup)} 字符 / 可触发入口 {len(ents)} 个")
    print("入口样例（前 18 个）:")
    for e in ents[:18]:
        print("   " + e[:110])
    print()
    bad = 0
    for nm in NAMES:
        occ = home.count(nm)
        in_ent = [e for e in ents if nm in e]
        print(f"  {nm}: 首页内出现 {occ} 次；出现在入口里 {len(in_ent)} 次")
        if in_ent:
            bad += 1
            print("     ⚠ " + str(in_ent[:3]))
    # 定义行号 + 该名字在别处（设置页）的活用法，供人工核对
    print("\n核对：这两个名字在本文件其它地方的出现位置")
    for nm in NAMES:
        for m in re.finditer(nm, src):
            ln = src[:m.start()].count("\n") + 1
            owner = [k for k, h in consts.items()
                     if h and m.start() >= src.find(h[:60]) and ln <= 0]  # 仅占位
            print(f"   L{ln}: {src.splitlines()[ln-1].strip()[:100]}")
    occ = {nm: home.count(nm) for nm in NAMES}
    print(f"\n首页内出现次数: {occ}")
    if bad:
        print("⚠ 有入口引用，需要重新判断")
        return 1
    if all(v == 0 for v in occ.values()):
        print("结论：已从首页删除，这两个名字只留在设置页（那份是活的）")
    else:
        print("结论：首页内各自只出现一次（定义本身）且不在任何入口里 "
              "→ 没有任何 UI 能调到它们（死代码）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
