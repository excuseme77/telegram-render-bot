"""Риск-менеджер: последнее слово по любой сделке. ИИ может только предлагать."""
import json
import logging
import os
from datetime import date, datetime, timezone

from config import Settings

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self, s: Settings, state_path: str | None = None):
        self.s = s
        self.state_path = state_path   # None в бэтесте
        self.peak = None
        self.day = None
        self.day_start = None
        self.halt_reason = None
        self.halt_kind = None  # "drawdown" | "daily"
        self._load()

    def _load(self):
        if self.state_path and os.path.exists(self.state_path):
            d = json.load(open(self.state_path))
            # Смена режима (paper -> live) меняет депозит на порядки. Если тащить
            # старый peak, новый запуск сразу получит "просадку 100%" и килл-китч.
            if d.get("mode") not in (None, self.s.mode):
                log.warning("Сброс риск-стата: режим изменился (%s -> %s)", d.get("mode"), self.s.mode)
                return
            self.peak, self.day_start = d.get("peak"), d.get("day_start")
            self.day = date.fromisoformat(d["day"]) if d.get("day") else None
            self.halt_reason, self.halt_kind = d.get("halt_reason"), d.get("halt_kind")

    def _save(self):
        if self.state_path:
            json.dump({"peak": self.peak, "day": str(self.day), "day_start": self.day_start,
                       "halt_reason": self.halt_reason, "halt_kind": self.halt_kind,
                       "mode": self.s.mode},
                      open(self.state_path, "w"))

    @property
    def halted(self) -> bool:
        return self.halt_reason is not None

    def _halt(self, kind: str, reason: str):
        self.halt_kind, self.halt_reason = kind, reason

    def update(self, equity: float, today: date | None = None):
        """Вызывать на каждом шаге с текущим капиталом."""
        today = today or datetime.now(timezone.utc).date()
        if not equity or equity <= 0:
            # Не даём нулевому или битому балансу обнулить peak и сработать стопу.
            return
        if self.peak and (equity > self.peak * 2 or equity * 2 < self.peak):
            # Депозит изменился в 2+ раза (перевод, смена режима, сброс просадки) —
            # старый пик нерелевантен. Раньше было 5x, но 5 vs 10.25 не триггерило.
            log.warning("Депозит изменился с %.2f на %.2f (в %.1fx) - сбрасываю базу риск-стата",
                        self.peak, equity, self.peak / equity if equity else 0)
            self.peak = self.day_start = equity
            self.day = today
            self.halt_reason = self.halt_kind = None
        if self.peak is None:
            self.peak = equity
        if self.day != today:
            self.day, self.day_start = today, equity
            if self.halt_kind == "daily":  # дневной лимит сбрасывается сам
                self.halt_reason = self.halt_kind = None
        self.peak = max(self.peak, equity)
        self._check(equity)
        self._save()

    def _check(self, equity: float):
        if self.halted:
            return
        drawdown = (self.peak - equity) / self.peak * 100
        if drawdown >= self.s.max_drawdown_pct:
            self._halt("drawdown", f"просадка {drawdown:.1f}% от пика")
            return
        day_loss = (self.day_start - equity) / self.day_start * 100
        if day_loss >= self.s.daily_loss_pct:
            self._halt("daily", f"дневной убыток {day_loss:.1f}%")

    def reset(self, equity: float):
        """Ручной сброс после остановки по просадке (команда /run)."""
        self.peak = equity
        self.halt_reason = self.halt_kind = None
        self._save()

    def plan(self, equity: float, side: str, entry: float, stop: float, open_count: int):
        """Возвращает (qty, причина). qty == 0 -> сделку не открывать."""
        s = self.s
        if self.halted:
            return 0.0, f"остановлено: {self.halt_reason}"
        if open_count >= s.max_open_positions:
            return 0.0, "достигнут лимит открытых позиций"
        if (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
            return 0.0, "стоп-лосс с неверной стороны"
        dist = abs(entry - stop) / entry
        if dist > 0.8 / s.leverage:
            return 0.0, "стоп слишком далеко: риск ликвидации раньше стопа"
        if dist * 100 < s.min_stop_pct:
            # На спотовых индексах и сырье (SPY, SILVER, USOIL) ATR ничтожен:
            # стоп в 0.02% и цель в 0.03%. Такая сделка — чистая комиссия,
            # биржевой спред съедает весь профит ещё до срабатывания тейка.
            return 0.0, (f"стоп слишком узкий ({dist * 100:.2f}% < {s.min_stop_pct:g}%) — "
                         f"весь профит съест комиссия и спред")

        risk_usdt = equity * s.risk_per_trade_pct / 100
        qty_by_risk = risk_usdt / abs(entry - stop)
        if s.position_value > 0:
            # Фиксированный объём в USDT — предсказуемее, чем процент от депозита.
            qty_cap = s.position_value / entry
        else:
            qty_cap = equity * s.max_position_pct / 100 * s.leverage / entry
        return min(qty_by_risk, qty_cap), "ok"
