import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
except ImportError:  # позволяет диагностировать конфигурацию до установки зависимостей
    def load_dotenv():
        return False

load_dotenv()


def _f(key: str, default: str) -> float:
    return float(os.getenv(key, default))


def _mexc_api_key() -> str:
    """Получить API ключ для Mexc (совместимо с новым именованием)."""
    return os.getenv("MEXC_API_KEY", os.getenv("BYBIT_API_KEY", ""))


def _mexc_api_secret() -> str:
    """Получить API secret для Mexc (совместимо с новым именованием)."""
    return os.getenv("MEXC_API_SECRET", os.getenv("BYBIT_API_SECRET", ""))


SUPPORTED_TIMEFRAMES = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"}
)

SUPPORTED_MARKET_TYPES = frozenset({"swap", "spot"})


def _normalize_symbol(raw: str, market_type: str) -> str:
    """Привести символ к unified-виду MEXC.

    Фьючерсы MEXC в ccxt называются ``BTC/USDT:USDT``, спот — ``BTC/USDT``.
    Раньше суффикс ``:USDT`` вырезался, из-за чего бот торговал спотом:
    не работало плечо, невозможны шорты, а ``set_leverage`` падал ArgumentsRequired.
    """
    symbol = raw.strip().upper().replace("/", ":").replace("::", ":")
    if not symbol:
        return ""
    base, _, settle = symbol.partition(":")
    quote = settle or "USDT"
    if market_type == "swap":
        return f"{base}/{quote}:{quote}"
    return f"{base}/{quote}"


def _openai_base_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("OPENAI_BASE_URL должен быть полным http(s) URL")
    path = parsed.path.rstrip("/")
    endpoint = "/chat/completions"
    if path.endswith(endpoint):
        path = path[:-len(endpoint)].rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/", "", ""))


@dataclass(frozen=True)
class Settings:
    mode: str              # paper | demo | live
    market_type: str       # swap (фьючерсы) | spot
    api_key: str
    api_secret: str
    tg_token: str
    admin_id: int
    # CryptoBot (платные подписки)
    cryptobot_api_token: str
    cryptobot_ipn_secret: str
    symbols: list
    timeframe: str
    loop_seconds: int
    entry_scan_seconds: int
    entry_cooldown_seconds: int
    leverage: int
    max_leverage: int
    risk_per_trade_pct: float   # сколько % депозита теряем, если сработает стоп
    max_position_pct: float     # макс. маржа в одной сделке, % депозита (если position_value=0)
    position_value: float       # фиксированный объём сделки в USDT; 0 = считать от max_position_pct
    max_open_positions: int
    max_drawdown_pct: float     # просадка от пика -> остановка бота
    daily_loss_pct: float       # дневной убыток -> остановка до следующего дня
    margin_mode: str            # isolated | cross — режим маржи для фьючерсных ордеров MEXC
    paper_balance: float
    stop_file: str
    positions_state_file: str      # JSON-файл с уровнями позиций (stop, tp, peak_pct)
    ai_enabled: bool
    ai_min_confidence: float
    openai_model: str
    openai_base_url: str
    ai_timeout_seconds: float
    ai_max_retries: int
    ai_fail_mode: str
    log_entry_rechecks: bool
    min_order_value: float      # минимальная сумма ордера в USDT (MEXC отклоняет меньше 1)
    equity_refresh_seconds: int  # как часто обновлять реальный баланс в live
    heartbeat_seconds: int      # как часто печатать «бот жив», даже если ничего не происходит
    orphan_sweep_seconds: int   # как часто снимать осиротевшие план-ордера (0 = не слать)
    # --- параметры стратегии (вынесены в .env, чтобы не лезть в код) ---
    ema_fast: int
    ema_slow: int
    rsi_period: int
    atr_period: int
    rsi_long_min: float
    rsi_long_max: float
    rsi_short_min: float
    rsi_short_max: float
    stop_atr: float
    take_atr: float
    # Цель в процентах от входа. Раньше тейк был только 3×ATR, и на 5-минутных
    # свечах ATR мелкий — цель выходила 0.5-1.9%, то есть меньше процента.
    take_profit_min_pct: float   # нижняя граница цели
    take_profit_max_pct: float   # верхняя граница цели
    max_stop_pct: float          # стоп шире этого — не открываем (риск ликвидации)
    min_reward_ratio: float      # минимальное отношение цели к стопу
    trailing_activate_pct: float   # прибыль % для активации трейлинга
    trailing_distance_pct: float   # откат от пика % для закрытия
    hard_stop_loss_pct: float      # жёсткий стоп % от входа (срабатывает первым)
    # --- отбор пар ---
    symbols_auto: bool        # SYMBOLS=AUTO -> бот сам берёт топ пар по обороту
    max_symbols: int          # сколько пар сканировать (больше = медленнее проход)
    min_symbol_volume: float  # минимальный оборот 24ч в USDT, чтобы отсечь неликвид
    symbol_refresh_minutes: int   # как часто пересобирать список пар
    min_stop_pct: float       # минимальная ширина стопа в % (иначе комиссия съедает профит)
    symbols_exclude: tuple    # пары, которые нельзя брать даже в режиме AUTO
    symbols_exclude_suffix: tuple   # ... и целые семейства по окончанию имени
    # --- авто-выключение ---
    daily_profit_target: float   # USDT: заработал столько за день → стоп (0 = откл)
    daily_loss_limit: float      # USDT: потерял столько за день → стоп (0 = откл)
    max_trades_per_day: int      # сделок за день → стоп (0 = откл)
    stop_at: str                 # "HH:MM" локальное время → стоп (пусто = откл)


