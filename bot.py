import os
import re
import html
import sqlite3
import logging
import asyncio
import threading
import hashlib
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

PORT = int(os.getenv("PORT", "10000"))

DATABASE = os.getenv("DATABASE", "kino_bot.db").strip()

CATALOG_CHANNEL_LINK = os.getenv(
    "CATALOG_CHANNEL_LINK",
    "https://t.me/wittkino",
).strip()

ADMIN_CONTACT_LINK = os.getenv(
    "ADMIN_CONTACT_LINK",
    "https://t.me/wittmen",
).strip()


def parse_admin_ids():
    raw = os.getenv(
        "ADMIN_IDS",
        os.getenv("ADMIN_ID", ""),
    )

    result = set()

    for item in raw.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            result.add(int(item))
        except ValueError:
            logging.warning(
                "Invalid ADMIN_IDS ignored: %s",
                item,
            )

    return result


ADMIN_IDS = parse_admin_ids()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("WITT-KinoBot")


# ============================================================
# CONSTANTS
# ============================================================

USER_STATUS = {
    "USER": "👤 Oddiy",
    "VIP": "💎 VIP",
    "PREMIUM": "👑 Premium",
    "ADMIN": "🛡 Admin",
}

MOVIE_ACCESS = {
    "FREE": "🆓 Bepul",
    "VIP": "💎 VIP",
    "PREMIUM": "👑 Premium",
}


# ============================================================
# CONVERSATION STATES
# ============================================================

(
    MOVIE_CODE,
    MOVIE_NAME,
    MOVIE_DESCRIPTION,
    MOVIE_CATEGORY,
    MOVIE_FILE,
    MOVIE_ACCESS_STATE,
) = range(6)

(
    STATUS_GIVE_STATE,
    STATUS_REMOVE_STATE,
) = range(10, 12)

BROADCAST_STATE = 20

(
    EDIT_FIELD_STATE,
    EDIT_VALUE_STATE,
) = range(30, 32)

(
    CHANNEL_ADD_ID_STATE,
    CHANNEL_ADD_LINK_STATE,
) = range(40, 42)


# ============================================================
# CACHE
# ============================================================

SUBSCRIPTION_CACHE = {}
SUBSCRIPTION_CACHE_SECONDS = 30

ADMIN_CACHE = {}

CHANNELS_CACHE = None

CHANNELS_CACHE_TIME = 0
CHANNELS_CACHE_SECONDS = 10

CACHE_LOCK = asyncio.Lock()


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_code(value: str) -> str:
    value = str(value or "").strip().upper()
    value = re.sub(r"\s+", "", value)
    return value[:100]


def normalize_url(value: str) -> Optional[str]:
    value = str(value or "").strip()

    if not value:
        return None

    if value.startswith("@"):
        username = value[1:].strip()

        if re.fullmatch(
            r"[A-Za-z0-9_]{5,32}",
            username,
        ):
            return f"https://t.me/{username}"

        return None

    if value.startswith("t.me/"):
        value = "https://" + value

    if value.startswith("telegram.me/"):
        value = "https://" + value

    if value.startswith("http://"):
        value = "https://" + value[7:]

    if not value.startswith("https://"):
        return None

    if not re.match(
        r"^https://(t\.me|telegram\.me)/.+",
        value,
    ):
        return None

    return value


def short_text(text, length=40):
    text = str(text or "")

    if len(text) <= length:
        return text

    return text[:length - 3] + "..."


def escape(value):
    return html.escape(str(value or ""))


def access_label(access):
    return MOVIE_ACCESS.get(
        access,
        access,
    )


def status_label(status):
    return USER_STATUS.get(
        status,
        status,
    )


def category_token(category: str) -> str:
    return hashlib.sha256(
        category.encode("utf-8")
    ).hexdigest()[:12]


# ============================================================
# DATABASE
# ============================================================

DB_LOCK = threading.RLock()


def db_connect():
    conn = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA busy_timeout = 30000"
    )

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    return conn


def ensure_column(
    conn,
    table,
    column,
    definition,
):
    columns = {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    if column not in columns:
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def init_db():

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute(
                "PRAGMA journal_mode=WAL"
            )

            conn.execute(
                "PRAGMA synchronous=NORMAL"
            )

            conn.execute(
                "PRAGMA temp_store=MEMORY"
            )

            conn.execute(
                "PRAGMA cache_size=-20000"
            )

            # ------------------------------------------------
            # USERS
            # ------------------------------------------------

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    status TEXT DEFAULT 'USER',
                    joined_at TEXT
                )
            """)

            # ------------------------------------------------
            # MOVIES
            # ------------------------------------------------

            conn.execute("""
                CREATE TABLE IF NOT EXISTS movies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    category TEXT DEFAULT 'Boshqa',
                    file_id TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    access TEXT DEFAULT 'FREE',
                    views INTEGER DEFAULT 0,
                    created_at TEXT
                )
            """)

            # ------------------------------------------------
            # RATINGS
            # ------------------------------------------------

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    movie_id INTEGER NOT NULL,
                    rating INTEGER NOT NULL,
                    created_at TEXT,
                    UNIQUE(user_id, movie_id)
                )
            """)

            # ------------------------------------------------
            # SETTINGS
            # ------------------------------------------------

            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # ------------------------------------------------
            # REQUIRED CHANNELS
            # ------------------------------------------------

            conn.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    invite_link TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT
                )
            """)
            # ------------------------------------------------
            # JOIN REQUESTS
            # ------------------------------------------------

            conn.execute("""
                CREATE TABLE IF NOT EXISTS join_requests (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    requested_at TEXT,
                    PRIMARY KEY (user_id, chat_id)
                )
            """)
            # ------------------------------------------------
            # MIGRATIONS
            # ------------------------------------------------

            ensure_column(
                conn,
                "users",
                "username",
                "TEXT",
            )

            ensure_column(
                conn,
                "users",
                "first_name",
                "TEXT",
            )

            ensure_column(
                conn,
                "users",
                "status",
                "TEXT DEFAULT 'USER'",
            )

            ensure_column(
                conn,
                "users",
                "joined_at",
                "TEXT",
            )

            ensure_column(
                conn,
                "movies",
                "description",
                "TEXT DEFAULT ''",
            )

            ensure_column(
                conn,
                "movies",
                "category",
                "TEXT DEFAULT 'Boshqa'",
            )

            ensure_column(
                conn,
                "movies",
                "access",
                "TEXT DEFAULT 'FREE'",
            )

            ensure_column(
                conn,
                "movies",
                "views",
                "INTEGER DEFAULT 0",
            )

            ensure_column(
                conn,
                "movies",
                "created_at",
                "TEXT",
            )

            # ------------------------------------------------
            # INDEXES
            # ------------------------------------------------

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_movies_code
                ON movies(code)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_movies_name
                ON movies(name)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_movies_category
                ON movies(category)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_movies_views
                ON movies(views DESC)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_users_status
                ON users(status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_ratings_movie
                ON ratings(movie_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_channels_enabled
                ON required_channels(enabled)
            """)

            conn.commit()

        finally:
            conn.close()

    logger.info(
        "Database initialized successfully."
    )


# ============================================================
# USER DATABASE
# ============================================================

def register_user(user):

    if not user:
        return

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute("""
                INSERT INTO users (
                    id,
                    username,
                    first_name,
                    status,
                    joined_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    'USER',
                    ?
                )
                ON CONFLICT(id)
                DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
            """, (
                user.id,
                user.username,
                user.first_name,
                now_iso(),
            ))

            conn.commit()

        finally:
            conn.close()


