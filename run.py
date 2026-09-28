"""Запуск из консоли:  python run.py
Остановка: Ctrl+C (позиции остаются со стопами) или создать файл STOP (закроет всё)."""
import os
import sys

# Enable UTF-8 mode for httpx/ccxt compatibility on Windows
# IMPORTANT: Run with: python -X utf8 run.py
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

if sys.flags.utf8_mode == 0:
    print("WARNING: Not running in UTF-8 mode! Use: python -X utf8 run.py", flush=True)
    print("Without -X utf8, ccxt/httpx may fail with encoding errors on Windows.", flush=True)

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)

import config
from broker import make_broker
from engine import Engine
from risk import RiskManager
from strategy import EmaRsiAtrStrategy
from telegram_bot import TelegramController
from ai_analyzer import AIAnalyzer

if __name__ == "__main__":
    s = config.load()
    # Баг-фикс: risk_state.json был общим для paper/demo/live. Peak/баланс из paper-теста
    # (например, 1000 USDT) не совместим по масштабу с реальным live-балансом (например,
    # 10 USDT) — риск-менеджер видел мнимую просадку ~99% и мгновенно бил kill-switch
    # сразу при старте, даже без единой сделки. Состояние теперь разделено по режиму.
    risk_state_path = f"risk_state_{s.mode}.json"
    # Брокер с callback для уведомлений о закрытии сделок
    broker = make_broker(s, trade_close_callback=lambda **kw: engine.notify_trade_close(**kw))
    engine = Engine(
        s,
        broker=broker,
        strategy=EmaRsiAtrStrategy(
            fast=s.ema_fast,
            slow=s.ema_slow,
            rsi_period=s.rsi_period,
            atr_period=s.atr_period,
            rsi_long=(s.rsi_long_min, s.rsi_long_max),
            rsi_short=(s.rsi_short_min, s.rsi_short_max),
            stop_atr=s.stop_atr,
            take_atr=s.take_atr,
            take_profit_min_pct=s.take_profit_min_pct,
            take_profit_max_pct=s.take_profit_max_pct,
            max_stop_pct=s.max_stop_pct,
            min_reward_ratio=s.min_reward_ratio,
        ),
        risk=RiskManager(s, state_path=risk_state_path),
        ai_analyzer=AIAnalyzer(s) if s.ai_enabled else None,
    )
    telegram = TelegramController(s, engine)
    engine._telegram_controller = telegram
    telegram.start()
    engine.run()
