import os
import logging
import asyncio
import datetime
import json
from dotenv import load_dotenv

# Aiogram импорты
from aiogram.filters import Command, StateFilter
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импорты для Машины Состояний (FSM)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# Базы данных и API
from supabase import create_client, Client
from openai import AsyncOpenAI

# Погода
from weather import weather_background_task, build_weather_message

admin_id_raw = os.getenv("ADMIN_ID")
ADMIN_ID = int(admin_id_raw) if admin_id_raw else None

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

# --- КЛАСС СОСТОЯНИЙ ДЛЯ УТОЧНЕНИЯ РОЛИ ---
class AdClarification(StatesGroup):
    waiting_for_role = State()


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

# --- КОМАНДА /start ---
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

# --- АДМИН ПАНЕЛЬ: ДОБАВЛЕНИЕ VIP ВОДИТЕЛЕЙ ---
@dp.message(F.photo & F.caption.startswith('/addvip'))
async def add_vip_driver(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    
    try:
        driver_id = int(message.caption.split()[1])
        photo_id = message.photo[-1].file_id
        
        supabase.table("premium_drivers").upsert({
            "user_id": driver_id,
            "photo_file_id": photo_id
        }).execute()
        
        await message.reply(f"✅ Водитель <code>{driver_id}</code> успешно добавлен в VIP-базу с этим фото!", parse_mode="HTML")
    
    except IndexError:
        await message.reply("❌ Ошибка формата. Отправь фото, а в подписи (caption) напиши:\n`/addvip ID_ВОДИТЕЛЯ`", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ Ошибка при добавлении в базу: {e}")


# --- КОМАНДА /id ---
@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    env_id = os.getenv("CHANNEL_ID")
    chat_id = message.chat.id
    is_match = str(chat_id) == str(env_id).strip()
    
    text = (
        f"🔎 <b>ТЕСТ ID</b>\n\n"
        f"ID этого чата: <code>{chat_id}</code>\n"
        f"ID в Railway: <code>{env_id}</code>\n"
        f"Они абсолютно равны? <b>{'✅ ДА' if is_match else '❌ НЕТ'}</b>"
    )
    await message.answer(text, parse_mode="HTML")


# =====================================================================
# --- ФУНКЦИЯ ДЛЯ ПАРСИНГА И ПУБЛИКАЦИИ ---
# =====================================================================
async def process_and_publish_ad(text_to_analyze: str, message: types.Message, msgs_to_delete: list = None):
    user_id = message.from_user.id
    
    prompt = f"""
    Проанализируй текст: "{text_to_analyze}"
    ВАЖНЫЕ ПРАВИЛА ДЛЯ КЫРГЫЗСКОГО ЯЗЫКА (ТАКСИ/ПОПУТКИ):
    ВНИМАНИЕ: Слова "кетем", "барам", "чыгам", "чыгабыз" используют И ВОДИТЕЛИ, И ПАССАЖИРЫ. Вообще не используй их для определения роли! Смотри только на следующие маркеры:

    1. АЙДООЧУ (Водитель): У него есть машина, он берет попутчиков. 
       Его маркеры: "адам керек", "киши керек", "алам", "алып кетем", "орун бар", "бош орун". Или упоминание своего авто: "женил машина", "камри", "портер" и т.д. 
       ПРАВИЛО: Если в тексте есть "керек" по отношению к людям (адам/киши), "орун бар" или марка машины — это СТРОГО "айдоочу"!

    2. ЖҮРГҮНЧҮ (Пассажир): У него НЕТ машины, он хочет уехать. 
       Его маркеры: "машина керек", "такси керек", "биз 2 адамбыз", "2 адам кетет". 
       ПРИ ЭТОМ они НЕ предлагают места ("орун бар") и НЕ ищут людей ("адам керек").

    3. УТОЧНЕНИЕ: Если из текста ВООБЩЕ НЕПОНЯТНО, кто это (например, просто "Таласка 2 адам 00:00" без слов "керек", "машина", "алам" и т.д.), верни null в поле "role". НЕ УГАДЫВАЙ!
   
    Верни строго JSON:
    "is_ad": boolean,
    "role": string ("айдоочу", "жүргүнчү", "посылка", "жүк ташуу" или null),
    "origin": string (откуда, или null),
    "destination": string (куда, или null),
    "time": string (время, или null),
    "price": string (цена, или null),
    "passenger_count": string (количество мест/людей, или null),
    "cargo_type": string (или null),
    "phone_number": string (или null),
    "car_model": string (или null)
    """

    try:
        response = await aclient.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        
        parsed_data = json.loads(response.choices[0].message.content)
        
        # Если это не реклама поездки - возвращаем статус спама
        if not parsed_data.get("is_ad"):
            return "SPAM"

        role = parsed_data.get("role")
        
        # Если GPT сомневается и вернул null - требуем уточнения
        if not role:
            return "NEEDS_CLARIFICATION"

        # Собираем данные
        phone = parsed_data.get("phone_number") or "Номери жок"
        car_model = parsed_data.get("car_model") or "Көрсөтүлгөн жок"
        origin = parsed_data.get("origin") or "Такталган жок"
        destination = parsed_data.get("destination") or "Такталган жок"
        time = parsed_data.get("time") or "Сүйлөшүү боюнча"
        price = parsed_data.get("price") or "Келишим баада"
        passenger_count = parsed_data.get("passenger_count") or "Такталган жок"
        cargo_type = parsed_data.get("cargo_type") or "Такталган жок"

        clean_phone = phone.replace(" ", "").replace("-", "")
        if clean_phone and not clean_phone.startswith('+') and clean_phone.replace('+','').isdigit():
            clean_phone = '+' + clean_phone

        # Формируем красивый текст
        if role == "посылка":
            icon, role_name = "📦", "ПОСЫЛКА"
            text = (f"{icon} <b>{role_name}</b>\n\n"
                    f"📤 <b>Каяктан</b>: {origin}\n📥 <b>Каякка</b>: {destination}\n🕒 <b>Убакыт</b>: {time}\n"
                    f"📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
                    f"👤 <b>Жөнөтүүчү</b>: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>")
        elif role == "жүк ташуу":
            icon, role_name = "🚛", "ЖҮК ТАШУУ"
            text = (f"{icon} <b>{role_name}</b>\n\n"
                    f"📍 <b>Каяктан</b>: {origin}\n🏁 <b>Каякка</b>: {destination}\n🕒 <b>Убакыт</b>: {time}\n"
                    f"🚛 <b>Унаа</b>: {car_model}\n📦 <b>Жүк</b>: {cargo_type}\n💰 <b>Баасы</b>: {price}\n"
                    f"📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
                    f"👤 <b>Жарыя ээси</b>: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>")
        else:
            role_name = "АЙДООЧУ" if role == "айдоочу" else "ЖҮРГҮНЧҮ"
            icon = "🚕" if role == "айдоочу" else "👤"
            text = (f"{icon} <b>{role_name}</b>\n\n📍 <b>Каяктан</b>: {origin}\n🏁 <b>Каякка</b>: {destination}\n🕒 <b>Убакыт</b>: {time}\n")
            if role == "айдоочу":
                text += f"🚗 <b>Унаа</b>: {car_model}\n"
            text += f"💰 <b>Баасы</b>: {price}\n"
            label = 'Орун' if role == 'айдоочу' else 'Адам'
            text += (f"👥 <b>{label}</b>: {passenger_count}\n📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
                     f"👤 <b>{role_name.capitalize()}</b>: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>")

        # Удаляем черновики (само сообщение юзера и вопросы бота, если они были)
        if msgs_to_delete is None:
            msgs_to_delete = [message.message_id]
        
        for msg_id in msgs_to_delete:
            try:
                await bot.delete_message(message.chat.id, msg_id)
            except Exception:
                pass

        # Аналитика постов
        count_res = supabase.table(TAXI_TABLE).select("id", count="exact").eq("user_id", user_id).eq("role", role).execute()
        post_count = (count_res.count or 0) + 1

        # Проверка на VIP
        vip_res = supabase.table("premium_drivers").select("photo_file_id").eq("user_id", user_id).execute()
        
        if vip_res.data and role == "айдоочу":
            photo_file_id = vip_res.data[0]["photo_file_id"]
            try:
                msg = await bot.send_photo(chat_id=message.chat.id, photo=photo_file_id, caption=text, parse_mode="HTML", reply_markup=get_channel_publish_kb())
            except:
                msg = await bot.send_message(chat_id=message.chat.id, text=text, parse_mode="HTML", reply_markup=get_channel_publish_kb())
        else:
            msg = await bot.send_message(chat_id=message.chat.id, text=text, parse_mode="HTML", reply_markup=get_channel_publish_kb())

        # Сохранение в БД
        db_payload = {
            "user_id": user_id, "role": role, "origin": origin, "destination": destination,
            "time": time, "passenger_count": str(passenger_count) if role != "жүк ташуу" else cargo_type,
            "phone_num": phone, "car_model": car_model, "price": price,
            "message_id": msg.message_id, "post_count": post_count,
            "created_at": datetime.datetime.now(TZ_BISHKEK).isoformat()
        }
        supabase.table(TAXI_TABLE).insert(db_payload).execute()
        
        return "SUCCESS"

    except Exception as e:
        logging.error(f"Ошибка GPT: {e}")
        return "ERROR"

# =====================================================================
# --- ХЭНДЛЕР №1: Ловит ОБЫЧНЫЕ сообщения ---
# =====================================================================
@dp.message(F.text & ~F.text.startswith('/'), StateFilter(None))
async def handle_new_ad(message: types.Message, state: FSMContext):
    text_lower = message.text.lower()
    
    # 1. Фильтр коротких сообщений и ссылок (АДМИНА НЕ ТРОГАЕМ)
    if "http" in text_lower or "t.me" in text_lower or "www." in text_lower or len(message.text.split()) < 3:
        if not ADMIN_ID or message.from_user.id != ADMIN_ID:
            try:
                await message.delete()
            except:
                pass
        return 

    # Отправляем в функцию парсинга
    status = await process_and_publish_ad(message.text, message)

    if status == "NEEDS_CLARIFICATION":
        bot_msg = await message.reply("🤔 Урматтуу колдонуучу, сиз **айдоочусузбу** же **жүргүнчүсүзбү**? (Ушул смске жооп жазып, тактап коюңуз)", parse_mode="Markdown")
        await state.set_state(AdClarification.waiting_for_role)
        await state.update_data(
            original_text=message.text,
            original_msg_id=message.message_id,
            bot_msg_id=bot_msg.message_id
        )
    # 2. Если GPT решил, что это просто спам/общение (АДМИНА НЕ ТРОГАЕМ)
    elif status == "SPAM":
        if not ADMIN_ID or message.from_user.id != ADMIN_ID:
            try:
                await message.delete()
            except:
                pass

# =====================================================================
# --- ХЭНДЛЕР №2: Ловит ОТВЕТ пользователя на вопрос бота ---
# =====================================================================
@dp.message(StateFilter(AdClarification.waiting_for_role))
async def handle_clarification_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    original_text = data.get("original_text")
    original_msg_id = data.get("original_msg_id")
    bot_msg_id = data.get("bot_msg_id")

    combined_text = f"Оригинальное сообщение: {original_text}\nУточнение от пользователя: {message.text}"
    msgs_to_delete = [original_msg_id, bot_msg_id, message.message_id]
    
    await state.clear()
    await process_and_publish_ad(combined_text, message, msgs_to_delete)

# --- УДАЛЕНИЕ МУСОРА (Стикеры, фото, видео и т.д.) ---
@dp.message()
async def delete_all_other_messages(message: types.Message):
    if message.chat.type == 'private':
        return
        
    # 3. Админ может скидывать любые фото/стикеры/видео (АДМИНА НЕ ТРОГАЕМ)
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        return
        
    try:
        await message.delete()
    except Exception as e:
        logging.warning(f"Не удалось удалить медиа/мусор: {e}")

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