def get_user_status(user_id):

    if user_id in ADMIN_IDS:
        return "ADMIN"

    with DB_LOCK:

        conn = db_connect()

        try:

            row = conn.execute(
                """
                SELECT status
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()

            return (
                row["status"]
                if row
                else "USER"
            )

        finally:
            conn.close()


def set_user_status(
    user_id,
    status,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute("""
                INSERT INTO users (
                    id,
                    status,
                    joined_at
                )
                VALUES (?, ?, ?)

                ON CONFLICT(id)
                DO UPDATE SET
                    status = excluded.status
            """, (
                user_id,
                status,
                now_iso(),
            ))

            conn.commit()

        finally:
            conn.close()

    ADMIN_CACHE.pop(user_id, None)


def get_all_user_ids():

    with DB_LOCK:

        conn = db_connect()

        try:

            rows = conn.execute(
                "SELECT id FROM users ORDER BY id"
            ).fetchall()

            return [
                row["id"]
                for row in rows
            ]

        finally:
            conn.close()


def get_user_count():

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

        finally:
            conn.close()


def get_recent_users(limit=20):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute("""
                SELECT
                    id,
                    username,
                    first_name,
                    status,
                    joined_at
                FROM users
                ORDER BY joined_at DESC
                LIMIT ?
            """, (
                limit,
            )).fetchall()

        finally:
            conn.close()


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id):

    if user_id in ADMIN_IDS:
        return True

    cached = ADMIN_CACHE.get(user_id)

    if cached is not None:
        return cached

    status = get_user_status(user_id)

    result = status == "ADMIN"

    ADMIN_CACHE[user_id] = result

    return result


# ============================================================
# MOVIE DATABASE
# ============================================================

def get_movie_by_id(movie_id):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute(
                """
                SELECT *
                FROM movies
                WHERE id = ?
                """,
                (movie_id,),
            ).fetchone()

        finally:
            conn.close()


def get_movie_by_code(code):

    code = normalize_code(code)

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute(
                """
                SELECT *
                FROM movies
                WHERE code = ?
                """,
                (code,),
            ).fetchone()

        finally:
            conn.close()


def search_movies(
    query,
    limit=10,
):

    query = query.strip()

    with DB_LOCK:

        conn = db_connect()

        try:

            pattern = f"%{query}%"

            return conn.execute("""
                SELECT *
                FROM movies
                WHERE code LIKE ?
                   OR name LIKE ?
                   OR category LIKE ?
                   OR description LIKE ?
                ORDER BY
                    views DESC,
                    id DESC
                LIMIT ?
            """, (
                pattern,
                pattern,
                pattern,
                pattern,
                limit,
            )).fetchall()

        finally:
            conn.close()


def latest_movies(limit=10):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute("""
                SELECT *
                FROM movies
                ORDER BY id DESC
                LIMIT ?
            """, (
                limit,
            )).fetchall()

        finally:
            conn.close()


def popular_movies(limit=10):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute("""
                SELECT *
                FROM movies
                ORDER BY
                    views DESC,
                    id DESC
                LIMIT ?
            """, (
                limit,
            )).fetchall()

        finally:
            conn.close()


def get_categories():

    with DB_LOCK:

        conn = db_connect()

        try:

            rows = conn.execute("""
                SELECT DISTINCT category
                FROM movies
                WHERE category IS NOT NULL
                  AND category != ''
                ORDER BY category
            """).fetchall()

            return [
                row["category"]
                for row in rows
            ]

        finally:
            conn.close()


def movies_by_category(
    category,
    limit=20,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute("""
                SELECT *
                FROM movies
                WHERE category = ?
                ORDER BY
                    views DESC,
                    id DESC
                LIMIT ?
            """, (
                category,
                limit,
            )).fetchall()

        finally:
            conn.close()


def add_movie(
    code,
    name,
    description,
    category,
    file_id,
    file_type,
    access,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute("""
                INSERT INTO movies (
                    code,
                    name,
                    description,
                    category,
                    file_id,
                    file_type,
                    access,
                    views,
                    created_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    0,
                    ?
                )
            """, (
                code,
                name,
                description,
                category,
                file_id,
                file_type,
                access,
                now_iso(),
            ))

            conn.commit()

            return True, None

        except sqlite3.IntegrityError:
            return False, "duplicate"

        except Exception as e:

            logger.exception(
                "Movie insert error"
            )

            return False, str(e)

        finally:
            conn.close()


def update_movie(
    movie_id,
    field,
    value,
):

    allowed = {
        "code",
        "name",
        "description",
        "category",
        "access",
    }

    if field not in allowed:
        return False, "invalid_field"

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute(
                f"""
                UPDATE movies
                SET {field} = ?
                WHERE id = ?
                """,
                (
                    value,
                    movie_id,
                ),
            )

            conn.commit()

            return True, None

        except sqlite3.IntegrityError:
            return False, "duplicate"

        except Exception as e:

            logger.exception(
                "Movie update error"
            )

            return False, str(e)

        finally:
            conn.close()


def delete_movie(movie_id):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute(
                """
                DELETE FROM ratings
                WHERE movie_id = ?
                """,
                (movie_id,),
            )

            conn.execute(
                """
                DELETE FROM movies
                WHERE id = ?
                """,
                (movie_id,),
            )

            conn.commit()

        finally:
            conn.close()


def increase_views(movie_id):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute(
                """
                UPDATE movies
                SET views = views + 1
                WHERE id = ?
                """,
                (movie_id,),
            )

            conn.commit()

        finally:
            conn.close()


# ============================================================
# RATINGS
# ============================================================

def save_rating(
    user_id,
    movie_id,
    rating,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute("""
                INSERT INTO ratings (
                    user_id,
                    movie_id,
                    rating,
                    created_at
                )
                VALUES (?, ?, ?, ?)

                ON CONFLICT(user_id, movie_id)
                DO UPDATE SET
                    rating = excluded.rating,
                    created_at = excluded.created_at
            """, (
                user_id,
                movie_id,
                rating,
                now_iso(),
            ))

            conn.commit()

        finally:
            conn.close()


def get_rating_info(movie_id):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute("""
                SELECT
                    COUNT(*) AS count,
                    AVG(rating) AS average
                FROM ratings
                WHERE movie_id = ?
            """, (
                movie_id,
            )).fetchone()

        finally:
            conn.close()


# ============================================================
# REQUIRED CHANNELS
# ============================================================

def invalidate_channels_cache():
    global CHANNELS_CACHE
    global CHANNELS_CACHE_TIME

    CHANNELS_CACHE = None
    CHANNELS_CACHE_TIME = 0


def get_required_channels():

    global CHANNELS_CACHE
    global CHANNELS_CACHE_TIME

    current = datetime.now().timestamp()

    if (
        CHANNELS_CACHE is not None
        and current - CHANNELS_CACHE_TIME
        < CHANNELS_CACHE_SECONDS
    ):
        return CHANNELS_CACHE

    with DB_LOCK:

        conn = db_connect()

        try:

            rows = conn.execute("""
                SELECT *
                FROM required_channels
                WHERE enabled = 1
                ORDER BY id ASC
            """).fetchall()

            CHANNELS_CACHE = rows
            CHANNELS_CACHE_TIME = current

            return rows

        finally:
            conn.close()


def get_all_required_channels():

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute("""
                SELECT *
                FROM required_channels
                ORDER BY id ASC
            """).fetchall()

        finally:
            conn.close()


def add_required_channel(
    chat_id,
    title,
    invite_link,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute("""
                INSERT INTO required_channels (
                    chat_id,
                    title,
                    invite_link,
                    enabled,
                    created_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    1,
                    ?
                )
            """, (
                chat_id,
                title,
                invite_link,
                now_iso(),
            ))

            conn.commit()

            invalidate_channels_cache()

            return True, None

        except sqlite3.IntegrityError:
            return False, "exists"

        except Exception as e:

            logger.exception(
                "Required channel insert error"
            )

            return False, str(e)

        finally:
            conn.close()


def delete_required_channel(
    channel_id,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            conn.execute(
                """
                DELETE FROM required_channels
                WHERE id = ?
                """,
                (channel_id,),
            )

            conn.commit()

            invalidate_channels_cache()

        finally:
            conn.close()


def get_required_channel(
    channel_id,
):

    with DB_LOCK:

        conn = db_connect()

        try:

            return conn.execute(
                """
                SELECT *
                FROM required_channels
                WHERE id = ?
                """,
                (channel_id,),
            ).fetchone()

        finally:
            conn.close()


# ============================================================
# LEGACY ENV IMPORT
# ============================================================

def import_legacy_channels():

    with DB_LOCK:

        conn = db_connect()

        try:

            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM required_channels
                """
            ).fetchone()[0]

        finally:
            conn.close()

    if count > 0:
        return

    raw_ids = os.getenv(
        "REQUIRED_CHAT_IDS",
        os.getenv(
            "REQUIRED_CHAT_ID",
            "",
        ),
    )

    raw_links = os.getenv(
        "REQUIRED_CHAT_LINKS",
        os.getenv(
            "REQUIRED_CHAT_LINK",
            "",
        ),
    )

    if not raw_ids or not raw_links:
        return

    ids = [
        item.strip()
        for item in raw_ids.split(",")
        if item.strip()
    ]

    links = [
        item.strip()
        for item in raw_links.split(",")
        if item.strip()
    ]

    pair_count = min(
        len(ids),
        len(links),
    )

    for i in range(pair_count):

        try:
            chat_id = int(ids[i])
        except ValueError:
            continue

        link = normalize_url(links[i])

        if not link:
            continue

        add_required_channel(
            chat_id,
            f"Legacy channel {chat_id}",
            link,
        )


# ============================================================
# SUBSCRIPTION
# ============================================================

def subscription_cache_key(user_id):
    return user_id


def clear_subscription_cache(user_id=None):

    if user_id is None:
        SUBSCRIPTION_CACHE.clear()
        return

    SUBSCRIPTION_CACHE.pop(
        subscription_cache_key(user_id),
        None,
    )

async def handle_join_request(update, context):
    request = update.chat_join_request

    if not request:
        return

    user_id = request.from_user.id
    chat_id = request.chat.id

    try:
        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO join_requests
                (user_id, chat_id, requested_at)
                VALUES (?, ?, ?)
                """,
                (
                    user_id,
                    chat_id,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

        clear_subscription_cache(user_id)

        logger.info(
            "Join request saved: user=%s chat=%s",
            user_id,
            chat_id,
        )

    except Exception:
        logger.exception(
            "Failed to save join request: user=%s chat=%s",
            user_id,
            chat_id,
        )
async def check_one_subscription(
    bot,
    user_id,
    channel,
):

    chat_id = channel["chat_id"]
    
        # Join Request yuborgan bo‘lsa,
    # hali qabul qilinmagan bo‘lsa ham a'zo deb hisoblaymiz.
    try:
        with sqlite3.connect(DATABASE) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM join_requests
                WHERE user_id = ? AND chat_id = ?
                LIMIT 1
                """,
                (user_id, chat_id),
            ).fetchone()

        if row:
            return True, None

    except Exception:
        logger.exception(
            "Failed to check join request: user=%s chat=%s",
            user_id,
            chat_id,
        )

    try:

        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        status = str(
            member.status
        ).lower()

        if "." in status:
            status = status.rsplit(
                ".",
                1,
            )[-1]

        if status in {
            "left",
            "kicked",
            "banned",
        }:
            return False, None

        if status == "restricted":

            is_member = bool(
                getattr(
                    member,
                    "is_member",
                    False,
                )
            )

            return is_member, None

        return True, None

    except RetryAfter as e:

        return (
            False,
            f"Telegram vaqtinchalik cheklovi: "
            f"{e.retry_after}s",
        )

    except Forbidden:

        return (
            False,
            "Botning kanal/guruhni tekshirish huquqi yo‘q.",
        )

    except BadRequest as e:

        return (
            False,
            f"Telegram xatosi: {e}",
        )

    except TelegramError as e:

        return (
            False,
            f"Telegram xatosi: {e}",
        )

    except Exception as e:

        logger.exception(
            "Subscription check failed for %s",
            chat_id,
        )

        return False, str(e)


