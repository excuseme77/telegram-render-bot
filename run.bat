@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Program Files\Python311\python.exe" -X utf8 "C:\Users\tihon\OneDrive\Рабочий стол\files (1)\run.py" %*
