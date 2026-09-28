"""Торговый движок. Не знает ни про Telegram, ни про конкретную стратегию:
берёт сигнал у strategy, прогоняет через RiskManager и отдаёт брокеру.

Принцип вывода: бот ничего не делает молча. По каждой паре на каждой свече
в журнал попадает ровно одна строка — либо вход с расчётом профита и риска,
либо «почему сделка не открыта». Плюс итог прохода и пульс, когда сделок нет.
"""
import logging
import os
import sqlite3
import time
import threading
from dataclasses import replace
from datetime import datetime, timezone, date

from colorama import Fore, Style, init as colorama_init
colorama_init(strip=False)

from broker import OrderRejected, SymbolUnavailable, _fmt, _money, contracts_word

log = logging.getLogger("engine")

# Цвета для терминала
GREEN = Fore.GREEN + Style.BRIGHT
RED = Fore.RED + Style.BRIGHT
YELLOW = Fore.YELLOW + Style.BRIGHT
CYAN = Fore.CYAN + Style.BRIGHT
MAGENTA = Fore.MAGENTA + Style.BRIGHT
RESET = Style.RESET_ALL

STOP_FILE = "STOP"

MODE_BADGE = {
    "paper": "📄 PAPER — сделки НЕ отправляются на биржу, всё считается локально",
    "demo": "🧪 DEMO — тестовая площадка MEXC, реальные деньги не рискуются",
    "live": "💰 LIVE — РЕАЛЬНЫЕ ДЕНЬГИ, ордера идут на биржу",
}