async def check_subscription(
    bot,
    user_id,
    force=False,
):

    if is_admin(user_id):
        return True, [], []

    key = subscription_cache_key(
        user_id
    )

    cached = SUBSCRIPTION_CACHE.get(key)

    now = datetime.now().timestamp()

    if (
        not force
        and cached
        and now - cached["time"]
        < SUBSCRIPTION_CACHE_SECONDS
    ):
        return (
            cached["ok"],
            cached["missing"],
            cached["errors"],
        )

    channels = get_required_channels()

    if not channels:

        result = (
            True,
            [],
            [],
        )

        SUBSCRIPTION_CACHE[key] = {
            "time": now,
            "ok": True,
            "missing": [],
            "errors": [],
        }

        return result

    tasks = [
        check_one_subscription(
            bot,
            user_id,
            channel,
        )
        for channel in channels
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    missing = []
    errors = []

    for channel, result in zip(
        channels,
        results,
    ):

        if isinstance(
            result,
            Exception,
        ):

            errors.append(
                f"{channel['title']}: {result}"
            )

            continue

        subscribed, error = result

        if error:

            errors.append(
                f"{channel['title']}: {error}"
            )

        elif not subscribed:

            missing.append(channel)

    ok = (
        len(missing) == 0
        and len(errors) == 0
    )

    SUBSCRIPTION_CACHE[key] = {
        "time": now,
        "ok": ok,
        "missing": missing,
        "errors": errors,
    }

    return (
        ok,
        missing,
        errors,
    )


# ============================================================
# SUBSCRIPTION KEYBOARD
# ============================================================

def subscription_keyboard(
    channels,
):

    rows = []

    for channel in channels:

        title = short_text(
            channel["title"],
            35,
        )

        link = normalize_url(
            channel["invite_link"]
        )

        if not link:
            continue

        rows.append([
            InlineKeyboardButton(
                f"📢 {title}",
                url=link,
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🔄 ✅ TEKSHIRISH",
            callback_data="check_sub",
        )
    ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# REQUIRE SUBSCRIPTION
# ============================================================

async def require_subscription(
    update,
    context,
    force=False,
):

    user = update.effective_user

    if not user:
        return True

    register_user(user)

    if is_admin(user.id):
        return True

    ok, missing, errors = await check_subscription(
        context.bot,
        user.id,
        force=force,
    )

    if ok:
        return True

    markup = subscription_keyboard(
        missing
    )

    text = (
        "🔐 <b>WITT KINO</b>\n\n"
        "🎬 Botdan foydalanish uchun "
        "quyidagi kanal/guruhlarga a’zo bo‘ling.\n\n"
        "👇 A’zo bo‘lgach, "
        "<b>TEKSHIRISH</b> tugmasini bosing."
    )

    if errors:
        text = (
            "⚠️ <b>TEKSHIRISHDA MUAMMO</b>\n\n"
            "Majburiy kanal/guruhlarni tekshirishda "
            "texnik muammo yuz berdi.\n\n"
            "Admin kanal sozlamalarini tekshirishi kerak."
        )

    if update.callback_query:

        target = update.callback_query.message

        try:

            await target.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )

        except BadRequest:

            pass

    elif update.effective_message:

        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

    return False


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard(user_id):

    rows = [
        [
            "🔎 Kino qidirish",
            "🔥 Mashhur",
        ],
        [
            "🆕 Yangi kinolar",
            "🎭 Janrlar",
        ],
        [
            "📚 Katalog",
            "👤 Profilim",
        ],
        [
            "ℹ️ Bot haqida",
        ],
    ]

    if is_admin(user_id):

        rows.append([
            "🛠 ADMIN PANEL",
        ])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


# ============================================================
# ADMIN KEYBOARD
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🎬 Kino qo‘shish",
                callback_data="admin_add",
            ),
            InlineKeyboardButton(
                "🗂 Kinolar",
                callback_data="admin_movies",
            ),
        ],

        [
            InlineKeyboardButton(
                "✏️ Tahrirlash",
                callback_data="admin_edit",
            ),
            InlineKeyboardButton(
                "🗑 O‘chirish",
                callback_data="admin_delete",
            ),
        ],

        [
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="admin_stats",
            ),
            InlineKeyboardButton(
                "👥 Foydalanuvchilar",
                callback_data="admin_users",
            ),
        ],

        [
            InlineKeyboardButton(
                "📢 Majburiy kanallar",
                callback_data="admin_channels",
            ),
        ],

        [
            InlineKeyboardButton(
                "🎟 Status boshqarish",
                callback_data="admin_status",
            ),
        ],

        [
            InlineKeyboardButton(
                "📣 Reklama yuborish",
                callback_data="admin_broadcast",
            ),
        ],

        [
            InlineKeyboardButton(
                "🏠 Bosh menyu",
                callback_data="home",
            ),
        ],
    ])


def status_admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "💎 VIP berish",
                callback_data="status_give_vip",
            ),
            InlineKeyboardButton(
                "👑 PREMIUM berish",
                callback_data="status_give_premium",
            ),
        ],

        [
            InlineKeyboardButton(
                "👤 USER qilish",
                callback_data="status_remove",
            ),
        ],

        [
            InlineKeyboardButton(
                "⬅️ Admin panel",
                callback_data="admin",
            ),
        ],
    ])


def channel_admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "➕ Kanal/guruh qo‘shish",
                callback_data="channels_add",
            ),
        ],

        [
            InlineKeyboardButton(
                "📋 Ro‘yxat",
                callback_data="channels_list",
            ),
            InlineKeyboardButton(
                "🩺 Tekshirish",
                callback_data="channels_check",
            ),
        ],

        [
            InlineKeyboardButton(
                "➖ Olib tashlash",
                callback_data="channels_remove",
            ),
        ],

        [
            InlineKeyboardButton(
                "⬅️ Admin panel",
                callback_data="admin",
            ),
        ],
    ])


# ============================================================
# MOVIE KEYBOARDS
# ============================================================

def movie_list_keyboard(movies):

    rows = []

    for movie in movies:

        rows.append([
            InlineKeyboardButton(
                f"🎬 {short_text(movie['name'], 35)}",
                callback_data=f"movie:{movie['id']}",
            )
        ])

    if not rows:
        return None

    return InlineKeyboardMarkup(rows)


def rating_keyboard(movie_id):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "⭐",
                callback_data=f"rate:{movie_id}:1",
            ),
            InlineKeyboardButton(
                "⭐⭐",
                callback_data=f"rate:{movie_id}:2",
            ),
            InlineKeyboardButton(
                "⭐⭐⭐",
                callback_data=f"rate:{movie_id}:3",
            ),
            InlineKeyboardButton(
                "⭐⭐⭐⭐",
                callback_data=f"rate:{movie_id}:4",
            ),
            InlineKeyboardButton(
                "⭐⭐⭐⭐⭐",
                callback_data=f"rate:{movie_id}:5",
            ),
        ],

        [
            InlineKeyboardButton(
                "🏠 Bosh menyu",
                callback_data="home",
            )
        ],
    ])


# ============================================================
# MOVIE ACCESS
# ============================================================

def can_access_movie(
    user_id,
    movie,
):

    required = movie["access"]

    if required == "FREE":
        return True

    if is_admin(user_id):
        return True

    status = get_user_status(
        user_id
    )

    if required == "VIP":

        return status in {
            "VIP",
            "PREMIUM",
        }

    if required == "PREMIUM":

        return status == "PREMIUM"

    return True


# ============================================================
# MOVIE CAPTION
# ============================================================

def movie_caption(movie):

    rating = get_rating_info(
        movie["id"]
    )

    average = rating["average"]

    if average is None:

        rating_text = (
            "⭐ Hali baholanmagan"
        )

    else:

        rating_text = (
            f"⭐ {float(average):.1f}/5"
        )

    caption = (
        f"🎬 <b>{escape(movie['name'])}</b>\n\n"
        f"🆔 Kod: <code>{escape(movie['code'])}</code>\n"
        f"🎭 Janr: <b>{escape(movie['category'])}</b>\n"
        f"🔐 Kirish: <b>{escape(access_label(movie['access']))}</b>\n"
        f"👁 Ko‘rishlar: <b>{movie['views'] + 1}</b>\n"
        f"{rating_text}\n\n"
        f"📝 <b>Ma’lumot:</b>\n"
        f"{escape(movie['description'])}"
    )

    return caption[:1000]


# ============================================================
# DELIVER MOVIE
# ============================================================

async def deliver_movie(
    bot,
    chat_id,
    movie,
):

    caption = movie_caption(
        movie
    )

    markup = rating_keyboard(
        movie["id"]
    )

    if movie["file_type"] == "video":

        await bot.send_video(
            chat_id=chat_id,
            video=movie["file_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            supports_streaming=True,
        )

    else:

        await bot.send_document(
            chat_id=chat_id,
            document=movie["file_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

    # View counter is intentionally after successful send.
    increase_views(
        movie["id"]
    )


# ============================================================
# START
# ============================================================

async def start_command(
    update,
    context,
):

    user = update.effective_user

    register_user(user)

    if not await require_subscription(
        update,
        context,
    ):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "✨ <b>WITT KINO</b> ga xush kelibsiz!\n\n"
        "🎬 Kino kodini yuboring.\n"
        "🔎 Yoki menyudan kerakli bo‘limni tanlang.\n\n"
        "🚀 Tez. Qulay. Kino.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(
            user.id
        ),
    )


# ============================================================
# HOME
# ============================================================

async def show_home(
    update,
    context,
):

    user = update.effective_user

    if not await require_subscription(
        update,
        context,
    ):
        return

    text = (
        "🏠 <b>WITT KINO</b>\n\n"
        "🎬 Kino kodini yuboring.\n"
        "🔥 Mashhur kinolarni ko‘ring.\n"
        "🆕 Yangi qo‘shilganlarni toping.\n"
        "🎭 Janr bo‘yicha qidiring.\n\n"
        "⚡ Siz uchun hammasi bir joyda."
    )

    # Callback orqali kelgan bo‘lsa,
    # eski xabarni edit qilishga urinmaymiz.
    # Chunki eski xabar video bo‘lishi mumkin.
    if update.callback_query:

        query = update.callback_query

        try:
            await query.answer()
        except Exception:
            pass

        try:
            await query.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(
                    user.id
                ),
            )
        except Exception:
            pass

    else:

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(
                user.id
            ),
        )

# ============================================================
# SAFE EDIT
# ============================================================

