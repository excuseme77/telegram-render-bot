"""Проверка защиты позиций на MEXC: у каждой ли есть стоп, и сколько висит сирот.

Главная боль: снять план-ордер на MEXC нельзя, а `planorder/list/orders`
возвращает ещё и давно отменённые. Поэтому «сколько заявок в списке» —
не ответ на вопрос «сколько заявок живых». Этот скрипт не пытается угадать
`state` (у всех записей `state: 2`, включая отменённые) и честно показывает,
что удалось проверить, а что нет.

Запуск: python -X utf8 check_plans.py
Результат: plan_report.txt (UTF-8) и plan_report.json
"""
import json
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import logging

logging.basicConfig(level=logging.ERROR)
logging.getLogger("ccxt").setLevel(logging.ERROR)

TXT = open(os.path.join(HERE, "plan_report.txt"), "w", encoding="utf-8")
# Имя не JSON: иначе затеняет модуль json в конце скрипта.
JSON_OUT = open(os.path.join(HERE, "plan_report.json"), "w", encoding="utf-8")


def say(*args):
    line = " ".join(str(a) for a in args)
    TXT.write(line + "\n")
    TXT.flush()


os.environ["MODE"] = "live"
os.environ["CONFIRM_LIVE"] = "YES"

import config
from broker import CcxtBroker, _fmt

s = config.load()
b = CcxtBroker(s, paper=False)
ex = b.exchange

say("=" * 78)
say("БАЛАНС И ПОЗИЦИИ")
say("=" * 78)
assets = (ex.contractPrivateGetAccountAssets() or {}).get("data") or []
equity = 0.0
for row in assets:
    if row.get("currency") == "USDT":
        equity = float(row.get("equity") or 0)
say(f"  эквити: {equity:.4f} USDT")

raw_positions = ex.fetch_positions() or []
live = []
for p in raw_positions:
    c = float(p.get("contracts") or 0)
    if c <= 0:
        continue
    side = "buy" if str(p.get("side", "")).lower() in ("long", "buy") else "sell"
    live.append({
        "symbol": p.get("symbol"),
        "side": side,
        "contracts": c,
        "entry": float(p.get("entryPrice") or 0),
    })

say()
for p in live:
    say(f"  {p['symbol']:<22}{'LONG' if p['side'] == 'buy' else 'SHORT':<6}"
        f"{p['contracts']:>10.4f} контрактов   вход {_fmt(p['entry'])}")

say()
say("=" * 78)
say("ПЛАН-ОРДЕРА (как отдаёт биржа — включая отменённые)")
say("=" * 78)
raw = (ex.contractPrivateGetPlanorderListOrders() or {}).get("data") or []
say(f"  всего записей: {len(raw)}")

plans = []
for o in raw:
    plans.append({
        "id": str(o.get("id")),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "trigger": float(o.get("triggerPrice") or 0),
        "vol": float(o.get("vol") or 0),
        "state": o.get("state"),
        "triggerType": o.get("triggerType"),
        "orderType": o.get("orderType"),
        "executeCycle": o.get("executeCycle"),
        "reduceOnly": o.get("reduceOnly"),
        "errorCode": o.get("errorCode"),
        "createTime": o.get("createTime"),
    })
for o in plans:
    kind = "СТОП" if int(o["triggerType"] or 0) == 2 else "ТЕЙК"
    act = "закрыть лонг" if int(o["side"] or 0) == 2 else "закрыть шорт"
    say(f"  {o['id']:<22}{str(o['symbol']):<12}{kind:<6}триггер {o['trigger']:<12}"
        f"vol {o['vol']:<8}state {o['state']}  {act}")

# ------------------------------------------------------------------ сопоставление
# У плана side: 2 = закрыть лонг, 4 = закрыть шорт (это НЕ сторона сделки).
live_keys = {f"{b._plan_symbol(p['symbol'])}|{p['side']}" for p in live}
matched, orphans = [], []
for o in plans:
    side = "buy" if int(o["side"] or 0) == 4 else "sell"
    key = f"{o['symbol']}|{side}"
    (matched if key in live_keys else orphans).append(o)

say()
say("=" * 78)
say("ЧТО УДАЛОСЬ ПРОВЕРИТЬ")
say("=" * 78)
say(f"  позиций на бирже:            {len(live)}")
say(f"  планов под позицией:          {len(matched)}")
say(f"  планов без позиции (сироты):  {len(orphans)}")
say()
say("  Проверить нельзя:")
say("    • снялся ли план — MEXC не подтверждает отмену (см. README)")
say("    • «state: 2» — значение не расшифровано, у отменённых оно то же")
say("    • planorder/list/orders {symbol} отдаёт пустой список")
say("    ⇒ Число сирот НЕ равно числу живых заявок. Проверяйте в приложении.")
say()

# Стопы по последним сделкам: у каждой позиции ожидается ровно один стоп
# (triggerType 2) с тем же объёмом. Расхождение — признак проблемы.
say("=" * 78)
say("ЗАЩИТА ПОЗИЦИЙ (ожидаем стоп на каждую)")
say("=" * 78)
stops = [o for o in matched if int(o["triggerType"] or 0) == 2]
takes = [o for o in matched if int(o["triggerType"] or 0) != 2]
unprotected = []
for p in live:
    key = f"{b._plan_symbol(p['symbol'])}|{p['side']}"
    my_stops = [o for o in stops if f"{o['symbol']}|"
                f"{'buy' if int(o['side'] or 0) == 4 else 'sell'}" == key]
    my_takes = [o for o in takes if f"{o['symbol']}|"
                f"{'buy' if int(o['side'] or 0) == 4 else 'sell'}" == key]
    vol_ok = any(abs(o["vol"] - p["contracts"]) < 1e-9 for o in my_stops)
    note = ""
    if not my_stops:
        note = "❌ СТОПА НЕТ"
        unprotected.append(p["symbol"])
    elif not vol_ok:
        note = (f"⚠️  объём стопа {'/'.join(_fmt(o['vol']) for o in my_stops)} "
                f"≠ позиции {_fmt(p['contracts'])}")
    else:
        note = f"✅ стоп {_fmt(my_stops[-1]['trigger'])}"
    say(f"  {p['symbol']:<22}{'LONG' if p['side'] == 'buy' else 'SHORT':<6}"
        f"{_fmt(p['contracts']):>8} к.   стопов {len(my_stops)} · "
        f"тейков {len(my_takes)}   {note}")

say()
if unprotected:
    say(f"🚨 БЕЗ СТОПА: {', '.join(unprotected)}")
    say("   Эти позиции могут уйти в минус без ограничения убытка.")
    say("   Бот их не знает: уровни стоп/цель живут в памяти и не сохраняются,")
    say("   поэтому новый запуск не сможет закрыть их по уровню.")
else:
    say("✅ У всех позиций есть стоп на бирже.")

json.dump({"equity": equity, "positions": live, "plans": plans,
           "matched": len(matched), "orphans": orphans,
           "unprotected": unprotected}, JSON_OUT, ensure_ascii=False, indent=2)
TXT.close()
JSON_OUT.close()
print("записано: plan_report.txt, plan_report.json")
