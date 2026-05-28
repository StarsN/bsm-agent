#!/bin/bash
cd /root/binance-monitor/bsm-agent

echo "=== 1. Python 语法检查 ==="

echo "--- config.py ---"
python -c "import config" 2>&1

echo "--- storage.py ---"
python -c "import py_compile; py_compile.compile('storage.py', doraise=True)" 2>&1

echo "--- trade_logic.py ---"
python -c "import py_compile; py_compile.compile('trade_logic.py', doraise=True)" 2>&1

echo "--- kol_agent.py ---"
python -c "import py_compile; py_compile.compile('kol_agent.py', doraise=True)" 2>&1

echo "--- auto_trader.py ---"
python -c "import py_compile; py_compile.compile('auto_trader.py', doraise=True)" 2>&1

echo "--- web.py ---"
python -c "import py_compile; py_compile.compile('web.py', doraise=True)" 2>&1

echo ""
echo "=== 2. 手动测试导入 ==="
python -c "
import sys
sys.path.insert(0, '.')
for mod in ['config','storage','trade_logic','kol_agent','auto_trader','web']:
    try:
        __import__(mod)
        print(f'{mod}: OK')
    except Exception as e:
        print(f'{mod}: FAIL - {e}')
"

echo ""
echo "=== 3. 进程状态 ==="
python manage_processes.py status 2>&1

echo ""
echo "=== 4. 最近系统日志 ==="
journalctl -u bsm-agent --no-pager -n 30 2>/dev/null || dmesg | tail -30 2>/dev/null || echo "无系统日志"