async def safe_edit(
    query,
    text,
    reply_markup=None,
):

    try:

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    except BadRequest as e:

        if (
            "message is not modified"
            not in str(e).lower()
        ):

            raise


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context,
):

    if not update.message:
        return

    user = update.effective_user
    text = update.message.text.strip()

    register_user(user)

    if not await require_subscription(
        update,
        context,
    ):
        return

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    if text == "🔎 Kino qidirish":

        context.user_data[
            "search_mode"
        ] = True

        await update.message.reply_text(
            "🔎 <b>Kino qidirish</b>\n\n"
            "🎬 Kino kodi yoki nomini yuboring:",
            parse_mode=ParseMode.HTML,
        )

        return

    # --------------------------------------------------------
    # POPULAR
    # --------------------------------------------------------

    if text == "🔥 Mashhur":

        movies = popular_movies(
            10
        )

        if not movies:

            await update.message.reply_text(
                "📭 Hozircha kinolar mavjud emas."
            )

            return

        await update.message.reply_text(
            "🔥 <b>ENG MASHHUR KINOLAR</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=movie_list_keyboard(
                movies
            ),
        )

        return

    # --------------------------------------------------------
    # LATEST
    # --------------------------------------------------------

    if text == "🆕 Yangi kinolar":

        movies = latest_movies(
            10
        )

        if not movies:

            await update.message.reply_text(
                "📭 Hozircha kinolar mavjud emas."
            )

            return

        await update.message.reply_text(
            "🆕 <b>YANGI QO‘SHILGAN KINOLAR</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=movie_list_keyboard(
                movies
            ),
        )

        return

    # --------------------------------------------------------
    # CATEGORIES
    # --------------------------------------------------------

    if text == "🎭 Janrlar":

        categories = get_categories()

        if not categories:

            await update.message.reply_text(
                "🎭 Hozircha janrlar mavjud emas."
            )

            return

        rows = []

        for category in categories:

            rows.append([
                InlineKeyboardButton(
                    f"🎭 {short_text(category, 30)}",
                    callback_data=(
                        f"cat:{category_token(category)}"
                    ),
                )
            ])

        await update.message.reply_text(
            "🎭 <b>JANRLAR</b>\n\n"
            "Kerakli janrni tanlang:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                rows
            ),
        )

        return

    # --------------------------------------------------------
    # CATALOG
    # --------------------------------------------------------

    if text == "📚 Katalog":

        link = normalize_url(
            CATALOG_CHANNEL_LINK
        )

        if link:

            await update.message.reply_text(
                "📚 <b>WITT KINO KATALOG</b>\n\n"
                "🎬 Barcha kino yangiliklari "
                "va treylerlar:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📚 KATALOGNI OCHISH",
                            url=link,
                        )
                    ]
                ]),
            )

        else:

            await update.message.reply_text(
                "📚 Katalog hozircha sozlanmagan."
            )

        return

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if text == "👤 Profilim":

        status = get_user_status(
            user.id
        )

        await update.message.reply_text(
            "👤 <b>SIZNING PROFILINGIZ</b>\n\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"👤 Ism: <b>{escape(user.first_name)}</b>\n"
            f"🔐 Status: <b>{escape(status_label(status))}</b>",
            parse_mode=ParseMode.HTML,
        )

        return

    # --------------------------------------------------------
    # ABOUT
    # --------------------------------------------------------

    if text == "ℹ️ Bot haqida":

        admin_link = normalize_url(
            ADMIN_CONTACT_LINK
        )

        markup = None

        if admin_link:

            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Admin bilan bog‘lanish",
                        url=admin_link,
                    )
                ]
            ])

        await update.message.reply_text(
            "ℹ️ <b>WITT KINO</b>\n\n"
            "🎬 Kino kodlari orqali "
            "tezkor film qidirish.\n"
            "🔥 Mashhur filmlar.\n"
            "🆕 Yangi qo‘shilgan filmlar.\n"
            "🎭 Janrlar.\n"
            "⭐ Reyting tizimi.\n",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

        return

    # --------------------------------------------------------
    # ADMIN PANEL
    # --------------------------------------------------------

    if text == "🛠 ADMIN PANEL":

        if not is_admin(user.id):

            await update.message.reply_text(
                "⛔ Ruxsat yo‘q."
            )

            return

        await update.message.reply_text(
            "🛠 <b>WITT KINO ADMIN CENTER</b>\n\n"
            "Quyidagi boshqaruvlardan foydalaning:",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )

        return

    # --------------------------------------------------------
    # MOVIE SEARCH / CODE
    # --------------------------------------------------------

    context.user_data[
        "search_mode"
    ] = False

    query = text

    movie = get_movie_by_code(
        query
    )

    if movie:

        if not can_access_movie(
            user.id,
            movie,
        ):

            await update.message.reply_text(
                "🔒 <b>Premium kontent</b>\n\n"
                f"Bu film uchun kirish darajasi: "
                f"<b>{escape(access_label(movie['access']))}</b>\n\n"
                "💎 Statusingizni yangilash uchun "
                "admin bilan bog‘laning.",
                parse_mode=ParseMode.HTML,
            )

            return

        try:

            await deliver_movie(
                context.bot,
                update.effective_chat.id,
                movie,
            )

        except TelegramError:

            logger.exception(
                "Movie delivery failed"
            )

            await update.message.reply_text(
                "⚠️ Filmni yuborishda xatolik yuz berdi.\n"
                "Admin film faylini tekshirishi kerak."
            )

        return

    movies = search_movies(
        query,
        10,
    )

    if movies:

        await update.message.reply_text(
            "🔎 <b>QIDIRUV NATIJALARI</b>\n\n"
            "Kerakli filmni tanlang:",
            parse_mode=ParseMode.HTML,
            reply_markup=movie_list_keyboard(
                movies
            ),
        )

        return

    catalog = normalize_url(
        CATALOG_CHANNEL_LINK
    )

    markup = None

    if catalog:

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📚 KATALOGNI KO‘RISH",
                    url=catalog,
                )
            ]
        ])

    await update.message.reply_text(
        "😕 <b>Film topilmadi.</b>\n\n"
        "🎬 Bu kod bo‘yicha film hali yuklanmagan.\n"
        "📚 Katalog yoki treylerlar kanalini "
        "ko‘rib chiqing.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def show_admin_panel(
    query,
):

    await safe_edit(
        query,
        "🛠 <b>WITT KINO ADMIN CENTER</b>\n\n"
        "🎯 Barcha boshqaruvlar shu yerda.\n\n"
        "🎬 Kinolar\n"
        "👥 Foydalanuvchilar\n"
        "📢 Majburiy kanallar\n"
        "📣 Reklama\n"
        "📊 Statistika\n"
        "🎟 Statuslar\n\n"
        "👇 Kerakli bo‘limni tanlang:",
        admin_keyboard(),
    )


# ============================================================
# ADMIN MOVIES
# ============================================================

async def admin_movies_page(
    query,
):

    movies = latest_movies(
        20
    )

    if not movies:

        await safe_edit(
            query,
            "🗂 <b>KINOLAR</b>\n\n"
            "📭 Hozircha hech qanday kino yo‘q.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "➕ Kino qo‘shish",
                        callback_data="admin_add",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Orqaga",
                        callback_data="admin",
                    )
                ],
            ]),
        )

        return

    text = (
        "🗂 <b>OXIRGI KINOLAR</b>\n\n"
    )

    for movie in movies:

        text += (
            f"🎬 <b>{escape(short_text(movie['name'], 35))}</b>\n"
            f"🆔 <code>{escape(movie['code'])}</code> | "
            f"👁 {movie['views']}\n\n"
        )

    rows = []

    for movie in movies:

        rows.append([
            InlineKeyboardButton(
                f"🎬 {short_text(movie['name'], 30)}",
                callback_data=(
                    f"movie:{movie['id']}"
                ),
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Admin panel",
            callback_data="admin",
        )
    ])

    await safe_edit(
        query,
        text[:3900],
        InlineKeyboardMarkup(rows),
    )


# ============================================================
# ADD MOVIE
# ============================================================

async def add_movie_start(
    update,
    context,
):

    query = update.callback_query

    if not is_admin(
        update.effective_user.id
    ):

        await query.answer(
            "⛔ Ruxsat yo‘q.",
            show_alert=True,
        )

        return ConversationHandler.END

    await query.answer()

    context.user_data.clear()

    await query.message.reply_text(
        "🎬 <b>KINO QO‘SHISH — 1/6</b>\n\n"
        "🆔 Kino kodini yuboring.\n\n"
        "Masalan:\n"
        "<code>WITT001</code>",
        parse_mode=ParseMode.HTML,
    )

    return MOVIE_CODE


async def add_movie_code(
    update,
    context,
):

    code = normalize_code(
        update.message.text
    )

    if not code:

        await update.message.reply_text(
            "❌ Kod bo‘sh bo‘lishi mumkin emas."
        )

        return MOVIE_CODE

    if get_movie_by_code(code):

        await update.message.reply_text(
            "⚠️ Bu kod allaqachon mavjud.\n"
            "Boshqa kod yuboring."
        )

        return MOVIE_CODE

    context.user_data[
        "movie_code"
    ] = code

    await update.message.reply_text(
        "🎬 <b>KINO QO‘SHISH — 2/6</b>\n\n"
        "Film nomini yuboring:",
        parse_mode=ParseMode.HTML,
    )

    return MOVIE_NAME


async def add_movie_name(
    update,
    context,
):

    name = update.message.text.strip()

    if not name:

        await update.message.reply_text(
            "❌ Film nomi bo‘sh bo‘lmasin."
        )

        return MOVIE_NAME

    context.user_data[
        "movie_name"
    ] = name[:200]

    await update.message.reply_text(
        "📝 <b>KINO QO‘SHISH — 3/6</b>\n\n"
        "Film haqida qisqacha ma’lumot yuboring.\n"
        "Agar kerak bo‘lmasa <code>-</code> yuboring.",
        parse_mode=ParseMode.HTML,
    )

    return MOVIE_DESCRIPTION


async def add_movie_description(
    update,
    context,
):

    description = (
        update.message.text.strip()
    )

    if description == "-":
        description = ""

    context.user_data[
        "movie_description"
    ] = description[:3000]

    await update.message.reply_text(
        "🎭 <b>KINO QO‘SHISH — 4/6</b>\n\n"
        "Janrini yuboring.\n\n"
        "Masalan:\n"
        "Action\n"
        "Comedy\n"
        "Horror\n"
        "Drama",
        parse_mode=ParseMode.HTML,
    )

    return MOVIE_CATEGORY


