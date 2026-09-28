"""Проверка новой логики цели: сколько реально приносит сделка.

Запуск: python -X utf8 check_target.py
"""
import os
import sys

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import logging
logging.basicConfig(level=logging.ERROR)
logging.getLogger("ccxt").setLevel(logging.ERROR)

import config
import strategy as st

PAIRS = ["LINK/USDT:USDT", "ADA/USDT:USDT", "SUI/USDT:USDT",
         "XRP/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"]
s = config.load()
old = st.EmaRsiAtrStrategy(
    fast=s.ema_fast, slow=s.ema_slow, rsi_period=s.rsi_period, atr_period=s.atr_period,
    rsi_long=(s.rsi_long_min, s.rsi_long_max), rsi_short=(s.rsi_short_min, s.rsi_short_max),
    stop_atr=s.stop_atr, take_atr=s.take_atr,
)
new = st.EmaRsiAtrStrategy(
    fast=s.ema_fast, slow=s.ema_slow, rsi_period=s.rsi_period, atr_period=s.atr_period,
    rsi_long=(s.rsi_long_min, s.rsi_long_max), rsi_short=(s.rsi_short_min, s.rsi_short_max),
    stop_atr=s.stop_atr, take_atr=s.take_atr,
    take_profit_min_pct=s.take_profit_min_pct, take_profit_max_pct=s.take_profit_max_pct,
    max_stop_pct=s.max_stop_pct, min_reward_ratio=s.min_reward_ratio,
)

import ccxt
ex = ccxt.mexc({"enableRateLimit": True})
ex.load_markets()

print(f"{'пара':<10}{'цена':>12}{'ATR':>11}"
      f"{'старая цель':>14}{'новая цель':>14}{'стоп':>10}{'R:R':>8}")
print("-" * 79)
for pair in PAIRS:
    try:
        df = ex.fetch_ohlcv(pair, s.timeframe, limit=250)
    except Exception as exc:
        print(f"{pair.split('/')[0]:<10} ошибка: {exc}")
        continue
    import pandas as pd
    data = pd.DataFrame(df, columns=["ts", "open", "high", "low", "close", "vol"])
    price = float(data["close"].iloc[-1])
    tr = pd.concat([data["high"] - data["low"],
                    (data["high"] - data["close"].shift()).abs(),
                    (data["low"] - data["close"].shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(s.atr_period).mean().iloc[-1])

    # Старая логика — чистая формула 3×ATR, без новых процентных границ.
    o_tp = price + s.take_atr * atr
    o_stop = price - s.stop_atr * atr
    n_stop, n_tp = new._levels(price, atr, "buy")
    o_pct = (o_tp - price) / price * 100
    n_pct = (n_tp - price) / price * 100
    s_pct = (price - n_stop) / price * 100
    rr = n_pct / s_pct if s_pct else 0
    print(f"{pair.split('/')[0]:<10}{price:>12.6f}{atr:>11.6f}"
          f"{o_pct:>13.2f}%{n_pct:>13.2f}%{s_pct:>9.2f}%{rr:>8.2f}")

print()
print(f"Плечо {s.leverage}x, объём сделки {s.position_value} USDT")
print(f"Маржа на позицию: {s.position_value / s.leverage:.3f} USDT")
print(f"При цели 2.5% профит по сделке: {s.position_value * 0.025:.4f} USDT")
print(f"При стопе 3%   убыток по сделке: {s.position_value * 0.03:.4f} USDT")
print(f"Ликвидация примерно на {100 / s.leverage:.1f}% против движения")
