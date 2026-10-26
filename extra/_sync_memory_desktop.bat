@echo off
rem ============================================================
rem  记忆同步：CLI 正本  <->  Claude 桌面版 Memory 面板副本
rem  ★ 仅 3p 定制版需要跑；E 盘官方版（E:\software\claude）与 CLI 是同一份库，无需本脚本
rem  双向合并，新的覆盖旧的（robocopy /XO），不改动任何一边的独有文件
rem  用法：双击运行；完成后重启 Claude 桌面版即可在 Memory 面板看到
rem ============================================================
set "CANON=C:\Users\Administrator\.claude\projects\C--Users-Administrator-Desktop---------Hermes-bsm-agent\memory"
set "PANEL=%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\Claude-3p\local-agent-mode-sessions\ec666376-d1e7-4ae9-9fa0-9da540015c91\00000000-0000-4000-8000-000000000001\spaces\c132571f-4311-412d-a199-74a40a410588\memory"

echo [1/2] canonical -> panel (newer wins) ...
robocopy "%CANON%" "%PANEL%" *.md /XO /NFL /NDL /NJH /NJS >nul

echo [2/2] panel -> canonical (newer wins) ...
robocopy "%PANEL%" "%CANON%" *.md /XO /NFL /NDL /NJH /NJS >nul

echo.
echo Done. Canonical files:
dir /b "%CANON%\*.md" | find /c /v ""
echo (Restart the Claude desktop app to refresh its Memory panel.)
pause