async def add_movie_category(
    update,
    context,
):

    category = (
        update.message.text.strip()
    )

    if not category:
        category = "Boshqa"

    context.user_data[
        "movie_category"
    ] = category[:100]

    await update.message.reply_text(
        "📤 <b>KINO QO‘SHISH — 5/6</b>\n\n"
        "🎬 Endi filmning videosini yoki "
        "document faylini yuboring.",
        parse_mode=ParseMode.HTML,
    )

    return MOVIE_FILE


async def add_movie_file(
    update,
    context,
):

    if update.message.video:

        context.user_data[
            "movie_file_id"
        ] = update.message.video.file_id

        context.user_data[
            "movie_file_type"
        ] = "video"

    elif update.message.document:

        context.user_data[
            "movie_file_id"
        ] = update.message.document.file_id

        context.user_data[
            "movie_file_type"
        ] = "document"

    else:

        await update.message.reply_text(
            "❌ Faqat video yoki document yuboring."
        )

        return MOVIE_FILE

    await update.message.reply_text(
        "🔐 <b>KINO QO‘SHISH — 6/6</b>\n\n"
        "Kirish turini yuboring:\n\n"
        "🆓 <code>FREE</code>\n"
        "💎 <code>VIP</code>\n"
        "👑 <code>PREMIUM</code>",
        parse_mode=ParseMode.HTML,
    )

    return MOVIE_ACCESS_STATE


async def add_movie_access(
    update,
    context,
):

    access = (
        update.message.text.strip().upper()
    )

    if access not in {
        "FREE",
        "VIP",
        "PREMIUM",
    }:

        await update.message.reply_text(
            "❌ Noto‘g‘ri qiymat.\n\n"
            "FREE, VIP yoki PREMIUM yuboring."
        )

        return MOVIE_ACCESS_STATE

    success, error = add_movie(
        context.user_data[
            "movie_code"
        ],
        context.user_data[
            "movie_name"
        ],
        context.user_data[
            "movie_description"
        ],
        context.user_data[
            "movie_category"
        ],
        context.user_data[
            "movie_file_id"
        ],
        context.user_data[
            "movie_file_type"
        ],
        access,
    )

    if not success:

        if error == "duplicate":

            await update.message.reply_text(
                "⚠️ Bu kino kodi allaqachon mavjud."
            )

        else:

            await update.message.reply_text(
                "❌ Kino saqlanmadi."
            )

        return ConversationHandler.END

    code = context.user_data[
        "movie_code"
    ]

    name = context.user_data[
        "movie_name"
    ]

    context.user_data.clear()

    await update.message.reply_text(
        "🎉 <b>KINO MUVAFFAQIYATLI YUKLANDI!</b>\n\n"
        f"🎬 <b>{escape(name)}</b>\n"
        f"🆔 Kod: <code>{escape(code)}</code>\n"
        f"🔐 Kirish: <b>{escape(access_label(access))}</b>\n\n"
        "🚀 Foydalanuvchilar endi ushbu kod "
        "orqali filmni olishi mumkin.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    return ConversationHandler.END


# ============================================================
# EDIT MOVIE
# ============================================================

async def edit_movie_start(
    update,
    context,
):

    query = update.callback_query

    if not is_admin(
        update.effective_user.id
    ):

        await query.answer(
            "⛔ Ruxsat yo‘q.",
            show_alert=True,
        )

        return ConversationHandler.END

    await query.answer()

    movies = latest_movies(
        20
    )

    if not movies:

        await safe_edit(
            query,
            "✏️ <b>KINO TAHRIRLASH</b>\n\n"
            "📭 Kinolar mavjud emas.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Orqaga",
                        callback_data="admin",
                    )
                ]
            ]),
        )

        return ConversationHandler.END

    rows = []

    for movie in movies:

        rows.append([
            InlineKeyboardButton(
                f"✏️ {short_text(movie['name'], 35)}",
                callback_data=(
                    f"editmovie:{movie['id']}"
                ),
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data="admin",
        )
    ])

    await safe_edit(
        query,
        "✏️ <b>KINONI TAHRIRLASH</b>\n\n"
        "Qaysi kinoni tahrirlashni tanlang:",
        InlineKeyboardMarkup(rows),
    )

    return EDIT_FIELD_STATE


async def edit_movie_select(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    data = query.data

    movie_id = int(
        data.split(":")[1]
    )

    movie = get_movie_by_id(
        movie_id
    )

    if not movie:

        await query.answer(
            "Film topilmadi.",
            show_alert=True,
        )

        return ConversationHandler.END

    context.user_data[
        "edit_movie_id"
    ] = movie_id

    await safe_edit(
        query,
        f"✏️ <b>{escape(movie['name'])}</b>\n\n"
        "Qaysi maydonni o‘zgartirmoqchisiz?",
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🆔 Kod",
                    callback_data=(
                        f"editfield:{movie_id}:code"
                    ),
                ),
                InlineKeyboardButton(
                    "🎬 Nom",
                    callback_data=(
                        f"editfield:{movie_id}:name"
                    ),
                ),
            ],

            [
                InlineKeyboardButton(
                    "📝 Ma'lumot",
                    callback_data=(
                        f"editfield:{movie_id}:description"
                    ),
                ),
            ],

            [
                InlineKeyboardButton(
                    "🎭 Janr",
                    callback_data=(
                        f"editfield:{movie_id}:category"
                    ),
                ),
                InlineKeyboardButton(
                    "🔐 Kirish",
                    callback_data=(
                        f"editfield:{movie_id}:access"
                    ),
                ),
            ],

            [
                InlineKeyboardButton(
                    "❌ Bekor qilish",
                    callback_data="admin",
                )
            ],
        ]),
    )

    return EDIT_FIELD_STATE


async def edit_field_selected(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    _, movie_id, field = (
        query.data.split(":", 2)
    )

    movie_id = int(movie_id)

    context.user_data[
        "edit_movie_id"
    ] = movie_id

    context.user_data[
        "edit_field"
    ] = field

    labels = {
        "code": "🆔 yangi kino kodi",
        "name": "🎬 yangi film nomi",
        "description": "📝 yangi ma'lumot",
        "category": "🎭 yangi janr",
        "access": "🔐 FREE / VIP / PREMIUM",
    }

    await query.message.reply_text(
        f"✏️ <b>{escape(labels.get(field, field))}</b>\n\n"
        "Yangi qiymatni yuboring:",
        parse_mode=ParseMode.HTML,
    )

    return EDIT_VALUE_STATE


async def edit_movie_value(
    update,
    context,
):

    movie_id = context.user_data.get(
        "edit_movie_id"
    )

    field = context.user_data.get(
        "edit_field"
    )

    if not movie_id or not field:
        return ConversationHandler.END

    value = update.message.text.strip()

    if field == "code":

        value = normalize_code(
            value
        )

        if not value:

            await update.message.reply_text(
                "❌ Kod bo‘sh bo‘lishi mumkin emas."
            )

            return EDIT_VALUE_STATE

    elif field == "access":

        value = value.upper()

        if value not in {
            "FREE",
            "VIP",
            "PREMIUM",
        }:

            await update.message.reply_text(
                "❌ Faqat FREE, VIP yoki PREMIUM."
            )

            return EDIT_VALUE_STATE

    elif field == "name":

        value = value[:200]

    elif field == "category":

        value = value[:100]

    elif field == "description":

        value = value[:3000]

    success, error = update_movie(
        movie_id,
        field,
        value,
    )

    context.user_data.clear()

    if not success:

        if error == "duplicate":

            await update.message.reply_text(
                "⚠️ Bu kod allaqachon ishlatilgan."
            )

        else:

            await update.message.reply_text(
                "❌ Ma'lumotni yangilashda xatolik."
            )

        return ConversationHandler.END

    await update.message.reply_text(
        "✅ <b>Kino ma’lumoti yangilandi!</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    return ConversationHandler.END


# ============================================================
# DELETE MOVIE
# ============================================================

async def admin_delete_page(
    query,
):

    movies = latest_movies(
        20
    )

    if not movies:

        await safe_edit(
            query,
            "🗑 <b>KINO O‘CHIRISH</b>\n\n"
            "📭 Kinolar mavjud emas.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Orqaga",
                        callback_data="admin",
                    )
                ]
            ]),
        )

        return

    rows = []

    for movie in movies:

        rows.append([
            InlineKeyboardButton(
                f"🗑 {short_text(movie['name'], 35)}",
                callback_data=(
                    f"delete_movie:{movie['id']}"
                ),
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data="admin",
        )
    ])

    await safe_edit(
        query,
        "🗑 <b>KINO O‘CHIRISH</b>\n\n"
        "⚠️ O‘chirilgan kino qayta tiklanmaydi.\n\n"
        "Kerakli kinoni tanlang:",
        InlineKeyboardMarkup(rows),
    )


async def delete_movie_confirm(
    query,
    movie_id,
):

    movie = get_movie_by_id(
        movie_id
    )

    if not movie:

        await query.answer(
            "Film topilmadi.",
            show_alert=True,
        )

        return

    await safe_edit(
        query,
        "⚠️ <b>DIQQAT!</b>\n\n"
        f"🎬 {escape(movie['name'])}\n"
        f"🆔 <code>{escape(movie['code'])}</code>\n\n"
        "Ushbu filmni butunlay "
        "o‘chirishni xohlaysizmi?",
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🗑 HA, O‘CHIRISH",
                    callback_data=(
                        f"confirm_delete:{movie_id}"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "↩️ Bekor qilish",
                    callback_data="admin_delete",
                )
            ],
        ]),
    )


# ============================================================
# STATISTICS
# ============================================================

def get_statistics():

    with DB_LOCK:

        conn = db_connect()

        try:

            users = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            movies = conn.execute(
                "SELECT COUNT(*) FROM movies"
            ).fetchone()[0]

            views = conn.execute(
                """
                SELECT COALESCE(
                    SUM(views),
                    0
                )
                FROM movies
                """
            ).fetchone()[0]

            ratings = conn.execute(
                "SELECT COUNT(*) FROM ratings"
            ).fetchone()[0]

            channels = conn.execute(
                """
                SELECT COUNT(*)
                FROM required_channels
                WHERE enabled = 1
                """
            ).fetchone()[0]

            vip = conn.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE status = 'VIP'
                """
            ).fetchone()[0]

            premium = conn.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE status = 'PREMIUM'
                """
            ).fetchone()[0]

            return {
                "users": users,
                "movies": movies,
                "views": views,
                "ratings": ratings,
                "channels": channels,
                "vip": vip,
                "premium": premium,
            }

        finally:
            conn.close()


