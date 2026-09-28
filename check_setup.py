"""Проверка настройки перед запуском. Ничего не торгует: режим принудительно paper.

Запуск:  python -X utf8 check_setup.py

Показывает реальный баланс, рабочие пары, текущий сигнал по каждой паре
и — главное — причину, по которой вход невозможен.
"""
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["MODE"] = "paper"   # принудительно paper — реальных ордеров не будет

import logging

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

import config
from broker import CcxtBroker, _fmt, contracts_word
from risk import RiskManager
from strategy import EmaRsiAtrStrategy

s = config.load()
size = (_fmt(s.position_value) + " USDT" if s.position_value > 0
        else f"{s.max_position_pct}% депозита × {s.leverage}x")

print("=" * 92)
print(f"РЕЖИМ         : {s.mode.upper()}  ⚠️  реальных ордеров на бирже не будет")
print(f"РЫНОК         : {s.market_type} (фьючерсы) · маржа {s.margin_mode}")
print(f"ТАЙМФРЕЙМ     : {s.timeframe}   (сигналов тем больше, чем меньше таймфрейм)")
print(f"ЭКВИТИ        : {_fmt(s.paper_balance)} USDT")
print(f"РАЗМЕР СДЕЛКИ : {size} · риск {s.risk_per_trade_pct}% · плечо {s.leverage}x")
print(f"ПОЗИЦИЙ       : максимум {s.max_open_positions}")
print(f"СТРАТЕГИЯ     : EMA{s.ema_fast}/{s.ema_slow} · RSI лонг {s.rsi_long_min:.0f}-{s.rsi_long_max:.0f} · "
      f"шорт {s.rsi_short_min:.0f}-{s.rsi_short_max:.0f} · SL {s.stop_atr:g} ATR / TP {s.take_atr:g} ATR")
print(f"ФИЛЬТРЫ       : мин. стоп {s.min_stop_pct:g}% · оборот от "
      f"{s.min_symbol_volume / 1e6:g} млн USDT/24ч")
print("=" * 92)

broker = CcxtBroker(s, paper=True)
supported = broker.supported_symbols or set()
pairs = broker.active_symbols()
if not pairs:
    print("🚨 AUTO-отбор не нашёл ни одной пары. Проверьте MIN_SYMBOL_VOLUME "
          "и POSITION_VALUE.")
    raise SystemExit(1)
if s.symbols_auto:
    all_swaps = sum(1 for m in broker.exchange.markets.values()
                    if m.get("swap") and m.get("settle") == "USDT"
                    and m.get("active") is not False)
    print(f"РЕЖИМ ПАР     : AUTO — на бирже {all_swaps} фьючерсов USDT, "
          f"бот отобрал {len(pairs)} (лимит MAX_SYMBOLS={s.max_symbols}, "
          f"оборот от {s.min_symbol_volume / 1e6:g} млн USDT/24ч)")
    print(f"                каждый проход = {len(pairs)} запросов к бирже")
missing = [sym for sym in pairs if sym not in supported]
if missing:
    print("⚠️  Нет на бирже:", ", ".join(missing))

# Пары, на которых физически нельзя выставить POSITION_VALUE: биржа требует
# минимум 1 контракт, а 1 контракт стоит дороже нашей суммы сделки.
too_expensive = []
for sym in pairs:
    if sym not in supported:
        continue
    try:
        broker.ohlcv(sym)
        floor = broker.min_notional(sym)
    except Exception:
        continue
    if s.position_value > 0 and floor > s.position_value:
        too_expensive.append((sym, broker.contract_size(sym), floor))
if too_expensive:
    print()
    print(f"⚠️  Недоступны при POSITION_VALUE={_fmt(s.position_value)} USDT "
          f"(минимальный лот 1 контракт дороже сделки):")
    for sym, size, floor in too_expensive:
        print(f"     {sym:<20} 1 контракт = {_fmt(size)} монет = {_fmt(floor)} USDT"
              f"  → поднимите POSITION_VALUE или уберите пару")
    print("=" * 92)

strat = EmaRsiAtrStrategy(
    fast=s.ema_fast, slow=s.ema_slow, rsi_period=s.rsi_period, atr_period=s.atr_period,
    rsi_long=(s.rsi_long_min, s.rsi_long_max), rsi_short=(s.rsi_short_min, s.rsi_short_max),
    stop_atr=s.stop_atr, take_atr=s.take_atr,
)
risk = RiskManager(s, state_path=None)
equity = broker.equity()
print()
print("%-18s %12s %6s %12s %12s  %s" % ("ПАРА", "ЦЕНА", "СИГНАЛ", "СТОП", "ТЕЙК", "ЧТО БУДЕТ"))
print("-" * 92)
entries = 0
margin_used = 0.0
for sym in pairs:
    if sym not in supported:
        continue
    try:
        df = broker.ohlcv(sym)
        price = broker.price(sym)
    except Exception as exc:
        print("%-18s ОШИБКА %s: %s" % (sym, type(exc).__name__, exc))
        continue

    sig = strat.generate(sym, df)
    if sig.side == "hold":
        print("%-18s %12s %6s %12s %12s  ⏭️  %s" % (sym, _fmt(price), "—", "—", "—",
                                                    sig.why_not or "нет сигнала"))
        continue

    qty, why = risk.plan(equity, sig.side, price, sig.stop, 0)
    fitted, why = (broker.fit_qty(sym, qty) if qty > 0 else (0.0, why))
    if fitted > 0:
        notional = broker.notional(sym, fitted, price)
        tp = ((sig.take_profit - price) if sig.side == "buy" else (price - sig.take_profit)) / price * 100
        entries += 1
        margin_used += notional / s.leverage
        what = ("✅ %s %s = %s USDT · маржа %s · профит %+.2f%%"
                % (_fmt(fitted), contracts_word(fitted), _fmt(notional),
                   _fmt(notional / s.leverage), tp))
    else:
        what = "🚫 НЕ ОТКРЫТЬ — %s" % (why if why != "ok" else "меньше минимума биржи")
    print("%-18s %12s %6s %12s %12s  %s" % (
        sym, _fmt(price), "ЛОНГ" if sig.side == "buy" else "ШОРТ",
        _fmt(sig.stop), _fmt(sig.take_profit), what))
print("-" * 92)
print(f"Сейчас готово к открытию: {entries} из {len(pairs)} пар · "
      f"лимит позиций {s.max_open_positions} · маржа при {min(entries, s.max_open_positions)} "
      f"сделках ≈ {_fmt(margin_used * min(entries, s.max_open_positions) / max(entries, 1))} USDT "
      f"из {_fmt(s.paper_balance)} USDT")
print("=" * 92)
