#!/usr/bin/env python3
"""诊断决策时间线 500 — 直调 API 函数，打完整 traceback"""
import sys, os, traceback, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from web import api_agent_timeline
    r = api_agent_timeline(strategy='kol_agent', limit=5)
    print("OK:", json.dumps(r, ensure_ascii=False, default=str, indent=2)[:500])
except Exception:
    traceback.print_exc()