async def admin_stats_page(
    query,
):

    stats = get_statistics()

    text = (
        "📊 <b>WITT KINO STATISTIKA</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{stats['users']}</b>\n"
        f"🎬 Kinolar: <b>{stats['movies']}</b>\n"
        f"👁 Jami ko‘rishlar: <b>{stats['views']}</b>\n"
        f"⭐ Baholar: <b>{stats['ratings']}</b>\n"
        f"📢 Majburiy kanallar: <b>{stats['channels']}</b>\n\n"
        f"💎 VIP: <b>{stats['vip']}</b>\n"
        f"👑 Premium: <b>{stats['premium']}</b>"
    )

    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🔄 Yangilash",
                    callback_data="admin_stats",
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Admin panel",
                    callback_data="admin",
                )
            ],
        ]),
    )


# ============================================================
# USERS
# ============================================================

async def admin_users_page(
    query,
):

    users = get_recent_users(
        20
    )

    text = (
        "👥 <b>SO‘NGGI FOYDALANUVCHILAR</b>\n\n"
    )

    if not users:

        text += (
            "📭 Foydalanuvchilar mavjud emas."
        )

    else:

        for user in users:

            name = (
                user["first_name"]
                or "Noma’lum"
            )

            username = (
                f"@{user['username']}"
                if user["username"]
                else "username yo‘q"
            )

            text += (
                f"👤 <b>{escape(short_text(name, 25))}</b>\n"
                f"🆔 <code>{user['id']}</code>\n"
                f"📱 {escape(username)}\n"
                f"🔐 {escape(status_label(user['status']))}\n\n"
            )

    await safe_edit(
        query,
        text[:3900],
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🔄 Yangilash",
                    callback_data="admin_users",
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Admin panel",
                    callback_data="admin",
                )
            ],
        ]),
    )


# ============================================================
# STATUS MANAGEMENT
# ============================================================

async def status_panel(
    query,
):

    await safe_edit(
        query,
        "🎟 <b>STATUS BOSHQARUVI</b>\n\n"
        "Foydalanuvchiga VIP yoki PREMIUM berishingiz "
        "yoki USER holatiga qaytarishingiz mumkin.",
        status_admin_keyboard(),
    )


async def status_give_start(
    update,
    context,
):

    query = update.callback_query

    if not is_admin(
        update.effective_user.id
    ):

        await query.answer(
            "⛔ Ruxsat yo‘q.",
            show_alert=True,
        )

        return ConversationHandler.END

    await query.answer()

    context.user_data[
        "status_to_give"
    ] = None

    if query.data == "status_give_vip":

        context.user_data[
            "status_to_give"
        ] = "VIP"

        status_name = "💎 VIP"

    else:

        context.user_data[
            "status_to_give"
        ] = "PREMIUM"

        status_name = "👑 PREMIUM"

    await query.message.reply_text(
        f"🎟 <b>{status_name} BERISH</b>\n\n"
        "Foydalanuvchi Telegram ID sini yuboring:\n\n"
        "<code>123456789</code>",
        parse_mode=ParseMode.HTML,
    )

    return STATUS_GIVE_STATE


async def status_give_process(
    update,
    context,
):

    try:

        user_id = int(
            update.message.text.strip()
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID raqam bo‘lishi kerak."
        )

        return STATUS_GIVE_STATE

    status = context.user_data.get(
        "status_to_give"
    )

    if status not in {
        "VIP",
        "PREMIUM",
    }:

        context.user_data.clear()

        return ConversationHandler.END

    set_user_status(
        user_id,
        status,
    )

    context.user_data.clear()

    await update.message.reply_text(
        "✅ <b>STATUS YANGILANDI</b>\n\n"
        f"🆔 User: <code>{user_id}</code>\n"
        f"🎟 Status: <b>{escape(status_label(status))}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    return ConversationHandler.END


async def status_remove_start(
    update,
    context,
):

    query = update.callback_query

    if not is_admin(
        update.effective_user.id
    ):

        await query.answer(
            "⛔ Ruxsat yo‘q.",
            show_alert=True,
        )

        return ConversationHandler.END

    await query.answer()

    await query.message.reply_text(
        "➖ <b>STATUSNI USER QILISH</b>\n\n"
        "Foydalanuvchi ID sini yuboring:",
        parse_mode=ParseMode.HTML,
    )

    return STATUS_REMOVE_STATE


async def status_remove_process(
    update,
    context,
):

    try:

        user_id = int(
            update.message.text.strip()
        )

    except ValueError:

        await update.message.reply_text(
            "❌ User ID faqat raqamlardan iborat."
        )

        return STATUS_REMOVE_STATE

    set_user_status(
        user_id,
        "USER",
    )

    await update.message.reply_text(
        "✅ Status oddiy USER holatiga qaytarildi.",
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    return ConversationHandler.END


# ============================================================
# BROADCAST
# ============================================================

async def broadcast_start(
    update,
    context,
):

    query = update.callback_query

    if not is_admin(
        update.effective_user.id
    ):

        await query.answer(
            "⛔ Ruxsat yo‘q.",
            show_alert=True,
        )

        return ConversationHandler.END

    await query.answer()

    await query.message.reply_text(
        "📣 <b>REKLAMA YUBORISH</b>\n\n"
        "Barcha foydalanuvchilarga yuboriladigan "
        "matnni yozing.\n\n"
        "⚠️ Faqat matnli reklama.",
        parse_mode=ParseMode.HTML,
    )

    return BROADCAST_STATE


async def perform_broadcast(
    application,
    admin_chat_id,
    text,
):

    user_ids = get_all_user_ids()

    success = 0
    failed = 0
    blocked = 0

    for user_id in user_ids:

        try:

            await application.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )

            success += 1

            await asyncio.sleep(
                0.05
            )

        except RetryAfter as e:

            await asyncio.sleep(
                min(
                    e.retry_after,
                    30,
                )
            )

            try:

                await application.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )

                success += 1

            except Forbidden:

                blocked += 1

            except Exception:

                failed += 1

        except Forbidden:

            blocked += 1

        except Exception:

            failed += 1

    try:

        await application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "📣 <b>REKLAMA YAKUNLANDI</b>\n\n"
                f"✅ Yetkazildi: <b>{success}</b>\n"
                f"🚫 Bloklaganlar: <b>{blocked}</b>\n"
                f"❌ Xatolar: <b>{failed}</b>\n"
                f"👥 Jami: <b>{len(user_ids)}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

    except Exception:

        logger.exception(
            "Broadcast result send failed"
        )


async def broadcast_process(
    update,
    context,
):

    text = (
        update.message.text.strip()
    )

    if not text:

        await update.message.reply_text(
            "❌ Reklama bo‘sh bo‘lishi mumkin emas."
        )

        return BROADCAST_STATE

    admin_id = (
        update.effective_chat.id
    )

    await update.message.reply_text(
        "🚀 <b>Reklama yuborish boshlandi!</b>\n\n"
        "Bot fon rejimida foydalanuvchilarga yuboradi.\n"
        "Jarayon tugagach sizga natija yuboriladi.",
        parse_mode=ParseMode.HTML,
    )

    context.application.create_task(
        perform_broadcast(
            context.application,
            admin_id,
            text,
        )
    )

    return ConversationHandler.END


# ============================================================
# CHANNEL ADMIN
# ============================================================

async def channels_page(
    query,
):

    channels = get_all_required_channels()

    text = (
        "📢 <b>MAJBURIY KANALLAR / GURUHLAR</b>\n\n"
        f"📊 Jami: <b>{len(channels)}</b>\n\n"
    )

    if not channels:

        text += (
            "📭 Hozircha hech qanday kanal/guruh "
            "qo‘shilmagan.\n\n"
            "➕ Istalgancha kanal yoki guruh qo‘shishingiz mumkin."
        )

    else:

        for index, channel in enumerate(
            channels,
            1,
        ):

            text += (
                f"{index}. 📢 <b>{escape(channel['title'])}</b>\n"
                f"🆔 <code>{channel['chat_id']}</code>\n"
                f"🔗 {escape(channel['invite_link'])}\n\n"
            )

    await safe_edit(
        query,
        text[:3900],
        channel_admin_keyboard(),
    )


async def channels_list_page(
    query,
):

    channels = get_all_required_channels()

    text = (
        "📋 <b>KANALLAR RO‘YXATI</b>\n\n"
    )

    if not channels:

        text += "📭 Ro‘yxat bo‘sh."

    else:

        for index, channel in enumerate(
            channels,
            1,
        ):

            text += (
                f"<b>{index}. {escape(channel['title'])}</b>\n"
                f"🆔 <code>{channel['chat_id']}</code>\n"
                f"🔗 {escape(channel['invite_link'])}\n\n"
            )

    await safe_edit(
        query,
        text[:3900],
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🔄 Yangilash",
                    callback_data="channels_list",
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Orqaga",
                    callback_data="admin_channels",
                )
            ],
        ]),
    )


async def channels_remove_page(
    query,
):

    channels = get_all_required_channels()

    if not channels:

        await safe_edit(
            query,
            "➖ <b>KANAL O‘CHIRISH</b>\n\n"
            "📭 O‘chirish uchun kanal mavjud emas.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Orqaga",
                        callback_data="admin_channels",
                    )
                ]
            ]),
        )

        return

    rows = []

    for channel in channels:

        rows.append([
            InlineKeyboardButton(
                f"➖ {short_text(channel['title'], 35)}",
                callback_data=(
                    f"channel_delete:{channel['id']}"
                ),
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data="admin_channels",
        )
    ])

    await safe_edit(
        query,
        "➖ <b>KANAL/GURUH O‘CHIRISH</b>\n\n"
        "O‘chirmoqchi bo‘lgan kanalni tanlang:",
        InlineKeyboardMarkup(rows),
    )


