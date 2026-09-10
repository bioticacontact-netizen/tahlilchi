import asyncio
import json
import logging
import sqlite3
import re
import io
import os
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, PollAnswer

# ==================== SIZNING SOZLAMALARINGIZ ====================
BOT_TOKEN = "8047123416:AAHmsDiUyZN2Qwzqa1wO_0r_XQT61qaiOjM"
GEMINI_API_KEY = "AQ.Ab8RN6Jsk4Fh0uiI1wJ2kvSovfYvMCh-jd7LehJy2-gvz6em-A"
# =================================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_FILE = "bot_database.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                full_name TEXT,
                username TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                thread_id INTEGER,
                title TEXT,
                total_questions INTEGER,
                duration_seconds INTEGER DEFAULT 10800,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                poll_id TEXT PRIMARY KEY,
                test_id INTEGER,
                chat_id INTEGER,
                thread_id INTEGER,
                message_id INTEGER,
                question_text TEXT,
                correct_option_id INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS answers (
                student_id INTEGER,
                poll_id TEXT,
                test_id INTEGER,
                chat_id INTEGER,
                chosen_option INTEGER,
                is_correct INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (student_id, poll_id)
            )
        """)
        for table in ["students", "tests", "questions", "answers"]:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN chat_id INTEGER")
            except Exception:
                pass
        try:
            cursor.execute("ALTER TABLE tests ADD COLUMN duration_seconds INTEGER DEFAULT 10800")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE tests ADD COLUMN thread_id INTEGER")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE questions ADD COLUMN thread_id INTEGER")
        except Exception:
            pass
        conn.commit()

init_db()

async def is_admin_of_chat(message: Message) -> bool:
    if not message.from_user:
        return False
    if message.chat.type in ["group", "supergroup"]:
        try:
            member = await bot.get_chat_member(message.chat.id, message.from_user.id)
            return member.status in ["creator", "administrator"]
        except Exception:
            return False
    return True

def calculate_delay_seconds(time_str: str) -> int:
    time_str = time_str.strip().lower()
    m_rel = re.match(r'^(\d+)\s*(m|min|daqiqa|h|soat)$', time_str)
    if m_rel:
        val = int(m_rel.group(1))
        unit = m_rel.group(2)
        return val * 60 if unit in ['m', 'min', 'daqiqa'] else val * 3600

    m_abs = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
    if m_abs:
        hour = int(m_abs.group(1))
        minute = int(m_abs.group(2))
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int((target - now).total_seconds())
    return 0

def parse_newtest_command(text: str):
    start_time_str = ""
    duration_seconds = 10800

    m_vaqt = re.search(r'vaqt:\s*([0-9:]+(?:\s*[a-zA-Z]+)?)', text, re.I)
    if m_vaqt:
        start_time_str = m_vaqt.group(1).strip()
        text = text.replace(m_vaqt.group(0), "")

    m_muddat = re.search(r'muddat:\s*([0-9]+(?:\s*[a-zA-Z]+)?)', text, re.I)
    if m_muddat:
        dur_raw = m_muddat.group(1).lower().strip()
        text = text.replace(m_muddat.group(0), "")
        m_val = re.match(r'^(\d+)\s*(h|soat|m|min)?$', dur_raw)
        if m_val:
            v = int(m_val.group(1))
            u = m_val.group(2) or "h"
            duration_seconds = v * 60 if u in ["m", "min"] else v * 3600

    text = " | ".join([p.strip() for p in text.split("|") if p.strip()])

    m_bracket = re.search(r'[<«\[\"]([^>»\]\"]+)[>»\]\"]', text)
    if m_bracket:
        title = m_bracket.group(1).strip()
        content = text.replace(m_bracket.group(0), "").strip(" |")
        return title, content, start_time_str, duration_seconds

    if "|" in text:
        pts = text.split("|", 1)
        return pts[0].strip(), pts.strip(), start_time_str, duration_seconds

    m_link = re.search(r'(https?://\S+)', text)
    if m_link:
        before = text[:m_link.start()].strip()
        if before:
            return before, text, start_time_str, duration_seconds

    return text[:30].strip(), text, start_time_str, duration_seconds

async def upload_file_bytes_to_gemini(session, file_bytes: bytes, mime_type="video/mp4") -> str:
    try:
        init_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
        headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(file_bytes)),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json"
        }
        async with session.post(init_url, headers=headers, json={"file": {"display_name": "telegram_video"}}) as resp:
            if resp.status != 200:
                return None
            upload_url = resp.headers.get("X-Goog-Upload-URL")

        upload_headers = {
            "Content-Length": str(len(file_bytes)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize"
        }
        async with session.post(upload_url, headers=upload_headers, data=file_bytes) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("file", {}).get("uri")
    except Exception as e:
        logging.error(f"Faylni yuklashda xato: {e}")
        return None

async def generate_quiz_with_gemini(topic_or_text: str, video_bytes: bytes = None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={GEMINI_API_KEY}"
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        parts = []

        if video_bytes:
            file_uri = await upload_file_bytes_to_gemini(session, video_bytes)
            if file_uri:
                parts.append({"fileData": {"fileUri": file_uri, "mimeType": "video/mp4"}})
                prompt = f"""
