import os
import logging
import asyncio
import datetime
import json
from dotenv import load_dotenv

# Aiogram импорты
from aiogram.filters import Command
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Базы данных и API
from supabase import create_client, Client
from openai import AsyncOpenAI

# Погода
from weather import weather_background_task, build_weather_message

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
 
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
raw_id = os.getenv("CHANNEL_ID")
CHANNEL_ID = int(raw_id) if raw_id else None
TAXI_TABLE = os.getenv("TABLE_NAME")
BOT_LINK = os.getenv("BOT_START_LINK")

# Настройка времени Бишкека
TZ_BISHKEK = datetime.timezone(datetime.timedelta(hours=6))

logging.basicConfig(level=logging.INFO)

# Инициализация клиентов
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
aclient = AsyncOpenAI(api_key=OPENAI_KEY)

# --- ФОНОВАЯ ЗАДАЧА: ОЧИСТКА СТАРЫХ ПОСТОВ (3 СУТОК) ---
async def cleanup_old_messages():
    while True:
        try:
            three_days_ago = (datetime.datetime.now(TZ_BISHKEK) - datetime.timedelta(days=3)).isoformat()
            res = supabase.table(TAXI_TABLE).select("id", "message_id").lt("created_at", three_days_ago).not_.is_("message_id", "null").execute()
            
            for record in res.data:
                try: 
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=record["message_id"])
                except: 
                    pass
                supabase.table(TAXI_TABLE).update({"message_id": None}).eq("id", record["id"]).execute()
        except Exception as e:
            logging.error(f"Ошибка очистки: {e}")
        await asyncio.sleep(3600)

# --- КНОПКА ПОД ПОСТОМ ---
def get_channel_publish_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🌤 Погода / Аба ырайы", url=f"{BOT_LINK}?start=show_weather"))
    return builder.as_markup()

