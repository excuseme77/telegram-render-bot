"""Снять осиротевшие план-ордера MEXC (стопы/тейки без позиции).

Это ЖИВЫЕ заявки на бирже: они пережили закрытие позиции и могут сработать
позже, открыв сделку, которой быть не должно.

Запуск: python -X utf8 cleanup_orphans.py [что снимать]
    без аргументов — только показать (ничего не меняет)
    --all        — снять все осиротевшие
    --side buy   — снять осиротевшие стопы лонгов
    --side sell  — снять осиротевшие стопы шортов
"""
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("ccxt").setLevel(logging.ERROR)

import config
from broker import CcxtBroker

args = sys.argv[1:]
DO_IT = "--all" in args
SIDE = None
for i, a in enumerate(args):
    if a == "--side" and i + 1 < len(args):
        SIDE = args[i + 1]

os.environ["MODE"] = "live"
os.environ["CONFIRM_LIVE"] = "YES"
s = config.load()
s = type(s)(**{**s.__dict__, "symbols": ["SUI/USDT:USDT"]})
b = CcxtBroker(s, paper=False)
ex = b.exchange

print("=" * 78)
print("ОТКРЫТЫЕ ПОЗИЦИИ")
print("=" * 78)
open_keys = set()
for p in ex.fetch_positions() or []:
    c = float(p.get("contracts") or 0)
    if c <= 0:
        continue
    side = "buy" if str(p.get("side", "")).lower() in ("long", "buy") else "sell"
    key = f"{b._plan_symbol(p.get('symbol'))}|{side}"
    open_keys.add(key)
    print(f"  {p.get('symbol'):<22}{side:<6}{c:>10.4f} контрактов   ключ {key}")
if not open_keys:
    print("  (нет)")

print()
print("=" * 78)
print("ПЛАН-ОРДЕРА НА БИРЖЕ")
print("=" * 78)
orders = b.plan_orders()
orphans = []
for o in orders:
    side = "buy" if int(o.get("side") or 0) == 4 else "sell"
    key = f"{o.get('symbol')}|{side}"
    is_orphan = key not in open_keys
    if is_orphan:
        orphans.append((o, side, key))
    print(f"  {b._plan_id(o):<22}{o.get('symbol'):<12}{side:<6}"
          f"триггер {str(o.get('triggerPrice')):<12}vol {str(o.get('vol')):<6}"
          f"{'⚠️  СИРОТА' if is_orphan else '✅ под позицией'}")

print()
print(f"Всего план-ордеров: {len(orders)}, осиротевших: {len(orphans)}")

if not DO_IT:
    print()
    print("Это только просмотр, ничего не изменено.")
    print("Снять сирот:  python -X utf8 cleanup_orphans.py --all")
    if orphans:
        print()
        print("⚠️  Эти заявки сейчас живые на бирже. Пока они не сняты,")
        print("    цена может дойти до триггера и открыть сделку без вас.")
    sys.exit(0)

print()
print("=" * 78)
print("СНИМАЮ ОСИРОТЕВШИЕ ПЛАН-ОРДЕРА")
print("=" * 78)
# Отмена на MEXC — больное место, и врать о результате нельзя:
#
#   * planorder/cancel (поштучно, по orderId) отвечает «code 600 Parameter
#     error» на ЛЮБЫЕ параметры: и строкой, и числом, и с symbol/side/
#     openType/positionMode. Снять конкретный стоп невозможно.
#   * planorder/cancel_all (списком по символу) отвечает «code: 0», но
#     заявка из planorder/list/orders после этого НЕ исчезает.
#   * planorder/list/orders {symbol} возвращает пустой список, а без
#     параметра — все записи, включая уже отменённые.
#   * у всех записей state: 2, и что это означает — неизвестно.
#
# Итог: снятие план-ордера на MEXC программно не подтверждается. Поэтому
# ниже честный отчёт «отправили запрос / биржа ответила», а не «снято».
# Реальный статус заявок виден только в приложении MEXC.
symbols = sorted({o.get("symbol") for o, _, _ in orphans})
n = 0
failed = 0
for sym in symbols:
    mine = [o for o, _, _ in orphans if o.get("symbol") == sym]
    if SIDE:
        mine = [o for o in mine
                if ("buy" if int(o.get("side") or 0) == 4 else "sell") == SIDE]
        if not mine:
            print(f"  пропущен (фильтр --side {SIDE}): {sym}")
            continue
    if b._plan_cancel_all(sym):
        n += len(mine)
        print(f"  📤 {sym}: отправлен cancel_all на {len(mine)} "
              f"(ответ code 0, триггеры "
              f"{', '.join(str(o.get('triggerPrice')) for o in mine)})")
    else:
        failed += 1
        print(f"  ❌ {sym}: биржа отказала")

print()
print(f"Запросов принято: {n}, отказов: {failed}.")
print()
print("⚠️  Проверить результат по API невозможно: MEXC возвращает в списке и")
print("    давно отменённые заявки, а поле state: 2 не расшифровано.")
print("    Откройте «Позиции → Открытые ордера» в приложении MEXC и убедитесь,")
print("    что висящих стопов нет. Пока они не сняты, цена может дойти до")
print("    триггера и открыть сделку без вас.")