def load() -> Settings:
    market_type = os.getenv("MARKET_TYPE", "swap").strip().lower()
    symbols_str = os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT")
    # AUTO: список пар формирует бот по обороту (см. broker.auto_symbols).
    symbols_auto = symbols_str.strip().upper() == "AUTO"
    symbols = [] if symbols_auto else [
        s for s in (_normalize_symbol(x, market_type) for x in symbols_str.split(",")) if s
    ]

    s = Settings(
        mode=os.getenv("MODE", "paper").lower(),
        market_type=market_type,
        api_key=_mexc_api_key(),
        api_secret=_mexc_api_secret(),
        tg_token=os.getenv("TELEGRAM_TOKEN", ""),
        admin_id=int(os.getenv("TELEGRAM_ADMIN_ID", "0")),
        cryptobot_api_token=os.getenv("CRYPTOBOT_API_TOKEN", ""),
        cryptobot_ipn_secret=os.getenv("CRYPTOBOT_IPN_SECRET", ""),
        symbols=symbols,
        timeframe=os.getenv("TIMEFRAME", "15m").strip().lower(),
        loop_seconds=int(_f("LOOP_SECONDS", "30")),
        entry_scan_seconds=int(_f("ENTRY_SCAN_SECONDS", os.getenv("LOOP_SECONDS", "30"))),
        entry_cooldown_seconds=int(_f("ENTRY_COOLDOWN_SECONDS", "15")),
        leverage=int(_f("LEVERAGE", "3")),
        max_leverage=int(_f("MAX_LEVERAGE", "5")),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", "1"),
        max_position_pct=_f("MAX_POSITION_PCT", "15"),
        position_value=_f("POSITION_VALUE", "0"),
        max_open_positions=int(_f("MAX_OPEN_POSITIONS", "2")),
        max_drawdown_pct=_f("MAX_DRAWDOWN_PCT", "15"),
        daily_loss_pct=_f("DAILY_LOSS_PCT", "5"),
        margin_mode=os.getenv("MARGIN_MODE", "isolated").strip().lower(),
        paper_balance=_f("PAPER_BALANCE", "1000"),
        stop_file=os.getenv("STOP_FILE", "STOP"),
        positions_state_file=os.getenv("POSITIONS_STATE_FILE", "positions_state.json"),
        ai_enabled=os.getenv("AI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        ai_min_confidence=_f("AI_MIN_CONFIDENCE", "0.75"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        openai_base_url=_openai_base_url(os.getenv("OPENAI_BASE_URL", "")),
        ai_timeout_seconds=_f("AI_TIMEOUT_SECONDS", "10"),
        ai_max_retries=int(_f("AI_MAX_RETRIES", "0")),
        ai_fail_mode=os.getenv("AI_FAIL_MODE", "hold").strip().lower(),
        log_entry_rechecks=os.getenv("LOG_ENTRY_RECHECKS", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        min_order_value=_f("MIN_ORDER_VALUE", "1"),
        equity_refresh_seconds=int(_f("EQUITY_REFRESH_SECONDS", "20")),
        heartbeat_seconds=int(_f("HEARTBEAT_SECONDS", "30")),
        orphan_sweep_seconds=int(_f("ORPHAN_SWEEP_SECONDS", "300")),
        ema_fast=int(_f("EMA_FAST", "20")),
        ema_slow=int(_f("EMA_SLOW", "50")),
        rsi_period=int(_f("RSI_PERIOD", "14")),
        atr_period=int(_f("ATR_PERIOD", "14")),
        rsi_long_min=_f("RSI_LONG_MIN", "50"),
        rsi_long_max=_f("RSI_LONG_MAX", "70"),
        rsi_short_min=_f("RSI_SHORT_MIN", "30"),
        rsi_short_max=_f("RSI_SHORT_MAX", "50"),
        stop_atr=_f("STOP_ATR", "2"),
        take_atr=_f("TAKE_ATR", "3"),
        take_profit_min_pct=_f("TAKE_PROFIT_MIN_PCT", "2"),
        take_profit_max_pct=_f("TAKE_PROFIT_MAX_PCT", "3"),
        max_stop_pct=_f("MAX_STOP_PCT", "4"),
        min_reward_ratio=_f("MIN_REWARD_RATIO", "1"),
        trailing_activate_pct=_f("TRAILING_ACTIVATE_PCT", "3"),
        trailing_distance_pct=_f("TRAILING_DISTANCE_PCT", "0.5"),
        hard_stop_loss_pct=_f("HARD_STOP_LOSS_PCT", "1"),
        symbols_auto=symbols_auto,
        max_symbols=int(_f("MAX_SYMBOLS", "20")),
        min_symbol_volume=_f("MIN_SYMBOL_VOLUME", "5000000"),
        symbol_refresh_minutes=int(_f("SYMBOL_REFRESH_MINUTES", "60")),
        min_stop_pct=_f("MIN_STOP_PCT", "0.3"),
        symbols_exclude=tuple(
            s.strip().upper().replace("/", ":")
            for s in os.getenv("SYMBOLS_EXCLUDE", "").split(",") if s.strip()
        ),
        symbols_exclude_suffix=tuple(
            s.strip().upper()
            for s in os.getenv("SYMBOLS_EXCLUDE_SUFFIX", "").split(",") if s.strip()
        ),
        daily_profit_target=_f("DAILY_PROFIT_TARGET", "0"),
        daily_loss_limit=_f("DAILY_LOSS_LIMIT", "0"),
        max_trades_per_day=int(_f("MAX_TRADES_PER_DAY", "0")),
        stop_at=os.getenv("STOP_AT", "").strip(),
    )
    if s.mode not in ("paper", "demo", "live"):
        raise SystemExit("MODE должен быть paper, demo или live")
    if s.market_type not in SUPPORTED_MARKET_TYPES:
        raise SystemExit("MARKET_TYPE должен быть swap или spot")
    if not s.symbols and not s.symbols_auto:
        raise SystemExit("SYMBOLS не может быть пустым (укажите пары или SYMBOLS=AUTO)")
    if s.max_symbols < 1:
        raise SystemExit("MAX_SYMBOLS должен быть минимум 1")
    if s.min_symbol_volume < 0:
        raise SystemExit("MIN_SYMBOL_VOLUME не может быть отрицательным")
    if s.symbol_refresh_minutes <= 0:
        raise SystemExit("SYMBOL_REFRESH_MINUTES должен быть больше 0")
    if s.min_stop_pct <= 0 or s.min_stop_pct >= 50:
        raise SystemExit("MIN_STOP_PCT должен быть в диапазоне 0..50 процентов")
    if s.symbols_exclude and not s.symbols_auto:
        raise SystemExit("SYMBOLS_EXCLUDE работает только вместе с SYMBOLS=AUTO")
    if s.timeframe not in SUPPORTED_TIMEFRAMES:
        allowed = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
        raise SystemExit(f"TIMEFRAME={s.timeframe!r} не поддерживается; допустимые: {allowed}")
    if s.leverage > s.max_leverage:
        raise SystemExit("LEVERAGE не может быть больше MAX_LEVERAGE")
    if s.margin_mode not in {"isolated", "cross"}:
        # Баг-фикс: без явного marginMode/openType MEXC (через ccxt) отклоняет
        # create_order ошибкой ArgumentsRequired — ни один ордер не проходил.
        raise SystemExit("MARGIN_MODE должен быть isolated или cross")
    if s.entry_scan_seconds <= 0 or s.entry_cooldown_seconds < 0:
        raise SystemExit("ENTRY_SCAN_SECONDS должен быть > 0, ENTRY_COOLDOWN_SECONDS не может быть отрицательным")
    if s.heartbeat_seconds <= 0:
        raise SystemExit("HEARTBEAT_SECONDS должен быть больше 0")
    if s.position_value < 0:
        raise SystemExit("POSITION_VALUE не может быть отрицательным (0 = считать от MAX_POSITION_PCT)")
    if s.max_open_positions < 1:
        raise SystemExit("MAX_OPEN_POSITIONS должен быть минимум 1")
    if s.ema_fast >= s.ema_slow:
        raise SystemExit("EMA_FAST должен быть меньше EMA_SLOW")
    if not 0 <= s.rsi_long_min < s.rsi_long_max <= 100:
        raise SystemExit("Нужен 0 <= RSI_LONG_MIN < RSI_LONG_MAX <= 100")
    if not 0 <= s.rsi_short_min < s.rsi_short_max <= 100:
        raise SystemExit("Нужен 0 <= RSI_SHORT_MIN < RSI_SHORT_MAX <= 100")
    if s.stop_atr <= 0 or s.take_atr <= 0:
        raise SystemExit("STOP_ATR и TAKE_ATR должны быть больше 0")
    if not 0 < s.take_profit_min_pct <= s.take_profit_max_pct <= 50:
        raise SystemExit("Нужно 0 < TAKE_PROFIT_MIN_PCT <= TAKE_PROFIT_MAX_PCT <= 50")
    if not 0 < s.max_stop_pct <= 50:
        raise SystemExit("MAX_STOP_PCT должен быть в диапазоне 0..50 процентов")
    if s.min_reward_ratio < 0:
        raise SystemExit("MIN_REWARD_RATIO не может быть отрицательным")
    if s.mode in ("demo", "live") and not (s.api_key and s.api_secret):
        raise SystemExit("Для demo/live нужны MEXC_API_KEY и MEXC_API_SECRET")
    if s.tg_token and s.admin_id <= 0:
        raise SystemExit("TELEGRAM_ADMIN_ID должен быть задан положительным числом")
    if not 0 <= s.ai_min_confidence <= 1:
        raise SystemExit("AI_MIN_CONFIDENCE должен быть в диапазоне 0..1")
    if s.ai_timeout_seconds <= 0:
        raise SystemExit("AI_TIMEOUT_SECONDS должен быть больше 0")
    if s.ai_max_retries < 0:
        raise SystemExit("AI_MAX_RETRIES не может быть отрицательным")
    if s.ai_fail_mode not in ("hold", "allow"):
        raise SystemExit("AI_FAIL_MODE должен быть hold или allow")
    if s.min_order_value <= 0:
        raise SystemExit("MIN_ORDER_VALUE должен быть больше 0")
    if s.equity_refresh_seconds <= 0:
        raise SystemExit("EQUITY_REFRESH_SECONDS должен быть больше 0")
    if s.market_type == "spot" and s.leverage > 1:
        print(
            f"ВНИМАНИЕ: MARKET_TYPE=spot, но LEVERAGE={s.leverage}. "
            f"На споте плечо не работает, SHORT невозможен. Поставьте MARKET_TYPE=swap."
        )
    if s.mode == "live" and os.getenv("CONFIRM_LIVE") != "YES":
        raise SystemExit("Для live-режима задайте CONFIRM_LIVE=YES в .env")
    return s
