"""Диагностика реального состояния счёта MEXC: позиции и план-ордера.

Показывает, что бот оставил после тестов, ничего не меняя.
Запуск: python -X utf8 diag_state.py
"""
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger("ccxt").setLevel(logging.ERROR)

import config
from broker import CcxtBroker

os.environ["MODE"] = "live"
os.environ["CONFIRM_LIVE"] = "YES"
s = config.load()
s = type(s)(**{**s.__dict__, "symbols": ["SUI/USDT:USDT"]})
b = CcxtBroker(s, paper=False)
ex = b.exchange

print("=" * 78)
print("БАЛАНС И Позиции")
print("=" * 78)
raw = ex.fetch_positions()
mine = ex.fetch_balance()
print("USDT на фьючерсах:", mine.get("USDT", {}).get("total"))
print(f"\n{'символ':<22}{'сторона':<8}{'контракты':>12}{'вход':>14}{'маржа':>12}{'PnL':>12}")
real = 0
for p in raw or []:
    c = float(p.get("contracts") or 0)
    if c <= 0:
        continue
    real += 1
    print(f"{p.get('symbol',''):<22}{str(p.get('side','')):<8}{c:>12.4f}"
          f"{float(p.get('entryPrice') or 0):>14.8f}"
          f"{float(p.get('initialMargin') or p.get('margin') or 0):>12.4f}"
          f"{float(p.get('unrealizedPnl') or 0):>12.4f}")
if not real:
    print("(открытых позиций нет)")

print()
print("=" * 78)
print("ПЛАН-ОРДЕРА (стопы и тейки), оставшиеся на бирже")
print("=" * 78)
raw_plan = ex.contractPrivateGetPlanorderListOrders({})
print("Сырой ответ:", repr(raw_plan)[:400])
data = (raw_plan or {}).get("data") or {}
if isinstance(data, dict):
    for bucket in ("success", "failed"):
        items = data.get(bucket) or []
        if not items:
            continue
        print(f"\n[{bucket}] {len(items)} шт.")
        for o in items:
            print("  ", repr(o))
else:
    print("Неожиданный формат:", type(data))