# --- КОМАНДА /start (Оставили только для погоды) ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.text and "show_weather" in message.text:
        try:
            status_msg = await message.answer("⏳ Аба ырайы тууралуу маалымат алынууда...") 
            weather_text = await build_weather_message()
            await status_msg.edit_text(weather_text, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка при загрузке погоды: {e}")
    else:
        await message.answer("👋 Саламатсызбы! Мен группадан жарыяларды автоматтык түрдө түзүүчү ботмун.")

# =====================================================================
# --- ГЛАВНЫЙ БЛОК: УМНЫЙ ПАРСИНГ СООБЩЕНИЙ ЧЕРЕЗ CHATGPT ---
# =====================================================================

@dp.message(F.text & ~F.text.startswith('/'))
async def process_free_text_ad(message: types.Message):
    user_id = message.from_user.id
    text_lower = message.text.lower()
    if "http" in text_lower or "t.me" in text_lower or "www." in text_lower:
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение со ссылкой: {e}")
        return # Останавливаем код, в ИИ не идем

    # 2. Фильтр коротких сообщений (меньше 3 слов)
    # split() разбивает текст на слова по пробелам
    words = message.text.split()
    if len(words) < 3:
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить короткое сообщение: {e}")
        return # Останавливаем код, в ИИ не идем

    # 1. Достаем последний пост юзера из БД
    try:
        res = supabase.table(TAXI_TABLE).select("*").eq("user_id", user_id).order("created_at", desc=True).limit(1).execute()
        past_data = res.data[0] if res.data else {}
    except Exception as e:
        logging.error(f"Ошибка БД при чтении: {e}")
        past_data = {}

    # 2. Промпт для GPT
    # 2. Строгий промпт для GPT
    # 2. Строгий промпт для GPT
    prompt = f"""
    Проанализируй текст и извлеки данные. ВАЖНО: Нас интересуют ТОЛЬКО объявления о поиске попутки, такси, пассажиров, отправке посылок или ГРУЗОПЕРЕВОЗКАХ.
    Текст: "{message.text}"
    
    Верни строго JSON со следующими ключами:
    "is_ad": boolean (true ТОЛЬКО если это такси, посылка, поиск машины или грузоперевозка. Если реклама других услуг (массаж и тд) или спам — ставь false!),
    "role": string ("айдоочу", "жүргүнчү", "посылка", "жүк ташуу" или null). ПРАВИЛО: Если водитель на легковой машине (Соната, Камри и тд) ищет пассажиров, но попутно берет посылку — это строго "айдоочу"! Роль "жүк ташуу" ставь ТОЛЬКО для грузовых авто (Спринтер, Портер, фура) или крупного переезда.,
    "origin": string (откуда выезд, или null),
    "destination": string (куда едут, или null),
    "time": string (время/дата отправления, или null),
    "price": string (цена, или null),
    "passenger_count": string (количество мест/людей, или null),
    "cargo_type": string (описание груза, что везут, если это "жүк ташуу", иначе null),
    "phone_number": string (номер телефона, или null),
    "car_model": string (марка машины, или null)
    """

    try:
        # 3. Отправляем запрос в OpenAI
        response = await aclient.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        
        parsed_data = json.loads(response.choices[0].message.content)
        
        # Игнорируем обычные сообщения ("привет", "как дела")
        if not parsed_data.get("is_ad"):
            try:
                await message.delete()
            except Exception as e:
                logging.warning(f"Не удалось удалить спам: {e}")
            return

        # 4. Склеиваем данные
        # Эти данные ПОСТОЯННЫЕ (берем из БД, если человек забыл их написать):
        role = parsed_data.get("role") or past_data.get("role", "айдоочу")
        phone = parsed_data.get("phone_number") or past_data.get("phone_num", "Номери жок")
        car_model = parsed_data.get("car_model") or past_data.get("car_model", "Көрсөтүлгөн жок")

        # А эти данные ДИНАМИЧЕСКИЕ (каждый рейс новые). Из старой БД их НЕ БЕРЕМ!
        origin = parsed_data.get("origin") or "Такталган жок"
        destination = parsed_data.get("destination") or "Такталган жок"
        time = parsed_data.get("time") or "Сүйлөшүү боюнча"
        price = parsed_data.get("price") or "Келишим баада"
        passenger_count = parsed_data.get("passenger_count") or "Такталган жок"

        # Форматируем номер телефона
        clean_phone = phone.replace(" ", "").replace("-", "")
        if clean_phone and not clean_phone.startswith('+') and clean_phone.replace('+','').isdigit(): 
            clean_phone = '+' + clean_phone

        # 5. Формируем красивый текст
        # Достаем тип груза, если он есть
        cargo_type = parsed_data.get("cargo_type") or "Такталган жок"

        # 5. Формируем красивый текст
        if role == "посылка":
            icon = "📦"
            role_name = "ПОСЫЛКА"
            text = (f"{icon} <b>{role_name}</b>\n\n"
                    f"📤 <b>Каяктан</b>: {origin}\n"
                    f"📥 <b>Каякка</b>: {destination}\n"
                    f"🕒 <b>Убакыт</b>: {time}\n"
                    f"📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
                    f"👤 <b>Жөнөтүүчү</b>: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>")
        
        elif role == "жүк ташуу":
            icon = "🚛"
            role_name = "ЖҮК ТАШУУ"
            text = (f"{icon} <b>{role_name}</b>\n\n"
                    f"📍 <b>Каяктан</b>: {origin}\n"
                    f"🏁 <b>Каякка</b>: {destination}\n"
                    f"🕒 <b>Убакыт</b>: {time}\n"
                    f"🚛 <b>Унаа</b>: {car_model}\n"
                    f"📦 <b>Жүк</b>: {cargo_type}\n"
                    f"💰 <b>Баасы</b>: {price}\n"
                    f"📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
                    f"👤 <b>Жарыя ээси</b>: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>")
            
        else:
            role_name = "АЙДООЧУ" if role == "айдоочу" else "ЖҮРГҮНЧҮ"
            icon = "🚕" if role == "айдоочу" else "👤"
            text = (f"{icon} <b>{role_name}</b>\n\n"
                    f"📍 <b>Каяктан</b>: {origin}\n"
                    f"🏁 <b>Каякка</b>: {destination}\n"
                    f"🕒 <b>Убакыт</b>: {time}\n")
            
            if role == "айдоочу":
                text += f"🚗 <b>Унаа</b>: {car_model}\n"
            text += f"💰 <b>Баасы</b>: {price}\n"

            label = 'Орун' if role == 'айдоочу' else 'Адам'
            text += (f"👥 <b>{label}</b>: {passenger_count}\n"
                     f"📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
                     f"👤 <b>{role_name.capitalize()}</b>: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>")

        # 6. Удаляем оригинальное сообщение юзера
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение (нужны права админа): {e}")

        # Считаем количество постов
        count_res = supabase.table(TAXI_TABLE).select("id", count="exact").eq("user_id", user_id).eq("role", role).execute()
        post_count = (count_res.count or 0) + 1

        # 7. Отправляем в канал/группу
        msg = await bot.send_message(chat_id=message.chat.id, text=text, parse_mode="HTML", reply_markup=get_channel_publish_kb())

        # 8. Сохраняем в Supabase
        db_payload = {
            "user_id": user_id, 
            "role": role, 
            "origin": origin,
            "destination": destination,
            "time": time, 
            "passenger_count": str(passenger_count) if role != "жүк ташуу" else cargo_type,
            "phone_num": phone, 
            "car_model": car_model, 
            "price": price, 
            "message_id": msg.message_id,
            "post_count": post_count,
            "created_at": datetime.datetime.now(TZ_BISHKEK).isoformat()
        }
        supabase.table(TAXI_TABLE).insert(db_payload).execute()

    except Exception as e:
        logging.error(f"Ошибка GPT: {e}")

@dp.message()
async def delete_all_other_messages(message: types.Message):
    try:
        await message.delete()
    except Exception as e:
        logging.warning(f"Не удалось удалить медиа: {e}")

# --- ЗАПУСК ---
async def main():
    await bot.set_my_commands([types.BotCommand(command="start", description="🚀 Баштоо")])
    asyncio.create_task(cleanup_old_messages())
    asyncio.create_task(weather_background_task(bot, CHANNEL_ID))
    
    await dp.start_polling(bot)

if __name__ == '__main__':
    try: 
        asyncio.run(main())
    except KeyboardInterrupt: 
        print("\nБот выключен")