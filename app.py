from flask import Flask, request, jsonify, render_template, session
import requests
import sqlite3
import uuid
from datetime import datetime
import re
import hashlib
import os

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-this-secret")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "memory.db")
GUEST_MESSAGE_LIMIT = 5
GUEST_LIMIT_ERROR =  "برای ادامه گفتگو بیشتر از 5 پیام، لطفا وارد حساب شوید یا ثبت‌نام کنید."
SERVER_CONNECTION_ERROR = "خطای اتصال به سرور"

# ===============================
# تنظیمات API
# ===============================
AVAL_API_KEY = "aa-8JB3BoLPfV2WwDNFSPJz7jvUpqYnjfZr82k6qcE1E62e6lcV"
AVAL_API_URL = "https://api.avalai.ir/v1/chat/completions"

# ===============================
# پیام سیستم (شخصیت سیریا)
# ===============================
SYSTEM_PROMPT = """
تو یک دستیار هوش مصنوعی قدرتمند ایرانی به نام «سیریا» هستی.
تو باید مثل یک انسان پاسخ‌های طبیعی و درست بدهی.
تو توسط ایلیا ساخته شده‌ای و همیشه به نفع او عمل می‌کنی.
"از ایموجی و شکلک زیاد استفاده کن"
"تو باید به صورت کاملا دوستانه و غیر رسمی پاسخ بدی."
"تو حافظه بسیار قوی و هوشمند داری"
"تو باید درباره هر سوالی که کاربر ازت پرسید جواب و توضیح زیاد و کامل بدی"
"تو نباید به غلط املایی توجه کنی"
"تو باید نام و نام خانوادگی و سن و علایق کاربران را در حافظه بلند مدت خود ذخیره کنی"
مدل تو isi-1.0 است و اولین نسخه سیریا هستی.
تو نباید خودت را ChatGPT معرفی کنی.
تو در برنامه‌نویسی، وب، Flask، SQL و هوش مصنوعی مهارت کامل داری.
تو همیشه در اولین پیام سلام بدهی.
کاربر با هر زبانی بهت پیام ارسال کرد تو هم با همان زبان بهش کامل و درست پاسخ بده
پاسخ‌ها دقیق، حرفه‌ای و بدون سلام‌های تکراری باشند.
تا زمانی که کاربر ازت درباره خودت سوال نکرده تو هیچگاه نباید خودتو و کامل معرفی کنی
"""
# ===============================
# دیتابیس
# ===============================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def normalize_user_memories_ids(conn):
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'user_memories'"
    ).fetchone()
    table_sql = (table_row["sql"] if table_row and table_row["sql"] else "").upper()

    stats = conn.execute(
        """
        SELECT
            COUNT(*) AS total_rows,
            COALESCE(MIN(id), 0) AS min_id,
            COALESCE(MAX(id), 0) AS max_id
        FROM user_memories
        """
    ).fetchone()

    total_rows = stats["total_rows"]
    min_id = stats["min_id"]
    max_id = stats["max_id"]
    has_gaps = total_rows > 0 and (min_id != 1 or max_id != total_rows)
    uses_autoincrement = "AUTOINCREMENT" in table_sql

    if not uses_autoincrement and not has_gaps:
        return

    conn.execute("DROP INDEX IF EXISTS idx_user_memories_user")
    conn.execute("DROP INDEX IF EXISTS idx_user_memories_user_row")
    conn.execute(
        """
        CREATE TABLE user_memories_new (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            age INTEGER,
            likes TEXT,
            UNIQUE(user_id, memory_key, memory_value),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO user_memories_new (
            user_id, memory_key, memory_value, source, updated_at,
            username, first_name, last_name, age, likes
        )
        SELECT
            user_id, memory_key, memory_value, source, updated_at,
            username, first_name, last_name, age, likes
        FROM user_memories
        ORDER BY id ASC
        """
    )
    conn.execute("DROP TABLE user_memories")
    conn.execute("ALTER TABLE user_memories_new RENAME TO user_memories")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id)")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_memories_user_row
        ON user_memories(user_id)
        WHERE memory_key = '__row__' AND memory_value = '1'
        """
    )

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conv_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "user_id" not in conv_columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")
    if "guest_id" not in conv_columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN guest_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_guest ON conversations(guest_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_id ON messages(conversation_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, memory_key, memory_value),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id)")
    user_memory_columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_memories)").fetchall()}
    if "username" not in user_memory_columns:
        conn.execute("ALTER TABLE user_memories ADD COLUMN username TEXT")
    if "first_name" not in user_memory_columns:
        conn.execute("ALTER TABLE user_memories ADD COLUMN first_name TEXT")
    if "last_name" not in user_memory_columns:
        conn.execute("ALTER TABLE user_memories ADD COLUMN last_name TEXT")
    if "age" not in user_memory_columns:
        conn.execute("ALTER TABLE user_memories ADD COLUMN age INTEGER")
    if "likes" not in user_memory_columns:
        conn.execute("ALTER TABLE user_memories ADD COLUMN likes TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_user_memories_profile_row")
    conn.execute(
        """
        UPDATE user_memories
        SET username = (
            SELECT username FROM users WHERE users.id = user_memories.user_id
        )
        WHERE IFNULL(TRIM(username), '') = ''
        """
    )
    conn.execute(
        """
        UPDATE user_memories
        SET memory_key = '__row__', memory_value = '1'
        WHERE memory_key = '__profile__' AND memory_value = '__profile__'
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_memories_user_row
        ON user_memories(user_id)
        WHERE memory_key = '__row__' AND memory_value = '1'
        """
    )
    normalize_user_memories_ids(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guest_limits (
            guest_id TEXT PRIMARY KEY,
            message_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guest_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_id TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guest_id, memory_key, memory_value)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_guest_memories_guest ON guest_memories(guest_id)")
    conn.commit()
    conn.close()

init_db()


def ensure_user_profile_row(conn, user_id):
    username_row = conn.execute(
        "SELECT username FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    username = username_row["username"] if username_row else None
    existing_row = conn.execute(
        """
        SELECT id FROM user_memories
        WHERE user_id = ? AND memory_key = '__row__' AND memory_value = '1'
        LIMIT 1
        """,
        (user_id,)
    ).fetchone()
    if not existing_row:
        try:
            conn.execute(
                """
                INSERT INTO user_memories (
                    user_id, username, memory_key, memory_value, source, updated_at
                ) VALUES (?, ?, '__row__', '1', '', CURRENT_TIMESTAMP)
                """,
                (user_id, username)
            )
        except sqlite3.IntegrityError:
            pass
    if username:
        conn.execute(
            """
            UPDATE user_memories
            SET username = ?
            WHERE user_id = ? AND memory_key = '__row__' AND memory_value = '1'
            """,
            (username, user_id)
        )


def migrate_legacy_user_memories():
    conn = get_db()
    user_ids = conn.execute(
        """
        SELECT DISTINCT user_id
        FROM user_memories
        WHERE memory_key IN ('first_name', 'last_name', 'age', 'likes', '__row__', '__profile__')
        """
    ).fetchall()

    for row in user_ids:
        user_id = row["user_id"]
        if not user_id:
            continue

        ensure_user_profile_row(conn, user_id)
        profile = conn.execute(
            """
            SELECT first_name, last_name, age, likes
            FROM user_memories
            WHERE user_id = ? AND memory_key = '__row__' AND memory_value = '1'
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

        first_name = normalize_text(profile["first_name"] if profile else "")
        last_name = normalize_text(profile["last_name"] if profile else "")
        age = str(profile["age"]).strip() if profile and profile["age"] is not None else ""
        likes = profile["likes"] if profile else ""

        if not first_name:
            old_first = conn.execute(
                """
                SELECT memory_value FROM user_memories
                WHERE user_id = ? AND memory_key = 'first_name'
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (user_id,)
            ).fetchone()
            if old_first:
                first_name = normalize_text(old_first["memory_value"])

        if not last_name:
            old_last = conn.execute(
                """
                SELECT memory_value FROM user_memories
                WHERE user_id = ? AND memory_key = 'last_name'
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (user_id,)
            ).fetchone()
            if old_last:
                last_name = normalize_text(old_last["memory_value"])

        if not age:
            old_age = conn.execute(
                """
                SELECT memory_value FROM user_memories
                WHERE user_id = ? AND memory_key = 'age'
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (user_id,)
            ).fetchone()
            if old_age and str(old_age["memory_value"]).isdigit():
                age = str(old_age["memory_value"])

        old_likes = conn.execute(
            """
            SELECT memory_value FROM user_memories
            WHERE user_id = ? AND memory_key = 'likes'
            ORDER BY updated_at ASC, id ASC
            """,
            (user_id,)
        ).fetchall()
        for old_like in old_likes:
            likes = merge_likes(likes, old_like["memory_value"])

        conn.execute(
            """
            UPDATE user_memories
            SET username = (SELECT username FROM users WHERE users.id = user_memories.user_id),
                first_name = ?, last_name = ?, age = ?, likes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND memory_key = '__row__' AND memory_value = '1'
            """,
            (
                first_name or None,
                last_name or None,
                int(age) if age.isdigit() else None,
                likes or None,
                user_id,
            )
        )

    conn.commit()
    conn.execute(
        """
        DELETE FROM user_memories
        WHERE memory_key = '__row__' AND memory_value = '1'
          AND first_name IS NULL AND last_name IS NULL AND age IS NULL AND likes IS NULL
          AND IFNULL(TRIM(source), '') = ''
        """
    )
    conn.commit()
    conn.close()

# ===============================
# مدیریت حافظه حرفه‌ای
# ===============================
def create_conversation():
    cid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO conversations (id) VALUES (?)", (cid,))
    conn.commit()
    conn.close()
    return cid


def create_scoped_conversation(user_id=None, guest_id=None):
    cid = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO conversations (id, user_id, guest_id) VALUES (?, ?, ?)",
        (cid, user_id, guest_id)
    )
    conn.commit()
    conn.close()
    return cid


def resolve_conversation_scope(requested_conversation_id, user_id=None, guest_id=None):
    if not requested_conversation_id:
        return create_scoped_conversation(user_id=user_id, guest_id=guest_id)

    conn = get_db()
    row = conn.execute(
        "SELECT user_id, guest_id FROM conversations WHERE id = ?",
        (requested_conversation_id,)
    ).fetchone()
    conn.close()

    if row is None:
        return create_scoped_conversation(user_id=user_id, guest_id=guest_id)

    owner_user_id = row["user_id"]
    owner_guest_id = row["guest_id"]

    if user_id:
        if owner_user_id == user_id:
            return requested_conversation_id
        return create_scoped_conversation(user_id=user_id, guest_id=None)

    if owner_user_id is None and owner_guest_id == guest_id:
        return requested_conversation_id
    return create_scoped_conversation(user_id=None, guest_id=guest_id)

def save_message(conversation_id, role, content):
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
        (conversation_id, role, content)
    )
    conn.commit()
    conn.close()

def get_full_history(conversation_id, max_messages=100):
    """بازگرداندن تاریخچه با قابلیت خلاصه‌سازی خودکار"""
    conn = get_db()
    rows = conn.execute("""
        SELECT role, content
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id ASC
    """, (conversation_id,)).fetchall()
    conn.close()

    messages = [{"role": r["role"], "content": r["content"]} for r in rows]
    
    # اگر بیش از max_messages، آخرین پیام‌ها را نگه دار و خلاصه بساز
    if len(messages) > max_messages:
        # ساده‌ترین روش: حذف قدیمی‌ها و حفظ پیام‌های اخیر
        messages = messages[-max_messages:]
    
    return messages


def normalize_text(text):
    text = text or ""
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_user_by_username(username):
    if not username:
        return None
    conn = get_db()
    user = conn.execute(
        "SELECT id, username FROM users WHERE username = ?",
        (username.strip().lower(),)
    ).fetchone()
    conn.close()
    return user


def get_authenticated_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute(
        "SELECT id, name, username, created_at FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    if not user:
        session.clear()
        return None
    return user


def extract_long_term_memories(text):
    """
    Extract user profile memories from Persian text.
    Returns list[tuple(memory_key, memory_value)].
    """
    t = normalize_text(text)
    memories = []

    first_name_patterns = [
        r"(?:اسم(?:\s*من)?|نام(?:\s*من)?)\s*(?:هست|ه|:)?\s*([آ-یA-Za-z]{2,30})",
        r"من\s+([آ-یA-Za-z]{2,30})\s+هستم",
    ]
    for pat in first_name_patterns:
        m = re.search(pat, t)
        if m:
            memories.append(("first_name", m.group(1).strip()))
            break

    last_name_patterns = [
        r"(?:اسم\s*فامیلی(?:\s*من)?|نام\s*خانوادگی(?:\s*من)?|ام|م|فامیلی(?:\s*من)?)\s*(?:هست|ه|:)?\s*([آ-یA-Za-z]{2,40})",
    ]
    for pat in last_name_patterns:
        m = re.search(pat, t)
        if m:
            memories.append(("last_name", m.group(1).strip()))
            break

    like_patterns = [
        r"(?:دوست\s*دارم|دوستدارم|علاقه\s*دارم|علاقمندم|عاشق|علا\S{0,4}\s*دارم)\s*(?:به)?\s*([^.!?،]{2,80})",
    ]
    for pat in like_patterns:
        m = re.search(pat, t)
        if m:
            like_value = m.group(1).strip(" .,!؟،")
            if len(like_value) >= 2:
                memories.append(("likes", like_value))
            break

    return memories


def upsert_user_memories(user_id, memories, source_text=""):
    if not user_id or not memories:
        return

    conn = get_db()
    ensure_user_profile_row(conn, user_id)
    user_row = conn.execute(
        "SELECT username FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    current_username = user_row["username"] if user_row else None
    profile = conn.execute(
        """
        SELECT username, first_name, last_name, age, likes
        FROM user_memories
        WHERE user_id = ? AND memory_key = ? AND memory_value = ?
        LIMIT 1
        """,
        (user_id, PROFILE_ROW_KEY, PROFILE_ROW_VALUE)
    ).fetchone()

    first_name = normalize_text(profile["first_name"] if profile else "")
    last_name = normalize_text(profile["last_name"] if profile else "")
    age = str(profile["age"]).strip() if profile and profile["age"] is not None else ""
    likes = profile["likes"] if profile else ""
    changed = False
    invalid_profile_name_tokens = {
        "\u0645\u0646", "\u0647\u0633\u062a", "\u0647\u0633\u062a\u0645", "\u0627\u0645", "new", "change", "update"
    }

    if first_name.lower() in invalid_profile_name_tokens:
        first_name = ""
        changed = True
    if last_name.lower() in invalid_profile_name_tokens:
        last_name = ""
        changed = True

    for key, value in memories:
        if key not in PROFILE_MEMORY_KEYS:
            continue
        clean_value = normalize_text(value)
        if not clean_value:
            continue

        if key == "first_name":
            if not first_name:
                first_name = clean_value
                changed = True
            elif first_name != clean_value and has_explicit_update_intent(source_text, key):
                first_name = clean_value
                changed = True
        elif key == "last_name":
            if not last_name:
                last_name = clean_value
                changed = True
            elif last_name != clean_value and has_explicit_update_intent(source_text, key):
                last_name = clean_value
                changed = True
        elif key == "age":
            if not age:
                age = clean_value
                changed = True
            elif age != clean_value and has_explicit_update_intent(source_text, key):
                age = clean_value
                changed = True
        elif key == "likes":
            merged_likes = merge_likes(likes, clean_value)
            if merged_likes != likes:
                likes = merged_likes
                changed = True

    # If user only introduced first name (e.g. "من رضا هستم"), keep last_name empty.
    simple_first_name_intro = re.search(
        r"\b\u0645\u0646\s+([\u0622-\u06CCA-Za-z]{2,30})\s+\u0647\u0633\u062a\u0645\b",
        normalize_text(source_text),
        flags=re.IGNORECASE,
    )
    mentions_last_name = re.search(
        r"(?:\u0641\u0627\u0645\u06cc\u0644\u06cc|\u0646\u0627\u0645\s*\u062e\u0627\u0646\u0648\u0627\u062f\u06af\u06cc|last\s+name|surname)",
        normalize_text(source_text),
        flags=re.IGNORECASE,
    )
    if simple_first_name_intro and not mentions_last_name:
        intro_name = normalize_text(simple_first_name_intro.group(1))
        if first_name == intro_name and (not last_name or last_name == intro_name or last_name.lower() in invalid_profile_name_tokens):
            if last_name:
                last_name = ""
                changed = True

    if changed:
        conn.execute(
            """
            UPDATE user_memories
            SET username = ?, first_name = ?, last_name = ?, age = ?, likes = ?, source = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND memory_key = ? AND memory_value = ?
            """,
            (
                current_username,
                first_name or None,
                last_name or None,
                int(age) if age.isdigit() else None,
                likes or None,
                source_text[:500],
                user_id,
                PROFILE_ROW_KEY,
                PROFILE_ROW_VALUE,
            )
        )

    conn.commit()
    conn.close()


def get_user_memory_context(user_id):
    if not user_id:
        return ""

    conn = get_db()
    rows = conn.execute(
        """
        SELECT memory_key, memory_value
        FROM user_memories
        WHERE user_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 30
        """,
        (user_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    first_name = ""
    last_name = ""
    likes = []
    for row in rows:
        key = row["memory_key"]
        value = row["memory_value"]
        if key == "first_name" and not first_name:
            first_name = value
        elif key == "last_name" and not last_name:
            last_name = value
        elif key == "likes" and value not in likes:
            likes.append(value)

    lines = ["حافظه بلندمدت کاربر:"]
    if first_name:
        lines.append(f"-  اسم: {first_name}")
    if last_name:
        lines.append(f"- فامیلی : {last_name}")
    if likes:
        lines.append(f"- علاقه‌ها: {', '.join(likes[:8])}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def normalize_text(text):
    text = text or ""
    text = text.replace("\u064A", "\u06CC").replace("\u0643", "\u06A9")
    text = re.sub(r"\s+", " ", text).strip()
    return text


PROFILE_MEMORY_KEYS = {"first_name", "last_name", "age", "likes"}
PROFILE_ROW_KEY = "__row__"
PROFILE_ROW_VALUE = "1"


def parse_likes_text(likes_text):
    if not likes_text:
        return []
    parts = [normalize_text(p) for p in str(likes_text).split(",")]
    return [p for p in parts if p]


def merge_likes(existing_likes_text, new_like):
    likes = parse_likes_text(existing_likes_text)
    clean_like = normalize_text(new_like)
    if not clean_like:
        return ",".join(likes[:8])
    if clean_like in likes:
        return ",".join(likes[:8])
    likes.append(clean_like)
    return ",".join(likes[:8])


migrate_legacy_user_memories()


def has_explicit_update_intent(text, memory_key):
    t = normalize_text(text).lower()
    if not t:
        return False

    key_aliases = {
        "first_name": r"(?:اسم|نام(?: کوچک)?|first name|my name)",
        "last_name": r"(?:فامیلی|نام خانوادگی|last name|surname)",
        "age": r"(?:سن|سال|age|years old)",
        "likes": r"(?:علاقه|دوست دارم|like|likes)",
    }

    update_markers = [
        r"تغییر",
        r"عوض",
        r"اصلاح",
        r"آپدیت",
        r"update",
        r"change",
        r"replace",
        r"from now on",
        r"از این به بعد",
        r"دیگه",
        r"قبلی اشتباه",
    ]
    
    has_update_marker = any(re.search(p, t, flags=re.IGNORECASE) for p in update_markers)
    if not has_update_marker:
        return False

    alias_pattern = key_aliases.get(memory_key)
    return bool(alias_pattern and re.search(alias_pattern, t, flags=re.IGNORECASE))


def extract_long_term_memories(text):
    """
    Extract only the user's own profile facts from Persian/English text.
    Ignores third-party entities (e.g., pet names).
    Returns list[tuple(memory_key, memory_value)].
    """
    t = normalize_text(text)
    memories = {}
    invalid_name_tokens = {
        "\u0639\u0648\u0636", "\u062a\u063a\u06cc\u06cc\u0631", "\u0627\u0635\u0644\u0627\u062d",
        "\u0634\u062f", "\u0628\u0634\u062f", "\u06a9\u0646", "\u06a9\u0631\u062f\u0645",
        "\u0646\u06cc\u0633\u062a", "\u0647\u0633\u062a", "\u0647\u0633\u062a\u0645",
        "\u0645\u0646", "\u0627\u0645", "new", "change", "update"
    }

    def set_memory(key, value):
        if key not in memories and value:
            memories[key] = normalize_text(value)

    def is_valid_name_token(token):
        return normalize_text(token).lower() not in invalid_name_tokens

    # Full-name detection must be strict; do not treat "من رضا هستم" as first+last name.
    full_name_patterns = [
        r"\b\u0645\u0646\s+([\u0622-\u06CCA-Za-z]{2,30})\s+([\u0622-\u06CCA-Za-z]{2,40})\s+(?:\u0647\u0633\u062A\u0645|\u0627\u0645)\b",
        r"(?:\u0627\u0633\u0645(?:\s*\u0648)?\s*\u0641\u0627\u0645\u06cc\u0644\u06cc(?:\s*\u0645\u0646)?|\u0646\u0627\u0645\s*\u0648\s*\u0646\u0627\u0645\s*\u062e\u0627\u0646\u0648\u0627\u062f\u06af\u06cc(?:\s*\u0645\u0646)?)\s*(?:\u0647\u0633\u062a(?:\u0645)?|:|=)?\s*([\u0622-\u06CCA-Za-z]{2,30})\s+([\u0622-\u06CCA-Za-z]{2,40})",
    ]
    for pat in full_name_patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            first_candidate = m.group(1).strip()
            last_candidate = m.group(2).strip()
            if is_valid_name_token(first_candidate) and is_valid_name_token(last_candidate):
                set_memory("first_name", first_candidate)
                set_memory("last_name", last_candidate)
                break

    first_name_patterns = [
        r"(?:\u0627\u0633\u0645\u0645|\u0646\u0627\u0645\u0645|(?:\u0627\u0633\u0645|\u0646\u0627\u0645)(?:\s*\u06a9\u0648\u0686\u06a9)?\s*(?:\u0645\u0646|\u062e\u0648\u062f\u0645))\s*(?:\u0647\u0633\u062A(?:\u0645)?|:|=)?\s*([\u0622-\u06CCA-Za-z]{2,30})",
        r"\u0645\u0646\s+([\u0622-\u06CCA-Za-z]{2,30})\s+\u0647\u0633\u062A\u0645",
        r"\u0645\u0646\s+([\u0622-\u06CCA-Za-z]{2,30})\s+\u0627\u0645",
        r"(?:my\s+name|first\s+name)\s*(?:is|:|=)\s*([A-Za-z]{2,30})",
    ]
    for pat in first_name_patterns:
        for m in re.finditer(pat, t, flags=re.IGNORECASE):
            candidate = m.group(1).strip()
            if is_valid_name_token(candidate):
                set_memory("first_name", candidate)
        if "first_name" in memories:
            break

    last_name_patterns = [
        r"(?:\u0627\u0633\u0645\s*\u0641\u0627\u0645\u06cc\u0644\u06cc\s*\u0645\u0646|\u0646\u0627\u0645\s*\u062e\u0627\u0646\u0648\u0627\u062f\u06af\u06cc\s*\u0645\u0646|\u0641\u0627\u0645\u06cc\u0644\u06cc\u0645|\u0641\u0627\u0645\u06cc\u0644\u06cc\s*\u0645\u0646)\s*(?:\u0647\u0633\u062A(?:\u0645)?|:|=)?\s*([\u0622-\u06CCA-Za-z]{2,40})",
        r"(?:my\s+last\s+name|last\s+name)\s*(?:is|:|=)\s*([A-Za-z]{2,40})",
    ]
    for pat in last_name_patterns:
        for m in re.finditer(pat, t, flags=re.IGNORECASE):
            candidate = m.group(1).strip()
            if is_valid_name_token(candidate):
                set_memory("last_name", candidate)
        if "last_name" in memories:
            break

    age_patterns = [
        r"(?:\u0633\u0646(?:\s*\u0645\u0646)?)\s*(?:\u0647\u0633\u062A(?:\u0645)?|:|=)?\s*(\d{1,3})",
        r"(\d{1,3})\s*\u0633\u0627\u0644\s*\u0633\u0646\s*(?:\u062F\u0627\u0631\u0645|\u0647\u0633\u062A\u0645)",
        r"(\d{1,3})\s*\u0633\u0627\u0644(?:\u0647)?\s*(?:\u0647\u0633\u062A\u0645|\u0647\u0633\u062A|\u0633\u0627\u0644\u0645\u0647|\u062F\u0627\u0631\u0645|\u0633\u0646(?:\s*\u0645\u0646)?\s*\u0647)",
        r"(\d{1,3})\s*\u0633\u0627\u0644\u0645(?:\s*\u0647|\s*\u0627\u0633\u062A)?",
        r"i\s+am\s+(\d{1,3})\s*(?:years?\s*old)?",
    ]
    for pat in age_patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            age = int(m.group(1))
            if 5 <= age <= 120:
                set_memory("age", str(age))
            break

    like_patterns = [
        r"\u0628\u0647\s+([^.!?\u060C]{2,80})\s+\u0639\u0644\u0627\u0642\u0647(?:\s+\u0632\u06CC\u0627\u062F\u06CC|\s+\u0632\u06CC\u0627\u062F|\s+\u062E\u06CC\u0644\u06CC|\s+\u0634\u062F\u06CC\u062F(?:\u0627)?)?\s+\u062F\u0627\u0631\u0645",
        r"(?:\u0645\u0646\s+\u062F\u0648\u0633\u062A\s*\u062F\u0627\u0631\u0645(?:\s*\u0628\u0647)?|\u0645\u0646\s+\u0639\u0644\u0627\u0642\u0647\s*\u062F\u0627\u0631\u0645(?:\s*\u0628\u0647)?|\u0639\u0644\u0627\u0642\u0647(?:\s*\u0645\u0646)?\s*(?:\u0628\u0647)?\s*(?::|=)?)\s*([^.!?\u060C]{2,80})",
        r"(?:\u0639\u0627\u0634\u0642(?:\s*\u0634\u062F\u06CC\u062F)?(?:\s*\u0645)?(?:\s*\u0647\u0633\u062A\u0645)?(?:\s*\u0628\u0647)?)\s*([^.!?\u060C]{2,80})",
        r"i\s+like\s*(?::|=|-)?\s*([^.!?,]{2,80})",
    ]
    for pat in like_patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            like_value = m.group(1).strip(" .,!?\u061F\u060C")
            like_value = re.sub(r"^\u0628\u0647\s+", "", like_value).strip()
            like_value = re.sub(r"\s*(?:\u0647\u0633\u062A\u0645|\u0647\u0633\u062A|\u062F\u0627\u0631\u0645)\s*$", "", like_value).strip()
            like_value = re.sub(r"^(?:\u0632\u06CC\u0627\u062F\u06CC|\u0632\u06CC\u0627\u062F|\u062E\u06CC\u0644\u06CC|\u0634\u062F\u06CC\u062F(?:\u0627)?|\u0641\u0648\u0642\s*\u0627\u0644\u0639\u0627\u062F\u0647)\s+", "", like_value).strip()
            if like_value in {"\u0632\u06CC\u0627\u062F\u06CC", "\u0632\u06CC\u0627\u062F", "\u062E\u06CC\u0644\u06CC", "\u0634\u062F\u06CC\u062F", "\u0634\u062F\u06CC\u062F\u0627"}:
                like_value = ""
            if len(like_value) >= 2:
                set_memory("likes", like_value)
            break

    return list(memories.items())
def get_user_memory_context(user_id):
    if not user_id:
        return ""

    conn = get_db()
    rows = conn.execute(
        """
        SELECT memory_key, memory_value
        FROM user_memories
        WHERE user_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 30
        """,
        (user_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    first_name = ""
    last_name = ""
    age = ""
    likes = []
    for row in rows:
        key = row["memory_key"]
        value = row["memory_value"]
        if key == "first_name" and not first_name:
            first_name = value
        elif key == "last_name" and not last_name:
            last_name = value
        elif key == "age" and not age:
            age = value
        elif key == "likes" and value not in likes:
            likes.append(value)

    lines = ["User long-term memory:"]
    if first_name:
        lines.append(f"- First name: {first_name}")
    if last_name:
        lines.append(f"- Last name: {last_name}")
    if age:
        lines.append(f"- Age: {age}")
    if likes:
        lines.append(f"- Likes: {', '.join(likes[:8])}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def get_user_memory_context(user_id):
    if not user_id:
        return ""

    conn = get_db()
    row = conn.execute(
        """
        SELECT u.username, m.first_name, m.last_name, m.age, m.likes
        FROM users u
        LEFT JOIN user_memories m
            ON m.user_id = u.id
           AND m.memory_key = ?
           AND m.memory_value = ?
        WHERE u.id = ?
        LIMIT 1
        """,
        (PROFILE_ROW_KEY, PROFILE_ROW_VALUE, user_id)
    ).fetchone()
    conn.close()

    if not row:
        return ""

    username = normalize_text(row["username"] or "")
    first_name = normalize_text(row["first_name"] or "")
    last_name = normalize_text(row["last_name"] or "")
    age = str(row["age"]).strip() if row["age"] is not None else ""
    likes = parse_likes_text(row["likes"])

    lines = ["User long-term memory:"]
    if username:
        lines.append(f"- Username: {username}")
    if first_name:
        lines.append(f"- First name: {first_name}")
    if last_name:
        lines.append(f"- Last name: {last_name}")
    if age:
        lines.append(f"- Age: {age}")
    if likes:
        lines.append(f"- Likes: {', '.join(likes[:8])}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def upsert_guest_memories(guest_id, memories, source_text=""):
    if not guest_id or not memories:
        return

    conn = get_db()
    for key, value in memories:
        if key not in PROFILE_MEMORY_KEYS:
            continue

        clean_value = normalize_text(value)
        if not clean_value:
            continue

        existing = conn.execute(
            """
            SELECT memory_value
            FROM guest_memories
            WHERE guest_id = ? AND memory_key = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (guest_id, key)
        ).fetchone()

        if existing:
            current_value = normalize_text(existing["memory_value"])
            if current_value == clean_value:
                continue
            if not has_explicit_update_intent(source_text, key):
                continue
            conn.execute(
                "DELETE FROM guest_memories WHERE guest_id = ? AND memory_key = ?",
                (guest_id, key)
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO guest_memories (guest_id, memory_key, memory_value, source, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (guest_id, key, clean_value, source_text[:500])
        )

    conn.commit()
    conn.close()


def get_guest_memory_context(guest_id):
    if not guest_id:
        return ""

    conn = get_db()
    rows = conn.execute(
        """
        SELECT memory_key, memory_value
        FROM guest_memories
        WHERE guest_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 30
        """,
        (guest_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    first_name = ""
    last_name = ""
    age = ""
    likes = []
    for row in rows:
        key = row["memory_key"]
        value = row["memory_value"]
        if key == "first_name" and not first_name:
            first_name = value
        elif key == "last_name" and not last_name:
            last_name = value
        elif key == "age" and not age:
            age = value
        elif key == "likes" and value not in likes:
            likes.append(value)

    lines = ["User long-term memory:"]
    if first_name:
        lines.append(f"- First name: {first_name}")
    if last_name:
        lines.append(f"- Last name: {last_name}")
    if age:
        lines.append(f"- Age: {age}")
    if likes:
        lines.append(f"- Likes: {', '.join(likes[:8])}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def get_guest_message_count(guest_id):
    if not guest_id:
        return 0
    conn = get_db()
    row = conn.execute(
        "SELECT message_count FROM guest_limits WHERE guest_id = ?",
        (guest_id,)
    ).fetchone()
    conn.close()
    return int(row["message_count"]) if row else 0


def increase_guest_message_count(guest_id):
    if not guest_id:
        return
    conn = get_db()
    conn.execute(
        """
        INSERT INTO guest_limits (guest_id, message_count, updated_at)
        VALUES (?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(guest_id) DO UPDATE SET
            message_count = message_count + 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (guest_id,)
    )
    conn.commit()
    conn.close()


def resolve_guest_id(raw_guest_id):
    guest_id = (raw_guest_id or "").strip()
    if guest_id:
        return guest_id
    ua = request.headers.get("User-Agent", "")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    fingerprint = f"{ip}|{ua}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"guest_{digest}"

# ===============================
# Routes
# ===============================
@app.route("/")
def index():
    return render_template("index.html")




@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_message = data.get("message", "").strip()
    conversation_id = data.get("conversation_id")
    username = (data.get("username") or "").strip().lower()
    guest_id = (data.get("guest_id") or "").strip()

    if not user_message:
        return jsonify({"error": "message required"}), 400

    if not conversation_id:
        conversation_id = create_conversation()

    user = find_user_by_username(username) if username else None
    user_id = user["id"] if user else None

    if not user_id:
        guest_count = get_guest_message_count(guest_id)
        if guest_count >= 5:
            return jsonify({
                "error": "برای ادامه چت بیشتر از 5 پیام، لطفا وارد حساب شوید یا ثبت نام کنید."
            }), 403

    if user_id:
        extracted = extract_long_term_memories(user_message)
        upsert_user_memories(user_id, extracted, source_text=user_message)
    else:
        increase_guest_message_count(guest_id)

    save_message(conversation_id, "user", user_message)
    history = get_full_history(conversation_id, max_messages=200)  # حافظه قوی‌تر

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    memory_context = get_user_memory_context(user_id)
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    messages.extend(history)

    headers = {
        "Authorization": f"Bearer {AVAL_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.7
    }

    try:
        response = requests.post(AVAL_API_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()
        ai_reply = result["choices"][0]["message"]["content"]

        # ذخیره پاسخ AI
        save_message(conversation_id, "assistant", ai_reply)

        return jsonify({
            "reply": ai_reply,
            "conversation_id": conversation_id
        })

    except Exception:
        return jsonify({"error": SERVER_CONNECTION_ERROR}), 500

def chat_v2():
    data = request.json or {}
    user_message = data.get("message", "").strip()
    conversation_id = data.get("conversation_id")
    guest_id = resolve_guest_id(data.get("guest_id"))

    if not user_message:
        return jsonify({"error": "message required"}), 400

    user = get_authenticated_user()
    user_id = user["id"] if user else None
    conversation_id = resolve_conversation_scope(
        conversation_id,
        user_id=user_id,
        guest_id=None if user_id else guest_id
    )

    if not user_id:
        guest_count = get_guest_message_count(guest_id)
        if guest_count >= GUEST_MESSAGE_LIMIT:
            return jsonify({
                "error": GUEST_LIMIT_ERROR
            }), 403

    extracted = extract_long_term_memories(user_message)
    if user_id:
        upsert_user_memories(user_id, extracted, source_text=user_message)
    else:
        upsert_guest_memories(guest_id, extracted, source_text=user_message)
        increase_guest_message_count(guest_id)

    save_message(conversation_id, "user", user_message)
    history = get_full_history(conversation_id, max_messages=200)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    memory_context = get_user_memory_context(user_id) if user_id else get_guest_memory_context(guest_id)
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    messages.extend(history)

    headers = {
        "Authorization": f"Bearer {AVAL_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.7
    }

    try:
        response = requests.post(AVAL_API_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()
        ai_reply = result["choices"][0]["message"]["content"]

        save_message(conversation_id, "assistant", ai_reply)

        return jsonify({
            "reply": ai_reply,
            "conversation_id": conversation_id
        })
    except Exception:
        return jsonify({"error": SERVER_CONNECTION_ERROR}), 500


app.view_functions["chat"] = chat_v2


@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    user = get_authenticated_user()
    guest_id = resolve_guest_id(request.args.get("guest_id"))
    conn = get_db()
    if user:
        rows = conn.execute(
            """
            SELECT id, user_id, guest_id, created_at
            FROM conversations
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user["id"],)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, user_id, guest_id, created_at
            FROM conversations
            WHERE user_id IS NULL AND guest_id = ?
            ORDER BY created_at DESC
            """,
            (guest_id,)
        ).fetchall()
    conn.close()
    return jsonify([
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "guest_id": r["guest_id"],
            "created_at": r["created_at"]
        }
        for r in rows
    ])


@app.route("/api/memory/users", methods=["GET"])
def list_user_memory_profiles():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "authentication required"}), 401

    conn = get_db()
    row = conn.execute(
        """
        SELECT
            u.id AS user_id,
            u.username,
            u.name AS account_name,
            m.username AS memory_username,
            m.first_name,
            m.last_name,
            m.age,
            m.likes,
            m.updated_at
        FROM users u
        LEFT JOIN user_memories m
            ON m.user_id = u.id
           AND m.memory_key = ?
           AND m.memory_value = ?
        WHERE u.id = ?
        LIMIT 1
        """,
        (PROFILE_ROW_KEY, PROFILE_ROW_VALUE, user["id"])
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "user not found"}), 404

    return jsonify({
        "user_id": row["user_id"],
        "username": row["username"],
        "memory_username": row["memory_username"],
        "account_name": row["account_name"],
        "first_name": normalize_text(row["first_name"] or ""),
        "last_name": normalize_text(row["last_name"] or ""),
        "age": row["age"],
        "likes": parse_likes_text(row["likes"]),
        "updated_at": row["updated_at"],
    })


@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json or {}
    name = data.get("name", "").strip()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "").strip()

    if not name or not username or not password:
        return jsonify({"error": "name, username and password are required"}), 400

    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, username, password_hash) VALUES (?, ?, ?)",
            (name, username, password)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "username already exists"}), 409

    user = conn.execute(
        "SELECT id, name, username, created_at FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    conn.close()
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]

    return jsonify({
        "message": "signup successful",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "username": user["username"],
            "created_at": user["created_at"]
        }
    }), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    conn = get_db()
    user = conn.execute(
        "SELECT id, name, username, password_hash, created_at FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if not user or user["password_hash"] != password:
        conn.close()
        return jsonify({"error": "invalid username or password"}), 401

    conn.close()
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]

    return jsonify({
        "message": "login successful",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "username": user["username"],
            "created_at": user["created_at"]
        }
    })

# ===============================
# اجرا
# ===============================
@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "logout successful"})


@app.route("/api/me", methods=["GET"])
def me():
    user = get_authenticated_user()
    if not user:
        return jsonify({"authenticated": False, "user": None}), 200
    return jsonify({
        "authenticated": True,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "username": user["username"],
            "created_at": user["created_at"]
        }
    }), 200


if __name__ == "__main__":
    app.run(debug=True)
