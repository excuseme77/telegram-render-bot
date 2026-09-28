"""Проверка выходов: закрываются ли позиции и освобождаются ли слоты.

Настоящие 2% цена за время теста не пройдёт, поэтому цена подставляется
вручную. Проверяем ровно то, из-за чего бот «останавливался после одного
круга»: позиция дошла до цели -> закрылась -> слот освободился.

Запуск: python -X utf8 check_exits.py
"""
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MODE"] = "paper"
os.environ["CONFIRM_LIVE"] = "NO"

import logging

# Отчёт пишем сами: PowerShell перекодирует stdout в консольную кодировку,
# и после перенаправления файл превращается в нечитаемую кашу из «╨Ю╤В║═Л».
REPORT = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "exits_report.txt"), "w", encoding="utf-8")


def say(*args):
    line = " ".join(str(a) for a in args)
    REPORT.write(line + "\n")
    REPORT.flush()


def report(*args):
    say(*args)


logging.basicConfig(level=logging.INFO, format="   %(message)s", stream=REPORT)
logging.getLogger("ccxt").setLevel(logging.ERROR)

import config
from broker import make_broker, _fmt
from strategy import EmaRsiAtrStrategy
from risk import RiskManager

s = config.load()
broker = make_broker(s)
strategy = EmaRsiAtrStrategy(
    fast=s.ema_fast, slow=s.ema_slow, rsi_period=s.rsi_period, atr_period=s.atr_period,
    rsi_long=(s.rsi_long_min, s.rsi_long_max), rsi_short=(s.rsi_short_min, s.rsi_short_max),
    stop_atr=s.stop_atr, take_atr=s.take_atr,
    take_profit_min_pct=s.take_profit_min_pct, take_profit_max_pct=s.take_profit_max_pct,
    max_stop_pct=s.max_stop_pct, min_reward_ratio=s.min_reward_ratio,
)
risk = RiskManager(s)

SYMBOL = "SUI/USDT:USDT"
fails = []


def check(title, condition, detail=""):
    say(f"   {'✅' if condition else '❌'} {title}" + (f" — {detail}" if detail else ""))
    if not condition:
        fails.append(title)

say("=" * 74)
say("1. Открываем позицию по сигналу стратегии")
say("=" * 74)
df = broker.ohlcv(SYMBOL)
sig = strategy.generate(SYMBOL, df)
say(f"   Сигнал: {sig.side}, стоп {_fmt(sig.stop)}, цель {_fmt(sig.take_profit)}")
if sig.side == "hold":
    say(f"   ⏭️  По SUI сейчас нет сигнала ({sig.why_not}). Пробуем другую пару.")
    found = False
    for alt in broker.active_symbols():
        s2 = strategy.generate(alt, broker.ohlcv(alt))
        if s2.side != "hold":
            SYMBOL, sig, found = alt, s2, True
            say(f"   ✅ Нашлась пара с сигналом: {alt} → {sig.side}, "
                  f"стоп {_fmt(sig.stop)}, цель {_fmt(sig.take_profit)}")
            break
    if not found:
        say("   ❌ Ни по одной паре нет сигнала — нечего тестировать.")
        sys.exit(1)
else:
    found = True

entry = broker.price(SYMBOL)
coins, why = risk.plan(broker.equity(), sig.side, entry, sig.stop, 0)
qty, why2 = broker.fit_qty(SYMBOL, coins)
say(f"   Размер: {qty} контрактов = {_fmt(broker.notional(SYMBOL, qty, entry))} USDT ({why2})")
fill = broker.open(SYMBOL, sig.side, qty, sig.stop, sig.take_profit, s.leverage)
pos = [p for p in broker.positions() if p.symbol == SYMBOL][0]
say(f"   Позиция: вход {_fmt(pos.entry)}, стоп {_fmt(pos.stop)}, цель {_fmt(pos.take_profit)}")
check("позиция открылась", pos.qty > 0)
check("у позиции есть стоп", pos.stop > 0)
check("у позиции есть цель", pos.take_profit > 0)