Siz professional o'qituvchi va metodistsiz. Taqdim etilgan video darsni to'liq ko'rib chiqib, unda aytilgan asosiy faktlar, qoidalar va tushunchalar asosida 5 ta sifatli test (viktorina) savolini tuzing.

Dars mavzusi: {topic_or_text}

Qoidalar:
1. Har bir savolda 4 ta variant (options) bo'lsin.
2. Har bir savol uchun faqat 1 ta to'g'ri javob indeksi (0, 1, 2 yoki 3) ko'rsatilsin (correct_option_id).
3. Savol matni 250 belgidan, variantlar 100 belgidan oshmasin.
4. Javobni FAQAT toza JSON formatida qaytaring:
[
  {{
    "question": "Savol matni...",
    "options": ["Variant A", "Variant B", "Variant C", "Variant D"],
    "correct_option_id": 0
  }}
]
"""
                parts.append({"text": prompt})

        if not parts:
            yt_match = re.search(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=[a-zA-Z0-9_-]+|youtu\.be/[a-zA-Z0-9_-]+)[^\s]*)', topic_or_text)
            if yt_match:
                yt_url = yt_match.group(1).replace("youtu.be/", "www.youtube.com/watch?v=")
                parts.append({"fileData": {"fileUri": yt_url, "mimeType": "video/*"}})
                prompt = """
Siz professional o'qituvchi va metodistsiz. Taqdim etilgan videoni to'liq ko'rib chiqib, unda aytilgan asosiy faktlar, qoidalar va tushunchalar asosida 5 ta sifatli test (viktorina) savolini tuzing.

Qoidalar:
1. Har bir savolda 4 ta variant (options) bo'lsin.
2. Har bir savol uchun faqat 1 ta to'g'ri javob indeksi (0, 1, 2 yoki 3) ko'rsatilsin (correct_option_id).
3. Savol matni 250 belgidan, har bir variant 100 belgidan oshmasin.
4. Javobni FAQAT quyidagi toza JSON formatida qaytaring:
[
  {
    "question": "Savol matni...",
    "options": ["Variant A", "Variant B", "Variant C", "Variant D"],
    "correct_option_id": 0
  }
]
"""
                parts.append({"text": prompt})

        if not parts:
            prompt = f"""
Siz professional o'qituvchi va metodistsiz. Quyidagi mavzu/dars matni asosida 5 ta sifatli test (viktorina) savolini tuzing.

Mavzu/dars:
{topic_or_text}

