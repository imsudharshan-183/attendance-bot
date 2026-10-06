import os, sqlite3, asyncio, threading, time, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from openpyxl import Workbook
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          MessageHandler, filters)

TZ = ZoneInfo("Asia/Kolkata")
FMT = "%Y-%m-%d %H:%M:%S"
WORDS = {"present": "Present", "p": "Present", "absent": "Absent", "a": "Absent"}
LEAVE_WORDS = {"leave", "leaving", "left", "l"}
PRANK_IDS = set()  # optional: add Telegram user IDs, e.g. {123456789}

# ---------- Health API (keeps Render free Web Service awake) ----------
PING_INTERVAL = 240  # seconds; must stay well under Render's 15 min idle limit


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence request logs
        pass


def run_health_api():
    port = int(os.environ.get("PORT", 10000))  # Render provides PORT
    ThreadingHTTPServer(("0.0.0.0", port), Health).serve_forever()


def self_ping():
    base = os.environ.get("RENDER_EXTERNAL_URL")  # set automatically by Render
    if not base:
        print("RENDER_EXTERNAL_URL not set, self-ping disabled (running locally?)")
        return
    while True:
        time.sleep(PING_INTERVAL)
        try:
            urllib.request.urlopen(base.rstrip("/") + "/health", timeout=10)
            print("health ping ok")
        except Exception as e:
            print("health ping failed:", e)


# ---------- Database ----------
DB_PATH = os.environ.get("DB_PATH", "attendance.db")
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("""CREATE TABLE IF NOT EXISTS att(
  chat_id INT, user_id INT, name TEXT, day TEXT, status TEXT, ts TEXT,
  leave_ts TEXT,
  PRIMARY KEY(chat_id, user_id, day))""")
try:  # migrate older databases
    db.execute("ALTER TABLE att ADD COLUMN leave_ts TEXT")
except sqlite3.OperationalError:
    pass
db.commit()

# ---------- Optional Google Sheets ----------
# Row 1 headers: Date | Name | Status | Time (IST) | Chat ID | Hours worked
sheet = None
SHEET_ID = os.environ.get("SHEET_ID")
if SHEET_ID and os.path.exists("creds.json") and os.path.getsize("creds.json") > 0:
    import gspread
    sheet = gspread.service_account(filename="creds.json").open_by_key(SHEET_ID).sheet1
    print("Google Sheets enabled")
else:
    print("Google Sheets disabled (SQLite + /export only)")

q = asyncio.Queue()


async def sheet_worker():
    while True:
        batch = [await q.get()]
        await asyncio.sleep(2)
        while not q.empty() and len(batch) < 50:
            batch.append(q.get_nowait())
        for attempt in range(3):
            try:
                await asyncio.to_thread(sheet.append_rows, batch,
                                        value_input_option="USER_ENTERED")
                break
            except Exception as e:
                print("Sheets error:", e)
                await asyncio.sleep(5 * (attempt + 1))


async def start_worker(app):
    if sheet:
        asyncio.create_task(sheet_worker())


