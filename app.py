import asyncio
import os
import sqlite3
import threading
from flask import Flask
from rubka import Robot

TOKEN = os.environ.get("RUBIKA_TOKEN", "").strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")

# Format:
# CHANNELS=@channel1|GUID1|https://rubika.ir/channel1|کانال ۱
# You can add multiple channels separated by newline.
CHANNELS_RAW = os.environ.get("CHANNELS", "").strip()

DB = "files.db"
app = Flask(__name__)

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS files(
        code TEXT PRIMARY KEY,
        source_chat TEXT NOT NULL,
        source_message TEXT NOT NULL,
        name TEXT,
        downloads INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id TEXT PRIMARY KEY,
        first_seen TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    con.commit()
    con.close()

def channels():
    out = []
    for line in CHANNELS_RAW.splitlines():
        line = line.strip()
        if not line:
            continue
        p = line.split("|", 3)
        if len(p) == 4:
            out.append({"username": p[0].lstrip("@"), "guid": p[1], "url": p[2], "title": p[3]})
    return out

def make_code():
    import secrets
    con = db()
    while True:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not con.execute("SELECT 1 FROM files WHERE code=?", (code,)).fetchone():
            con.close()
            return code

def remember_user(uid):
    con = db()
    con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (str(uid),))
    con.commit()
    con.close()

def get_file(code):
    con = db()
    row = con.execute("SELECT * FROM files WHERE code=?", (code,)).fetchone()
    con.close()
    return row

def save_file(code, chat_id, message_id, name):
    con = db()
    con.execute(
        "INSERT INTO files(code,source_chat,source_message,name) VALUES(?,?,?,?)",
        (code, str(chat_id), str(message_id), name or "فایل")
    )
    con.commit()
    con.close()

def inc_download(code):
    con = db()
    con.execute("UPDATE files SET downloads=downloads+1 WHERE code=?", (code,))
    con.commit()
    con.close()

def is_admin(message):
    return ADMIN_ID and str(message.sender_id) == ADMIN_ID

def join_buttons(bot):
    # Uses the library's built-in join-channel buttons.
    ch = channels()
    if not ch:
        return None
    try:
        from rubka.button import InlineBuilder
        b = InlineBuilder()
        for c in ch:
            b = b.row(InlineBuilder().button_join_channel(
                text=f"📢 عضویت در {c['title']}",
                id="join",
                username=c["username"]
            ))
        return b.row(InlineBuilder().button_callback(
            text="✅ بررسی عضویت",
            id="check"
        )).build()
    except Exception:
        return None

async def require_join(bot, message):
    ch = channels()
    if not ch:
        return True
    missing = []
    for c in ch:
        try:
            ok = await bot.check_join(c["guid"], message.sender_id)
        except Exception:
            ok = False
        if not ok:
            missing.append(c)
    if not missing:
        return True

    buttons = join_buttons(bot)
    text = "🔒 برای دریافت فایل، ابتدا در کانال‌های زیر عضو شوید.\n\nبعد از عضویت روی «بررسی عضویت» بزنید."
    if buttons:
        await bot.send_message(message.chat_id, text, inline_keypad=buttons)
    else:
        links = "\n".join(f"📢 {c['title']}: {c['url']}" for c in missing)
        await message.reply(text + "\n\n" + links + "\n\nسپس /start CODE را دوباره بفرستید.")
    return False

bot = Robot(token=TOKEN)

@bot.on_message(commands=["id"])
async def my_id(bot, message):
    await message.reply(f"🆔 شناسه شما:\n`{message.sender_id}`")

@bot.on_message(commands=["start"])
async def start(bot, message):
    remember_user(message.sender_id)
    args = getattr(message, "args", []) or []
    code = args[0].strip() if args else ""

    if not code:
        await message.reply(
            "🤖 ربات فایل\n\n"
            "لینک فایل را از مدیر دریافت کنید.\n"
            "مدیر: برای دریافت شناسه، /id را بفرست."
        )
        return

    row = get_file(code)
    if not row:
        await message.reply("❌ لینک فایل نامعتبر یا منقضی است.")
        return

    if not await require_join(bot, message):
        return

    try:
        await bot.forward_message(
            from_chat_id=row["source_chat"],
            message_id=row["source_message"],
            to_chat_id=message.chat_id
        )
        inc_download(code)
        await bot.send_message(message.chat_id, "✅ فایل ارسال شد.")
    except Exception as e:
        await message.reply("❌ ارسال فایل انجام نشد. مدیر باید فایل اصلی را همچنان در چت ربات نگه دارد.")
        print("forward error:", repr(e))

@bot.on_callback()
async def callbacks(bot, message):
    remember_user(message.sender_id)
    data = getattr(getattr(message, "aux_data", None), "button_id", None)
    if data == "check":
        # The code is not always available in callback context, so ask user
        # to press the original file link again if the check passes.
        ok = True
        for c in channels():
            try:
                if not await bot.check_join(c["guid"], message.sender_id):
                    ok = False
                    break
            except Exception:
                ok = False
                break
        if ok:
            await message.reply("✅ عضویت تأیید شد.\nحالا لینک فایل را دوباره باز کنید.")
        else:
            await message.reply("❌ هنوز عضویت شما در همه کانال‌ها تأیید نشده است.")

@bot.on_message_file()
async def receive_file(bot, message):
    if not is_admin(message):
        await message.reply("⛔ فقط مدیر می‌تواند فایل ثبت کند.")
        return

    code = make_code()
    name = "فایل"
    try:
        f = getattr(message, "file", None)
        if f:
            name = getattr(f, "file_name", None) or getattr(f, "name", None) or name
    except Exception:
        pass

    save_file(code, message.chat_id, message.message_id, name)

    link = f"https://rubika.ir/{BOT_USERNAME}?start={code}" if BOT_USERNAME else f"/start {code}"
    await message.reply(
        "✅ فایل ثبت شد.\n\n"
        f"📄 نام: {name}\n"
        f"🔑 کد: {code}\n"
        f"🔗 لینک:\n{link}\n\n"
        "این پیام را می‌توانی برای کاربران بفرستی."
    )

@bot.on_message(commands=["stats"])
async def stats(bot, message):
    if not is_admin(message):
        return
    con = db()
    files_count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    downloads = con.execute("SELECT COALESCE(SUM(downloads),0) FROM files").fetchone()[0]
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    await message.reply(
        f"📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"📁 فایل‌ها: {files_count}\n"
        f"📥 دریافت‌ها: {downloads}"
    )

@app.get("/")
def health():
    return "Rubika File Bot is running."

@app.get("/health")
def health2():
    return {"ok": True}

def run_bot():
    if not TOKEN:
        raise RuntimeError("RUBIKA_TOKEN is not set")
    if not ADMIN_ID:
        print("WARNING: ADMIN_ID is not set. Send /id to the bot after setting it.")
    bot.run()

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