Qoidalar:
1. Har bir savolda 4 ta variant (options) bo'lsin.
2. Har bir savol uchun faqat 1 ta to'g'ri javob indeksi (0, 1, 2 yoki 3) ko'rsatilsin (correct_option_id).
3. Savol matni 250 belgidan, har bir variant 100 belgidan oshmasin.
4. Javobni FAQAT quyidagi toza JSON formatida qaytaring:
[
  {{
    "question": "Savol matni...",
    "options": ["Variant A", "Variant B", "Variant C", "Variant D"],
    "correct_option_id": 0
  }}
]
"""
            parts.append({"text": prompt})

        payload = {"contents": [{"parts": parts}]}

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logging.error(f"Gemini API xatosi: {resp.status} - {err_text}")
                    return None
                data = await resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                clean_json = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
                return json.loads(clean_json)
        except Exception as e:
            logging.error(f"Gemini so'rovida xatolik: {e}")
            return None

def get_test_stats(test_id: int, chat_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT title, total_questions FROM tests WHERE id = ?", (test_id,))
        test_info = cursor.fetchone()
        if not test_info:
            return None
        title, total_q = test_info

        cursor.execute("SELECT id, full_name, username, user_id FROM students WHERE chat_id = ?", (chat_id,))
        all_students = cursor.fetchall()
        total_students = len(all_students)

        cursor.execute("""
            SELECT student_id, SUM(is_correct), COUNT(poll_id)
            FROM answers
            WHERE test_id = ? AND chat_id = ?
            GROUP BY student_id
        """, (test_id, chat_id))
        answered_data = {sid: (int(s or 0), int(c)) for sid, s, c in cursor.fetchall()}

        good_results = []
        low_results = []
        unsolved_tags = []
        unsolved_list = []

        for s_id, name, username, uid in all_students:
            clean_username = username.replace("@", "").strip() if username else ""
            tag_name = f"@{clean_username}" if clean_username else (f"[{name}](tg://user?id={uid})" if uid else name)

            if s_id in answered_data:
                correct_count, total_answered = answered_data[s_id]
                if total_answered >= total_q:
                    if correct_count >= 4:
                        good_results.append((name, correct_count))
                    else:
                        low_results.append((name, correct_count))
                else:
                    low_results.append((f"{name} ({total_answered}/{total_q})", correct_count))
            else:
                unsolved_tags.append(tag_name)
                unsolved_list.append(name)

        solved_count = len(good_results) + len(low_results)
        return {
            "title": title,
            "total_q": total_q,
            "total_students": total_students,
            "solved_count": solved_count,
            "good_results": good_results,
            "low_results": low_results,
            "unsolved_tags": unsolved_tags,
            "unsolved_list": unsolved_list
        }

def format_reminder_text(stats: dict, reminder_num: int):
    title = stats["title"]
    unsolved_tags = stats["unsolved_tags"]
    unsolved_list = stats["unsolved_list"]

    mentions_str = " ".join(unsolved_tags) if unsolved_tags else "barcha ustozlar"

    text = f"⚠️📢 ESLATMA! ({reminder_num}/3) ⏰\n\n"
    text += f"Hurmatli {mentions_str},\n"
    text += f"«{title}» video darsi bo'yicha testni hali ishlamadingiz! ❗️\n\n"
    text += "🎥 Iltimos, video darsni ko'rib, testni ishlang. Bu majburiy! ✍️✅\n\n"
    text += f"📊 {title} — statistika\n"
    text += f"❓ Savollar: {stats['total_q']} | 👥 Ustozlar: {stats['total_students']} | ✅ Yechdi: {stats['solved_count']}\n\n"

    text += f"⚠️ 3 va undan kam to'g'ri ({len(stats['low_results'])}):\n"
    if stats["low_results"]:
        for name, score in stats["low_results"]:
            text += f"   • {name} — {score}/{stats['total_q']}\n"
    else:
        text += "   — yo'q 👍\n"

    text += f"\n✅ Yaxshi natija (4-5 to'g'ri) ({len(stats['good_results'])}):\n"
    if stats["good_results"]:
        for name, score in stats["good_results"]:
            text += f"   • {name} — {score}/{stats['total_q']}\n"
    else:
        text += "   — hali yo'q\n"

    text += f"\n❌ Umuman yechmaganlar ({len(unsolved_list)}):\n"
    if unsolved_list:
        for item in unsolved_list:
            text += f"   • {item}\n"
    else:
        text += "   — barcha o'quvchilar yechdi! 🎉\n"

    return text

async def close_test_polls(test_id: int, chat_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE tests SET is_active = 0 WHERE id = ?", (test_id,))
        cursor.execute("SELECT message_id, thread_id FROM questions WHERE test_id = ?", (test_id,))
        q_rows = cursor.fetchall()
        cursor.execute("SELECT thread_id FROM tests WHERE id = ?", (test_id,))
        row_t = cursor.fetchone()
        thread_id = row_t[0] if row_t else None
        conn.commit()

    for row in q_rows:
        mid = row[0]
        if mid:
            try:
                await bot.stop_poll(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    stats = get_test_stats(test_id, chat_id)
    if stats:
        report = format_reminder_text(stats, reminder_num=3)
        closing_msg = (
            f"🔒 **«{stats['title']}» testi rasman yakunlandi!**\n\n"
            f"Viktorina savollari yopildi. Belgilangan vaqt tugagani sababli endi testni qayta ishlab bo'lmaydi.\n\n"
        )
        closing_msg += report.replace("⚠️📢 ESLATMA! (3/3) ⏰\n\n", "📊 YAKUNIY NATIJALAR 📊\n\n")
        try:
            await bot.send_message(chat_id=chat_id, text=closing_msg, parse_mode="Markdown", message_thread_id=thread_id)
        except Exception:
            pass

async def run_scheduled_test(test_id: int, chat_id: int, thread_id: int, lesson_title: str, questions: list, delay: int, duration_seconds: int):
    if delay > 0:
        await asyncio.sleep(delay)

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM tests WHERE id = ?", (test_id,))
        res = cursor.fetchone()
        if not res or res[0] == 0:
            return

    dur_hours = duration_seconds // 3600
    intro_text = (
        f"📝✨ {lesson_title} — TEST ✨📝\n\n"
        f"🎥 Avval video darsni to'liq ko'rib chiqing, so'ng quyidagi {len(questions)} ta savolga javob bering. 👇\n\n"
        f"⏰ Testni yechish uchun **{dur_hours} soat** vaqt berildi. Muddat tugagach savollar avtomatik yopiladi!\n"
        f"☝️ Har savolga faqat bir marta javob bera olasiz — diqqat bilan tanlang! ✅"
    )
    await bot.send_message(chat_id=chat_id, text=intro_text, parse_mode="Markdown", message_thread_id=thread_id)

    for idx, q in enumerate(questions, start=1):
        q_text = f"{lesson_title} | {idx}. {q['question']}"
        poll_msg = await bot.send_poll(
            chat_id=chat_id,
            question=q_text,
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct_option_id"],
            is_anonymous=False,
            message_thread_id=thread_id
        )

        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO questions (poll_id, test_id, chat_id, thread_id, message_id, question_text, correct_option_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (poll_msg.poll.id, test_id, chat_id, thread_id, poll_msg.message_id, q["question"], q["correct_option_id"]))
            conn.commit()

        await asyncio.sleep(1)

    step = duration_seconds // 3
    intervals = [step, step * 2]

    for idx, rem_delay in enumerate(intervals, start=1):
        await asyncio.sleep(step)
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_active FROM tests WHERE id = ?", (test_id,))
            res = cursor.fetchone()
            if not res or res[0] == 0:
                return

        stats = get_test_stats(test_id, chat_id)
        if stats and stats["unsolved_tags"]:
            msg_text = format_reminder_text(stats, reminder_num=idx)
            try:
                await bot.send_message(chat_id=chat_id, text=msg_text, message_thread_id=thread_id)
            except Exception as e:
                logging.error(f"Eslatma yuborishda xato: {e}")

    await asyncio.sleep(step)
    await close_test_polls(test_id, chat_id)

@dp.message(Command("addstudents"))
async def cmd_addstudents(message: Message):
    if not await is_admin_of_chat(message):
        await message.reply("⛔️ Bu buyruqdan faqat guruh administratorlari foydalanishi mumkin.")
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    if message.chat.type not in ["group", "supergroup"]:
        await message.reply("⚠️ Iltimos, bu buyruqni tegishli guruh ichida yozing.")
        return

    text = message.text.replace("/addstudents", "").strip()

    try:
        await message.delete()
    except Exception:
        pass

    if not text:
        info_msg = await bot.send_message(chat_id, "Iltimos, ustozlar ro'yxatini yuboring.", message_thread_id=thread_id)
        await asyncio.sleep(5)
        try:
            await info_msg.delete()
        except Exception:
            pass
        return

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    added_count = 0
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        for line in lines:
            m = re.search(r"@([a-zA-Z0-9_]{4,32})", line)
            username = m.group(1) if m else ""
            name = re.sub(r"@([a-zA-Z0-9_]{4,32})", "", line).strip()
            if not name:
                name = f"@{username}" if username else line

            cursor.execute("SELECT id FROM students WHERE chat_id = ? AND LOWER(full_name) = LOWER(?)", (chat_id, name))
            exist = cursor.fetchone()
            if not exist:
                cursor.execute("INSERT INTO students (chat_id, full_name, username) VALUES (?, ?, ?)", (chat_id, name, username))
                added_count += 1
            else:
                if username:
                    cursor.execute("UPDATE students SET username = ? WHERE id = ?", (username, exist[0]))

        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM students WHERE chat_id = ?", (chat_id,))
        total_count = cursor.fetchone()[0]

    confirm_msg = await bot.send_message(chat_id, f"✅ Ushbu guruhga {added_count} ta ustoz qo'shildi! Guruhdagi jami ro'yxat: {total_count} ta.", message_thread_id=thread_id)
    await asyncio.sleep(7)
    try:
        await confirm_msg.delete()
    except Exception:
        pass

@dp.message(Command("liststudents"))
async def cmd_liststudents(message: Message):
    if not await is_admin_of_chat(message):
        await message.reply("⛔️ Bu buyruqdan faqat guruh administratorlari foydalanishi mumkin.")
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, full_name, username FROM students WHERE chat_id = ? ORDER BY full_name", (chat_id,))
        rows = cursor.fetchall()

    if not rows:
        await message.reply("Ushbu guruhda o'quvchilar ro'yxati hali kiritilmagan. `/addstudents` orqali qo'shishingiz mumkin.")
        return

    text = f"📋 **Ushbu guruh ustozlari ro'yxati (Jami: {len(rows)} ta):**\n\n"
    for idx, (sid, name, uname) in enumerate(rows, start=1):
        uname_str = f" (@{uname})" if uname else ""
        text += f"{idx}. {name}{uname_str}\n"

    await bot.send_message(chat_id, text, parse_mode="Markdown", message_thread_id=thread_id)

@dp.message(Command("clearstudents"))
async def cmd_clearstudents(message: Message):
    if not await is_admin_of_chat(message):
        await message.reply("⛔️ Bu buyruqdan faqat guruh administratorlari foydalanishi mumkin.")
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    try:
        await message.delete()
    except Exception:
        pass

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM students WHERE chat_id = ?", (chat_id,))
        conn.commit()

    msg = await bot.send_message(chat_id, "🗑 Ushbu guruhdagi o'quvchilar ro'yxati tozalandi.", message_thread_id=thread_id)
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except Exception:
        pass

@dp.message(Command("stoptest", "stop"))
async def cmd_stoptest(message: Message):
    if not await is_admin_of_chat(message):
        await message.reply("⛔️ Bu buyruqdan faqat guruh administratorlari foydalanishi mumkin.")
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    try:
        await message.delete()
    except Exception:
        pass

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if thread_id:
            cursor.execute("SELECT id FROM tests WHERE chat_id = ? AND thread_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1", (chat_id, thread_id))
        else:
            cursor.execute("SELECT id FROM tests WHERE chat_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1", (chat_id,))
        row = cursor.fetchone()

    if not row:
        msg = await bot.send_message(chat_id, "Ushbu topikda faol test mavjud emas.", message_thread_id=thread_id)
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    test_id = row[0]
    await close_test_polls(test_id, chat_id)

@dp.message(Command("newtest"))
@dp.message(F.video | F.forward_from_chat | F.forward_date)
async def cmd_newtest(message: Message):
    if not await is_admin_of_chat(message):
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    if message.chat.type not in ["group", "supergroup"]:
        await message.reply("⚠️ Iltimos, testni guruhingiz va topikingiz ichida boshlang.")
        return

    raw_text = (message.caption or message.text or "").strip()
    raw_text = raw_text.replace("/newtest", "").strip()

    title, content, start_time_str, duration_seconds = parse_newtest_command(raw_text)

    video_bytes = None
    target_video = message.video or (message.document if message.document and message.document.mime_type and "video" in message.document.mime_type else None)
    
    if target_video:
        if target_video.file_size > 20 * 1024 * 1024:
            size_mb = round(target_video.file_size / (1024 * 1024), 1)
            await message.reply(
                f"⚠️ Ushbu video hajmi {size_mb} MB (Telegram botlarida yuklash limiti 20 MB).\n\n"
                f"Katta hajmdagi videoni to'liq ko'rib tahlil qilishi uchun uni YouTube'ga (hatto unlisted qilib) yuklab, linkini berishingiz mumkin!"
            )
            return
        
        status_msg = await bot.send_message(chat_id, "⏳ Video Telegram'dan yuklab olinmoqda va Gemini'ga yuborilmoqda...", message_thread_id=thread_id)
        try:
            file_info = await bot.get_file(target_video.file_id)
            stream = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=stream)
            video_bytes = stream.getvalue()
        except Exception as e:
            await status_msg.edit_text(f"❌ Videoni yuklab olishda xatolik: {e}")
            return
    else:
        status_msg = await bot.send_message(chat_id, f"⏳ Gemini **«{title}»** testi savollarini tayyorlamoqda...", message_thread_id=thread_id)

    try:
        await message.delete()
    except Exception:
        pass

    questions = await generate_quiz_with_gemini(content if content else title, video_bytes=video_bytes)

    if not questions:
        await status_msg.edit_text("❌ Savollarni tuzishda xatolik yuz berdi. Mavzuni matn ko'rinishida yozib ko'ring.")
        await asyncio.sleep(7)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if thread_id:
            cursor.execute("UPDATE tests SET is_active = 0 WHERE chat_id = ? AND thread_id = ? AND is_active = 1", (chat_id, thread_id))
        else:
            cursor.execute("UPDATE tests SET is_active = 0 WHERE chat_id = ? AND is_active = 1", (chat_id,))
        cursor.execute("INSERT INTO tests (chat_id, thread_id, title, total_questions, duration_seconds) VALUES (?, ?, ?, ?, ?)", (chat_id, thread_id, title, len(questions), duration_seconds))
        test_id = cursor.lastrowid
        conn.commit()

    delay = calculate_delay_seconds(start_time_str) if start_time_str else 0
    dur_hours = duration_seconds // 3600

    if delay > 0:
        await status_msg.edit_text(
            f"✅ **«{title}» testi ushbu topik uchun muvaffaqiyatli rejalashtirildi!**\n\n"
            f"🕒 Guruhga chiqarilish vaqti: **{start_time_str}**\n"
            f"⏳ Ishlash uchun berilgan muddat: **{dur_hours} soat**\n\n"
            f"*(Guruh toza turishi uchun bu bildirishnoma 10 soniyadan so'ng avtomatik o'chadi)*",
            parse_mode="Markdown"
        )
        await asyncio.sleep(10)
        try:
            await status_msg.delete()
        except Exception:
            pass
    else:
        try:
            await status_msg.delete()
        except Exception:
            pass

    asyncio.create_task(run_scheduled_test(test_id, chat_id, thread_id, title, questions, delay, duration_seconds))

@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    user_id = poll_answer.user.id
    poll_id = poll_answer.poll_id
    user_full_name = poll_answer.user.full_name
    raw_username = poll_answer.user.username or ""
    chosen_option = poll_answer.option_ids[0] if poll_answer.option_ids else -1

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT test_id, chat_id, correct_option_id FROM questions WHERE poll_id = ?", (poll_id,))
        q_data = cursor.fetchone()
        if not q_data:
            return

        test_id, chat_id, correct_option_id = q_data

        cursor.execute("SELECT id FROM students WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        row = cursor.fetchone()
        if not row:
            cursor.execute("SELECT id FROM students WHERE chat_id = ? AND (LOWER(full_name) = LOWER(?) OR (username != '' AND LOWER(username) = LOWER(?)))", (chat_id, user_full_name.strip(), raw_username.strip()))
            name_row = cursor.fetchone()
            if name_row:
                student_id = name_row[0]
                cursor.execute("UPDATE students SET user_id = ?, username = ? WHERE id = ?", (user_id, raw_username, student_id))
            else:
                cursor.execute("INSERT INTO students (chat_id, user_id, full_name, username) VALUES (?, ?, ?, ?)", (chat_id, user_id, user_full_name, raw_username))
                student_id = cursor.lastrowid
        else:
            student_id = row[0]

        is_correct = 1 if chosen_option == correct_option_id else 0
        cursor.execute("""
            INSERT OR REPLACE INTO answers (student_id, poll_id, test_id, chat_id, chosen_option, is_correct)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (student_id, poll_id, test_id, chat_id, chosen_option, is_correct))
        conn.commit()

@dp.message(Command("stat"))
async def cmd_stat(message: Message):
    if not await is_admin_of_chat(message):
        await message.reply("⛔️ Bu buyruqdan faqat guruh administratorlari foydalanishi mumkin.")
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if thread_id:
            cursor.execute("SELECT id FROM tests WHERE chat_id = ? AND thread_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1", (chat_id, thread_id))
        else:
            cursor.execute("SELECT id FROM tests WHERE chat_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1", (chat_id,))
        row = cursor.fetchone()

    if not row:
        await message.reply("Ushbu topikda faol test mavjud emas.")
        return

    test_id = row[0]
    stats = get_test_stats(test_id, chat_id)
    if stats:
        report = format_reminder_text(stats, reminder_num=1)
        await bot.send_message(chat_id, report, message_thread_id=thread_id)

# ==================== RENDER UCHUN PING VA PORT ====================
async def handle_ping(request):
    return web.Response(text="Bot is running online 24/7!")

# BOTNI ISHGA TUSHIRISH (PORT OCHADIGAN TO'LIQ FUNKSIYA)
async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    # Render serverining 8080-portini ochish
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Bot 24/7 server rejimida (Port: {port}) muvaffaqiyatli ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