class Engine:
    def __init__(self, s, broker, strategy, risk, notify=None, ai_analyzer=None):
        self.s, self.broker, self.strategy, self.risk = s, broker, strategy, risk
        self.ai_analyzer = ai_analyzer
        self.notify = notify or (lambda text: None)   # сюда потом подключим Telegram
        self._telegram_controller = None
        self._last_entry_scan = 0.0
        self._last_entry_attempt: dict[str, tuple[int, str, float]] = {}
        self._last_logged_candle: dict[str, int] = {}
        self._flat_after_halt = False
        self._running = threading.Event()
        self._running.set()
        self._shutdown = threading.Event()
        self._limit_logged = False
        self._untradeable: dict = {}   # пары, слишком дорогие для заданного объёма сделки
        self._last_activity = time.monotonic()
        self._last_heartbeat = time.monotonic()
        self._last_entry: tuple[str, float] | None = None
        self.db = sqlite3.connect("trades.db")
        self.db.execute("CREATE TABLE IF NOT EXISTS trades(ts INT, symbol TEXT, side TEXT,"
                        " qty REAL, entry REAL, sl REAL, tp REAL, reason TEXT)")

    def notify_trade_open(self, symbol: str, side: str, qty: float, entry: float,
                          stop: float, take: float, notional: float):
        """Передаёт уведомление об открытии сделки в Telegram."""
        if self._telegram_controller:
            self._telegram_controller.notify_trade_open(symbol, side, qty, entry, stop, take, notional)

    def notify_trade_close(self, symbol: str, side: str, qty: float, entry: float,
                           exit_price: float, pct: float, pnl: float, reason: str):
        """Передаёт уведомление о закрытии сделки в Telegram."""
        if self._telegram_controller:
            self._telegram_controller.notify_trade_close(symbol, side, qty, entry, exit_price, pct, pnl, reason)

    def _say(self, text: str, *args):
        self._last_activity = time.monotonic()
        log.info(text, *args)
        self.notify(text % args if args else text)

    def _note(self, text: str, *args, level: int = logging.INFO, **kwargs):
        """Строка без Telegram-дублирования, но она сбрасывает таймер пульса."""
        self._last_activity = time.monotonic()
        log.log(level, text, *args, **kwargs)

    @staticmethod
    def _candle_iso(timestamp: int) -> str:
        timestamp_seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

    def _min_rows(self) -> int:
        return max(55, self.s.ema_slow + 5)

    def _heartbeat(self, equity: float, open_count: int, blocked: str | None = None):
        """Пульс: показывает, что бот жив, даже когда сделок нет.

        Раньше при достижении лимита позиций движок просто return-ил и терминал
        замолкал наглухо — выглядело как зависание.
        """
        now = time.monotonic()
        if now - self._last_heartbeat < self.s.heartbeat_seconds:
            return
        self._last_heartbeat = now
        if now - self._last_activity < self.s.heartbeat_seconds:
            return  # только что было что-то важное, не засоряем
        if self._last_entry:
            sym, at = self._last_entry
            ago = int(now - at)
            last = f"{sym} {ago // 60} мин назад" if ago < 3600 else f"{sym} {ago // 3600} ч назад"
        else:
            last = "ещё не было"
        self._note(
            "💓 Бот жив · сканирую %d пар каждые %ds · позиции %d/%d · эквити %s USDT · "
            "последний вход: %s%s",
            len(self._symbols()) - len(self._untradeable), self.s.entry_scan_seconds,
            open_count, self.s.max_open_positions, _money(equity), last,
            f" · ⏸️ {blocked}" if blocked else "",
        )

    def _symbols(self) -> list:
        """Активный список пар: из .env либо подобранный ботом (SYMBOLS=AUTO)."""
        getter = getattr(self.broker, "active_symbols", None)
        return list(getter()) if getter else list(self.s.symbols)

    def _entry_report(self, symbol, sig, qty, fill, equity) -> str:
        """Карточка входа. Профит и риск — отдельными строками, с деньгами. Цветной вывод."""
        is_buy = sig.side == "buy"
        notional = self.broker.notional(symbol, qty, fill)
        margin = notional / self.s.leverage
        tp_pct = ((sig.take_profit - fill) / fill * 100) if is_buy else ((fill - sig.take_profit) / fill * 100)
        sl_pct = ((fill - sig.stop) / fill * 100) if is_buy else ((sig.stop - fill) / fill * 100)
        
        # Цветной заголовок
        if is_buy:
            head = f"{GREEN}🟢 ВХОД LONG 📈{RESET}"
            side_color = GREEN
        else:
            head = f"{RED}🔴 ВХОД SHORT 📉{RESET}"
            side_color = RED
            
        lines = [
            f"{head}  {symbol}",
            f"   💵 Вход:       {_fmt(qty)} {contracts_word(qty)} = {_money(notional)} USDT @ {_fmt(fill)}",
            f"   {GREEN}💰 Профит (TP): {tp_pct:+.2f}%  →  {_fmt(sig.take_profit)}  {RESET}"
            f"({GREEN}{notional * tp_pct / 100:+.3f} USDT{RESET})",
            f"   {RED}🛑 Убыток (SL): −{sl_pct:.2f}%  →  {_fmt(sig.stop)}  {RESET}"
            f"({RED}−{abs(notional * sl_pct / 100):.3f} USDT{RESET})",
            f"   ⚙️  Маржа {_money(margin)} USDT"
            + (f" ({margin / equity * 100:.1f}% депозита) · " if equity > 0 else " · ")
            + f"плечо {self.s.leverage}x",
        ]
        target = self.s.position_value
        if target > 0 and notional < target * 0.9:
            reason = self._size_reason(symbol, sig, fill, notional, target)
            lines.append(f"   📉 Размер ниже POSITION_VALUE={_money(target)} USDT: "
                         f"получилось {_money(notional)} — {reason}")
        lines.append(f"   📌 {sig.reason}")
        return "\n".join(lines)

    def _size_reason(self, symbol, sig, fill, notional, target) -> str:
        """Почему фактический номинал меньше заданного POSITION_VALUE."""
        risk_budget = 0.0
        try:
            risk_budget = self.broker.equity() * self.s.risk_per_trade_pct / 100
            stop_dist = abs(fill - sig.stop)
            if stop_dist > 0:
                allowed = risk_budget / stop_dist * fill
                if allowed < target * 0.9:
                    return (f"лимит риска {self.s.risk_per_trade_pct}% от депозита "
                            f"({_money(risk_budget)} USDT) при стопе "
                            f"{abs(fill - sig.stop) / fill * 100:.2f}% разрешает только "
                            f"{_money(allowed)} USDT")
        except Exception:
            pass
        # Обычная причина — биржа торгует только целым числом контрактов.
        step = self.broker.coins(symbol, 1) * fill
        if step > 0:
            return f"биржа торгует целыми контрактами по {_money(step)} USDT, " \
                   f"округление вниз дало меньше"
        return "округление размера вниз под требования биржи"

    # --------------------------------------------------------------------- main

    def step(self) -> bool:
        """Один проход. False = движок должен остановиться."""
        for msg in self.broker.tick():
            self._say(msg)

        # Периодическая чистка осиротевших план-ордеров (live).
        # Висячие стопы без позиции могут случайно сработать — лучше слать
        # cancel_all регулярно, хоть MEXC и не подтверждает отмену.
        now = time.monotonic()
        if (not self.broker.paper and 
            now - self._last_orphan_sweep >= self._orphan_sweep_interval):
            self._last_orphan_sweep = now
            sweep = getattr(self.broker, "sweep_orphan_plan_orders", None)
            if sweep:
                try:
                    dropped = sweep()
                    if dropped:
                        self._say(f"🧹 Периодическая чистка: снято {dropped} висячих план-ордеров")
                except Exception as exc:
                    log.warning("⚠️ периодическая чистка план-ордеров: %s: %s",
                                type(exc).__name__, exc)

        equity = self.broker.equity()
        today = date.today()
        self.risk.update(equity, today)

        if self.risk.halted:
            if not self._flat_after_halt:
                self.broker.close_all()
                self._flat_after_halt = True
                self._say(f"🛑 КИЛЛ-СВИТЧ: {self.risk.halt_reason}. Все позиции закрыты, "
                          f"новые входы заблокированы.")
            self._heartbeat(equity, 0, blocked=f"остановлено: {self.risk.halt_reason}")
            return True
        self._flat_after_halt = False

        open_syms = {p.symbol for p in self.broker.positions()}
        now = time.monotonic()
        if now - self._last_entry_scan < self.s.entry_scan_seconds:
            self._heartbeat(equity, len(open_syms))
            return True
        self._last_entry_scan = now

        # Баг-фикс: раньше тут был голый return без единой строки в лог, и при
        # заполненном лимите позиций терминал замолкал — казалось, что бот завис.
        if len(open_syms) >= self.s.max_open_positions:
            if not self._limit_logged:
                self._limit_logged = True
                self._say(f"⏸️  Лимит позиций {len(open_syms)}/{self.s.max_open_positions} — "
                          f"новые входы не проверяю, жду освобождения слота")
                self._say(f"   Бот не остановился: каждые {self.s.loop_seconds:g} с "
                          f"проверяю открытые позиции. Цена дошла до цели или стопа — "
                          f"закрываю, слот освобождается, ищу новую сделку.")
            self._heartbeat(equity, len(open_syms), blocked="лимит позиций")
            return True
        self._limit_logged = False

        min_rows = self._min_rows()
        stats = {"scanned": 0, "held": 0, "no_signal": 0, "blocked": 0,
                 "limit": 0, "entries": 0}

        for sym in self._symbols():
            if sym in self._untradeable:
                continue
            # Лимит может исчерпаться прямо в этом проходе: открыли 5-ю позицию
            # на третьей паре, а остальные 17 проверять смысла нет. Раньше каждая
            # из них писала одинаковое «достигнут лимит открытых позиций».
            if len(open_syms) >= self.s.max_open_positions:
                if not self._limit_logged:
                    self._limit_logged = True
                    self._note("⏸️  Лимит позиций %d/%d — остальные пары в этом проходе "
                               "не проверяю", len(open_syms), self.s.max_open_positions)
                stats["limit"] += 1
                continue
            # Баг-фикс: без этой проверки движок мог повторно открывать/усреднять
            # позицию по уже открытому символу, пока не исчерпан общий лимит.
            if sym in open_syms:
                stats["held"] += 1
                continue
            if hasattr(self.broker, "is_symbol_supported") and not self.broker.is_symbol_supported(sym):
                stats["blocked"] += 1
                self._note("⚠️  %s — биржа не знает такую пару, пропускаю", sym,
                           level=logging.WARNING)
                continue
            try:
                df = self.broker.ohlcv(sym)
            except SymbolUnavailable as exc:
                stats["blocked"] += 1
                self._note("⚠️  %s — нет данных с биржи (%s)", sym, exc, level=logging.WARNING)
                continue
            if df is None or df.empty or len(df) < min_rows or "timestamp" not in df.columns:
                rows = 0 if df is None else len(df)
                stats["blocked"] += 1
                self._note("⚠️  %s — мало свечей для анализа (%d, нужно %d)",
                           sym, rows, min_rows, level=logging.WARNING)
                continue

            candle = int(df["timestamp"].iloc[-1])
            candle_iso = self._candle_iso(candle)
            new_candle = self._last_logged_candle.get(sym) != candle
            if new_candle:
                self._last_logged_candle[sym] = candle
            stats["scanned"] += 1

            sig = self.strategy.generate(sym, df)
            if sig.side == "hold":
                stats["no_signal"] += 1
                if new_candle:
                    # Причина отсутствия входа — с числами, чтобы видеть рынок.
                    self._note("⏭️  %s — %s", sym, sig.why_not or "нет сигнала")
                continue

            previous = self._last_entry_attempt.get(sym)
            if previous:
                previous_candle, previous_side, previous_at = previous
                if previous_candle == candle and previous_side == sig.side:
                    if new_candle:
                        self._note("🔄 %s — сигнал тот же, что на прошлой свече, не дублю", sym)
                    continue
                if now - previous_at < self.s.entry_cooldown_seconds:
                    if new_candle:
                        self._note("⏱️ %s — пауза между входами (%ds)",
                                   sym, self.s.entry_cooldown_seconds)
                    continue
            if self.ai_analyzer:
                ai = self.ai_analyzer.analyze(sym, df)
                if not ai.available and self.s.ai_fail_mode == "allow":
                    self._note("🤖 %s — ИИ недоступен, AI_FAIL_MODE=allow, пропускаю фильтр", sym)
                    ai = None
                if ai is not None:
                    expected = "long" if sig.side == "buy" else "short"
                    if ai.signal != expected:
                        self._note("🤖 %s — ИИ отклонил сделку: %s", sym, ai.reason)
                        continue

            try:
                entry = self.broker.price(sym)
                # Стоп и тейк округляем под точность биржи: с лишними знаками
                # MEXC не примет защитный plan-ордер и позиция останется без стопа.
                sig = replace(sig,
                              stop=self.broker.round_price(sym, sig.stop),
                              take_profit=self.broker.round_price(sym, sig.take_profit))
                qty, why = self.risk.plan(equity, sig.side, entry, sig.stop, len(open_syms))
                if qty > 0:
                    qty, why = self.broker.fit_qty(sym, qty)
            except SymbolUnavailable as exc:
                self._note("⚠️  %s — символ недоступен, вход пропущен (%s)", sym, exc,
                           level=logging.WARNING)
                continue
            if qty <= 0:
                stats["blocked"] += 1
                if new_candle:
                    self._note("🚫 %s — %s", sym, why if why != "ok" else "нечего открывать")
                continue

            # Метку времени ставим только после успеха: раньше неудача на одной
            # свече блокировала символ на все 15 минут и бот "молчал" в логах.
            self._last_entry_attempt[sym] = (candle, sig.side, now)
            try:
                fill = self.broker.open(sym, sig.side, qty, sig.stop, sig.take_profit, self.s.leverage)
            except (OrderRejected, SymbolUnavailable) as exc:
                # Ошибка биржи — снимаем метку, чтобы повторить на следующем проходе.
                self._last_entry_attempt.pop(sym, None)
                stats["blocked"] += 1
                self._say(f"❌ {sym} — ордер не прошёл: {exc}")
                continue
            open_syms.add(sym)
            stats["entries"] += 1
            self._last_entry = (sym, time.monotonic())
            self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
                            (int(time.time()), sym, sig.side, qty, fill, sig.stop,
                             sig.take_profit, sig.reason))
            self.db.commit()
            self._say(self._entry_report(sym, sig, qty, fill, equity))
            # Уведомление в Telegram о новой сделке
            notional = self.broker.notional(sym, qty, fill)
            if hasattr(self, 'notify') and callable(getattr(self, 'notify_trade_open', None)):
                self.notify_trade_open(sym, sig.side, qty, fill, sig.stop, sig.take_profit, notional)

        total = len(self._symbols())
        parts = [f"пар {total - len(self._untradeable)}/{total}"]
        if stats["scanned"]:
            parts.append(f"просканировано {stats['scanned']}")
        if stats["no_signal"]:
            parts.append(f"нет сигнала {stats['no_signal']}")
        if stats["held"]:
            parts.append(f"уже в позиции {stats['held']}")
        if stats["limit"]:
            parts.append(f"не проверено (лимит) {stats['limit']}")
        if stats["blocked"]:
            parts.append(f"заблокировано {stats['blocked']}")
        parts.append(f"входов {stats['entries']}")
        parts.append(f"позиции {len(open_syms)}/{self.s.max_open_positions}")
        parts.append(f"эквити {_money(equity)} USDT")
        self._note("━━━━━━ ИТОГ ━━━━━━ " + " · ".join(parts))
        return True

    # ---------------------------------------------------------------- controls

    def pause(self):
        self._running.clear()
        self._say("⏸️  Пауза: новые входы остановлены, открытые позиции и стопы остаются.")

    def resume(self):
        if self.risk.halt_kind == "daily" or not self.risk.halted:
            self._running.set()
            self._say("▶️  Продолжаю работу.")

    def emergency_stop(self):
        self.broker.close_all()
        self._shutdown.set()

    def reset_risk(self):
        """Ручной сброс halt по просадке (см. RiskManager.reset). Вызывается из /reset."""
        equity = self.broker.equity()
        self.risk.reset(equity)
        self._flat_after_halt = False
        self._limit_logged = False
        self._running.set()
        self._say(f"✅ Риск сброшен. Эквити {_fmt(equity)} USDT, торговля возобновлена.")

    def status(self):
        return (f"mode={self.s.mode}; running={self._running.is_set()}; "
                f"equity={self.broker.equity():.2f}; positions={len(self.broker.positions())}; "
                f"halt={self.risk.halt_reason or 'нет'}")

    # --------------------------------------------------------------------- loop

    def run(self):
        badge = MODE_BADGE.get(self.s.mode, self.s.mode)
        self._say("=" * 74)
        self._say(f"🤖 Торговый бот v2 · Таймфрейм {self.s.timeframe} · "
                  f"Плечо {self.s.leverage}x · До {self.s.max_open_positions} позиций")
        self._say(f"   Режим: {badge}")
        if self.s.mode == "paper":
            self._say("   ⚠️  Это бумажный режим. Реальных ордеров на MEXC не будет — "
                      "все «сделки» только в этом окне.")
            self._say("   📋 Но заявки собираются по-настоящему: строки 📤 показывают "
                      "точный запрос, который уйдёт на биржу в live.")
            if getattr(self.broker, "swap", False):
                self._say("   ⚠️  Одно исключение: поля hedge-режима (positionMode, "
                          "hedged) требуют авторизации, поэтому в paper их нет. "
                          "Проверить их может только live-прогон.")
            self._say("   💡 Чтобы торговать на реальные деньги: в .env поставьте "
                      "MODE=live и CONFIRM_LIVE=YES")
        pairs = self._symbols()
        if self.s.symbols_auto:
            self._say(f"   Пары: AUTO, бот сам отобрал {len(pairs)} по обороту "
                      f"(лимит MAX_SYMBOLS={self.s.max_symbols}, "
                      f"порог {self.s.min_symbol_volume / 1e6:g} млн USDT/24ч)")
        self._say(f"   Список ({len(pairs)}): "
                  + ", ".join(p.split("/")[0] for p in pairs))
        self._say(f"   Объём сделки: "
                  + (f"{_fmt(self.s.position_value)} USDT" if self.s.position_value > 0
                     else f"{self.s.max_position_pct}% депозита × {self.s.leverage}x")
                  + f" · риск {self.s.risk_per_trade_pct}% на сделку")
        self._say(f"   Эквити: {_fmt(self.broker.equity())} USDT")
        self._say("=" * 74)

        # Стартовая чистка: заявки без позиции — это живые ордера на бирже,
        # которые пережили закрытие. Оставленные без присмотра, они могут
        # сработать и открыть сделку, которой быть не должно.
        sweep = getattr(self.broker, "sweep_orphan_plan_orders", None)
        if sweep and not self.broker.paper:
            try:
                dropped = sweep()
                if dropped:
                    self._say(f"🧹 Снято висящих заявок без позиции: {dropped}")
            except Exception as exc:
                self._say(f"⚠️ Не удалось проверить висящие заявки ({type(exc).__name__}: {exc}). "
                          f"Проверьте их в приложении MEXC вручную.")

        # Периодическая чистка каждые N секунд (по умолчанию 5 мин).
        # MEXC не подтверждает отмену, но запрос лучше слать регулярно.
        self._last_orphan_sweep = 0
        self._orphan_sweep_interval = getattr(self.s, "orphan_sweep_seconds", 300)

        live_positions = self.broker.positions()
        if not self.broker.paper and live_positions:
            self._say(f"📂 На бирже уже открыто {len(live_positions)} позиций: "
                      + ", ".join(f"{p.symbol.split('/')[0]}"
                                  f"{'↑' if p.side == 'buy' else '↓'}" for p in live_positions))
            blind = [p for p in live_positions if not p.stop and not p.take_profit]
            if blind:
                self._say(f"⚠️ У {len(blind)} из них бот не знает стоп и цель — они "
                          f"открыты прошлым запуском. Бот не сможет закрыть их по "
                          f"уровню, следите за ними сами или закройте вручную.")
            self._last_activity = time.monotonic()

        # Пары, которые заведомо нельзя открыть: минимальный лот биржи дороже
        # заданного объёма сделки. Считаем один раз, иначе отказ печатался бы
        # по каждой свече и засорял лог.
        probe = getattr(self.broker, "untradeable", None)
        if probe and self.s.position_value > 0:
            self._untradeable = probe(self.s.position_value)
            if self._untradeable:
                self._say(f"🚫 Недоступные пары ({len(self._untradeable)}) — минимальный лот "
                          f"биржи дороже {self.s.position_value:g} USDT:")
                for sym, floor in self._untradeable.items():
                    self._say(f"     {sym} — 1 контракт = {_fmt(floor)} USDT")
                self._say(f"   Либо поднимите POSITION_VALUE, либо уберите эти пары из SYMBOLS. "
                          f"Они не будут сканироваться.")
                self._last_activity = time.monotonic()

        errors = 0
        try:
            while True:
                if self._shutdown.is_set() or os.path.exists(self.s.stop_file):
                    self.broker.close_all()
                    if os.path.exists(self.s.stop_file):
                        os.remove(self.s.stop_file)
                    self._say("🛑 Файл STOP найден: позиции закрыты, бот остановлен.")
                    return
                if not self._running.is_set():
                    time.sleep(1)
                    continue
                try:
                    if not self.step():
                        self._say("⏹️ Движок остановлен. Разберитесь и запустите вручную.")
                        return
                    errors = 0
                except Exception:
                    errors += 1
                    log.exception("🚨 Ошибка шага (%d подряд)", errors)
                    if errors >= 10:
                        self._say("🚨 10 ошибок подряд, бот остановлен. "
                                  "Проверьте позиции на бирже вручную!")
                        return
                time.sleep(min(self.s.loop_seconds, self.s.entry_scan_seconds))
        except KeyboardInterrupt:
            log.info("⏹️ Ctrl+C: бот остановлен. Позиции остаются на бирже со стопами.")