async def channel_delete_confirm(
    query,
    channel_id,
):

    channel = get_required_channel(
        channel_id
    )

    if not channel:

        await query.answer(
            "Kanal topilmadi.",
            show_alert=True,
        )

        return

    await safe_edit(
        query,
        "⚠️ <b>TASDIQLASH</b>\n\n"
        f"📢 {escape(channel['title'])}\n"
        f"🆔 <code>{channel['chat_id']}</code>\n\n"
        "Ushbu majburiy kanalni o‘chirasizmi?",
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🗑 HA, O‘CHIRISH",
                    callback_data=(
                        f"confirm_channel_delete:{channel_id}"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "↩️ Bekor qilish",
                    callback_data="channels_remove",
                )
            ],
        ]),
    )


async def channels_check_page(
    query,
    context,
):

    channels = get_all_required_channels()

    if not channels:

        await safe_edit(
            query,
            "🩺 <b>KANAL TEKSHIRUVI</b>\n\n"
            "📭 Hech qanday kanal qo‘shilmagan.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Orqaga",
                        callback_data="admin_channels",
                    )
                ]
            ]),
        )

        return

    async def check_channel(channel):

        try:

            chat, bot_member = await asyncio.gather(

                context.bot.get_chat(
                    channel["chat_id"]
                ),

                context.bot.get_chat_member(
                    channel["chat_id"],
                    context.bot.id,
                ),
            )

            status = str(
                bot_member.status
            ).lower()

            if "." in status:

                status = status.rsplit(
                    ".",
                    1,
                )[-1]

            if status in {
                "administrator",
                "creator",
            }:

                status_text = "🟢 Bot admin"

            else:

                status_text = (
                    f"🟡 Bot status: {status}"
                )

            return (
                f"📢 <b>{escape(chat.title)}</b>\n"
                f"🆔 <code>{chat.id}</code>\n"
                f"{status_text}\n"
            )

        except Exception as e:

            logger.warning(
                "Channel check failed: %s",
                e,
            )

            return (
                f"🔴 <b>{escape(channel['title'])}</b>\n"
                "❌ Tekshirishda xatolik\n"
            )

    lines = [
        "🩺 <b>MAJBURIY KANALLAR TEKSHIRUVI</b>\n"
    ]

    results = await asyncio.gather(
        *[
            check_channel(channel)
            for channel in channels
        ]
    )

    lines.extend(results)

    await safe_edit(
        query,
        "\n".join(lines)[:3900],
        InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🔄 Qayta tekshirish",
                    callback_data="channels_check",
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Orqaga",
                    callback_data="admin_channels",
                )
            ],
        ]),
    )


# ============================================================
# ADD REQUIRED CHANNEL
# ============================================================

async def channel_add_start(
    update,
    context,
):

    query = update.callback_query

    if not is_admin(
        update.effective_user.id
    ):

        await query.answer(
            "⛔ Ruxsat yo‘q.",
            show_alert=True,
        )

        return ConversationHandler.END

    await query.answer()

    context.user_data.clear()

    await query.message.reply_text(
        "➕ <b>MAJBURIY KANAL/GURUH QO‘SHISH — 1/2</b>\n\n"
        "📢 Kanal yoki guruh ID sini yuboring.\n\n"
        "Masalan:\n"
        "<code>-1001234567890</code>\n\n"
        "Yoki public kanal bo‘lsa:\n"
        "<code>@channel_username</code>",
        parse_mode=ParseMode.HTML,
    )

    return CHANNEL_ADD_ID_STATE


