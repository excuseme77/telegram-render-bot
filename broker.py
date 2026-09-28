"""Брокеры с единым интерфейсом. Live-режим намеренно требует явного подтверждения.

Особенности MEXC, которые здесь учтены:
  * фьючерсы в ccxt называются ``BTC/USDT:USDT``, спот — ``BTC/USDT``;
  * ``set_leverage`` на MEXC требует ``openType`` и ``positionType``;
  * биржа работает в режиме hedge (long/short раздельно) или one-way,
    и сторона закрывающего ордера в этих режимах ИНВЕРТИРОВАНА;
  * стоп-лосс и тейк-профит на фьючерсах ставятся ОТДЕЛЬНЫМИ plan-ордерами:
    единый ``create_order`` MEXC параметры stopLoss/takeProfit не понимает.
"""
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Protocol

from colorama import Fore, Style, init as colorama_init
colorama_init(strip=False)

import pandas as pd

log = logging.getLogger("broker")

# Цвета для терминала
GREEN = Fore.GREEN + Style.BRIGHT
RED = Fore.RED + Style.BRIGHT
YELLOW = Fore.YELLOW + Style.BRIGHT
CYAN = Fore.CYAN + Style.BRIGHT
MAGENTA = Fore.MAGENTA + Style.BRIGHT
RESET = Style.RESET_ALL


@dataclass
class Position:
    symbol: str
    side: str
    qty: float          # КОЛИЧЕСТВО КОНТРАКТОВ, а не монет
    entry: float
    stop: float
    take_profit: float
    contract_size: float = 1.0   # монет в одном контракте (MEXC: BTC=0.0001, DOGE=100)
    # Трейлинг-тейк: пик прибыли в % от входа (для отката от пика).
    peak_pct: float = 0.0

    @property
    def notional(self) -> float:
        """Номинал позиции в USDT. Размер контрактов в цене не учитывался — это
        давало ошибку в 100–100000 раз и мгновенную ликвидацию."""
        return self.qty * self.contract_size * self.entry


class SymbolUnavailable(Exception):
    """A single configured symbol cannot be used by the exchange."""


class OrderRejected(Exception):
    """Биржа отклонила ордер — это не проблема символа, повтор сразу не поможет."""


class Broker(Protocol):
    def tick(self) -> list[str]: ...
    def equity(self) -> float: ...
    def positions(self) -> list[Position]: ...
    def ohlcv(self, symbol: str) -> pd.DataFrame: ...
    def price(self, symbol: str) -> float: ...
    def fit_qty(self, symbol: str, qty: float) -> float: ...
    def open(self, symbol: str, side: str, qty: float, stop: float, take_profit: float, leverage: int) -> float: ...
    def close_all(self) -> None: ...


def _strip(text: str) -> str:
    """Убрать хвостовые нули только после запятой (100.0 -> 100, а не 1)."""
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt(value: float) -> str:
    """Число без запятых, без экспоненты и без шума float'а.

    Иначе в логах мем-монеты выглядят как ``PEPE @ 0.00``, а BTC — как
    ``83876.899999999994``. Оставляем 8 значащих цифр — этого хватает для
    любой цены MEXC (у них priceScale 4-8), а хвост из 15 цифр только мешает.
    """
    v = float(value)
    if v == 0:
        return "0"
    if abs(v) >= 1:
        # Фиксированная запись: .12f у больших чисел выводит погрешность
        # двоичного представления (83876.899999999994).
        digits = max(0, 8 - int(math.log10(abs(v))) - 1)
        return _strip(f"{v:.{digits}f}")
    return _strip(f"{v:.8f}") or f"{v:.6g}"


def plural(n: float, one: str, few: str, many: str) -> str:
    """Русские склонения: 1 контракт / 2 контракта / 5 контрактов."""
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def contracts_word(n: float) -> str:
    return f"{plural(n, 'контракт', 'контракта', 'контрактов')}"


def _money(v: float) -> str:
    """Денежная сумма в USDT без шума float'а: 0.35 / 1.99 / 12.50."""
    v = float(v)
    if abs(v) >= 1:
        return f"{v:.2f}"
    return _strip(f"{v:.4f}") or "0"


def _matches_exclude(symbol: str, exclude, suffixes=()) -> bool:
    """Совпадает ли пара со списком исключений.

    Исключение задаётся именем монеты, а не полным символом: в .env удобнее
    написать ``SPY,SPX500``, чем ``SPY/USDT:USDT,SPX500/USDT:USDT``.
    Суффиксы ловят целые семейства: MEXC держит много токенизированных
    акций вида ``SKHYNIXSTOCK``, ``SNDKSTOCK``, ``MSTRSTOCK``.
    """
    if not exclude and not suffixes:
        return False
    full = str(symbol).upper()
    base = full.split("/")[0].split(":")[0]
    if base in exclude or full in exclude:
        return True
    return any(base.endswith(str(sfx).upper()) for sfx in suffixes or ())