# ---------- Helpers ----------
def duration(start_ts: str, end_ts: str) -> str:
    secs = int((datetime.strptime(end_ts, FMT) - datetime.strptime(start_ts, FMT)).total_seconds())
    h, m = divmod(max(secs, 0) // 60, 60)
    return f"{h}h {m}m"


def msg_time(update: Update):
    # Telegram's own message time, so delays/restarts don't shift timestamps
    return update.message.date.astimezone(TZ)


# ---------- Handlers ----------
async def record(update: Update, status: str):
    u, c, now = update.effective_user, update.effective_chat, msg_time(update)
    if u.id in PRANK_IDS:
        await update.message.reply_text(
            "⚠️ Error 500: attendance server is on fire. Please retry in 3 business years.")
        await asyncio.sleep(2)
    day, ts = now.strftime("%Y-%m-%d"), now.strftime(FMT)
    cur = db.execute(
        "INSERT OR IGNORE INTO att(chat_id,user_id,name,day,status,ts) VALUES(?,?,?,?,?,?)",
        (c.id, u.id, u.full_name, day, status, ts))
    db.commit()
    if cur.rowcount == 0:
        row = db.execute("SELECT status, ts FROM att WHERE chat_id=? AND user_id=? AND day=?",
                         (c.id, u.id, day)).fetchone()
        await update.message.reply_text(
            f"{u.full_name}: attendance already marked ({row[0]} at {row[1]})")
        return
    if sheet:
        q.put_nowait([day, u.full_name, status, ts, str(c.id), ""])
    await update.message.reply_text(f"{u.full_name}: {status} at {now:%d-%b-%Y %H:%M:%S}")


async def leave(update: Update, _):
    u, c, now = update.effective_user, update.effective_chat, msg_time(update)
    day, ts = now.strftime("%Y-%m-%d"), now.strftime(FMT)
    row = db.execute("SELECT status, ts, leave_ts FROM att WHERE chat_id=? AND user_id=? AND day=?",
                     (c.id, u.id, day)).fetchone()
    if not row:
        await update.message.reply_text(f"{u.full_name}: mark Present first.")
        return
    if row[0] != "Present":
        await update.message.reply_text(f"{u.full_name}: marked Absent today, can't record leaving.")
        return
    if row[2]:
        await update.message.reply_text(
            f"{u.full_name}: leaving already marked at {row[2]} (total worked: {duration(row[1], row[2])})")
        return
    cur = db.execute(
        "UPDATE att SET leave_ts=? WHERE chat_id=? AND user_id=? AND day=? AND leave_ts IS NULL",
        (ts, c.id, u.id, day))
    db.commit()
    if cur.rowcount == 0:
        await update.message.reply_text(f"{u.full_name}: leaving already marked.")
        return
    worked = duration(row[1], ts)
    if sheet:
        q.put_nowait([day, u.full_name, "Left", ts, str(c.id), worked])
    await update.message.reply_text(
        f"{u.full_name}: Leaving at {now:%d-%b-%Y %H:%M:%S}\n"
        f"Note: total worked {worked} (in at {row[1]})")


async def present(update: Update, _): await record(update, "Present")
async def absent(update: Update, _):  await record(update, "Absent")


async def myid(update: Update, _):
    await update.message.reply_text(str(update.effective_user.id))


async def text(update: Update, _):
    t = (update.message.text or "").strip().lower()
    if t in LEAVE_WORDS:
        await leave(update, _)
    elif t in WORDS:
        await record(update, WORDS[t])


async def export(update: Update, _):
    rows = db.execute(
        "SELECT day,name,status,ts,leave_ts FROM att WHERE chat_id=? ORDER BY day,name",
        (update.effective_chat.id,)).fetchall()
    wb = Workbook(); ws = wb.active
    ws.append(["Date", "Name", "Status", "In time (IST)", "Leave time (IST)", "Hours worked"])
    for day, name, status, ts, lts in rows:
        ws.append([day, name, status, ts, lts or "", duration(ts, lts) if lts else ""])
    fn = f"attendance_{update.effective_chat.id}.xlsx"
    wb.save(fn)
    with open(fn, "rb") as f:
        await update.message.reply_document(f)


# ---------- Start ----------
app = (ApplicationBuilder().token(os.environ["BOT_TOKEN"])
       .concurrent_updates(True).post_init(start_worker).build())
app.add_handler(CommandHandler("present", present))
app.add_handler(CommandHandler("absent", absent))
app.add_handler(CommandHandler("leave", leave))
app.add_handler(CommandHandler("export", export))
app.add_handler(CommandHandler("id", myid))
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, text))

# Start health API + self-ping in background threads
threading.Thread(target=run_health_api, daemon=True).start()
threading.Thread(target=self_ping, daemon=True).start()

print("Bot running. Ctrl+C to stop.")
app.run_polling()