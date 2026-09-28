"""CryptoBot payment integration для платных подписок.

API: https://help.crypt.bot/crypto-pay-api
Документация: https://github.com/CryptoBot/crypto-pay-api
"""
import asyncio
import hmac
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("cryptobot_pay")


@dataclass
class Invoice:
    invoice_id: int
    pay_url: str
    amount: float
    currency: str
    status: str
    payload: str  # user_id:plan


class CryptoBotPay:
    BASE_URL = "https://pay.crypt.bot/api"

    def __init__(self, api_token: str, ipn_secret: str = ""):
        self.api_token = api_token
        self.ipn_secret = ipn_secret
        self.headers = {"Crypto-Pay-API-Token": api_token, "Content-Type": "application/json"}
        self.session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def create_invoice(self, amount: float, currency: str, payload: str,
                             description: str = "Подписка на торгового бота",
                             paid_btn_name: str = "openBot",
                             paid_btn_url: str = "https://t.me/YourBotUsername",
                             expires_in: int = 3600) -> Invoice:
        """Создать инвойс на оплату.

        Args:
            amount: сумма в USD (конвертируется в крипту по курсу CryptoBot)
            currency: "USDT", "TON", "BTC", "ETH", "USDC" и т.д.
            payload: пользовательские данные (user_id:plan)
            description: описание товара
            paid_btn_name: название кнопки после оплаты
            paid_btn_url: ссылка для кнопки после оплаты
            expires_in: время жизни инвойса в секундах (по умолчанию 1 час)

        Returns:
            Invoice с invoice_id, pay_url, amount, currency, status, payload
        """
        await self._ensure_session()
        data = {
            "asset": currency,
            "amount": str(amount),
            "description": description,
            "hidden_message": "Спасибо за оплату! Подписка активируется автоматически.",
            "paid_btn_name": paid_btn_name,
            "paid_btn_url": paid_btn_url,
            "payload": payload,
            "expires_in": expires_in,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        async with self.session.post(f"{self.BASE_URL}/createInvoice", json=data) as resp:
            result = await resp.json()
            if not result.get("ok"):
                raise RuntimeError(f"CryptoBot error: {result.get('error', 'unknown')}")
            inv = result["result"]
            return Invoice(
                invoice_id=inv["invoice_id"],
                pay_url=inv["pay_url"],
                amount=float(inv["amount"]),
                currency=inv["asset"],
                status=inv["status"],
                payload=inv.get("payload", "")
            )

    async def get_invoice(self, invoice_id: int) -> Optional[Invoice]:
        """Проверить статус инвойса по ID."""
        await self._ensure_session()
        async with self.session.get(f"{self.BASE_URL}/getInvoices", params={"invoice_ids": invoice_id}) as resp:
            result = await resp.json()
            if not result.get("ok") or not result["result"]["items"]:
                return None
            inv = result["result"]["items"][0]
            return Invoice(
                invoice_id=inv["invoice_id"],
                pay_url=inv["pay_url"],
                amount=float(inv["amount"]),
                currency=inv["asset"],
                status=inv["status"],
                payload=inv.get("payload", "")
            )

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        """Проверить подпись вебхука (IPN)."""
        if not self.ipn_secret:
            return True  # если секрет не задан — пропускаем
        expected = hmac.new(
            self.ipn_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def handle_webhook(self, data: dict) -> Optional[Invoice]:
        """Обработать входящий вебхук от CryptoBot.

        Ожидает update_type = "invoice_paid" и возвращает Invoice.
        """
        if data.get("update_type") != "invoice_paid":
            return None
        inv = data.get("payload", {})
        return Invoice(
            invoice_id=inv.get("invoice_id"),
            pay_url=inv.get("pay_url", ""),
            amount=float(inv.get("amount", 0)),
            currency=inv.get("asset", ""),
            status=inv.get("status", ""),
            payload=inv.get("payload", "")
        )


# Планы подписки
PLANS = {
    "day": {"name": "1 день", "price_usd": 50, "days": 1},
    "week": {"name": "1 неделя", "price_usd": 100, "days": 7},
    "month": {"name": "1 месяц", "price_usd": 250, "days": 30},
    "lifetime": {"name": "Пожизненная", "price_usd": 0, "days": 36500},  # 100 лет
}


def get_plan_key(price_usd: int) -> Optional[str]:
    for k, v in PLANS.items():
        if v["price_usd"] == price_usd:
            return k
    return None