class CcxtBroker:
    def __init__(self, settings, paper: bool = False, exchange_name: str = "mexc",
                 trade_close_callback=None):
        import ccxt

        self.s, self.paper = settings, paper
        self.swap = settings.market_type == "swap"
        exchange_class = getattr(ccxt, exchange_name)
        self.exchange = exchange_class({
            "apiKey": settings.api_key,
            "secret": settings.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": settings.market_type},
        })
        if settings.mode == "demo":
            self.exchange.set_sandbox_mode(True)

        self._positions: dict[str, Position] = {}
        self._balance = settings.paper_balance
        self._last_prices: dict[str, float] = {}
        self.supported_symbols: set[str] | None = None
        self._live_equity: float | None = None
        self._equity_at = 0.0
        self._protection: dict[str, dict[str, object]] = {}
        self._hedge_mode = False
        self._contract_sizes: dict[str, float] = {}
        self._warned_contract_size = False
        self._auto: list = []
        self._auto_at = 0.0
        # MEXC не даёт снять план-ордер поштучно: planorder/cancel отвечает
        # «code 600 Parameter error» на любые параметры. Чтобы не получать
        # двадцать одинаковых WARNING в лог, об отказе сообщаем один раз.
        self._plan_cancel_broken = False
        # Файл для сохранения уровней позиций (stop, tp, peak_pct) между перезапусками.
        self._positions_state_file = getattr(settings, "positions_state_file", "positions_state.json")
        self._trade_close_callback = trade_close_callback

        self._validate_symbols()
        if not self.paper and self.swap:
            self._detect_position_mode()
        # Загружаем сохранённые уровни позиций.
        self._load_positions_state()

    # ------------------------------------------------------------------ startup

    def _validate_symbols(self):
        """Best-effort startup validation; a market-data outage must not break paper mode."""
        try:
            markets = self.exchange.load_markets()
        except Exception as exc:
            log.warning("⚠️ Failed to load markets (%s: %s); deferred.", type(exc).__name__, exc)
            return
        self.supported_symbols = set(markets)
        unsupported = [symbol for symbol in self.s.symbols if symbol not in self.supported_symbols]
        if unsupported:
            log.warning("⚠️ Unsupported symbols skipped: %s", ", ".join(unsupported))

    def _detect_position_mode(self):
        """MEXC: 1 = hedge (long/short раздельно), 2 = one-way.

        От режима зависит сторона закрывающего ордера, поэтому проверяем на старте.
        """
        getter = getattr(self.exchange, "contractPrivateGetPositionPositionMode", None)
        if getter is None:
            return
        try:
            mode = (getter() or {}).get("data")
        except Exception as exc:
            log.warning("⚠️ не удалось определить режим позиций MEXC (%s: %s)", type(exc).__name__, exc)
            return
        self._hedge_mode = str(mode) == "1"
        log.info("ℹ️ Режим позиций MEXC: %s",
                 "HEDGE (long и short раздельно)" if self._hedge_mode else "ONE-WAY (net position)")

    def is_symbol_supported(self, symbol: str) -> bool:
        return self.supported_symbols is None or symbol in self.supported_symbols

    # ------------------------------------------------------- размеры контрактов

    def contract_size(self, symbol: str) -> float:
        """Сколько базовой монеты в одном контракте.

        ВАЖНО. На MEXC-фьючерсах ``contractSize`` НЕ равен 1:
        BTC = 0.0001, DOGE = 100, MEME = 100, MEW = 1000, MELANIA = 0.1.
        То есть 1 контракт DOGE — это 100 монет, а не одна.

        Раньше номинал считался как ``контракты * цена``, из-за чего позиция
        завышалась в 100–100000 раз. При депозите 10 USDT это мгновенная
        ликвидация, а на бумаге выглядело как «сделка на $2».
        """
        cached = self._contract_sizes.get(symbol)
        if cached is not None:
            return cached
        try:
            size = float(self.exchange.market(symbol).get("contractSize") or 1.0) or 1.0
        except Exception as exc:
            raise SymbolUnavailable(f"{symbol}: не удалось узнать размер контракта "
                                    f"({type(exc).__name__}: {exc})") from exc
        self._contract_sizes[symbol] = size
        if size != 1.0 and not self._warned_contract_size:
            self._warned_contract_size = True
            log.info("ℹ️ MEXC futures: размер контрактов разный (BTC 0.0001, DOGE 100, "
                     "MEME 100...). Номинал считается как контракты × размер × цена.")
        return size

    def _safe_contract_size(self, symbol: str) -> float:
        """contract_size без исключений — для чтения позиций с биржи.

        Если по символу нет метаданных, вернём дефолт 1.0, но один раз
        предупредим: номинал такой позиции посчитается неверно.
        """
        try:
            return self.contract_size(symbol)
        except Exception:
            log.warning("⚠️ %s: нет метаданных контракта, номинал позиции может быть неверным",
                        symbol)
            return 1.0

    def to_contracts(self, symbol: str, coins: float) -> float:
        """Перевести количество монет в количество контрактов."""
        return coins / self.contract_size(symbol)

    def coins(self, symbol: str, contracts: float) -> float:
        """Перевести количество контрактов в количество монет."""
        return contracts * self.contract_size(symbol)

    def notional(self, symbol: str, contracts: float, price: float | None = None) -> float:
        """Номинал позиции в USDT по количеству контрактов."""
        if price is None:
            price = self._last_prices.get(symbol) or 0.0
        return contracts * self.contract_size(symbol) * price

    def min_notional(self, symbol: str) -> float:
        """Минимальный номинал, который вообще пропустит биржа (1 контракт)."""
        try:
            market = self.exchange.market(symbol)
            limits = market.get("limits") or {}
            min_amount = float(((limits.get("amount") or {}).get("min")) or 1) or 1
            price = self._last_prices.get(symbol) or float(market.get("last") or 0)
            if not price:
                price = float(self.price(symbol))
            return min_amount * self.contract_size(symbol) * price
        except Exception:
            return 0.0

    def round_price(self, symbol: str, price: float) -> float:
        """Округлить цену под точность биржи.

        MEXC отклоняет стоп и тейк, если после запятой больше цифр, чем
        ``priceScale``: защитный ордер просто не поставится.
        """
        if not price or price <= 0:
            return 0.0
        try:
            return float(self.exchange.price_to_precision(symbol, price)) or float(price)
        except Exception:
            return float(price)

    # --------------------------------------------------- авто-отбор пар (AUTO)

    def auto_symbols(self, force: bool = False) -> list:
        """Подобрать пары по обороту, если в .env стоит ``SYMBOLS=AUTO``.

        На MEXC больше тысячи фьючерсов. Сканировать их все нельзя: один
        проход — это один HTTP-запрос на пару, то есть десятки минут ожидания.
        Но тикеры MEXC приходят ОДНИМ запросом, поэтому фильтр по обороту
        почти бесплатный.

        Берём топ по обороту 24ч, отбрасываем то, что дороже POSITION_VALUE,
        и режем до MAX_SYMBOLS.
        """
        if not getattr(self.s, "symbols_auto", False):
            return list(self.s.symbols)
        now = time.monotonic()
        ttl = self.s.symbol_refresh_minutes * 60
        # Кэш живёт symbol_refresh_minutes. Без этой проверки auto_symbols()
        # дёргала биржу на каждом вызове — и старт бота занимал секунды вместо
        # одного запроса.
        if not force and self._auto and now - self._auto_at < ttl:
            return list(self._auto)

        try:
            tickers = self.exchange.fetch_tickers()
        except Exception as exc:
            log.warning("⚠️ не удалось обновить список пар (%s: %s) — беру прошлый",
                        type(exc).__name__, exc)
            return list(self._auto or self.s.symbols)

        exclude = {str(x).upper() for x in getattr(self.s, "symbols_exclude", ()) or ()}
        suffixes = getattr(self.s, "symbols_exclude_suffix", ()) or ()
        rows = []
        excluded = []
        for symbol, market in self.exchange.markets.items():
            if not (market.get("swap") and market.get("settle") == "USDT"
                    and market.get("active") is not False):
                continue
            if not self.swap and market.get("swap"):
                continue
            ticker = tickers.get(symbol) or {}
            volume = float(ticker.get("quoteVolume") or 0)
            if volume < self.s.min_symbol_volume:
                continue
            last = float(ticker.get("last") or 0)
            if last <= 0:
                continue
            if _matches_exclude(symbol, exclude, suffixes):
                excluded.append(symbol)
                continue
            # Отсекаем пары, у которых минимальный контракт дороже сделки.
            min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 1) or 1
            size = float(market.get("contractSize") or 1)
            if self.s.position_value > 0 and min_amount * size * last > self.s.position_value:
                continue
            rows.append((volume, symbol, last))

        rows.sort(reverse=True)
        picked = rows[: self.s.max_symbols]
        if exclude:
            log.info("🚫 SYMBOLS_EXCLUDE отсёк %d пар: %s", len(excluded),
                     ", ".join(sorted(s.split("/")[0] for s in excluded)))
        if not picked:
            log.error("🚨 AUTO-отбор не нашёл ни одной пары (порог оборота %s USDT, "
                      "исключено %d)", _fmt(self.s.min_symbol_volume), len(excluded))
            return list(self._auto or self.s.symbols)

        self._auto = [s for _, s, _ in picked]
        self._auto_at = now
        log.info("🔎 AUTO-пары (%d из %d доступных): %s",
                 len(self._auto), len(rows),
                 ", ".join(s.split("/")[0] for s in self._auto))
        return list(self._auto)

    def untradeable(self, target_value: float) -> dict:
        """Пары, на которых нельзя открыть сделку нужного размера.

        Считается один раз на старте, чтобы не получать отказ по каждой
        свече: у MEXC минимальный лот — 1 контракт, а 1 контракт BTC стоит
        $8.40, DOGE — $9.77. При POSITION_VALUE=3 такие пары недоступны.
        """
        blocked: dict = {}
        if target_value <= 0:
            return blocked
        for sym in self.active_symbols():
            if not self.is_symbol_supported(sym):
                continue
            try:
                self.price(sym)  # заодно кладёт цену в кэш
                floor = self.min_notional(sym)
            except SymbolUnavailable:
                continue
            if floor > target_value:
                blocked[sym] = floor
        return blocked

    # ------------------------------------------------------------------- public

    def active_symbols(self) -> list:
        """Список пар, по которым бот реально работает (с учётом SYMBOLS=AUTO)."""
        return self.auto_symbols()

    def tick(self) -> list:
        """Раз в проход: обновления списка пар и работа с открытыми позициями.

        В paper здесь же проверяются стоп-лосс и тейк-профит. Раньше этого не
        было: бумажные позиции не закрывались НИКОГДА, баланс оставался
        равным PAPER_BALANCE, и симуляция не показывала ни одного закрытия.
        """
        messages = []
        if getattr(self.s, "symbols_auto", False):
            before = list(self._auto)
            after = self.auto_symbols()
            if after != before:
                messages.append("🔎 Список пар обновлён: "
                                + ", ".join(a.split("/")[0] for a in after))
        if self.paper:
            messages.extend(self._paper_check_exits())
        else:
            messages.extend(self._live_check_exits())
            # Сверяем живые позиции с записанными уровнями: пока этого не было,
            # в live не закрывал их никто, и бот упирался в лимит позиций.
            messages.extend(self.manage_exits())
        return messages

    def _paper_check_exits(self) -> list:
        """Закрыть бумажные позиции, у которых сработал стоп/тейк/трейлинг/хард."""
        messages = []
        trail_act = getattr(self.s, "trailing_activate_pct", 3.0)
        trail_dist = getattr(self.s, "trailing_distance_pct", 0.5)
        hard_stop = getattr(self.s, "hard_stop_loss_pct", 1.0)

        for key, p in list(self._positions.items()):
            current = self._last_prices.get(p.symbol)
            if not current or current <= 0:
                continue

            if p.side == "buy":
                pct = (current - p.entry) / p.entry * 100
            else:
                pct = (p.entry - current) / p.entry * 100

            hit = None
            label = ""
            at = 0.0

            # 1. Жёсткий стоп
            if pct <= -hard_stop:
                label = f"🛑 Жёсткий стоп ({-hard_stop:+.1f}%)"
                at = current
                hit = (label, at)

            # 2. Трейлинг-тейк
            if not hit and pct >= trail_act:
                if pct > p.peak_pct:
                    p.peak_pct = pct
                if p.peak_pct - pct >= trail_dist:
                    label = f"🎯 Трейлинг-тейк (пик {p.peak_pct:.2f}%, откат {trail_dist}%)"
                    at = current
                    hit = (label, at)

            # 3. Обычные стоп/тейк
            if not hit:
                if p.side == "buy":
                    if p.stop and current <= p.stop:
                        hit = ("🛑 Стоп-лосс", p.stop)
                    elif p.take_profit and current >= p.take_profit:
                        hit = ("🎯 Тейк-профит", p.take_profit)
                else:
                    if p.stop and current >= p.stop:
                        hit = ("🛑 Стоп-лосс", p.stop)
                    elif p.take_profit and current <= p.take_profit:
                        hit = ("🎯 Тейк-профит", p.take_profit)

            if not hit:
                continue

            label, at = hit
            pnl = ((at - p.entry) if p.side == "buy" else (p.entry - at)) * p.qty * p.contract_size
            self._balance += pnl
            self._positions.pop(key, None)
            if not self.paper:
                self._remove_saved_position(p.symbol, p.side)
            verdict = "прибыль" if pnl > 0 else "убыток"
            pct = (at - p.entry) / p.entry * 100 * (1 if p.side == "buy" else -1)
            color = GREEN if pnl > 0 else RED
            messages.append(
                f"{label} {p.symbol} {'LONG' if p.side == 'buy' else 'SHORT'} "
                f"{_fmt(p.qty)} {contracts_word(p.qty)}: вход {_fmt(p.entry)} → "
                f"{_fmt(at)} ({color}{pct:+.2f}%{RESET}) · {_fmt(p.notional)} USDT · "
                f"PnL {color}{pnl:+.4f} USDT{RESET} ({verdict}) · "
                f"баланс {_money(self._balance)} USDT · {GREEN}слот освобождён, ищу новую сделку{RESET}"
            )
        return messages

    def _live_check_exits(self) -> list:
        """Сообщить о позициях, которых больше нет на бирже (сработал SL/TP вручную)."""
        messages = []
        try:
            raw = self.exchange.fetch_positions()
        except Exception as exc:
            log.debug("проверка позиций: %s: %s", type(exc).__name__, exc)
            return messages
        live = set()
        for item in raw or []:
            try:
                if float(item.get("contracts") or 0) > 0:
                    side = "buy" if str(item.get("side", "")).lower() in ("long", "buy") else "sell"
                    live.add(f"{item.get('symbol')}|{side}")
            except (TypeError, ValueError):
                continue
        vanished = 0
        for key in list(self._positions):
            if key in live or "|" not in key:
                continue
            symbol, side = key.split("|", 1)
            messages.append(
                f"🔔 Позиция {symbol} {'LONG' if side == 'buy' else 'SHORT'} больше не "
                f"на бирже — её закрыл стоп, тейк или операция вручную. "
                f"Реальный PnL смотрите в истории MEXC."
            )
            self._protection.pop(key, None)
            self._positions.pop(key, None)
            self._remove_saved_position(symbol, side)
            vanished += 1
        # Исчезнувшая позиция оставляет после себя висящие план-ордера. Раньше
        # они просто выбрасывались из кэша и оставались живыми на бирже: такая
        # заявка может сработать и открыть сделку, которой быть не должно.
        if vanished:
            try:
                dropped = self.sweep_orphan_plan_orders()
                if dropped:
                    messages.append(f"🧹 Снято висящих план-ордеров без позиции: {dropped}")
            except Exception as exc:
                log.warning("⚠️ уборка висящих план-ордеров не удалась (%s: %s)",
                            type(exc).__name__, exc)
        return messages

    def equity(self) -> float:
        if self.paper:
            return max(self._paper_equity(), 0.0)
        return max(self._refresh_live_equity(), 0.0)

    def positions(self) -> list[Position]:
        if self.paper:
            return list(self._positions.values())
        try:
            raw = self.exchange.fetch_positions()
        except Exception as exc:
            log.warning("⚠️ не удалось получить позиции с биржи (%s: %s)", type(exc).__name__, exc)
            return list(self._positions.values())

        found: dict[str, Position] = {}
        for p in raw or []:
            try:
                contracts = float(p.get("contracts") or 0)
            except (TypeError, ValueError):
                continue
            if contracts <= 0:
                continue
            side = "buy" if str(p.get("side", "")).lower() in ("long", "buy") else "sell"
            symbol = p.get("symbol")
            key = f"{symbol}|{side}"
            # Сохраняем стоп и тейк из кэша: биржа их в позиции не возвращает,
            # а без них лог в live не показывает, чем позиция защищена.
            known = self._positions.get(key)
            found[key] = Position(
                symbol=symbol,
                side=side,
                qty=contracts,
                entry=float(p.get("entryPrice") or 0.0),
                stop=known.stop if known else 0.0,
                take_profit=known.take_profit if known else 0.0,
                contract_size=self._safe_contract_size(symbol),
            )
        self._positions = found
        # Накладываем сохранённый peak_pct (стоп/тейк НЕ восстанавливаем).
        self._apply_saved_levels(found)
        # Жёсткие уровни для ВСЕХ позиций: стоп 1% (HARD_STOP_LOSS_PCT), тейк 4%.
        hard_stop_pct = getattr(self.s, "hard_stop_loss_pct", 1.0)
        tp_pct = getattr(self.s, "take_profit_min_pct", 4.0)
        for key, p in found.items():
            if p.entry > 0:
                if p.side == "buy":
                    p.stop = p.entry * (1 - hard_stop_pct / 100)
                    p.take_profit = p.entry * (1 + tp_pct / 100)
                else:
                    p.stop = p.entry * (1 + hard_stop_pct / 100)
                    p.take_profit = p.entry * (1 - tp_pct / 100)
                log.info("🔧 %s: жёсткие уровни — стоп %.4f (%.1f%%), тейк %.4f (%.1f%%)",
                         key, p.stop, hard_stop_pct, p.take_profit, tp_pct)
        return list(found.values())

    def _load_positions_state(self):
        """Загрузить уровни позиций из файла."""
        try:
            with open(self._positions_state_file, "r", encoding="utf-8") as f:
                self._saved_positions = json.load(f)
                log.info("📂 Загружены уровни позиций из %s: %d записей",
                         self._positions_state_file, len(self._saved_positions))
        except FileNotFoundError:
            self._saved_positions = {}
        except Exception as exc:
            log.warning("⚠️ не удалось загрузить %s (%s: %s)",
                        self._positions_state_file, type(exc).__name__, exc)
            self._saved_positions = {}

    def _save_positions_state(self):
        """Сохранить уровни позиций в файл."""
        try:
            data = {}
            for key, p in self._positions.items():
                data[key] = {
                    "symbol": p.symbol,
                    "side": p.side,
                    "stop": p.stop,
                    "take_profit": p.take_profit,
                    "peak_pct": getattr(p, "peak_pct", 0.0),
                    "entry": p.entry,
                    "qty": p.qty,
                }
            with open(self._positions_state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.warning("⚠️ не удалось сохранить %s (%s: %s)",
                        self._positions_state_file, type(exc).__name__, exc)

    def _apply_saved_levels(self, found: dict):
        """Наложить сохранённый peak_pct на позиции с биржи.
        Стоп и тейк НЕ восстанавливаем — всегда используем жёсткие настройки:
        HARD_STOP_LOSS_PCT (1%) и TAKE_PROFIT_MIN_PCT (4%).
        """
        if not self._saved_positions:
            return
        applied = 0
        for key, p in found.items():
            saved = self._saved_positions.get(key)
            if saved:
                p.peak_pct = saved.get("peak_pct", 0.0)
                applied += 1
        if applied:
            log.info("🔧 Восстановлен peak_pct для %d позиций из сохранённого состояния", applied)

    def _remove_saved_position(self, symbol: str, side: str):
        """Удалить позицию из сохранённого состояния (при закрытии)."""
        key = f"{symbol}|{side}"
        if key in self._saved_positions:
            del self._saved_positions[key]
            self._save_positions_state()

    def ohlcv(self, symbol):
        try:
            rows = self.exchange.fetch_ohlcv(symbol, self.s.timeframe, limit=250)
        except Exception as exc:
            raise SymbolUnavailable(f"{symbol}: ohlcv {type(exc).__name__}: {exc}") from exc
        if not rows:
            raise SymbolUnavailable(f"{symbol}: биржа вернула пустой OHLCV")
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        self._last_prices[symbol] = float(frame["close"].iloc[-1])
        return frame

    def price(self, symbol) -> float:
        cached = self._last_prices.get(symbol)
        try:
            ticker = self.exchange.fetch_ticker(symbol)
        except Exception as exc:
            if cached:
                log.warning("⚠️ %s: ticker не ответил (%s), беру цену последней свечи %s",
                            symbol, type(exc).__name__, _fmt(cached))
                return cached
            raise SymbolUnavailable(f"{symbol}: ticker {type(exc).__name__}: {exc}") from exc

        last = float(ticker.get("last") or 0)
        if last <= 0:
            # Часть мем-монет MEXC отдаёт last=0 — берём bid/ask, иначе кэш свечи.
            last = float(ticker.get("ask") or 0) or float(ticker.get("bid") or 0)
        if last <= 0:
            if cached:
                return cached
            raise SymbolUnavailable(f"{symbol}: биржа вернула цену 0")
        self._last_prices[symbol] = last
        return last

    def fit_qty(self, symbol, coins) -> tuple:
        """Привести желаемое количество МОНЕТ к количеству КОНТРАКТОВ биржи.

        Возвращает ``(контракты, причина)``. Контрактов 0 -> сделку не открывать,
        в ``причина`` — почему.

        ВАЖНО: ccxt.amount_to_precision округляет ВНИЗ (TRUNCATE), поэтому
        номинал часто выходил 30-50% ниже POSITION_VALUE. Теперь перебираем
        floor/ceil и берём вариант, чей номинал БЛИЖЕ ВСЕГО к цели.
        """
        if coins <= 0:
            return 0.0, "размер позиции получился нулевым"
        try:
            market = self.exchange.market(symbol)
        except Exception as exc:
            raise SymbolUnavailable(f"{symbol}: market {type(exc).__name__}: {exc}") from exc

        size = self.contract_size(symbol)
        price = self._last_prices.get(symbol) or float(market.get("last") or 0) or 0
        if price <= 0:
            return 0.0, "нет цены для расчёта размера"

        limits = market.get("limits") or {}
        min_amount = float(((limits.get("amount") or {}).get("min")) or 0)
        min_value = float((limits.get("cost") or {}).get("min") or self.s.min_order_value)

        # Идеальное число контрактов (может быть дробным)
        ideal = coins / size
        if ideal < 1e-12:
            return 0.0, "слишком мало монет"

        # Берём floor и ceil через amount_to_precision (ccxt умеет только TRUNCATE).
        # Пробуем оба и выбираем, какой ближе к целевому номиналу.
        target_notional = self.s.position_value
        candidates = []

        # 1. floor через ccxt
        try:
            floor_qty = float(self.exchange.amount_to_precision(symbol, ideal))
        except Exception:
            floor_qty = math.floor(ideal / min_amount) * min_amount if min_amount else math.floor(ideal)

        # 2. ceil = floor + шаг
        step = min_amount if min_amount and min_amount > 0 else 1.0
        ceil_qty = floor_qty + step

        for c in (floor_qty, ceil_qty):
            if c < (min_amount or 1):
                continue
            notional = c * size * price
            if notional < min_value:
                continue
            candidates.append((c, notional, abs(notional - target_notional)))

        if not candidates:
            # Ни один вариант не прошёл минимум — вернём floor, пусть сработает проверка ниже
            pass
        else:
            # Выбираем вариант с МИНИМАЛЬНЫМ отклонением от $2
            best = min(candidates, key=lambda x: x[2])
            return best[0], f"ok (номинал {_fmt(best[1])} USDT, цель {_fmt(target_notional)})"

        # Фоллбэк — старое поведение
        try:
            return float(self.exchange.amount_to_precision(symbol, ideal)), "ok"
        except Exception as exc:
            raise SymbolUnavailable(f"{symbol}: precision {type(exc).__name__}: {exc}") from exc

    def _submit(self, what: str, symbol: str, side: str, qty: float, params: dict) -> dict:
        """Единственное место, где бот что-то отправляет на биржу.

        Заявка собирается одинаково в paper и live, и в обоих режимах
        печатается в лог целиком. Разница ровно одна: в paper сеть не
        трогается. Поэтому бумажный прогон показывает РОВНО тот запрос,
        который уйдёт на биржу, и ошибки параметров видны заранее.
        """
        pretty = " ".join(f"{k}={v}" for k, v in params.items() if k != "leverage")
        log.info("📤 %s · %s %s %s %s · %s", what, symbol, side, _fmt(qty),
                 contracts_word(qty), pretty)
        if self.paper:
            return {"id": f"PAPER-{symbol}-{int(time.time() * 1000)}", "average": None}
        try:
            return self.exchange.create_order(symbol, "market", side, qty, None, params)
        except Exception as exc:
            raise OrderRejected(f"{symbol}: {what} отклонён ({type(exc).__name__}: {exc})") from exc

    def open(self, symbol, side, qty, stop, take_profit, leverage) -> float:
        """``qty`` — количество контрактов (результат fit_qty)."""
        price = self.price(symbol)
        size = self.contract_size(symbol)

        # Плечо и маржа выставляются ДО входа и в обоих режимах: в paper сеть
        # не трогаем, но проверяем, что параметры строятся и не падают.
        if self.swap and not self.paper:
            self._set_leverage(symbol, side, leverage)

        order = self._submit("ВХОД", symbol, side, qty, self._entry_params(leverage))
        fill = float(order.get("average") or order.get("price") or 0) or price
        if not fill or fill <= 0:
            fill = price

        if self.swap:
            # Защитные ордера строятся и проверяются в обоих режимах. Раньше
            # в paper этот код не выполнялся НИ РАЗУ, поэтому все ошибки
            # MEXC в plan-ордерах всплывали только на реальных деньгах.
            self._place_protection(symbol, side, qty, fill, stop, take_profit, leverage)

        self._positions[f"{symbol}|{side}"] = Position(symbol, side, qty, fill, stop,
                                                       take_profit, size)
        self._save_positions_state()
        return fill

    def close_all(self):
        if self.paper:
            for p in list(self._positions.values()):
                current = self._last_prices.get(p.symbol, p.entry)
                # PnL считаем в монетах, умножая на размер контракта.
                pnl = ((current - p.entry) if p.side == "buy" else (p.entry - current)) \
                    * p.qty * p.contract_size
                self._balance += pnl
                log.info("💰 Закрыта (paper) %s %s: %s %s = %s USDT, "
                         "PnL %+.4f USDT, баланс %s USDT",
                         p.symbol, "LONG" if p.side == "buy" else "SHORT",
                         _fmt(p.qty), contracts_word(p.qty), _fmt(p.notional),
                         pnl, _money(self._balance))
            self._positions.clear()
            return

        for p in self.positions():
            # Порядок важен: сначала закрываем позицию, и только потом снимаем
            # защиту. Наоборот — при неудачном закрытии позиция осталась бы
            # ещё и без стопа.
            try:
                self._submit("ЗАКРЫТИЕ", p.symbol, self._close_side(p.side), p.qty,
                             self._reduce_params())
                self._cancel_protection(p.symbol, p.side)
                self._remove_saved_position(p.symbol, p.side)
                log.info("💰 Закрыта позиция %s %s %s %s", p.symbol,
                         "LONG" if p.side == "buy" else "SHORT",
                         _fmt(p.qty), contracts_word(p.qty))
            except Exception as exc:
                log.error("🚨 Не удалось закрыть %s %s (%s: %s) — пробую закрыть всё по символу",
                          p.symbol, p.side, type(exc).__name__, exc)
                self._close_all_on_exchange(p.symbol)
                self._cancel_protection(p.symbol, p.side)
        self._positions.clear()

    # ------------------------------------------------------------------ internal

    def _paper_equity(self) -> float:
        total = self._balance
        for p in self._positions.values():
            current = self._last_prices.get(p.symbol, p.entry)
            total += ((current - p.entry) if p.side == "buy" else (p.entry - current)) \
                * p.qty * p.contract_size
        return total

    def _refresh_live_equity(self) -> float:
        """Реальный капитал с биржи.

        Раньше здесь возвращался PAPER_BALANCE (1000$), из-за чего live-бот
        считал размер позиции по вымышленному депозиту и получал отказы.
        """
        now = time.monotonic()
        if self._live_equity is not None and now - self._equity_at < self.s.equity_refresh_seconds:
            return self._live_equity
        try:
            equity = self._fetch_exchange_equity()
        except Exception as exc:
            log.warning("⚠️ не удалось обновить баланс (%s: %s)", type(exc).__name__, exc)
            return self._live_equity if self._live_equity is not None else self._balance
        if equity <= 0:
            log.error("🚨 Баланс на бирже 0 USDT — риск-менеджер остановит новые входы")
        self._live_equity, self._equity_at = equity, now
        return equity

    def _fetch_exchange_equity(self) -> float:
        if self.swap:
            rows = (self.exchange.contractPrivateGetAccountAssets() or {}).get("data") or []
            for row in rows:
                if row.get("currency") == "USDT":
                    return float(row.get("equity") or 0.0)
            raise OrderRejected("в фьючерсном кошельке нет USDT")
        balance = self.exchange.fetch_balance()
        return float((balance.get("total") or {}).get("USDT") or 0.0)

    def _close_side(self, side: str) -> str:
        """Сторона ордера для ЗАКРЫТИЯ позиции — всегда противоположная.

        У MEXC поле ``side`` — это реальное направление сделки, а число в
        ответе биржи кодирует открыть/закрыть. Закрыть ЛОНГ — это всегда
        продажа (``sell``), закрыть ШОРТ — всегда покупка (``buy``), и в
        hedge, и в one-way режиме одинаково.

        Раньше здесь для hedge-режима возвращалась та же сторона, и это было
        перепутано ровно наоборот: живой тест показал ``ЗАКРЫТИЕ · SUI buy``
        для открытого лонга, то есть бот просил биржу закрыть шорт, которого
        не существует. Стоп-лосс и тейк-профит ставились с той же неверной
        стороной — они висели на бирже, но защищать позицию не могли.
        """
        return "sell" if side == "buy" else "buy"

    def _open_type(self) -> int:
        """MEXC openType: 1 = isolated, 2 = cross."""
        return 1 if self.s.margin_mode == "isolated" else 2

    def _margin_params(self, leverage: int | None = None) -> dict:
        """Параметры маржи для ордера. На isolated MEXC требует ``leverage``."""
        params: dict = {"openType": self._open_type()}
        if self.s.margin_mode == "isolated" and leverage:
            params["leverage"] = leverage
        if self._hedge_mode:
            params["hedged"] = True
            params["positionMode"] = 1
        return params

    def _entry_params(self, leverage: int | None = None) -> dict:
        if not self.swap:
            return {}
        return self._margin_params(leverage)

    def _reduce_params(self, leverage: int | None = None) -> dict:
        """Параметры закрывающего ордера.

        Плечо подставляется по умолчанию из настроек. Раньше ``close_all``
        звал ``_reduce_params()`` без аргумента, и на isolated-марже MEXC
        отвечал ``createSwapOrder() requires a leverage parameter for
        isolated margin orders`` — то есть закрыть позицию штатным ордером
        было невозможно вообще.
        """
        if not self.swap:
            return {"reduceOnly": True}
        if leverage is None:
            leverage = self.s.leverage
        return {"reduceOnly": True, **self._margin_params(leverage)}

    def _set_leverage(self, symbol, side, leverage):
        """MEXC требует openType (1 isolated / 2 cross) и positionType (1 long / 2 short).

        Без них ccxt падает с ArgumentsRequired и ордер не доходит до биржи.
        """
        try:
            self.exchange.set_leverage(leverage, symbol, {
                "openType": self._open_type(),
                "positionType": 1 if side == "buy" else 2,
            })
        except Exception as exc:
            # Плечо не всегда можно менять без открытой позиции — не критично.
            log.warning("⚠️ %s: не удалось выставить плечо %sx (%s: %s)",
                        symbol, leverage, type(exc).__name__, exc)

    def _place_protection(self, symbol, side, qty, fill, stop, take_profit, leverage=None):
        """Стоп-лосс и тейк-профит на MEXC-фьючерсах — отдельные plan-ордера.

        Единый ``create_order`` параметры stopLoss/takeProfit на MEXC
        игнорирует: защиту нужно ставить двумя заявками с ``orderType: 5``
        (market) и ``executeCycle: 2`` (7 дней, не 24 часа).
        """
        placed: dict[str, object] = {}
        close_side = self._close_side(side)
        # triggerType: 1 = сработать при цене >= триггера, 2 = при <=.
        # Лонг: SL при падении (2), TP при росте (1). Шорт: наоборот.
        trigger_types = {"sl": 2, "tp": 1} if side == "buy" else {"sl": 1, "tp": 2}
        for label, trigger in (("sl", stop), ("tp", take_profit)):
            if not trigger or trigger <= 0:
                continue
            name = "СТОП-ЛОСС" if label == "sl" else "ТЕЙК-ПРОФИТ"
            params = {
                "triggerPrice": trigger,
                "triggerType": trigger_types[label],
                "orderType": 5,      # 5 = market
                "executeCycle": 2,   # 2 = 7 дней (1 = только 24 часа)
                **self._reduce_params(leverage),
            }
            try:
                order = self._submit(f"ЗАЩИТА {name} @{_fmt(trigger)}",
                                     symbol, close_side, qty, params)
                # ccxt теряет id план-ордера, поэтому ищем его у биржи сами.
                order_id = str(order.get("id") or "") or self.find_plan_order(
                    symbol, close_side, trigger)
                placed[label] = order_id or None
                if not order_id:
                    log.warning("⚠️ %s: %s отправлен, но биржа не вернула его номер — "
                                "защита подлежит проверке вручную", symbol, name)
                log.info("🛡️ %s: %s на %s — %s %s%s",
                         symbol, name, _fmt(trigger), _fmt(qty), contracts_word(qty),
                         f" (ордер {order_id})" if order_id else "")
            except OrderRejected as exc:
                log.error("🚨 %s: не удалось поставить %s на %s (%s) — ПОЗИЦИЯ БЕЗ ЗАЩИТЫ!",
                          symbol, name, _fmt(trigger), exc)
            except Exception as exc:
                log.error("🚨 %s: не удалось поставить %s на %s (%s: %s) — ПОЗИЦИЯ БЕЗ ЗАЩИТЫ!",
                          symbol, name, _fmt(trigger), type(exc).__name__, exc)
        self._protection[f"{symbol}|{side}"] = placed

        if not self.paper and placed:
            # Отправка — ещё не protection. План-ордер мог уйти с неверной
            # стороной и тихо висеть, не защищая позицию, поэтому в live
            # перепроверяем список план-ордеров на самой бирже.
            self._verify_protection(symbol, side, placed)

        if "sl" not in placed:
            log.error("🚨 %s: стоп-лосс НЕ выставлен. Закройте позицию вручную!", symbol)

    def _verify_protection(self, symbol: str, side: str, placed: dict) -> None:
        """Перепроверить на бирже, что защита реально висит и стороны верны.

        Живой прогон показал: план-ордер уходит без ошибки, но если ему
        передать неверную сторону, он не защищает позицию — просто висит.
        Поэтому в live отправка не считается успехом, пока биржа не покажет
        ордер в своём списке план-ордеров.
        """
        orders = self.plan_orders(symbol)
        if not orders:
            log.error("🚨 %s: план-ордера на бирже нет, хотя отправка прошла без ошибки — "
                      "защита НЕ работает. Закройте позицию вручную!", symbol)
            return
        ours = {v for v in placed.values() if v}
        found = [o for o in orders if self._plan_id(o) in ours]
        log.info("🛡️ %s: план-ордеров на бирже %d, наших %d (%s)",
                 symbol, len(orders), len(found),
                 ", ".join(f"{k}={v}" for k, v in placed.items() if v) or "—")
        if len(found) < len(placed):
            log.error("🚨 %s: из %d защитных ордеров биржа подтвердила только %d — "
                      "проверьте позицию вручную!", symbol, len(placed), len(found))

    def plan_orders(self, symbol: str = None) -> list:
        """План-ордера MEXC (стоп-лосс и тейк-профит) — сырые ответы биржи.

        Их не видно через ``fetch_open_orders``: ccxt там спрашивает обычные
        заявки, а стоп и тейк уходят отдельным вызовом ``planorder/place`` и
        лежат в своём списке.

        Внимание к полям: MEXC отдаёт ``id`` и ``vol``, а не ``orderId`` и
        ``quantity``. Из-за неверных имён проверка «поставлен ли стоп»
        показывала ноль своих ордеров, даже когда стоп на бирже висел.
        ``symbol=None`` — по всем парам сразу (так чистим осиротевшие).
        """
        getter = getattr(self.exchange, "contractPrivateGetPlanorderListOrders", None)
        if getter is None:
            return []
        params = {}
        if symbol:
            params["symbol"] = self.exchange.market(symbol)["id"]
        try:
            response = getter(params) or {}
        except Exception as exc:
            log.warning("⚠️ %s: не удалось получить план-ордера (%s: %s)",
                        symbol or "все пары", type(exc).__name__, exc)
            return []
        if not response.get("success", True):
            log.warning("⚠️ MEXC не отдал план-ордера: %s", response.get("message"))
            return []
        data = response.get("data") or []
        if isinstance(data, list):
            return data
        found = []
        for bucket in ("success", "failed"):
            found.extend(data.get(bucket) or [])
        return found

    @staticmethod
    def _plan_id(order: dict) -> str:
        """Идентификатор план-ордера: MEXC зовёт поле ``id``."""
        return str(order.get("id") or order.get("orderId") or "")

    def find_plan_order(self, symbol: str, side: str, trigger: float) -> str:
        """Найти id уже выставленного план-ордера по паре, стороне и триггеру.

        ccxt теряет id план-ордера: ``createSwapOrder`` читает ``data`` как
        словарь, а MEXC на ``planorder/place`` отдаёт там просто строку с
        номером. В итоге ``order["id"]`` всегда был ``None``, и бот не знал
        свой ордер, хотя биржа его прекрасно видела. Поэтому id ищем сами.
        """
        want_side = 2 if side == "sell" else 4     # 2 = закрыть лонг, 4 = закрыть шорт
        market_id = self.exchange.market(symbol)["id"]
        for order in self.plan_orders(symbol):
            if str(order.get("symbol") or "") != str(market_id):
                continue
            if int(order.get("side") or 0) != want_side:
                continue
            try:
                if abs(float(order.get("triggerPrice") or 0) - float(trigger)) > 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            return self._plan_id(order)
        return ""

    @staticmethod
    def _plan_symbol(unified: str) -> str:
        """Символ MEXC из внутреннего вида ccxt.

        План-ордера приходят как ``SUI_USDT``, а позиции — как
        ``SUI/USDT:USDT``. Без перевода строки не совпали бы, и чистка
        осиротевших заявок решила бы, что защита осиротела, и снесла её
        вместе с настоящими сиротами.
        """
        text = str(unified or "")
        if "/" not in text:
            return text
        base, _, rest = text.partition("/")
        quote = rest.split(":")[0]
        return f"{base}_{quote}"

    def manage_exits(self) -> list:
        """Закрыть позиции, у которых цена дошла до стопа или цели.

        До этого в live выход был возложен исключительно на стоп-ордера биржи.
        Проверить их и снять не удалось, поэтому позиции не закрывались: слоты
        кончались, и бот крутился, не открывая ничего нового — ровно то
        «остановился после одного круга», на которое жаловались.

        Теперь бот сам сверяет цену с записанными уровнями на каждом шаге и
        закрывает позицию обычным ордером. Если стоп на бирже сработает раньше
        и закроет позицию, лишний запрос вернёт «Position is nonexistent» —
        это штатная ситуация, а не ошибка.

        Дополнительно (клиентски, не зависят от план-ордеров MEXC):
        - Жёсткий стоп: закрыть при убытке >= HARD_STOP_LOSS_PCT% от входа.
        - Трейлинг-тейк: прибыль >= TRAILING_ACTIVATE_PCT% → включаем тень
          TRAILING_DISTANCE_PCT%. Откат от пика на это расстояние → закрыть.
        """
        messages: list = []
        if self.paper:
            return self._paper_check_exits()

        trail_act = getattr(self.s, "trailing_activate_pct", 3.0)
        trail_dist = getattr(self.s, "trailing_distance_pct", 0.5)
        hard_stop = getattr(self.s, "hard_stop_loss_pct", 1.0)

        for key, p in list(self._positions.items()):
            if not p.stop and not p.take_profit:
                continue
            try:
                current = self.price(p.symbol)
            except Exception as exc:
                log.debug("проверка выхода %s: %s: %s", p.symbol, type(exc).__name__, exc)
                continue
            if not current or current <= 0:
                continue

            # Текущая прибыль/убыток в % от входа
            if p.side == "buy":
                pct = (current - p.entry) / p.entry * 100
            else:
                pct = (p.entry - current) / p.entry * 100

            hit = None
            label = ""
            at = 0.0

            # 1. Жёсткий стоп — срабатывает ПЕРВЫМ, строже план-ордера.
            if pct <= -hard_stop:
                label = f"🛑 Жёсткий стоп ({-hard_stop:+.1f}%)"
                at = current
                hit = (label, at)

            # 2. Трейлинг-тейк: обновляем пик, проверяем откат.
            if not hit and pct >= trail_act:
                # Обновляем пик прибыли
                if pct > p.peak_pct:
                    p.peak_pct = pct
                # Если от пика откатились на trail_dist — закрываем
                if p.peak_pct - pct >= trail_dist:
                    label = f"🎯 Трейлинг-тейк (пик {p.peak_pct:.2f}%, откат {trail_dist}%)"
                    at = current
                    hit = (label, at)

            # 3. Обычные стоп/тейк из стратегии (план-ордера на бирже).
            if not hit:
                if p.side == "buy":
                    if p.stop and current <= p.stop:
                        hit = ("🛑 Стоп-лосс", p.stop)
                    elif p.take_profit and current >= p.take_profit:
                        hit = ("🎯 Цель достигнута", p.take_profit)
                else:
                    if p.stop and current >= p.stop:
                        hit = ("🛑 Стоп-лосс", p.stop)
                    elif p.take_profit and current <= p.take_profit:
                        hit = ("🎯 Цель достигнута", p.take_profit)

            if not hit:
                continue

            label, at = hit
            # Закрываем позицию, и лишь потом снимаем защиту. Наоборот —
            # при неудачном закрытии позиция осталась бы ещё и без стопа.
            try:
                self._submit("ЗАКРЫТИЕ ПО УРОВНЮ", p.symbol, self._close_side(p.side),
                             p.qty, self._reduce_params())
            except Exception as exc:
                message = str(exc)
                if "nonexistent" in message or "2009" in message:
                    # Позицию уже закрыл стоп на бирже — это не поломка.
                    self._positions.pop(key, None)
                    self._cancel_protection(p.symbol, p.side)
                    self._remove_saved_position(p.symbol, p.side)
                    messages.append(f"{label} {p.symbol}: позицию уже закрыл стоп биржи")
                    continue
                log.error("🚨 %s: не удалось закрыть по уровню (%s: %s)",
                          p.symbol, type(exc).__name__, exc)
                messages.append(f"⚠️ {p.symbol}: не удалось закрыть по уровню — "
                                f"закройте вручную ({label})")
                continue
            self._cancel_protection(p.symbol, p.side)
            self._remove_saved_position(p.symbol, p.side)
            pnl = ((current - p.entry) if p.side == "buy" else (p.entry - current)) \
                * p.qty * p.contract_size
            self._positions.pop(key, None)
            # Callback для уведомления в Telegram
            if self._trade_close_callback:
                try:
                    self._trade_close_callback(
                        symbol=p.symbol,
                        side=p.side,
                        qty=p.qty,
                        entry=p.entry,
                        exit_price=current,
                        pct=pct,
                        pnl=pnl,
                        reason=label
                    )
                except Exception as e:
                    log.warning(f"Trade close callback error: {e}")
            color = GREEN if pnl > 0 else RED
            messages.append(
                f"{label} {p.symbol} {'LONG' if p.side == 'buy' else 'SHORT'} "
                f"{_fmt(p.qty)} {contracts_word(p.qty)}: вход {_fmt(p.entry)} → "
                f"{_fmt(current)} ({color}{pct:+.2f}%{RESET}) · PnL {color}{pnl:+.4f} USDT{RESET} · "
                f"{GREEN}слот освобождён, ищу новую сделку{RESET}"
            )
        return messages

    def _plan_cancel_all(self, symbol_id: str) -> bool:
        """Снять ВСЕ план-ордера одной пары. Единственный путь, который работает.

        Вызывать можно только когда по этой паре нет открытой позиции:
        ``cancel_all`` не разбирает, что стоп, а что тейк, и снесёт защиту
        живой позиции вместе с мусором.
        """
        cancel_all = getattr(self.exchange, "contractPrivatePostPlanorderCancelAll", None)
        if cancel_all is None:
            return False
        try:
            cancel_all({"symbol": symbol_id})
            return True
        except Exception as exc:
            if not self._plan_cancel_broken:
                self._plan_cancel_broken = True
                log.warning("⚠️ MEXC не даёт снять план-ордера даже списком: %s: %s. "
                            "Дальше повторять не буду — снимайте висящие стопы "
                            "в приложении MEXC вручную.",
                            type(exc).__name__, exc)
            return False

    def sweep_orphan_plan_orders(self) -> int:
        """Снять план-ордера, под которыми нет позиции.

        Живой прогон выявил неприятное: когда позиция закрывается, её стоп и
        тейк остаются на бирже. Это не безобидный мусор — висящая заявка на
        закрытие может сработать позже и открыть сделку, которой быть не
        должно. Поэтому при старте и после закрытия позиции все
        «осиротевшие» план-ордера снимаются.

        Возвращает количество снятых.
        """
        if self.paper:
            return 0
        orders = self.plan_orders()
        if not orders:
            return 0
        try:
            open_symbols = set()
            for p in self.exchange.fetch_positions() or []:
                if float(p.get("contracts") or 0) > 0:
                    open_symbols.add(self._plan_symbol(p.get("symbol")))
        except Exception as exc:
            log.warning("⚠️ не удалось получить позиции для чистки план-ордеров (%s: %s) — "
                        "ничего не снимаю", type(exc).__name__, exc)
            return 0

        # Группируем по паре: снимать можно только пары, где позиций нет
        # вообще ни по одной стороне. Иначе cancel_all снёс бы стоп живой
        # позиции вместе с мусором.
        orphan_symbols = {str(o.get("symbol") or "") for o in orders} - open_symbols
        orphan_symbols.discard("")
        if not orphan_symbols:
            return 0

        dropped = 0
        for symbol_id in sorted(orphan_symbols):
            if self._plan_cancel_all(symbol_id):
                dropped += sum(1 for o in orders if str(o.get("symbol") or "") == symbol_id)
        if dropped:
            log.info("🧹 Снято висящих план-ордеров без позиции: %d (пары: %s)",
                     dropped, ", ".join(sorted(orphan_symbols)))
        return dropped

    def _cancel_protection(self, symbol, side):
        """Снять стоп и тейк позиции, которая уже закрыта.

        Вызывать только ПОСЛЕ успешного закрытия позиции. Раньше это делалось
        до, и при неудачном закрытии позиция оставалась вообще без защиты.
        """
        placed = self._protection.pop(f"{symbol}|{side}", None)
        if not placed or self.paper:
            return
        if not self.swap:
            for label, order_id in placed.items():
                if not order_id:
                    continue
                name = "СТОП-ЛОСС" if label == "sl" else "ТЕЙК-ПРОФИТ"
                try:
                    self.exchange.cancel_order(order_id, symbol, self._reduce_params())
                    log.info("🛡️ %s: %s снят (ордер %s)", symbol, name, order_id)
                except Exception as exc:
                    log.warning("⚠️ %s: не удалось снять %s (ордер %s: %s: %s)",
                                symbol, name, order_id, type(exc).__name__, exc)
            return

        # Фьючерсы: поштучная отмена plan-ордер на MEXC не работает вовсе
        # (code 600 на любые параметры), снимаем все планы пары разом — но
        # лишь если по этой паре больше нет открытых позиций.
        try:
            still_open = any(self._plan_symbol(p.get("symbol")) == self._plan_symbol(symbol)
                             and float(p.get("contracts") or 0) > 0
                             for p in self.exchange.fetch_positions() or [])
        except Exception:
            still_open = True           # не знаем — безопаснее не трогать
        if still_open:
            log.info("🛡️ %s: план-ордера оставлены, по паре есть другая "
                     "открытая позиция", symbol)
            return
        if self._plan_cancel_all(self._plan_symbol(symbol)):
            log.info("🛡️ %s: стоп и тейк сняты вместе с закрытой позицией", symbol)

    def _close_all_on_exchange(self, symbol):
        """Аварийное закрытие всех позиций по символу через API MEXC."""
        closer = getattr(self.exchange, "contractPrivatePostPositionCloseAll", None)
        if closer is None:
            return
        try:
            closer({"symbol": self.exchange.market(symbol)["id"]})
            log.info("💰 %s: все позиции закрыты через contract/position/close_all", symbol)
        except Exception as exc:
            log.error("🚨 %s: аварийное закрытие не удалось (%s: %s) — ЗАКРОЙТЕ ВРУЧНУЮ!",
                      symbol, type(exc).__name__, exc)


def make_broker(settings, trade_close_callback=None):
    if settings.mode == "paper":
        return CcxtBroker(settings, paper=True, trade_close_callback=trade_close_callback)
    if settings.mode in ("demo", "live"):
        return CcxtBroker(settings, paper=False, trade_close_callback=trade_close_callback)
    raise ValueError(f"unsupported mode: {settings.mode}")
