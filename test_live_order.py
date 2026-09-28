"""Проверка живой торговли на MEXC одной минимальной сделкой.

Запуск:  python -X utf8 test_live_order.py

Зачем это нужно. Бумажный режим не выполняет запросы к бирже, поэтому
"всё работает" в paper ничего не говорит о реальных заявках. Скрипт
последовательно проверяет КАЖДЫЙ вызов, который делает бот в live:

  1. связь с биржей и реальный баланс;
  2. режим позиции (hedge / one-way) и режим маржи;
  3. установку плеча;
  4. открытие позиции;
  5. постановку стоп-лосса как plan-ордера;
  6. что стоп действительно ВИДЕН в списке открытых заявок биржи;
  7. закрытие позиции и снятие защиты.

Каждый шаг печатает ✅ или ❌ с ответом биржи. Скрипт в конце всегда
закрывает позицию и снимает защитные ордера, даже если упал на середине.

ВНИМАНИЕ: это реальная сделка на реальные деньги. Сумма берётся из
TEST_POSITION_VALUE (по умолчанию минимально допустимая).
"""
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

import dataclasses
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from broker import CcxtBroker, _fmt, _money, contracts_word

TEST_POSITION_VALUE = float(os.getenv("TEST_POSITION_VALUE", "0"))
TEST_SYMBOL = os.getenv("TEST_SYMBOL", "").strip().upper()

results = []


def step(title):
    """Один шаг проверки. Ошибка записывается и ПРОБРАСЫВАЕТСЯ дальше.

    Раньше здесь стоял ``return True``, и проверка продолжалась после
    неудачного шага: если размер не посчитался, бирже уходил ордер с
    нулевым количеством. Проверка обязана останавливаться на первом
    красном шаге — иначе следующие шаги проверяют уже не то.
    """
    class _Step:
        def __enter__(self):
            print(f"\n▶ {title}")
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                print(f"✅ {title}")
                results.append((title, True, ""))
                return False
            print(f"❌ {title} — {type(exc).__name__}: {exc}")
            results.append((title, False, f"{type(exc).__name__}: {exc}"))
            return False        # не глушим — вызывающий код сам решит, что делать
    return _Step()


