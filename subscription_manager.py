"""Менеджер подписок: БД пользователей, проверка доступа, активация по платежу."""
import sqlite3
import time
import logging
from dataclasses import dataclass
from typing import Optional, List

log = logging.getLogger("subscriptions")


@dataclass
class User:
    user_id: int
    username: str
    paid_until: int  # unix timestamp
    is_active: bool
    plan: str  # day/week/month
    last_payment_id: int


class SubscriptionManager:
    def __init__(self, db_path: str = "subscriptions.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    paid_until INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 0,
                    plan TEXT DEFAULT '',
                    last_payment_id INTEGER DEFAULT 0,
                    created_at INTEGER DEFAULT (strftime('%s','now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    invoice_id INTEGER,
                    amount_usd REAL,
                    currency TEXT,
                    plan TEXT,
                    status TEXT,
                    created_at INTEGER DEFAULT (strftime('%s','now')),
                    paid_at INTEGER
                )
            """)
            conn.commit()

    def get_user(self, user_id: int) -> Optional[User]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            if row:
                return User(
                    user_id=row["user_id"],
                    username=row["username"] or "",
                    paid_until=row["paid_until"],
                    is_active=bool(row["is_active"]),
                    plan=row["plan"] or "",
                    last_payment_id=row["last_payment_id"]
                )
            return None

    def create_or_update_user(self, user_id: int, username: str) -> User:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)
            """, (user_id, username))
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            conn.commit()
        return self.get_user(user_id) or User(user_id, username, 0, False, "", 0)

    def has_active_subscription(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        if not user.is_active:
            return False
        now = int(time.time())
        if user.paid_until > 0 and user.paid_until < now:
            # Подписка истекла — деактивируем
            self.deactivate(user_id)
            return False
        return True

    def activate_subscription(self, user_id: int, plan: str, days: int, invoice_id: int, amount_usd: float, currency: str):
        now = int(time.time())
        paid_until = now + days * 86400
        with sqlite3.connect(self.db_path) as conn:
            # Обновляем пользователя
            conn.execute("""
                UPDATE users SET
                    paid_until=?, is_active=1, plan=?, last_payment_id=(
                        SELECT id FROM payments WHERE invoice_id=? ORDER BY id DESC LIMIT 1
                    )
                WHERE user_id=?
            """, (paid_until, plan, invoice_id, user_id))
            # Обновляем платёж
            conn.execute("""
                UPDATE payments SET status='paid', paid_at=? WHERE invoice_id=?
            """, (now, invoice_id))
            conn.commit()
        log.info("✅ Подписка активирована: user=%s plan=%s до=%s", user_id, plan, paid_until)

    def deactivate(self, user_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (user_id,))
            conn.commit()
        log.info("❌ Подписка деактивирована: user=%s", user_id)

    def record_invoice(self, user_id: int, invoice_id: int, amount_usd: float,
                       currency: str, plan: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO payments (user_id, invoice_id, amount_usd, currency, plan, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            """, (user_id, invoice_id, amount_usd, currency, plan))
            conn.commit()

    def get_subscription_info(self, user_id: int) -> str:
        user = self.get_user(user_id)
        if not user:
            return "❌ Пользователь не найден"
        now = int(time.time())
        if user.is_active and user.paid_until > now:
            left = (user.paid_until - now) // 3600
            return (f"✅ <b>Активная подписка</b>\n"
                    f"Тариф: {user.plan}\n"
                    f"Осталось: {left} ч.\n"
                    f"До: {time.strftime('%d.%m.%Y %H:%M', time.localtime(user.paid_until))}")
        else:
            return "❌ <b>Подписка неактивна</b>\nИспользуйте /subscribe для покупки."

    def get_all_active_users(self) -> List[User]:
        now = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM users WHERE is_active=1 AND paid_until > ?", (now,))
            return [User(r["user_id"], r["username"] or "", r["paid_until"],
                         True, r["plan"] or "", r["last_payment_id"]) for r in cur.fetchall()]


# Глобальный экземпляр (инициализируется в run.py)
sub_manager: Optional[SubscriptionManager] = None