say()
say("=" * 74)
say("2. Цена дошла до ЦЕЛИ — ждём закрытия")
say("=" * 74)
target_price = pos.take_profit * 1.001
broker._last_prices[SYMBOL] = target_price
say(f"   Подставляю цену {_fmt(target_price)} (цель была {_fmt(pos.take_profit)})")
# Сравниваем _balance (реализованный кэш), а НЕ equity(): в paper equity
# включает нереализованный PnL ещё открытой позиции по текущей цене биржи,
# поэтому до и после закрытия она почти одинакова и ничего не проверяет.
cash_before = broker._balance
msgs = broker.manage_exits()
for m in msgs:
    say(f"   → {m}")
left = [p for p in broker.positions() if p.symbol == SYMBOL]
check("позиция закрылась по цели", not left, f"осталось {len(left)}")
check("слот освободился", len(broker.positions()) < 1)
check("реальный кэш вырос", broker._balance > cash_before,
      f"{cash_before:.4f} -> {broker._balance:.4f} "
      f"(+{broker._balance - cash_before:.4f} USDT)")
check("в лог попало освобождение слота", any("слот освобождён" in m for m in msgs))

say()
say("=" * 74)
say("3. Открываем снова и роняем цену на СТОП")
say("=" * 74)
broker2 = make_broker(s)
entry = broker2.price(SYMBOL)
coins, _ = risk.plan(broker2.equity(), sig.side, entry, sig.stop, 0)
qty, _ = broker2.fit_qty(SYMBOL, coins)
broker2.open(SYMBOL, sig.side, qty, sig.stop, sig.take_profit, s.leverage)
pos2 = [p for p in broker2.positions() if p.symbol == SYMBOL][0]
if pos2.side == "buy":
    stop_price = pos2.stop * 0.999
else:
    stop_price = pos2.stop * 1.001
broker2._last_prices[SYMBOL] = stop_price
say(f"   Подставляю цену {_fmt(stop_price)} (стоп был {_fmt(pos2.stop)})")
cash_before = broker2._balance
msgs = broker2.manage_exits()
for m in msgs:
    say(f"   → {m}")
left = [p for p in broker2.positions() if p.symbol == SYMBOL]
check("позиция закрылась по стопу", not left, f"осталось {len(left)}")
check("реальный кэш упал", broker2._balance < cash_before,
      f"{cash_before:.4f} -> {broker2._balance:.4f} "
      f"({broker2._balance - cash_before:.4f} USDT)")

say()
say("=" * 74)
say("4. Бот не застревает: пять циклов вход → цель → выход")
say("=" * 74)
# Прежняя версия этого шага просто печатала True и ничего не проверяла.
# Настоящая проверка «не останавливается после одного круга»: каждый цикл
# должен открыть позицию, довести её до цели и освободить слот. Если слоты
# не возвращаются, на втором цикле открыться будет уже нечем.
broker3 = make_broker(s)
cycles_ok = 0
for i in range(1, 6):
    entry = broker3.price(SYMBOL)
    coins, _ = risk.plan(broker3.equity(), sig.side, entry, sig.stop, 0)
    q, _ = broker3.fit_qty(SYMBOL, coins)
    if q <= 0:
        say(f"   Проход {i}: ❌ не удалось рассчитать размер")
        break
    broker3.open(SYMBOL, sig.side, q, sig.stop, sig.take_profit, s.leverage)
    p = [x for x in broker3.positions() if x.symbol == SYMBOL]
    if not p:
        say(f"   Проход {i}: ❌ вход не зарегистрирован")
        break
    p = p[0]
    broker3._last_prices[SYMBOL] = p.take_profit * 1.001
    msgs3 = broker3.manage_exits()
    still = [x for x in broker3.positions() if x.symbol == SYMBOL]
    freed = not still
    say(f"   Цикл {i}: вход {_fmt(p.entry)} → цель {_fmt(p.take_profit)}, "
        f"слот {'✅ освобождён' if freed else '❌ занят'}")
    if not freed:
        break
    cycles_ok += 1
check("слот освобождается и бот торгует дальше", cycles_ok == 5,
      f"пройдено циклов {cycles_ok} из 5, позиций на бирже "
      f"{len(broker3.positions())}, кэш {broker3._balance:.4f} USDT")

say()
say("=" * 74)
if fails:
    say(f"❌ НЕ ПРОШЛИ: {len(fails)}")
    for t in fails:
        say("   ·", t)
    REPORT.close()
    sys.exit(1)
say("✅ Все проверки выходов прошли — позиции закрываются, слоты освобождаются.")
REPORT.close()