def main():
    os.environ["MODE"] = "live"
    os.environ["CONFIRM_LIVE"] = "YES"
    s = config.load()
    # Тестовая сделка должна быть маленькой и ровно одна, иначе рискнем всем
    # депозитом. POSITION_VALUE из .env здесь намеренно игнорируется.
    s = dataclasses.replace(
        s,
        mode="live",
        symbols_auto=False,
        symbols=[config._normalize_symbol(TEST_SYMBOL or "SUI", s.market_type)],
        # Размер — как в .env. Раньше здесь жёстко стояло 1.0 USDT, и почти
        # любая пара отклонялась: минимальный лот MEXC (1 контракт) у SUI
        # равен 1.17 USDT, XRP — больше. Тест падал на расчёте размера, а не
        # проверял то, ради чего написан. TEST_POSITION_VALUE переопределяет.
        position_value=TEST_POSITION_VALUE or s.position_value or 3.0,
        max_open_positions=1,
    )
    symbol = s.symbols[0]

    print("=" * 78)
    print("ПРОВЕРКА ЖИВОЙ ТОРГОВЛИ MEXC — это реальная сделка на реальные деньги")
    print(f"Пара {symbol}, сделка ≈ {_money(s.position_value)} USDT, плечо {s.leverage}x")
    print("=" * 78)

    broker = CcxtBroker(s, paper=False)
    qty = 0
    entry = 0.0
    protection = {}
    side_was = "buy"

    try:
        with step("1. Связь с биржей и реальный баланс"):
            equity = broker.equity()
            print(f"   Баланс USDT на фьючерсах: {_money(equity)}")
            print(f"   Рынок {s.market_type}, маржа {s.margin_mode}, плечо {s.leverage}x")
            if equity < 1:
                raise RuntimeError(f"на балансе {_money(equity)} USDT — торговать нечем")

        with step(f"2. Метаданные пары {symbol}"):
            size = broker.contract_size(symbol)
            market = broker.exchange.market(symbol)
            price = broker.price(symbol)
            print(f"   1 контракт = {_fmt(size)} монет = {_money(size * price)} USDT")
            print(f"   Минимум биржи: {market.get('limits')}")
            print(f"   Цена: {_fmt(price)}")

        with step(f"3. Режим позиции (hedge/one-way) и маржа {s.margin_mode}"):
            broker._detect_position_mode()
            print(f"   Hedge mode: {broker._hedge_mode}  "
                  f"(в one-way сторона закрытия инвертируется)")

        with step(f"4. Установка плеча {s.leverage}x"):
            broker._set_leverage(symbol, "buy", s.leverage)
            print(f"   Параметры: {broker._entry_params(s.leverage)}")

        with step("5. Расчёт размера позиции"):
            # Очень узкий стоп, чтобы тестовая сделка получилась максимально
            # маленькой: нам нужно проверить проводку, а не заработать.
            stop = broker.round_price(symbol, price * 0.999)
            take = broker.round_price(symbol, price * 1.002)
            coins = s.position_value / price
            qty, why = broker.fit_qty(symbol, coins)
            if qty <= 0:
                raise RuntimeError(f"размер не проходит биржу: {why}")
            entry = price
            print(f"   {qty} {contracts_word(qty)} = {_money(broker.notional(symbol, qty, price))} USDT")
            print(f"   Стоп {_fmt(stop)} · тейк {_fmt(take)}")

        with step("6. Открытие позиции (create_order)"):
            fill = broker.open(symbol, side_was, qty, stop, take, s.leverage)
            print(f"   Исполнено по цене {_fmt(fill)}")
            print(f"   Номинал: {_money(broker.notional(symbol, qty, fill))} USDT")
            print(f"   Сторона закрытия по расчёту бота: {broker._close_side(side_was)}")

        with step("7. Проверка позиции на бирже"):
            positions = broker.positions()
            mine = [p for p in positions if p.symbol == symbol]
            if not mine:
                raise RuntimeError("биржа не показывает открытую позицию")
            print(f"   Позиция на бирже: {mine[0].qty} {contracts_word(mine[0].qty)} "
                  f"по {_fmt(mine[0].entry)}")
            protection = broker._protection.get(f"{symbol}|buy", {})

        with step("8. Стоп-лосс виден в план-ордерах биржи"):
            # Именно planorder/list/orders, а не fetch_open_orders: стоп и тейк
            # MEXC держит в отдельном списке и в обычных заявках их нет.
            orders = broker.plan_orders(symbol)
            print(f"   План-ордеров по паре: {len(orders)}")
            for o in orders:
                print(f"   · {o.get('orderId')} side={o.get('side')} "
                      f"trigger={o.get('triggerPrice')} type={o.get('orderType')} "
                      f"triggerType={o.get('triggerType')} holdSide={o.get('holdSide')} "
                      f"qty={o.get('quantity') or o.get('qty')}")
            if not orders:
                raise RuntimeError(
                    "ни одного план-ордера нет — стоп-лосс НЕ выставлен. "
                    "Позиция без защиты, закрывайте вручную прямо сейчас")
            ours = [o for o in orders
                    if str(o.get("orderId")) in {str(v) for v in protection.values() if v}]
            if not ours:
                raise RuntimeError(f"наши ордера {protection} биржа не показывает")
            # Сторона обязана быть обратной входу: вошли buy -> выходим sell.
            wrong = [o for o in ours if str(o.get("side")).lower()
                     in ("buy", "1", "4") and side_was == "buy"]
            if wrong:
                raise RuntimeError(
                    f"план-ордеры {wrong} имеют ту же сторону, что и вход ({side_was}) — "
                    "они защищают не ту позицию")
            if not protection.get("sl"):
                raise RuntimeError("стоп-лосс не подтверждён брокером")
            print(f"   ✅ Подтверждено биржей: {[o.get('orderId') for o in ours]}")

    except Exception as exc:
        print(f"\n🚨 Проверка прервана: {type(exc).__name__}: {exc}")

    finally:
        print("\n" + "-" * 78)
        print("Закрываю позицию и снимаю защиту...")
        try:
            broker.close_all()
            left = [p for p in broker.positions() if p.symbol == symbol]
            print(f"✅ Позиций по {symbol} на бирже осталось: {len(left)}")
            if left:
                print("🚨 ОСТАЛАСЬ ПОЗИЦИЯ — ЗАКРОЙТЕ ВРУЧНУЮ В ПРИЛОЖЕНИИ MEXC!")
        except Exception as exc:
            print(f"🚨 Не удалось закрыть автоматически ({type(exc).__name__}: {exc})")
            print("🚨 ЗАКРОЙТЕ ПОЗИЦИЮ ВРУЧНУЮ В ПРИЛОЖЕНИИ MEXC!")

    print("=" * 78)
    print("ИТОГ ПРОВЕРКИ")
    print("=" * 78)
    for title, ok, err in results:
        print(f"  {'✅' if ok else '❌'} {title}" + (f"  → {err}" if err else ""))
    failed = [t for t, ok, _ in results if not ok]
    if failed:
        print(f"\nНе прошли: {len(failed)} из {len(results)}")
        print("Пока эти шаги не зелёные, MODE=live запускать рано — "
              "иначе позиции останутся без стоп-лосса.")
        return 1
    print(f"\nВсе {len(results)} шагов прошли. Живая торговля работает, "
          f"можно ставить MODE=live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
