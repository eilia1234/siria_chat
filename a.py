from flask import Flask, request, jsonify, render_template
import requests
import sqlite3
import uuid
from datetime import datetime
import re
import hashlib

app = Flask(__name__)
GUEST_MESSAGE_LIMIT = 5
GUEST_LIMIT_ERROR = "برای ادامه گفتگو بیشتر از 5 پیام، لطفا وارد حساب شوید یا ثبت‌نام کنید."
SERVER_CONNECTION_ERROR = "خطای اتصال به سرور"

# ===============================
# تنظیمات API
# ===============================
AVAL_API_KEY = "aa-hKTwRT5ktYvyMvQ9byUL4YGhKgnlXUiU3ZP7UFb4VDDDJnF7"
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
"تو باید نام و نام خانوادگی و سن و علایق کاربران را در حافظه بلند مدت خود ذخیره کنی"
مدل تو isi-1.0 است و اولین نسخه سیریا هستی.
تو نباید خودت را ChatGPT معرفی کنی.
تو در برنامه‌نویسی، وب، Flask، SQL و هوش مصنوعی مهارت کامل داری.
تو همیشه در اولین پیام سلام بدهی.
پاسخ‌ها دقیق، حرفه‌ای و بدون سلام‌های تکراری باشند.
"""

# ===============================
# دیتابیس
# ===============================
def get_db():
    conn = sqlite3.connect("memory.db")
    conn.row_factory = sqlite3.Row
    return conn

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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        r"(?:اسم\s*فامیلی(?:\s*من)?|نام\s*خانوادگی(?:\s*من)?|فامیلی(?:\s*من)?)\s*(?:هست|ه|:)?\s*([آ-یA-Za-z]{2,40})",
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
    for key, value in memories:
        clean_value = normalize_text(value)
        if not clean_value:
            continue

        if key in ("first_name", "last_name"):
            conn.execute(
                "DELETE FROM user_memories WHERE user_id = ? AND memory_key = ?",
                (user_id, key)
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO user_memories (user_id, memory_key, memory_value, source, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (user_id, key, clean_value, source_text[:500])
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


def extract_long_term_memories(text):
    """
    Extract user profile memories from Persian/English text.
    Only stores values when explicit keywords exist.
    Returns list[tuple(memory_key, memory_value)].
    """
    t = normalize_text(text)
    memories = []

    first_name_patterns = [
        r"(?:\u0627\u0633\u0645(?:\s*\u0645\u0646|\u0645)?|\u0646\u0627\u0645(?:\s*\u0645\u0646|\u0645)?)\s*(?:\u0647\u0633\u062A(?:\u0645)?|:|=)?\s*([\u0622-\u06CCA-Za-z]{2,30})",
        r"(?:my\s+name|first\s+name)\s*(?:is|:|=)\s*([A-Za-z]{2,30})",
    ]
    for pat in first_name_patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            memories.append(("first_name", m.group(1).strip()))
            break

    last_name_patterns = [
        r"(?:\u0627\u0633\u0645\s*\u0641\u0627\u0645\u06CC\u0644\u06CC(?:\s*\u0645\u0646)?|\u0646\u0627\u0645\s*\u062E\u0627\u0646\u0648\u0627\u062F\u06AF\u06CC(?:\s*\u0645\u0646)?|\u0641\u0627\u0645\u06CC\u0644\u06CC(?:\s*\u0645\u0646)?)\s*(?:\u0647\u0633\u062A(?:\u0645)?|:|=)?\s*([\u0622-\u06CCA-Za-z]{2,40})",
        r"(?:my\s+last\s+name|last\s+name)\s*(?:is|:|=)\s*([A-Za-z]{2,40})",
    ]
    for pat in last_name_patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            memories.append(("last_name", m.group(1).strip()))
            break

    like_patterns = [
        r"(?:\u0639\u0644\u0627\u0642\u0647(?:\s*\u0645\u0646)?|\u062F\u0648\u0633\u062A\s*\u062F\u0627\u0631\u0645(?:\s*\u0628\u0647)?|\u0639\u0644\u0627\u0642\u0647\s*\u062F\u0627\u0631\u0645(?:\s*\u0628\u0647)?)\s*(?::|=|-)?\s*([^.!?\u060C]{2,80})",
        r"i\s+like\s*(?::|=|-)?\s*([^.!?,]{2,80})",
    ]
    for pat in like_patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            like_value = m.group(1).strip(" .,!?\u061F\u060C")
            if len(like_value) >= 2:
                memories.append(("likes", like_value))
            break

    return memories


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

    lines = ["User long-term memory:"]
    if first_name:
        lines.append(f"- First name: {first_name}")
    if last_name:
        lines.append(f"- Last name: {last_name}")
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
        clean_value = normalize_text(value)
        if not clean_value:
            continue

        if key in ("first_name", "last_name"):
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

    lines = ["User long-term memory:"]
    if first_name:
        lines.append(f"- First name: {first_name}")
    if last_name:
        lines.append(f"- Last name: {last_name}")
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
    username = (data.get("username") or "").strip().lower()
    guest_id = resolve_guest_id(data.get("guest_id"))

    if not user_message:
        return jsonify({"error": "message required"}), 400

    user = find_user_by_username(username) if username else None
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
    conn = get_db()
    rows = conn.execute("""
        SELECT id, user_id, guest_id, created_at
        FROM conversations
        ORDER BY created_at DESC
    """).fetchall()
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
    conn.close()

    if not user or user["password_hash"] != password:
        return jsonify({"error": "invalid username or password"}), 401

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
if __name__ == "__main__":
    app.run(debug=True)

