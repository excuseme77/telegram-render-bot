"""Необязательный AI-фильтр сигналов.

В OpenAI отправляются только агрегированные числовые признаки последних свечей.
Ошибки AI не должны останавливать торговый движок.
"""
import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import pandas as pd

log = logging.getLogger("ai_analyzer")


@dataclass(frozen=True)
class AIAnalysis:
    signal: str
    confidence: float
    reason: str
    available: bool = True


class AIAnalyzer:
    def __init__(self, settings):
        self.settings = settings
        self._client = None

    def _hold(self, reason: str) -> AIAnalysis:
        return AIAnalysis("hold", 0.0, reason, available=False)

    @staticmethod
    def _response_content(response) -> str:
        """Extract chat.completions content without assuming response.output_text."""
        def field(value, name):
            if isinstance(value, dict):
                return value.get(name)
            return getattr(value, name, None)

        choices = field(response, "choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ValueError("response shape invalid: choices missing/empty")
        message = field(choices[0], "message")
        if message is None:
            raise ValueError("response shape invalid: message missing")
        content = field(message, "content")
        if content is None:
            raise ValueError("response shape invalid: content missing")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                else:
                    text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            if parts:
                return "".join(parts)
        raise ValueError(f"chat message content type invalid: {type(content).__name__}")

    def _log_request_path(self):
        base = urlsplit(self.settings.openai_base_url or "https://api.openai.com/v1/")
        path = base.path.rstrip("/") + "/chat/completions"
        log.debug("AI request: POST %s", path)

    @staticmethod
    def _parse_result(content: str) -> tuple[str, float, str]:
        try:
            result = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"response JSON invalid: {type(exc).__name__}") from exc
        if not isinstance(result, dict):
            raise ValueError("response JSON must be an object")
        required = {"signal", "confidence", "reason"}
        if set(result) != required:
            raise ValueError("response JSON fields invalid")
        signal = result["signal"]
        confidence = result["confidence"]
        reason = result["reason"]
        if signal not in {"long", "short", "hold"}:
            raise ValueError("response signal invalid")
        if isinstance(confidence, bool):
            raise ValueError("response confidence type invalid")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("response confidence type invalid") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("response confidence out of range")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("response reason invalid")
        return signal, confidence, reason[:240]

    def _features(self, df: pd.DataFrame) -> dict[str, float]:
        close = df["close"].astype(float)
        high, low = df["high"].astype(float), df["low"].astype(float)
        returns = close.pct_change().dropna()
        ema_fast = close.ewm(span=20, adjust=False).mean()
        ema_slow = close.ewm(span=50, adjust=False).mean()
        tr = pd.concat(
            [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(14).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        volume = df["volume"].astype(float) if "volume" in df else pd.Series(dtype=float)
        features = {
            "last_return_pct": float(returns.iloc[-1] * 100),
            "return_5_pct": float((close.iloc[-1] / close.iloc[-6] - 1) * 100),
            "return_20_pct": float((close.iloc[-1] / close.iloc[-21] - 1) * 100),
            "ema20_minus_ema50_pct": float((ema_fast.iloc[-1] / ema_slow.iloc[-1] - 1) * 100),
            "rsi14": float(rsi.iloc[-1]),
            "atr14_pct": float(atr.iloc[-1] / close.iloc[-1] * 100),
            "last_range_pct": float((high.iloc[-1] - low.iloc[-1]) / close.iloc[-1] * 100),
        }
        if len(volume) >= 20 and float(volume.iloc[-20:].mean()) > 0:
            features["volume_ratio_20"] = float(volume.iloc[-1] / volume.iloc[-20:].mean())
        else:
            features["volume_ratio_20"] = 1.0
        return {key: round(value, 6) for key, value in features.items() if pd.notna(value)}

    def analyze(self, symbol: str, df: pd.DataFrame) -> AIAnalysis:
        if not self.settings.ai_enabled:
            return AIAnalysis("hold", 0.0, "AI отключён", available=False)
        if len(df) < 55:
            return self._hold("недостаточно агрегированных данных")
        try:
            if self._client is None:
                try:
                    from openai import OpenAI
                except ModuleNotFoundError as exc:
                    missing = exc.name or "неизвестный модуль"
                    log.warning("%s: OpenAI SDK/dependency отсутствует: %s", symbol, missing)
                    return self._hold(f"отсутствует Python-модуль: {missing}")
                api_key = os.getenv("OPENAI_API_KEY", "")
                if not api_key:
                    return self._hold("OPENAI_API_KEY отсутствует")
                client_options = {
                    "api_key": api_key,
                    "timeout": self.settings.ai_timeout_seconds,
                    "max_retries": self.settings.ai_max_retries,
                }
                if self.settings.openai_base_url:
                    client_options["base_url"] = self.settings.openai_base_url
                self._client = OpenAI(**client_options)
            features = self._features(df)
            self._log_request_path()
            response = self._client.chat.completions.create(
                model=self.settings.openai_model,
                temperature=0,
                max_tokens=180,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "signal_filter",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "signal": {"type": "string", "enum": ["long", "short", "hold"]},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "reason": {"type": "string", "maxLength": 240},
                            },
                            "required": ["signal", "confidence", "reason"],
                        },
                    },
                },
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a conservative signal filter. Use only the supplied aggregated "
                            "market features. Never invent prices, news, positions, or guarantees. "
                            "Return HOLD when uncertain."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({"symbol": symbol, "features": features}, separators=(",", ":")),
                    },
                ],
            )
            content = self._response_content(response)
            signal, confidence, reason = self._parse_result(content)
            if signal != "hold" and confidence < self.settings.ai_min_confidence:
                return AIAnalysis("hold", confidence, f"confidence ниже порога: {reason}")
            log.info("%s: AI=%s confidence=%.2f reason=%s", symbol, signal, confidence, reason)
            return AIAnalysis(signal, confidence, reason)
        except ValueError as exc:
            log.warning("%s: AI-ответ отклонён (%s)", symbol, str(exc))
            if self.settings.ai_fail_mode == "allow":
                return AIAnalysis("hold", 0.0, "AI-ответ невалиден: пропуск фильтра", available=False)
            return self._hold("AI-ответ невалиден: безопасный HOLD")
        except Exception as exc:
            category = type(exc).__name__
            log.warning("%s: AI-фильтр недоступен (%s)", symbol, category)
            if self.settings.ai_fail_mode == "allow":
                return AIAnalysis("hold", 0.0, "AI ошибка: пропуск фильтра", available=False)
            return self._hold("AI ошибка: безопасный HOLD")