async def channel_add_id(
    update,
    context,
):

    value = update.message.text.strip()

    chat_id = None

    if re.fullmatch(
        r"-?\d+",
        value,
    ):

        try:
            chat_id = int(value)
        except ValueError:
            pass

    elif value.startswith("@"):

        chat_id = value

    elif "t.me/" in value:

        username = (
            value.rstrip("/")
            .split("/")[-1]
        )

        if username.startswith("+"):

            await update.message.reply_text(
                "❌ Private invite linkni ID o‘rniga yubormang.\n\n"
                "Avval botni guruh/kanalga qo‘shing "
                "va Telegram chat ID sini yuboring."
            )

            return CHANNEL_ADD_ID_STATE

        chat_id = f"@{username}"

    if chat_id is None:

        await update.message.reply_text(
            "❌ Chat ID noto‘g‘ri.\n\n"
            "Masalan:\n"
            "<code>-1001234567890</code>\n"
            "yoki <code>@channelname</code>",
            parse_mode=ParseMode.HTML,
        )

        return CHANNEL_ADD_ID_STATE

    try:

        chat = await context.bot.get_chat(
            chat_id
        )

    except TelegramError as e:

        await update.message.reply_text(
            "❌ Telegram bu kanal/guruhni topa olmadi.\n\n"
            "Tekshiring:\n"
            "• ID to‘g‘ri ekanini\n"
            "• bot kanal/guruhga qo‘shilganini\n"
            "• botga yetarli huquq berilganini\n\n"
            f"Texnik ma’lumot: {escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )

        return CHANNEL_ADD_ID_STATE

    context.user_data[
        "channel_chat_id"
    ] = chat.id

    context.user_data[
        "channel_title"
    ] = (
        chat.title
        or chat.username
        or str(chat.id)
    )

    await update.message.reply_text(
        "🔗 <b>MAJBURIY KANAL/GURUH QO‘SHISH — 2/2</b>\n\n"
        f"📢 Topildi: <b>{escape(context.user_data['channel_title'])}</b>\n"
        f"🆔 <code>{chat.id}</code>\n\n"
        "Foydalanuvchi bosadigan invite/public linkni yuboring.\n\n"
        "Public kanal:\n"
        "<code>https://t.me/channelname</code>\n\n"
        "Private kanal/guruh:\n"
        "<code>https://t.me/+xxxxxxxx</code>",
        parse_mode=ParseMode.HTML,
    )

    return CHANNEL_ADD_LINK_STATE


async def channel_add_link(
    update,
    context,
):

    raw_link = (
        update.message.text.strip()
    )

    link = normalize_url(
        raw_link
    )

    if not link:

        await update.message.reply_text(
            "❌ Link noto‘g‘ri.\n\n"
            "To‘g‘ri misollar:\n"
            "https://t.me/channelname\n"
            "https://t.me/+xxxxxxxx"
        )

        return CHANNEL_ADD_LINK_STATE

    chat_id = context.user_data.get(
        "channel_chat_id"
    )

    title = context.user_data.get(
        "channel_title",
        "Telegram channel",
    )

    if chat_id is None:

        await update.message.reply_text(
            "❌ Kanal ma’lumotlari topilmadi. "
            "Qaytadan urinib ko‘ring."
        )

        context.user_data.clear()

        return ConversationHandler.END

    success, error = add_required_channel(
        chat_id,
        title,
        link,
    )

    context.user_data.clear()

    if not success:

        if error == "exists":

            await update.message.reply_text(
                "⚠️ Bu kanal/guruh allaqachon "
                "majburiy ro‘yxatda."
            )

        else:

            await update.message.reply_text(
                "❌ Kanalni saqlashda xatolik."
            )

        return ConversationHandler.END

    clear_subscription_cache()

    await update.message.reply_text(
        "🎉 <b>MAJBURIY KANAL QO‘SHILDI!</b>\n\n"
        f"📢 <b>{escape(title)}</b>\n"
        f"🆔 <code>{chat_id}</code>\n"
        f"🔗 {escape(link)}\n\n"
        "🛡 Bot foydalanuvchilarning ushbu "
        "kanal/guruhga a’zoligini tekshiradi.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    return ConversationHandler.END


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context,
):

    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    user = update.effective_user

    # --------------------------------------------------------
    # ADMIN-ONLY CALLBACK CHECK
    # --------------------------------------------------------

    admin_callbacks = {
        "admin",
        "admin_movies",
        "admin_stats",
        "admin_users",
        "admin_delete",
        "admin_channels",
        "admin_status",
        "channels_list",
        "channels_remove",
        "channels_check",
    }

    if (
        data in admin_callbacks
        or data.startswith("delete_movie:")
        or data.startswith("confirm_delete:")
        or data.startswith("channel_delete:")
        or data.startswith("confirm_channel_delete:")
    ):

        if not is_admin(user.id):

            await query.answer(
                "⛔ Ruxsat yo‘q.",
                show_alert=True,
            )

            return

    try:
        await query.answer()
    except Exception:
        pass

    register_user(user)

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":

        await show_home(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # SUBSCRIPTION
    # --------------------------------------------------------

    if data == "check_sub":

        ok, missing, errors = (
            await check_subscription(
                context.bot,
                user.id,
                force=True,
            )
        )

        if ok:

            await safe_edit(
                query,
                "🎉 <b>OBUNA TASDIQLANDI!</b>\n\n"
                "✅ Barcha kerakli kanal/guruhlarga "
                "a’zo bo‘lgansiz.\n\n"
                "🚀 Endi WITT KINO'dan foydalanishingiz mumkin.",
            )

            clear_subscription_cache(
                user.id
            )

            await query.message.reply_text(
                "👇 Kino kodini yuboring:",
                reply_markup=main_keyboard(
                    user.id
                ),
            )

        else:

            if errors:

                text = (
                    "⚠️ <b>TEKSHIRISHDA MUAMMO</b>\n\n"
                    "Ba’zi kanal/guruhlarni tekshirib bo‘lmadi.\n"
                    "Admin sozlamalarni tekshirishi kerak."
                )

            else:

                text = (
                    "🔐 <b>HALI OBUNA BO‘LMAGANSIZ</b>\n\n"
                    "Quyidagi kanal/guruhlarga a’zo bo‘ling "
                    "va qayta tekshiring."
                )

            await safe_edit(
                query,
                text,
                subscription_keyboard(
                    missing
                ),
            )

        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if data == "admin":

        await show_admin_panel(
            query
        )

        return

    if data == "admin_movies":

        await admin_movies_page(
            query
        )

        return

    if data == "admin_stats":

        await admin_stats_page(
            query
        )

        return

    if data == "admin_users":

        await admin_users_page(
            query
        )

        return

    if data == "admin_status":

        await status_panel(
            query
        )

        return

    if data == "admin_delete":

        await admin_delete_page(
            query
        )

        return

    # --------------------------------------------------------
    # DELETE MOVIE
    # --------------------------------------------------------

    if data.startswith(
        "delete_movie:"
    ):

        movie_id = int(
            data.split(":")[1]
        )

        await delete_movie_confirm(
            query,
            movie_id,
        )

        return

    if data.startswith(
        "confirm_delete:"
    ):

        movie_id = int(
            data.split(":")[1]
        )

        movie = get_movie_by_id(
            movie_id
        )

        if movie:

            name = movie["name"]

            delete_movie(
                movie_id
            )

            await safe_edit(
                query,
                "🗑 <b>KINO O‘CHIRILDI</b>\n\n"
                f"🎬 {escape(name)}\n\n"
                "✅ Ma’lumotlar bazasidan olib tashlandi.",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Admin panel",
                            callback_data="admin",
                        )
                    ]
                ]),
            )

        return

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    # These are handled by ConversationHandler.
    if data in {
        "status_give_vip",
        "status_give_premium",
        "status_remove",
    }:
        return

    # --------------------------------------------------------
    # CHANNEL ADMIN
    # --------------------------------------------------------

    if data == "admin_channels":

        await channels_page(
            query
        )

        return

    if data == "channels_list":

        await channels_list_page(
            query
        )

        return

    if data == "channels_remove":

        await channels_remove_page(
            query
        )

        return

    if data.startswith(
        "channel_delete:"
    ):

        channel_id = int(
            data.split(":")[1]
        )

        await channel_delete_confirm(
            query,
            channel_id,
        )

        return

    if data.startswith(
        "confirm_channel_delete:"
    ):

        channel_id = int(
            data.split(":")[1]
        )

        channel = get_required_channel(
            channel_id
        )

        if channel:

            title = channel["title"]

            delete_required_channel(
                channel_id
            )

            clear_subscription_cache()

            await safe_edit(
                query,
                "🗑 <b>MAJBURIY KANAL O‘CHIRILDI</b>\n\n"
                f"📢 {escape(title)}\n\n"
                "✅ Endi foydalanuvchilardan bu kanalga "
                "obuna bo‘lish talab qilinmaydi.",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Kanallar",
                            callback_data="admin_channels",
                        )
                    ]
                ]),
            )

        return

    if data == "channels_check":

        await channels_check_page(
            query,
            context,
        )

        return

    # --------------------------------------------------------
    # MOVIE
    # --------------------------------------------------------

    if data.startswith(
        "movie:"
    ):

        movie_id = int(
            data.split(":")[1]
        )

        if not await require_subscription(
            update,
            context,
        ):
            return

        movie = get_movie_by_id(
            movie_id
        )

        if not movie:

            await query.answer(
                "Film topilmadi.",
                show_alert=True,
            )

            return

        if not can_access_movie(
            user.id,
            movie,
        ):

            await query.message.reply_text(
                "🔒 Bu film uchun sizda "
                "yetarli status yo‘q.\n\n"
                f"Kerakli status: "
                f"<b>{escape(access_label(movie['access']))}</b>",
                parse_mode=ParseMode.HTML,
            )

            return

        try:

            await deliver_movie(
                context.bot,
                query.message.chat_id,
                movie,
            )

        except TelegramError:

            logger.exception(
                "Movie callback delivery error"
            )

            await query.message.reply_text(
                "⚠️ Filmni yuborishda xatolik yuz berdi."
            )

        return

    # --------------------------------------------------------
    # RATING
    # --------------------------------------------------------

    if data.startswith(
        "rate:"
    ):

        try:

            _, movie_id, rating = (
                data.split(":")
            )

            movie_id = int(
                movie_id
            )

            rating = int(
                rating
            )

        except Exception:

            return

        if rating < 1 or rating > 5:
            return

        save_rating(
            user.id,
            movie_id,
            rating,
        )

        await query.answer(
            f"⭐ {rating}/5 baho saqlandi!",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # CATEGORY
    # --------------------------------------------------------

    if data.startswith(
        "cat:"
    ):

        token = data.split(
            ":",
            1,
        )[1]

        categories = get_categories()

        selected = None

        for category in categories:

            if category_token(
                category
            ) == token:

                selected = category

                break

        if not selected:

            await query.answer(
                "Janr topilmadi.",
                show_alert=True,
            )

            return

        movies = movies_by_category(
            selected,
            20,
        )

        await safe_edit(
            query,
            f"🎭 <b>{escape(selected)}</b>\n\n"
            "Kinolar:",
            movie_list_keyboard(
                movies
            ),
        )

        return

    logger.info(
        "Unhandled callback: %s",
        data,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    error = context.error

    if isinstance(
        error,
        RetryAfter,
    ):

        logger.warning(
            "Telegram rate limit: %ss",
            error.retry_after,
        )

        return

    logger.error(
        "Unhandled exception: %r",
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
    )

    try:

        if isinstance(
            update,
            Update,
        ):

            message = (
                update.effective_message
            )

            if message:

                await message.reply_text(
                    "⚠️ Kutilmagan texnik xatolik yuz berdi.\n"
                    "Iltimos, birozdan keyin qayta urinib ko‘ring."
                )

    except Exception:

        pass


# ============================================================
# CANCEL
# ============================================================

async def cancel_command(
    update,
    context,
):

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Amal bekor qilindi.",
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    return ConversationHandler.END


# ============================================================
# HEALTH SERVER
# ============================================================

async def health_handler(
    request,
):

    return web.json_response({
        "status": "ok",
        "service": "WITT KinoBot",
        "timestamp": now_iso(),
    })


async def run_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "Health server started on port %s",
        PORT,
    )

    await asyncio.Event().wait()


def start_health_server():

    thread = threading.Thread(
        target=lambda: asyncio.run(
            run_health_server()
        ),
        daemon=True,
    )

    thread.start()


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    if not ADMIN_IDS:

        logger.warning(
            "ADMIN_IDS is empty. "
            "No administrator account configured."
        )

    init_db()

    import_legacy_channels()

    start_health_server()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(
        ChatJoinRequestHandler(
            handle_join_request
        )
    )

    # ========================================================
    # ADD MOVIE
    # ========================================================

    add_movie_conversation = ConversationHandler(

        entry_points=[

            CallbackQueryHandler(
                add_movie_start,
                pattern=r"^admin_add$",
            )
        ],

        states={

            MOVIE_CODE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_code,
                )
            ],

            MOVIE_NAME: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_name,
                )
            ],

            MOVIE_DESCRIPTION: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_description,
                )
            ],

            MOVIE_CATEGORY: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_category,
                )
            ],

            MOVIE_FILE: [

                MessageHandler(
                    filters.VIDEO
                    | filters.Document.ALL,
                    add_movie_file,
                )
            ],

            MOVIE_ACCESS_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_access,
                )
            ],
        },

        fallbacks=[

            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    # ========================================================
    # EDIT
    # ========================================================

    edit_movie_conversation = ConversationHandler(

        entry_points=[

            CallbackQueryHandler(
                edit_movie_start,
                pattern=r"^admin_edit$",
            )
        ],

        states={

            EDIT_FIELD_STATE: [

                CallbackQueryHandler(
                    edit_movie_select,
                    pattern=r"^editmovie:\d+$",
                ),

                CallbackQueryHandler(
                    edit_field_selected,
                    pattern=(
                        r"^editfield:"
                        r"\d+:"
                        r"(code|name|description|category|access)$"
                    ),
                ),
            ],

            EDIT_VALUE_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    edit_movie_value,
                )
            ],
        },

        fallbacks=[

            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    # ========================================================
    # STATUS GIVE
    # ========================================================

    status_give_conversation = ConversationHandler(

        entry_points=[

            CallbackQueryHandler(
                status_give_start,
                pattern=r"^status_give_(vip|premium)$",
            )
        ],

        states={

            STATUS_GIVE_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    status_give_process,
                )
            ]
        },

        fallbacks=[

            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    # ========================================================
    # STATUS REMOVE
    # ========================================================

    status_remove_conversation = ConversationHandler(

        entry_points=[

            CallbackQueryHandler(
                status_remove_start,
                pattern=r"^status_remove$",
            )
        ],

        states={

            STATUS_REMOVE_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    status_remove_process,
                )
            ]
        },

        fallbacks=[

            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    # ========================================================
    # BROADCAST
    # ========================================================

    broadcast_conversation = ConversationHandler(

        entry_points=[

            CallbackQueryHandler(
                broadcast_start,
                pattern=r"^admin_broadcast$",
            )
        ],

        states={

            BROADCAST_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    broadcast_process,
                )
            ]
        },

        fallbacks=[

            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    # ========================================================
    # CHANNEL ADD
    # ========================================================

    channel_add_conversation = ConversationHandler(

        entry_points=[

            CallbackQueryHandler(
                channel_add_start,
                pattern=r"^channels_add$",
            )
        ],

        states={

            CHANNEL_ADD_ID_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    channel_add_id,
                )
            ],

            CHANNEL_ADD_LINK_STATE: [

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    channel_add_link,
                )
            ],
        },

        fallbacks=[

            CommandHandler(
                "cancel",
                cancel_command,
            )
        ],

        allow_reentry=True,
    )

    # ========================================================
    # CONVERSATIONS FIRST
    # ========================================================

    application.add_handler(
        add_movie_conversation
    )

    application.add_handler(
        edit_movie_conversation
    )

    application.add_handler(
        status_give_conversation
    )

    application.add_handler(
        status_remove_conversation
    )

    application.add_handler(
        broadcast_conversation
    )

    application.add_handler(
        channel_add_conversation
    )

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    # ========================================================
    # CALLBACK ROUTER
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # ========================================================
    # TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    # ========================================================
    # ERRORS
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    # ========================================================
    # LOGS
    # ========================================================

    logger.info(
        "=============================================="
    )

    logger.info(
        "WITT KinoBot is starting..."
    )

    logger.info(
        "Admins: %s",
        sorted(ADMIN_IDS),
    )

    logger.info(
        "Port: %s",
        PORT,
    )

    logger.info(
        "Required channels: %s",
        len(get_required_channels()),
    )

    logger.info(
        "Database: %s",
        DATABASE,
    )

    logger.info(
        "=============================================="
    )

    # ========================================================
    # RUN
    # ========================================================

    application.run_polling(
        drop_pending_updates=True,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()