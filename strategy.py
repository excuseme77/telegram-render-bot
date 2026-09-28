"""Техническая стратегия: тренд по EMA + фильтр RSI + стоп/тейк по ATR.

Стратегия не является обещанием доходности и не размещает ордера самостоятельно.

Главное отличие от «молчаливых» версий: если входа нет, поле ``why_not``
всегда заполнено человекочитаемой причиной с числами. Движок печатает её
в терминал, поэтому по каждой паре видно, что именно бот видит и чего ждёт.
"""
from dataclasses import dataclass, field

import pandas as pd

from broker import _fmt


@dataclass(frozen=True)
class Signal:
    side: str = "hold"                 # buy | sell | hold
    stop: float = 0.0
    take_profit: float = 0.0
    reason: str = ""                   # почему открываем (для buy/sell)
    why_not: str = ""                  # почему НЕ открываем (для hold)
    indicators: dict = field(default_factory=dict)


class EmaRsiAtrStrategy:
    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        rsi_long: tuple = (50, 70),
        rsi_short: tuple = (30, 50),
        stop_atr: float = 2.0,
        take_atr: float = 3.0,
        take_profit_min_pct: float = 2.0,
        take_profit_max_pct: float = 3.0,
        max_stop_pct: float = 4.0,
        min_reward_ratio: float = 1.0,
    ):
        self.fast, self.slow = fast, slow
        self.rsi_period, self.atr_period = rsi_period, atr_period
        self.rsi_long, self.rsi_short = rsi_long, rsi_short
        self.stop_atr, self.take_atr = stop_atr, take_atr
        self.tp_min = take_profit_min_pct
        self.tp_max = take_profit_max_pct
        self.max_stop_pct = max_stop_pct
        self.min_rr = min_reward_ratio

    def _levels(self, price: float, atr: float, side: str):
        """Стоп и цель для входа.

        Раньше и стоп, и цель брались из ATR, и цель на 5-минутных свечах
        выходила 0.5-1.9% — меньше процента, комиссия съедала смысл сделки.
        Теперь цель задаётся процентами и удерживается в диапазоне
        TAKE_PROFIT_MIN_PCT..TAKE_PROFIT_MAX_PCT, а стоп расширяется так, чтобы
        отношение цели к стопу было не хуже MIN_REWARD_RATIO, но не шире
        MAX_STOP_PCT.
        """
        tp_pct = min(max(self.take_atr * atr / price * 100, self.tp_min), self.tp_max)
        tp_dist = price * tp_pct / 100
        stop_dist = self.stop_atr * atr
        # Стоп не уже цели/min_rr, иначе сделка заведомо проигрышная.
        stop_dist = max(stop_dist, tp_dist / self.min_rr if self.min_rr > 0 else 0.0)
        stop_dist = min(stop_dist, price * self.max_stop_pct / 100)
        if side == "buy":
            return price - stop_dist, price + tp_dist
        return price + stop_dist, price - tp_dist

    # ------------------------------------------------------------------ helpers

    def _hold(self, why: str, indicators: dict | None = None) -> Signal:
        return Signal(why_not=why, indicators=indicators or {})

    def _trend_label(self) -> str:
        return f"EMA{self.fast}/EMA{self.slow}"

    # ------------------------------------------------------------------- main

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        required = {"open", "high", "low", "close"}
        if df is None or not required.issubset(getattr(df, "columns", [])):
            return self._hold("биржа не отдала OHLCV")
        if len(df) < self.slow + 2:
            return self._hold(
                f"мало свечей: {len(df)}, нужно {self.slow + 2} ({self._trend_label()})"
            )

        close = df["close"].astype(float)
        high, low = df["high"].astype(float), df["low"].astype(float)

        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rsi = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))

        tr = pd.concat(
            [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()

        price = float(close.iloc[-1])
        ema_f, ema_s = float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])
        rsi_now = float(rsi.iloc[-1])
        vol = float(atr.iloc[-1])

        if pd.isna(rsi_now) or pd.isna(vol) or not vol or vol <= 0:
            return self._hold("индикаторы ещё не рассчитались (мало истории)")

        ind = {
            "price": price,
            "rsi": rsi_now,
            "ema_fast": ema_f,
            "ema_slow": ema_s,
            "atr": vol,
            "trend": "вверх" if ema_f > ema_s else ("вниз" if ema_f < ema_s else "боковик"),
        }

        long_lo, long_hi = self.rsi_long
        short_lo, short_hi = self.rsi_short
        trend_up = ema_f > ema_s
        trend_down = ema_f < ema_s
        long_ok = long_lo <= rsi_now <= long_hi
        short_ok = short_lo <= rsi_now <= short_hi

        if trend_up and long_ok:
            stop, target = self._levels(price, vol, "buy")
            return Signal(
                "buy", stop, target,
                reason=(
                    f"📈 тренд вверх {self._trend_label()} {_fmt(ema_f)}>{_fmt(ema_s)}"
                    f" · RSI {rsi_now:.1f} в лонге {long_lo:g}-{long_hi:g}"
                    f" · цель {abs(target - price) / price * 100:.1f}%"
                ),
                indicators=ind,
            )
        if trend_down and short_ok:
            stop, target = self._levels(price, vol, "sell")
            return Signal(
                "sell", stop, target,
                reason=(
                    f"📉 тренд вниз {self._trend_label()} {_fmt(ema_f)}<{_fmt(ema_s)}"
                    f" · RSI {rsi_now:.1f} в шорте {short_lo:g}-{short_hi:g}"
                    f" · цель {abs(target - price) / price * 100:.1f}%"
                ),
                indicators=ind,
            )

        # ---- разбор, почему входа нет -------------------------------------
        if trend_up and not long_ok and short_ok:
            why = (f"тренд вверх, а RSI {rsi_now:.1f} в зоне шорта "
                   f"{short_lo:g}-{short_hi:g} — ждём отката")
        elif trend_down and not short_ok and long_ok:
            why = (f"тренд вниз, а RSI {rsi_now:.1f} в зоне лонга "
                   f"{long_lo:g}-{long_hi:g} — ждём отбоя")
        elif trend_up:
            why = f"тренд вверх, но RSI {rsi_now:.1f} вне лонга {long_lo:g}-{long_hi:g}"
        elif trend_down:
            why = f"тренд вниз, но RSI {rsi_now:.1f} вне шорта {short_lo:g}-{short_hi:g}"
        else:
            why = f"{self._trend_label()} {_fmt(ema_f)}≈{_fmt(ema_s)} — тренда нет, RSI {rsi_now:.1f}"
        return self._hold(why, ind)
