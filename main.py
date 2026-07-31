import asyncio
import os
import logging
import json
import tempfile
import html
from collections import defaultdict

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- 2. ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И РОЛИ ---
user_locks = defaultdict(asyncio.Lock)
MAX_HISTORY_PAIRS = 10 

ROLES = {
    "default": "Ты умный, вежливый и лаконичный ИИ-ассистент.",
    "video_creator": "Ты креативный режиссер. Пишешь смешные, детальные промпты для нейросетей-генераторов видео. Главные герои — животные (например, собаки) в человеческих абсурдных ситуациях. Описывай свет, ракурс и динамику."
}

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# --- 3. БАЗА ДАННЫХ (aiosqlite) ---
async def init_db():
    async with aiosqlite.connect("bot_data.db") as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users 
                            (user_id INTEGER PRIMARY KEY, model TEXT, role TEXT, history TEXT)''')
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect("bot_data.db") as db:
        async with db.execute("SELECT model, role, history FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"model": row[0], "role": row[1], "history": json.loads(row[2])}
            
            default_state = {"model": "gemini-1.5-flash", "role": "default", "history": []}
            await db.execute("INSERT INTO users (user_id, model, role, history) VALUES (?, ?, ?, ?)",
                             (user_id, default_state["model"], default_state["role"], json.dumps([])))
            await db.commit()
            return default_state

async def update_user_setting(user_id: int, field: str, value: str):
    async with aiosqlite.connect("bot_data.db") as db:
        await db.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
        await db.commit()

async def update_history(user_id: int, new_history: list):
    async with aiosqlite.connect("bot_data.db") as db:
        await db.execute("UPDATE users SET history = ? WHERE user_id = ?", (json.dumps(new_history), user_id))
        await db.commit()

# --- 4. УТИЛИТА: БРОНЕБОЙНЫЙ ВЫВОД ТЕКСТА ---
async def safe_send_text(message: types.Message, wait_message: types.Message, text: str):
    """Пытается отправить Markdown. Если Telegram ругается на символы — слать сырой текст."""
    try:
        await wait_message.edit_text(text, parse_mode="Markdown")
    except TelegramBadRequest:
        try:
            # Вторая попытка с HTML
            await wait_message.edit_text(html.escape(text), parse_mode="HTML")
        except TelegramBadRequest:
            # Если оба упали — шлем без форматирования вообще
            await wait_message.edit_text(text, parse_mode=None)

# --- 5. КОМАНДЫ И МЕНЮ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я продвинутый мультимодальный ИИ.\n\n"
        "Пиши текст, кидай фото, PDF-документы или записывай голосовые сообщения!\n\n"
        "Настройки: /settings\nЛимиты: /limits"
    )

@dp.message(Command("limits"))
async def cmd_limits(message: types.Message):
    await message.answer("📊 Лимиты Free API:\n15 запросов в минуту\n1 млн токенов в минуту")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    state = await get_user(message.from_user.id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡️ Flash" if state['model'] == "gemini-1.5-flash" else "Flash", callback_data="model_flash")
    builder.button(text="🧠 Pro" if state['model'] == "gemini-1.5-pro" else "Pro", callback_data="model_pro")
    builder.button(text="🤖 Обычный" if state['role'] == "default" else "Обычный", callback_data="role_default")
    builder.button(text="🎬 Режиссер Видео" if state['role'] == "video_creator" else "Режиссер Видео", callback_data="role_video")
    builder.button(text="🧹 Очистить контекст", callback_data="clear_context")
    builder.adjust(2, 2, 1)
    
    await message.answer("⚙️ Настройки:\nВыбирай модель и личность бота:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("model_"))
async def cb_model(callback: types.CallbackQuery):
    model_name = "gemini-1.5-flash" if "flash" in callback.data else "gemini-1.5-pro"
    await update_user_setting(callback.from_user.id, "model", model_name)
    await callback.message.edit_text(f"✅ Модель переключена на: {model_name}")

@dp.callback_query(F.data.startswith("role_"))
async def cb_role(callback: types.CallbackQuery):
    role_name = "video_creator" if "video" in callback.data else "default"
    await update_user_setting(callback.from_user.id, "role", role_name)
    await callback.message.edit_text(f"✅ Роль переключена на: {role_name}")

@dp.callback_query(F.data == "clear_context")
async def cb_clear(callback: types.CallbackQuery):
    await update_history(callback.from_user.id, [])
    await callback.message.edit_text("🧹 История диалога удалена. Я всё забыл.")

# --- 6. ЯДРО: МУЛЬТИМОДАЛЬНЫЙ ОБРАБОТЧИК ---
@dp.message(F.text | F.photo | F.document | F.voice)
async def core_handler(message: types.Message):
    user_id = message.from_user.id
    user_lock = user_locks[user_id]
    
    if user_lock.locked():
        await message.reply("⏳ Пишу ответ на предыдущий вопрос. Подожди секунду!")
        return

    async with user_lock:
        state = await get_user(user_id)
        wait_message = await message.answer("⏳ Принимаю данные...")
        
        uploaded_file = None
        temp_path = None
        
        try:
            model = genai.GenerativeModel(
                model_name=state["model"],
                system_instruction=ROLES[state["role"]],
                safety_settings=SAFETY_SETTINGS
            )
            
            trimmed_history = state["history"][-(MAX_HISTORY_PAIRS * 2):] if state["history"] else []
            formatted_history = [{"role": m["role"], "parts": [m["parts"][0]]} for m in trimmed_history]
            chat = model.start_chat(history=formatted_history)
            
            content_to_send = []
            
            if not message.text:
                await wait_message.edit_text("⏳ Загружаю файл в нейросеть...")
                file_id = message.voice.file_id if message.voice else (message.document.file_id if message.document else message.photo[-1].file_id)
                file_info = await bot.get_file(file_id)
                downloaded_file = await bot.download_file(file_info.file_path)
                
                ext = ".ogg" if message.voice else ".jpg" if message.photo else f".{file_info.file_path.split('.')[-1]}"
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                    temp_file.write(downloaded_file.read())
                    temp_path = temp_file.name
                
                uploaded_file = genai.upload_file(path=temp_path)
                content_to_send.append(uploaded_file)
                
                if message.caption:
                    content_to_send.append(message.caption)
            else:
                content_to_send.append(message.text)

            await wait_message.edit_text("✍️ Формулирую ответ...")
            response = await chat.send_message_async(content_to_send)
            
            new_history = [{"role": msg.role, "parts": [msg.parts[0].text]} for msg in chat.history]
            await update_history(user_id, new_history)
            
            await safe_send_text(message, wait_message, response.text)
            
        except Exception as e:
            logging.error(f"Error handling message: {e}")
            await wait_message.edit_text(f"❌ Ошибка генерации:\n{e}")
            
        finally:
            if uploaded_file:
                try:
                    genai.delete_file(uploaded_file.name)
                except Exception as e:
                    logging.warning(f"Failed to delete API file: {e}")
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

# --- 7. ЗАПУСК БОТА ---
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен и база данных инициализирована.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
