"""Telegram-управление + уведомления + платные подписки через @CryptoBot.

Использует webhook-режим для Telegram (быстро, не блокирует).
Mini App запускается в отдельном процессе.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from aiohttp import web
from payment_cryptobot import CryptoBotPay, Invoice, PLANS
from subscription_manager import SubscriptionManager, sub_manager

log = logging.getLogger("telegram_bot")


class TelegramController:
    def __init__(self, settings, engine):
        self.settings = settings
        self.engine = engine
        self.app = None
        self.cryptobot = None
        self._webhook_pending = {}  # invoice_id -> user_id
        self._bot_process = None
        self._mini_app_process = None

    def start(self):
        if not self.settings.tg_token:
            return

        # Передаём notify-функцию в движок
        self.engine.notify = self._make_notify()

        # Инициализируем CryptoBot
        if self.settings.cryptobot_api_token:
            self.cryptobot = CryptoBotPay(
                self.settings.cryptobot_api_token,
                self.settings.cryptobot_ipn_secret
            )

        # Инициализируем менеджер подписок
        global sub_manager
        sub_manager = SubscriptionManager()

        # Авто-регистрация админа с пожизненной подпиской
        admin_id = self.settings.admin_id
        if admin_id and admin_id > 0:
            sub_manager.create_or_update_user(admin_id, "admin")
            sub_manager.activate_subscription(admin_id, "lifetime", 36500, 0, 0, "ADMIN")
            log.info(f"✅ Админ {admin_id} автоматически зарегистрирован с пожизненной подпиской")

        # Запускаем Mini App в отдельном процессе
        self._start_mini_app()

        # Запускаем Telegram бота в отдельном потоке (webhook mode)
        self._bot_thread = threading.Thread(target=self._run_bot, name="telegram-bot", daemon=True)
        self._bot_thread.start()

        log.info("✅ Telegram controller started")

    def _start_mini_app(self):
        """Запускает Mini App FastAPI в отдельном процессе."""
        mini_app_path = os.path.join(os.path.dirname(__file__), "mini_app", "main.py")
        if not os.path.exists(mini_app_path):
            log.warning("Mini App not found, skipping")
            return

        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))

        self._mini_app_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mini_app.main:app",
             "--host", "0.0.0.0", "--port", os.getenv("PORT", "8000")],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        log.info(f"🌐 Mini App started on port 8000 (PID: {self._mini_app_process.pid})")

    def _make_notify(self) -> Callable[[str], None]:
        """Создаёт синхронную функцию для отправки сообщений в Telegram."""
        def notify(text: str):
            # Используем requests для синхронной отправки
            import requests
            try:
                url = f"https://api.telegram.org/bot{self.settings.tg_token}/sendMessage"
                requests.post(url, json={
                    "chat_id": self.settings.admin_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }, timeout=5)
            except Exception as e:
                log.warning(f"Notify error: {e}")
        return notify

    # --- Trade Notifications ---
    def notify_trade_open(self, symbol: str, side: str, qty: float, entry: float,
                          stop: float, take: float, notional: float):
        """Уведомление об открытии сделки админу."""
        text = self._format_trade_open(symbol, side, qty, entry, stop, take, notional)
        self._send_to_admin_sync(text)

    def notify_trade_close(self, symbol: str, side: str, qty: float, entry: float,
                           exit_price: float, pct: float, pnl: float, reason: str):
        """Уведомление о закрытии сделке админу."""
        text = self._format_trade_close(symbol, side, qty, entry, exit_price, pct, pnl, reason)
        self._send_to_admin_sync(text)

    def _format_trade_open(self, symbol: str, side: str, qty: float, entry: float,
                           stop: float, take: float, notional: float) -> str:
        emoji = "🟢" if side == "buy" else "🔴"
        side_name = "LONG 📈" if side == "buy" else "SHORT 📉"
        sl_pct = abs((entry - stop) / entry * 100) if side == "buy" else abs((stop - entry) / entry * 100)
        tp_pct = abs((take - entry) / entry * 100) if side == "buy" else abs((entry - take) / entry * 100)
        return (
            f"{emoji} <b>НОВАЯ СДЕЛКА: {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Тип:</b> {side_name}\n"
            f"💰 <b>Объём:</b> {notional:.2f} USDT ({qty:.4f} контрактов)\n"
            f"🎯 <b>Вход:</b> {entry:.6f}\n"
            f"🛑 <b>Стоп:</b> {stop:.6f} ({sl_pct:.2f}%)\n"
            f"🎯 <b>Тейк:</b> {take:.6f} ({tp_pct:.2f}%)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Авто-управление: жёсткий стоп 1%, трейлинг 3%→0.5%"
        )

    def _format_trade_close(self, symbol: str, side: str, qty: float, entry: float,
                            exit_price: float, pct: float, pnl: float, reason: str) -> str:
        is_profit = pnl > 0
        emoji = "🟢" if is_profit else "🔴"
        side_name = "LONG" if side == "buy" else "SHORT"
        reason_emoji = {
            "take": "🎯 Тейк-профит",
            "stop": "🛑 Стоп-лосс",
            "trailing": "📈 Трейлинг",
            "hard": "🛑 Жёсткий стоп",
            "exchange": "🏛 Биржа"
        }.get(reason.lower(), reason)
        return (
            f"{emoji} <b>СДЕЛКА ЗАКРЫТА: {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Тип:</b> {side_name}\n"
            f"💰 <b>Объём:</b> {qty:.4f} контрактов\n"
            f"📈 <b>Вход:</b> {entry:.6f} → <b>Выход:</b> {exit_price:.6f}\n"
            f"📊 <b>Результат:</b> {pct:+.2f}%\n"
            f"💵 <b>PnL:</b> <b>{pnl:+.4f} USDT</b>\n"
            f"📋 <b>Причина:</b> {reason_emoji.get(reason.lower(), reason)}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

    def _send_to_admin_sync(self, text: str):
        """Синхронная отправка админу через requests."""
        import requests
        try:
            url = f"https://api.telegram.org/bot{self.settings.tg_token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.settings.admin_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=5)
        except Exception as e:
            log.warning(f"Notify error: {e}")

    def _send_to_user_sync(self, user_id: int, text: str):
        import requests
        try:
            url = f"https://api.telegram.org/bot{self.settings.tg_token}/sendMessage"
            requests.post(url, json={
                "chat_id": user_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=5)
        except Exception as e:
            log.warning(f"Notify user error: {e}")

    def _run_bot(self):
        """Запускает Telegram бота в webhook режиме."""
        # Создаём новый event loop для этого потока
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            from telegram import Update
            from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
        except ImportError:
            raise RuntimeError("Установите python-telegram-bot: pip install python-telegram-bot")

        async def main():
            """Главная асинхронная функция для запуска бота."""
            async def allowed(update: Update) -> bool:
                return bool(update.effective_user and update.effective_user.id == self.settings.admin_id)

            async def user_allowed(update: Update) -> bool:
                return bool(update.effective_user and sub_manager.has_active_subscription(update.effective_user.id))

            # --- Команды ---
            async def status_cmd(update: Update, context):
                user_id = update.effective_user.id
                if user_id == self.settings.admin_id:
                    await update.message.reply_text(self.engine.status(), parse_mode="HTML")
                elif sub_manager.has_active_subscription(user_id):
                    await update.message.reply_text(self.engine.status(), parse_mode="HTML")
                else:
                    await update.message.reply_text("❌ Нет активной подписки. Используйте /subscribe")

            async def start_cmd(update: Update, context):
                user_id = update.effective_user.id
                if user_id == self.settings.admin_id:
                    self.engine.resume()
                    await update.message.reply_text("✅ Движок запущен.")
                elif sub_manager.has_active_subscription(user_id):
                    self.engine.resume()
                    await update.message.reply_text("✅ Торговля возобновлена.")
                else:
                    await update.message.reply_text("❌ Нет активной подписки. Используйте /subscribe")

            async def stop_cmd(update: Update, context):
                user_id = update.effective_user.id
                if user_id == self.settings.admin_id:
                    self.engine.pause()
                    await update.message.reply_text("⏸ Движок на паузе. Новые сделки не открываются.")
                elif sub_manager.has_active_subscription(user_id):
                    self.engine.pause()
                    await update.message.reply_text("⏸ Торговля на паузе. Позиции остаются открытыми.")
                else:
                    await update.message.reply_text("❌ Нет активной подписки.")

            async def panic_cmd(update: Update, context):
                user_id = update.effective_user.id
                if user_id == self.settings.admin_id:
                    self.engine.emergency_stop()
                    await update.message.reply_text("🚨 Аварийный стоп: все позиции закрыты, бот остановлен.")
                elif sub_manager.has_active_subscription(user_id):
                    self.engine.emergency_stop()
                    await update.message.reply_text("🚨 Аварийный стоп: ваши позиции закрыты.")

            async def subscribe_cmd(update: Update, context):
                user_id = update.effective_user.id
                username = update.effective_user.username or f"user_{user_id}"
                sub_manager.create_or_update_user(user_id, username)

                if sub_manager.has_active_subscription(user_id):
                    info = sub_manager.get_subscription_info(user_id)
                    await update.message.reply_text(f"✅ У вас уже есть активная подписка:\n{info}", parse_mode="HTML")
                    return

                if not self.cryptobot:
                    await update.message.reply_text("❌ Система оплаты не настроена. Обратитесь к админу.")
                    return

                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = []
                for plan_key, plan in PLANS.items():
                    keyboard.append([InlineKeyboardButton(
                        f"{plan['name']} — ${plan['price_usd']}",
                        callback_data=f"pay_{plan_key}"
                    )])
                keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="pay_cancel")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.message.reply_text(
                    "<b>Выберите тариф подписки:</b>\n\n"
                    "💡 Оплата криптой через @CryptoBot (USDT, TON, BTC, ETH и др.)\n"
                    "После оплаты подписка активируется автоматически.",
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )

            async def help_cmd(update: Update, context):
                user_id = update.effective_user.id
                is_admin = user_id == self.settings.admin_id
                has_sub = sub_manager.has_active_subscription(user_id)

                text = "<b>Доступные команды:</b>\n\n"
                text += "/status — статус бота и позиций\n"
                text += "/start — возобновить торговлю\n"
                text += "/stop — пауза (нет новых сделок)\n"
                text += "/subscribe — купить/продлить подписку\n"
                text += "/my_sub — информация о вашей подписке\n"
                if is_admin:
                    text += "\n<b>Админ:</b>\n"
                    text += "/panic — аварийный стоп (закрыть всё)\n"
                text += "\n/help — это сообщение"
                await update.message.reply_text(text, parse_mode="HTML")

            async def my_sub_cmd(update: Update, context):
                user_id = update.effective_user.id
                info = sub_manager.get_subscription_info(user_id)
                await update.message.reply_text(info, parse_mode="HTML")

            # --- Админ-команды ---
            async def admin_users_cmd(update: Update, context):
                if not await self._allowed(update):
                    return
                users = sub_manager.get_all_active_users()
                if not users:
                    await update.message.reply_text("👥 Активных подписчиков нет.")
                    return
                text = "<b>Активные подписчики:</b>\n\n"
                for u in users:
                    left = (u.paid_until - int(time.time())) // 3600
                    text += f"👤 <code>{u.user_id}</code> @{u.username or '—'} — {u.plan} ({left} ч.)\n"
                await update.message.reply_text(text, parse_mode="HTML")

            async def admin_grant_cmd(update: Update, context):
                if not await self._allowed(update):
                    return
                try:
                    args = context.args
                    if len(args) < 2:
                        await update.message.reply_text("Использование: /grant <user_id> <day|week|month>")
                        return
                    target_id = int(args[0])
                    plan_key = args[1]
                    if plan_key not in PLANS:
                        await update.message.reply_text("Тариф: day, week или month")
                        return
                    plan = PLANS[plan_key]
                    sub_manager.create_or_update_user(target_id, "")
                    sub_manager.activate_subscription(target_id, plan_key, plan["days"], 0, plan["price_usd"], "ADMIN")
                    await update.message.reply_text(f"✅ Подписка {plan['name']} выдана пользователю {target_id}")
                    # Уведомляем пользователя
                    await self._send_to_user(target_id, f"🎁 Админ выдал вам подписку: <b>{plan['name']}</b>")
                except Exception as e:
                    await update.message.reply_text(f"❌ Ошибка: {e}")

            # --- Callback Query Handler (оплата) ---
            async def callback_query(update: Update, context):
                query = update.callback_query
                await query.answer()
                user_id = query.from_user.id
                data = query.data

                if data == "pay_cancel":
                    await query.edit_message_text("❌ Отменено.")
                    return

                if data.startswith("pay_"):
                    plan_key = data[4:]
                    if plan_key not in PLANS:
                        await query.edit_message_text("❌ Неверный тариф.")
                        return

                    plan = PLANS[plan_key]
                    sub_manager.create_or_update_user(user_id, query.from_user.username or f"user_{user_id}")

                    # Создаём инвойс в CryptoBot
                    payload = f"{user_id}:{plan_key}"
                    try:
                        inv = await self.cryptobot.create_invoice(
                            amount=plan["price_usd"],
                            currency="USDT",
                            payload=payload,
                            description=f"Подписка: {plan['name']}",
                            paid_btn_name="openBot",
                            paid_btn_url="https://t.me/YourBotUsername",
                            expires_in=3600
                        )
                        # Сохраняем инвойс
                        sub_manager.record_invoice(user_id, inv.invoice_id, plan["price_usd"], "USDT", plan_key)
                        # Ждём оплату
                        self._webhook_pending[inv.invoice_id] = user_id

                        keyboard = [[InlineKeyboardButton(f"💳 Оплатить ${plan['price_usd']} (USDT)", url=inv.pay_url)],
                                    [InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_{inv.invoice_id}"),
                                     InlineKeyboardButton("❌ Отмена", callback_data="pay_cancel")]]
                        await query.edit_message_text(
                            f"<b>Инвойс создан: {plan['name']} — ${plan['price_usd']}</b>\n\n"
                            f"Нажмите кнопку ниже для оплаты USDT через @CryptoBot.\n"
                            f"Инвойс действителен 1 час.\n\n"
                            f"После оплаты нажмите «✅ Я оплатил» или подождите — система проверит автоматически.",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    except Exception as e:
                        log.error(f"Ошибка создания инвойса: {e}")
                        await query.edit_message_text(f"❌ Ошибка создания инвойса: {e}")

                elif data.startswith("check_"):
                    invoice_id = int(data[6:])
                    await query.answer("Проверяю...")
                    if not self.cryptobot:
                        await query.edit_message_text("❌ CryptoBot не настроен.")
                        return
                    inv = await self.cryptobot.get_invoice(invoice_id)
                    if inv and inv.status == "paid":
                        await self._activate_subscription_from_invoice(inv)
                        await query.edit_message_text("✅ Оплата получена! Подписка активирована.")
                    elif inv:
                        await query.edit_message_text(f"⏳ Статус: {inv.status}. Ожидайте или нажмите позже.")
                    else:
                        await query.edit_message_text("❌ Инвойс не найден.")

        # --- Обработка вебхуков от CryptoBot ---
            async def webhook_handler(request):
                """Обработчик вебхуков от CryptoBot (aiohttp handler)."""
                try:
                    body = await request.read()
                    signature = request.headers.get("Crypto-Pay-API-Signature", "")
                    if self.cryptobot and not self.cryptobot.verify_webhook(body, signature):
                        log.warning("CryptoBot webhook: неверная подпись")
                        return web.Response(status=401)

                    data = json.loads(body)
                    inv = await self.cryptobot.handle_webhook(data)
                    if inv:
                        await self._activate_subscription_from_invoice(inv)
                    return web.Response(status=200)
                except Exception as e:
                    log.error(f"Webhook error: {e}")
                    return web.Response(status=500)

            # --- Вспомогательные ---
            async def _activate_subscription_from_invoice(inv: Invoice):
                """Активировать подписку по оплаченному инвойсу."""
                try:
                    payload = inv.payload  # "user_id:plan"
                    if not payload or ":" not in payload:
                        return
                    user_id_str, plan_key = payload.split(":", 1)
                    user_id = int(user_id_str)
                    plan = PLANS.get(plan_key)
                    if not plan:
                        return
                    sub_manager.activate_subscription(
                        user_id, plan_key, plan["days"], inv.invoice_id, inv.amount, inv.currency
                    )
                    # Уведомляем пользователя
                    await self._send_to_user(user_id,
                        f"✅ <b>Подписка активирована!</b>\n"
                        f"Тариф: {plan['name']}\n"
                        f"Оплачено: ${inv.amount} {inv.currency}")
                    # Удаляем из ожидающих
                    self._webhook_pending.pop(inv.invoice_id, None)
                except Exception as e:
                    log.error(f"Ошибка активации подписки: {e}")

            async def _send_to_user(self, user_id: int, text: str):
                import aiohttp
                try:
                    url = f"https://api.telegram.org/bot{self.settings.tg_token}/sendMessage"
                    async with aiohttp.ClientSession() as session:
                        await session.post(url, json={
                            "chat_id": user_id,
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True
                        }, timeout=aiohttp.ClientTimeout(total=5))
                except Exception as e:
                    log.warning(f"Notify user error: {e}")

            async def _allowed(update) -> bool:
                return bool(update.effective_user and update.effective_user.id == self.settings.admin_id)

            # --- Регистрация хендлеров ---
            from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
            from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

            self.app = Application.builder().token(self.settings.tg_token).build()

            # Команды
            self.app.add_handler(CommandHandler("status", status_cmd))
            self.app.add_handler(CommandHandler("start", start_cmd))
            self.app.add_handler(CommandHandler("stop", stop_cmd))
            self.app.add_handler(CommandHandler("panic", panic_cmd))
            self.app.add_handler(CommandHandler("subscribe", subscribe_cmd))
            self.app.add_handler(CommandHandler("my_sub", my_sub_cmd))
            self.app.add_handler(CommandHandler("help", help_cmd))
            self.app.add_handler(CommandHandler("users", admin_users_cmd))
            self.app.add_handler(CommandHandler("grant", admin_grant_cmd))

            # Callbacks
            self.app.add_handler(CallbackQueryHandler(callback_query))

            # Вебхук для CryptoBot
            try:
                from aiohttp import web
                self.webhook_app = web.Application()
                self.webhook_app.router.add_post("/cryptobot_webhook", webhook_handler)
                # Запускаем веб-сервер в фоне
                asyncio.create_task(self._start_webhook_server())
            except ImportError:
                log.warning("aiohttp не установлен — вебхуки CryptoBot не будут работать")

            # Настраиваем webhook для Telegram
            webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL")
            if webhook_url:
                await self._setup_webhook(webhook_url)
            else:
                log.warning("TELEGRAM_WEBHOOK_URL не задан — используем polling как fallback")
                await self._start_polling()

        # Запускаем главную асинхронную функцию
        self.loop.run_until_complete(main())
        # Держим event loop живым
        self.loop.run_forever()

        # Запускаем главный цикл
        self.loop.run_until_complete(main())
        # Держим event loop живым
        self.loop.run_forever()

    def _run_bot(self):
        """Запускает Telegram бота в webhook режиме."""
        # Создаём новый event loop для этого потока
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            from telegram import Update
            from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
        except ImportError:
            raise RuntimeError("Установите python-telegram-bot: pip install python-telegram-bot")

        async def main():
            """Главная асинхронная функция для запуска бота."""
            if user_id == self.settings.admin_id:
                self.engine.emergency_stop()
                await update.message.reply_text("🚨 Аварийный стоп: все позиции закрыты, бот остановлен.")
            elif sub_manager.has_active_subscription(user_id):
                self.engine.emergency_stop()
                await update.message.reply_text("🚨 Аварийный стоп: ваши позиции закрыты.")

        async def subscribe_cmd(update: Update, context):
            user_id = update.effective_user.id
            username = update.effective_user.username or f"user_{user_id}"
            sub_manager.create_or_update_user(user_id, username)

            if sub_manager.has_active_subscription(user_id):
                info = sub_manager.get_subscription_info(user_id)
                await update.message.reply_text(f"✅ У вас уже есть активная подписка:\n{info}", parse_mode="HTML")
                return

            if not self.cryptobot:
                await update.message.reply_text("❌ Система оплаты не настроена. Обратитесь к админу.")
                return

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = []
            for plan_key, plan in PLANS.items():
                keyboard.append([InlineKeyboardButton(
                    f"{plan['name']} — ${plan['price_usd']}",
                    callback_data=f"pay_{plan_key}"
                )])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="pay_cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "<b>Выберите тариф подписки:</b>\n\n"
                "💡 Оплата криптой через @CryptoBot (USDT, TON, BTC, ETH и др.)\n"
                "После оплаты подписка активируется автоматически.",
                parse_mode="HTML",
                reply_markup=reply_markup
            )

        async def help_cmd(update: Update, context):
            user_id = update.effective_user.id
            is_admin = user_id == self.settings.admin_id
            has_sub = sub_manager.has_active_subscription(user_id)

            text = "<b>Доступные команды:</b>\n\n"
            text += "/status — статус бота и позиций\n"
            text += "/start — возобновить торговлю\n"
            text += "/stop — пауза (нет новых сделок)\n"
            text += "/subscribe — купить/продлить подписку\n"
            text += "/my_sub — информация о вашей подписке\n"
            if is_admin:
                text += "\n<b>Админ:</b>\n"
                text += "/panic — аварийный стоп (закрыть всё)\n"
            text += "\n/help — это сообщение"
            await update.message.reply_text(text, parse_mode="HTML")

        async def my_sub_cmd(update: Update, context):
            user_id = update.effective_user.id
            info = sub_manager.get_subscription_info(user_id)
            await update.message.reply_text(info, parse_mode="HTML")

        # --- Админ-команды ---
        async def admin_users_cmd(update: Update, context):
            if not await self._allowed(update):
                return
            users = sub_manager.get_all_active_users()
            if not users:
                await update.message.reply_text("👥 Активных подписчиков нет.")
                return
            text = "<b>Активные подписчики:</b>\n\n"
            for u in users:
                left = (u.paid_until - int(time.time())) // 3600
                text += f"👤 <code>{u.user_id}</code> @{u.username or '—'} — {u.plan} ({left} ч.)\n"
            await update.message.reply_text(text, parse_mode="HTML")

        async def admin_grant_cmd(update: Update, context):
            if not await self._allowed(update):
                return
            try:
                args = context.args
                if len(args) < 2:
                    await update.message.reply_text("Использование: /grant <user_id> <day|week|month>")
                    return
                target_id = int(args[0])
                plan_key = args[1]
                if plan_key not in PLANS:
                    await update.message.reply_text("Тариф: day, week или month")
                    return
                plan = PLANS[plan_key]
                sub_manager.create_or_update_user(target_id, "")
                sub_manager.activate_subscription(target_id, plan_key, plan["days"], 0, plan["price_usd"], "ADMIN")
                await update.message.reply_text(f"✅ Подписка {plan['name']} выдана пользователю {target_id}")
                # Уведомляем пользователя
                await self._send_to_user(target_id, f"🎁 Админ выдал вам подписку: <b>{plan['name']}</b>")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка: {e}")

        # --- Callback Query Handler (оплата) ---
        async def callback_query(update: Update, context):
            query = update.callback_query
            await query.answer()
            user_id = query.from_user.id
            data = query.data

            if data == "pay_cancel":
                await query.edit_message_text("❌ Отменено.")
                return

            if data.startswith("pay_"):
                plan_key = data[4:]
                if plan_key not in PLANS:
                    await query.edit_message_text("❌ Неверный тариф.")
                    return

                plan = PLANS[plan_key]
                sub_manager.create_or_update_user(user_id, query.from_user.username or f"user_{user_id}")

                # Создаём инвойс в CryptoBot
                payload = f"{user_id}:{plan_key}"
                try:
                    inv = await self.cryptobot.create_invoice(
                        amount=plan["price_usd"],
                        currency="USDT",
                        payload=payload,
                        description=f"Подписка: {plan['name']}",
                        paid_btn_name="openBot",
                        paid_btn_url="https://t.me/YourBotUsername",
                        expires_in=3600
                    )
                    # Сохраняем инвойс
                    sub_manager.record_invoice(user_id, inv.invoice_id, plan["price_usd"], "USDT", plan_key)
                    # Ждём оплату
                    self._webhook_pending[inv.invoice_id] = user_id

                    keyboard = [[InlineKeyboardButton(f"💳 Оплатить ${plan['price_usd']} (USDT)", url=inv.pay_url)],
                                [InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_{inv.invoice_id}"),
                                 InlineKeyboardButton("❌ Отмена", callback_data="pay_cancel")]]
                    await query.edit_message_text(
                        f"<b>Инвойс создан: {plan['name']} — ${plan['price_usd']}</b>\n\n"
                        f"Нажмите кнопку ниже для оплаты USDT через @CryptoBot.\n"
                        f"Инвойс действителен 1 час.\n\n"
                        f"После оплаты нажмите «✅ Я оплатил» или подождите — система проверит автоматически.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except Exception as e:
                    log.error(f"Ошибка создания инвойса: {e}")
                    await query.edit_message_text(f"❌ Ошибка создания инвойса: {e}")

            elif data.startswith("check_"):
                invoice_id = int(data[6:])
                await query.answer("Проверяю...")
                if not self.cryptobot:
                    await query.edit_message_text("❌ CryptoBot не настроен.")
                    return
                inv = await self.cryptobot.get_invoice(invoice_id)
                if inv and inv.status == "paid":
                    await self._activate_subscription_from_invoice(inv)
                    await query.edit_message_text("✅ Оплата получена! Подписка активирована.")
                elif inv:
                    await query.edit_message_text(f"⏳ Статус: {inv.status}. Ожидайте или нажмите позже.")
                else:
                    await query.edit_message_text("❌ Инвойс не найден.")

        # --- Обработка вебхуков от CryptoBot ---
        async def webhook_handler(request):
            """Обработчик вебхуков от CryptoBot (aiohttp handler)."""
            try:
                body = await request.read()
                signature = request.headers.get("Crypto-Pay-API-Signature", "")
                if self.cryptobot and not self.cryptobot.verify_webhook(body, signature):
                    log.warning("CryptoBot webhook: неверная подпись")
                    return web.Response(status=401)

                data = json.loads(body)
                inv = await self.cryptobot.handle_webhook(data)
                if inv:
                    await self._activate_subscription_from_invoice(inv)
                return web.Response(status=200)
            except Exception as e:
                log.error(f"Webhook error: {e}")
                return web.Response(status=500)

        # --- Вспомогательные ---
        async def _activate_subscription_from_invoice(self, inv: Invoice):
            """Активировать подписку по оплаченному инвойсу."""
            try:
                payload = inv.payload  # "user_id:plan"
                if not payload or ":" not in payload:
                    return
                user_id_str, plan_key = payload.split(":", 1)
                user_id = int(user_id_str)
                plan = PLANS.get(plan_key)
                if not plan:
                    return
                sub_manager.activate_subscription(
                    user_id, plan_key, plan["days"], inv.invoice_id, inv.amount, inv.currency
                )
                # Уведомляем пользователя
                await self._send_to_user(user_id,
                    f"✅ <b>Подписка активирована!</b>\n"
                    f"Тариф: {plan['name']}\n"
                    f"Оплачено: ${inv.amount} {inv.currency}")
                # Удаляем из ожидающих
                self._webhook_pending.pop(inv.invoice_id, None)
            except Exception as e:
                log.error(f"Ошибка активации подписки: {e}")

        async def _send_to_user(self, user_id: int, text: str):
            import aiohttp
            try:
                url = f"https://api.telegram.org/bot{self.settings.tg_token}/sendMessage"
                async with aiohttp.ClientSession() as session:
                    await session.post(url, json={
                        "chat_id": user_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    }, timeout=aiohttp.ClientTimeout(total=5))
            except Exception as e:
                log.warning(f"Notify user error: {e}")

        # --- Регистрация хендлеров ---
            from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
            from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

            self.app = Application.builder().token(self.settings.tg_token).build()

            # Команды
            self.app.add_handler(CommandHandler("status", status_cmd))
            self.app.add_handler(CommandHandler("start", start_cmd))
            self.app.add_handler(CommandHandler("stop", stop_cmd))
            self.app.add_handler(CommandHandler("panic", panic_cmd))
            self.app.add_handler(CommandHandler("subscribe", subscribe_cmd))
            self.app.add_handler(CommandHandler("my_sub", my_sub_cmd))
            self.app.add_handler(CommandHandler("help", help_cmd))
            self.app.add_handler(CommandHandler("users", admin_users_cmd))
            self.app.add_handler(CommandHandler("grant", admin_grant_cmd))

            # Callbacks
            self.app.add_handler(CallbackQueryHandler(callback_query))

            # Вебхук для CryptoBot
            try:
                from aiohttp import web
                self.webhook_app = web.Application()
                self.webhook_app.router.add_post("/cryptobot_webhook", webhook_handler)
                # Запускаем веб-сервер в фоне
                asyncio.create_task(self._start_webhook_server())
            except ImportError:
                log.warning("aiohttp не установлен — вебхуки CryptoBot не будут работать")

            # Настраиваем webhook для Telegram
            webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL")
            if webhook_url:
                await self._setup_webhook(webhook_url)
            else:
                log.warning("TELEGRAM_WEBHOOK_URL не задан — используем polling как fallback")
                await self._start_polling()

    async def _setup_webhook(self, webhook_url: str):
        """Настраивает webhook для Telegram."""
        await self.app.bot.set_webhook(webhook_url + "/telegram_webhook")
        log.info(f"🔗 Telegram webhook set: {webhook_url}")
        # Запуск webhook сервера для Telegram
        self.webhook_app.router.add_post("/telegram_webhook", self._telegram_webhook_handler)
        asyncio.create_task(self._start_webhook_server())
        log.info("🤖 Telegram bot started (webhook mode)")

    async def _start_polling(self):
        """Запуск polling режима."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("🤖 Telegram bot started (polling mode)")

    async def _telegram_webhook_handler(self, request):
        """Обработчик вебхуков от Telegram."""
        try:
            data = await request.json()
            update = Update.de_json(data, self.app.bot)
            await self.app.process_update(update)
            return web.Response(status=200)
        except Exception as e:
            log.error(f"Telegram webhook error: {e}")
            return web.Response(status=500)

    async def _start_webhook_server(self):
        from aiohttp import web
        runner = web.AppRunner(self.webhook_app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()
        log.info("🌐 Webhook сервер запущен на порту 8080")

import time
import json