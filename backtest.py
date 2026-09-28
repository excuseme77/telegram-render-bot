"""Бэктест на истории с теми же стратегией и риск-менеджером, что в боевом движке.

  python backtest.py --symbol BTC/USDT --timeframe 15m --days 90

Допущения (сознательно пессимистичные): вход по цене открытия свечи + проскальзывание,
если в одной свече задеты и стоп, и тейк - считается, что сработал стоп, комиссия на входе и выходе.
"""
import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timezone, date

import pandas as pd

import config
from risk import RiskManager
from strategy import EmaRsiAtrStrategy

TAKER_FEE = 0.00055
SLIPPAGE = 0.0005
WARMUP = 60
WINDOW = 300


def load_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    os.makedirs("data", exist_ok=True)
    path = f"data/{symbol.replace('/', '_')}_{timeframe}_{days}d.csv"
    if os.path.exists(path):
        return pd.read_csv(path)

    import ccxt
    ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    step_ms = ex.parse_timeframe(timeframe) * 1000
    now = ex.milliseconds()
    since, rows = now - days * 86_400_000, []
    while since < now:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + step_ms
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


@dataclass
class Open:
    side: str
    qty: float
    entry: float
    sl: float
    tp: float
    ts: int


def run_backtest(df: pd.DataFrame, s, strategy, symbol: str = "X"):
    risk = RiskManager(s)                      # без сохранения состояния
    balance = s.paper_balance
    pos: Open | None = None
    trades, curve = [], []

    def close(px: float, ts: int, why: str):
        nonlocal balance, pos
        d = 1 if pos.side == "buy" else -1
        pnl = d * (px - pos.entry) * pos.qty - px * pos.qty * TAKER_FEE
        balance += pnl
        trades.append({"open_ts": pos.ts, "close_ts": ts, "side": pos.side, "qty": pos.qty,
                       "entry": pos.entry, "exit": px, "pnl": pnl, "why": why})
        pos = None

    for i in range(WARMUP, len(df)):
        row = df.iloc[i]
        ts = int(row["timestamp"])

        # 1) вход в начале свечи, если позиции нет
        if pos is None and not risk.halted:
            window = df.iloc[max(0, i - WINDOW + 1): i + 1].copy()
            window.iloc[-1, window.columns.get_loc("close")] = row["open"]   # без подглядывания
            sig = strategy.generate(symbol, window)
            if sig.side != "hold":
                qty, _ = risk.plan(balance, sig.side, float(row["open"]), sig.stop, 0)
                if qty > 0:
                    fill = row["open"] * (1 + SLIPPAGE if sig.side == "buy" else 1 - SLIPPAGE)
                    balance -= fill * qty * TAKER_FEE
                    pos = Open(sig.side, qty, float(fill), sig.stop, sig.take_profit, ts)

        # 2) стоп/тейк внутри этой свечи (стоп приоритетнее)
        if pos:
            long = pos.side == "buy"
            if (long and row["low"] <= pos.sl) or (not long and row["high"] >= pos.sl):
                close(pos.sl * (1 - SLIPPAGE if long else 1 + SLIPPAGE), ts, "stop-loss")
            elif (long and row["high"] >= pos.tp) or (not long and row["low"] <= pos.tp):
                close(pos.tp, ts, "take-profit")

        # 3) капитал на конец свечи и риск-лимиты
        upnl = 0.0
        if pos:
            upnl = (1 if pos.side == "buy" else -1) * (row["close"] - pos.entry) * pos.qty
        equity = balance + upnl
        curve.append(equity)
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        risk.update(equity, day)
        if risk.halted and pos:
            close(float(row["close"]), ts, "kill-switch")
        if risk.halt_kind == "drawdown":
            break

    if pos:
        close(float(df.iloc[-1]["close"]), int(df.iloc[-1]["timestamp"]), "конец данных")
    return pd.DataFrame(trades), pd.Series(curve, dtype=float), risk


def report(trades: pd.DataFrame, curve: pd.Series, start_balance: float, risk):
    if trades.empty:
        print("Сделок не было.")
        return
    wins, losses = trades[trades.pnl > 0], trades[trades.pnl <= 0]
    dd = ((curve.cummax() - curve) / curve.cummax()).max() * 100
    pf = wins.pnl.sum() / abs(losses.pnl.sum()) if len(losses) and losses.pnl.sum() else float("inf")
    print(f"Сделок:            {len(trades)}")
    print(f"Доля прибыльных:   {len(wins) / len(trades) * 100:.1f}%")
    print(f"Profit factor:     {pf:.2f}")
    print(f"Средняя сделка:    {trades.pnl.mean():+.2f} USDT")
    print(f"Итог:              {curve.iloc[-1] - start_balance:+.2f} USDT "
          f"({(curve.iloc[-1] / start_balance - 1) * 100:+.1f}%)")
    print(f"Макс. просадка:    {dd:.1f}%")
    if risk.halted:
        print(f"Тест остановлен kill-switch: {risk.halt_reason}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()

    s = config.load()
    data = load_history(a.symbol, a.timeframe, a.days)
    print(f"{a.symbol} {a.timeframe}: {len(data)} свечей, старт {s.paper_balance} USDT, "
          f"плечо {s.leverage}x, риск {s.risk_per_trade_pct}%/сделка\n")
    tr, eq, rk = run_backtest(data, s, EmaRsiAtrStrategy(), a.symbol)
    report(tr, eq, s.paper_balance, rk)
    tr.to_csv("backtest_trades.csv", index=False)
