import os
import re
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
from weather import weather_and_promo_task, build_weather_message, get_and_increment_weather_count

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


# --- КЛАССЫ СОСТОЯНИЙ ---
class BuyVIP(StatesGroup):
    waiting_for_car_photo = State()  # Теперь только фото машины, без чека

class AdminReject(StatesGroup):
    waiting_for_reason = State()


# =====================================================================
# --- МЭТЧИНГ: настройки, вспомогательные функции, рассылка в личку ---
# =====================================================================

# Какой роли соответствует "противоположная" роль для мэтчинга.
# Расширяется только на явные пары водитель <-> пассажир (по ТЗ).
OPPOSITE_ROLE = {
    "айдоочу": "жүргүнчү",
    "жүргүнчү": "айдоочу",
}

UNKNOWN_DESTINATION = "такталган жок"
MATCH_TIME_WINDOW_HOURS = 3        # окно совпадения по времени выезда
MATCH_SUBSCRIPTION_HOURS = 1       # сколько держится "подписка" на новые совпадения
MATCH_SEARCH_LOOKBACK_HOURS = 24   # как далеко в прошлое искать уже существующие объявления


def normalize_destination(text: str) -> str:
    """Приводит текст направления к сравнимому виду (регистр, скобки, пробелы)."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[().,!?\"']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def destinations_match(dest_a: str, dest_b: str) -> bool:
    """Совпадение по направлению 'куда едет'. Неизвестные направления не мэтчатся."""
    if not dest_a or not dest_b:
        return False
    if dest_a.strip().lower() == UNKNOWN_DESTINATION or dest_b.strip().lower() == UNKNOWN_DESTINATION:
        return False

    na, nb = normalize_destination(dest_a), normalize_destination(dest_b)
    if not na or not nb:
        return False

    return na == nb or na in nb or nb in na


def time_to_minutes(time_str: str):
    """Пытается вытащить HH:MM из свободного текста. Если не получилось — None (время 'гибкое')."""
    if not time_str:
        return None
    match = re.search(r"([01]?\d|2[0-3])[:.\-]([0-5]\d)", time_str)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute


def times_match(time_a: str, time_b: str, window_hours: int = MATCH_TIME_WINDOW_HOURS) -> bool:
    """Совпадение по времени в пределах окна. Если время не распарсилось — считаем 'гибким' (совпадает всегда)."""
    minutes_a, minutes_b = time_to_minutes(time_a), time_to_minutes(time_b)
    if minutes_a is None or minutes_b is None:
        return True
    diff = abs(minutes_a - minutes_b)
    diff = min(diff, 1440 - diff)  # на случай перехода через полночь
    return diff <= window_hours * 60


def build_match_notification_text(row: dict) -> str:
    """Формирует текст карточки объявления для рассылки в личку — в том же стиле, что и оригинальный пост в канале."""
    role = row.get("role")
    icon = "🚕" if role == "айдоочу" else "👤"
    role_name = "АЙДООЧУ" if role == "айдоочу" else "ЖҮРГҮНЧҮ"

    phone = row.get("phone_num") or "Номери жок"
    origin = row.get("origin") or "Такталган жок"
    destination = row.get("destination") or "Такталган жок"
    time_str = row.get("time") or "Сүйлөшүү боюнча"
    price = row.get("price") or "Келишим баада"
    car_model = row.get("car_model") or "Көрсөтүлгөн жок"
    passenger_count = row.get("passenger_count") or "Такталган жок"
    poster_user_id = row.get("user_id")
    poster_name = row.get("user_name")

    text = (
        f"🔔 <b>Сизге ылайыктуу жарыя табылды!</b>\n\n"
        f"{icon} <b>{role_name}</b>\n\n"
        f"📍 <b>Каяктан</b>: {origin}\n"
        f"🏁 <b>Каякка</b>: {destination}\n"
        f"🕒 <b>Убакыт</b>: {time_str}\n"
    )
    if role == "айдоочу":
        text += f"🚗 <b>Унаа</b>: {car_model}\n"

    label = "Орун" if role == "айдоочу" else "Адам"
    text += f"👥 <b>{label}</b>: {passenger_count}\n💰 <b>Баасы</b>: {price}\n"

    if phone and phone != "Номери жок":
        text += f"📞 <b>Тел.</b>: <a href='tel:{phone}'><code>{phone}</code></a>\n"
    else:
        text += f"📞 <b>Тел.</b>: {phone}\n"

    if poster_user_id:
        if poster_name:
            # Если имя есть, делаем ссылку с именем
            text += f"\n👤 <b>Байланышуу</b>: <a href='tg://user?id={poster_user_id}'>{poster_name}</a>"
        else:
            # Если имени нет (для старых записей), пишем стандартный текст
            text += f"\n👤 <b>Байланышуу</b>: <a href='tg://user?id={poster_user_id}'>Telegram-дан жазуу</a>"

    return text


async def find_matches(want_role: str, destination: str, time_str: str, since_iso: str, exclude_user_id: int):
    """
    Ищет объявления роли want_role, опубликованные не раньше since_iso,
    подходящие по направлению и времени. Используется и для 'найти уже
    существующие мэтчи', и для 'кто ещё не позже часа назад писал похожее'
    — разница только в since_iso.
    """
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TAXI_TABLE)
            .select("*, user_name")
            .eq("role", want_role)
            .not_.is_("message_id", "null")
            .gte("created_at", since_iso)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
    except Exception as e:
        logging.error(f"Ошибка поиска совпадений: {e}")
        return []

    matches = []
    for row in res.data or []:
        if row.get("user_id") == exclude_user_id:
            continue
        if not destinations_match(destination, row.get("destination")):
            continue
        if not times_match(time_str, row.get("time")):
            continue
        matches.append(row)
    return matches


async def send_match_notification(target_user_id: int, row: dict):
    text = build_match_notification_text(row)
    try:
        await bot.send_message(chat_id=target_user_id, text=text, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Не удалось отправить мэтч юзеру {target_user_id}: {e}")


async def handle_matching(new_row: dict, role: str, destination: str, time_str: str, user_id: int):
    """
    Главная точка входа: вызывается после публикации нового объявления айдоочу/жүргүнчү.
    Никаких отдельных таблиц не нужно — 'подписка на час' это просто фильтр
    по created_at существующей таблицы объявлений.
    """
    want_role = OPPOSITE_ROLE.get(role)
    if not want_role:
        return
    if not destination or destination.strip().lower() == UNKNOWN_DESTINATION:
        logging.info(f"[MATCH] Пост id={new_row.get('id')} role={role}: направление не определено — мэтчинг пропущен")
        return

    now = datetime.datetime.now(TZ_BISHKEK)
    lookback_since = (now - datetime.timedelta(hours=MATCH_SEARCH_LOOKBACK_HOURS)).isoformat()
    recent_cutoff = now - datetime.timedelta(hours=MATCH_SUBSCRIPTION_HOURS)

    candidates = await find_matches(want_role, destination, time_str, lookback_since, exclude_user_id=user_id)
    logging.info(f"[MATCH] Новый пост id={new_row.get('id')} role={role} dest={destination!r} time={time_str!r} "
                 f"user={user_id} -> найдено кандидатов ({want_role}): {len(candidates)}")

    # 1) Сразу шлём автору нового поста все уже существующие подходящие объявления
    for cand in candidates:
        try:
            await send_match_notification(user_id, cand)
            logging.info(f"[MATCH] Step1: отправлено user={user_id} <- пост id={cand.get('id')}")
        except Exception as e:
            logging.error(f"[MATCH] Step1: ошибка отправки user={user_id} <- пост id={cand.get('id')}: {e}")

    # 2) Тем из кандидатов, кто сам написал своё объявление не позже часа назад,
    #    отправляем именно этот новый пост — это и есть "подписка на час":
    #    как только истечёт час с их публикации, они перестанут сюда попадать.
    already_notified = set()
    for cand in candidates:
        try:
            cand_created_raw = cand.get("created_at")
            if not cand_created_raw:
                continue
            cand_created_at = datetime.datetime.fromisoformat(str(cand_created_raw).replace('Z', '+00:00'))
            if cand_created_at.tzinfo is None:
                # На случай, если Supabase вернул время без таймзоны — считаем его бишкекским.
                cand_created_at = cand_created_at.replace(tzinfo=TZ_BISHKEK)

            if cand_created_at < recent_cutoff:
                continue  # этот кандидат уже вне своего часового окна

            cand_user_id = cand.get("user_id")
            if not cand_user_id or cand_user_id in already_notified:
                continue
            already_notified.add(cand_user_id)

            await send_match_notification(cand_user_id, new_row)
            logging.info(f"[MATCH] Step2: отправлено user={cand_user_id} <- новый пост id={new_row.get('id')}")
        except Exception as e:
            logging.error(f"[MATCH] Step2: ошибка обработки кандидата id={cand.get('id')}: {e}")


# --- ФОНОВАЯ ЗАДАЧА: ОЧИСТКА СТАРЫХ ПОСТОВ (3 СУТОК) ---
async def cleanup_old_messages():
    while True:
        try:
            three_days_ago = (datetime.datetime.now(TZ_BISHKEK) - datetime.timedelta(days=3)).isoformat()
            
            res = await asyncio.to_thread(
                lambda: supabase.table(TAXI_TABLE).select("id", "message_id").lt("created_at", three_days_ago).not_.is_("message_id", "null").execute()
            )
            
            for record in res.data:
                try:
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=record["message_id"])
                except:
                    pass
                
                await asyncio.to_thread(
                    lambda r=record: supabase.table(TAXI_TABLE).update({"message_id": None}).eq("id", r["id"]).execute()
                )
        except Exception as e:
            logging.error(f"Ошибка очистки: {e}")
        finally:
            await asyncio.sleep(3600)

# --- КНОПКА ПОД ПОСТОМ ---
def get_channel_publish_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🌤 Погода / Аба ырайы", url=f"{BOT_LINK}?start=show_weather"))
    builder.row(types.InlineKeyboardButton(text="👑 Сүрөт кошуу (1 Жылга Бекер!)", url=f"{BOT_LINK}?start=buy_vip"))
    return builder.as_markup()

# --- КОМАНДА /start ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    if message.text and "show_weather" in message.text:
        try:
            status_msg = await message.answer("⏳ Аба ырайы тууралуу маалымат алынууда...")
            
            weather_text = await build_weather_message()
            current_count = get_and_increment_weather_count()
            
            if ADMIN_ID and message.from_user.id == ADMIN_ID:
                weather_text += f"\n\n📊 <b>Статистика админа:</b>\n<i>Бул баскычты бот иштегени <b>{current_count} жолу</b> басышты.</i>"

            await status_msg.edit_text(weather_text, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка при загрузке погоды: {e}")
            
    elif message.text and "buy_vip" in message.text:
        text = (
            "👑 <b>Сүрөтү менен жарыя киргизүү — 1 ЖЫЛГА БЕКЕР!</b>\n\n"
            "Унааңыздын сүрөтүн кошуп, жарыяларыңызды чектөөсүз жана сүрөтүңүз менен жарыялаңыз!\n\n"
            "📸 <b>Унааңыздын реалдуу сүрөтүн жөнөтүңүз</b> — админ текшерип, "
            "сизге 1 жылга бекер активациялап берет.\n\n"
            "👇 Азыр эле унааңыздын сүрөтүн жибериңиз:"
        )
        await message.answer(text, parse_mode="HTML")
        await state.set_state(BuyVIP.waiting_for_car_photo)
        
    else:
        await message.answer("👋 Саламатсызбы! Мен группадан жарыяларды автоматтык түрдө түзүүчү ботмун.")

# --- ЗАЩИТА ОТ ДУРАКА: Ловим PDF, файлы, текст и стикеры ---
@dp.message(~F.photo, StateFilter(BuyVIP.waiting_for_car_photo))
async def handle_invalid_format(message: types.Message):
    await message.answer(
        "⚠️ <b>Кечиресиз, файл, PDF же текст кабыл алынбайт.</b>\n\n"
        "Сураныч, унааңыздын кадимки <b>сүрөтүн (скриншот эмес, реалдуу фото)</b> жөнөтүңүз 📸", 
        parse_mode="HTML"
    )

# --- ТЕПЕРЬ ТОЛЬКО ОДНО СОСТОЯНИЕ: ФОТО МАШИНЫ ---
@dp.message(F.photo, StateFilter(BuyVIP.waiting_for_car_photo))
async def handle_car_photo(message: types.Message, state: FSMContext):
    car_photo_id = message.photo[-1].file_id
    user_id = message.from_user.id
    username = message.from_user.username or "Без юзернейма"
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Одобрить (1 год)", callback_data=f"apprvip_365_{user_id}"),
    )
    builder.row(types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_vip_{user_id}"))
    
    admin_text = (
        f"🆕 <b>Заявка на бесплатный VIP (1 год)!</b>\n"
        f"👤 Юзер: @{username} (<code>{user_id}</code>)\n\n"
        f"Проверь фото авто — реальное ли это фото машины?"
    )
    
    if ADMIN_ID:
        await bot.send_photo(chat_id=ADMIN_ID, photo=car_photo_id, caption="🚗 ФОТО АВТО")
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=builder.as_markup())
        
        await asyncio.to_thread(
            lambda: supabase.table("premium_drivers").upsert({
                "user_id": user_id,
                "photo_file_id": car_photo_id
            }).execute()
        )

    await message.answer(
        "✅ <b>Рахмат! Сиздин унааңыздын сүрөтү жөнөтүлдү.</b>\n\n"
        "⏳ Администратор текшерип, 1 жылга акысыз активациялап берет. "
        "Тастыктагандан кийин сизге билдирүү келет.",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("apprvip_"))
async def admin_approve_vip(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    days_to_add = int(parts[1])  # Всегда 365
    user_id = int(parts[2])
    
    now = datetime.datetime.now(TZ_BISHKEK)
    
    user_data = await asyncio.to_thread(
        lambda: supabase.table("premium_drivers").select("expires_at").eq("user_id", user_id).execute()
    )
    
    new_expires_at = now + datetime.timedelta(days=days_to_add)
    
    if user_data.data and user_data.data[0].get("expires_at"):
        old_expires_str = user_data.data[0]["expires_at"]
        old_expires_date = datetime.datetime.fromisoformat(old_expires_str.replace('Z', '+00:00'))
        
        if old_expires_date > now:
            # Продлеваем от текущей даты окончания
            new_expires_at = old_expires_date + datetime.timedelta(days=days_to_add)
            
    expires_at_iso = new_expires_at.isoformat()
    
    await asyncio.to_thread(
        lambda: supabase.table("premium_drivers").update({
            "expires_at": expires_at_iso
        }).eq("user_id", user_id).execute()
    )
    
    await callback.message.edit_text(
        f"✅ Водитель {user_id} одобрен! VIP активен до {expires_at_iso[:10]} (1 год)."
    )
    
    try:
        await bot.send_message(
            chat_id=user_id, 
            text=(
                f"🎉 <b>Куттуктайбыз!</b> Сиздин өтүнүчүңүз тастыкталды.\n\n"
                f"👑 Сизге <b>1 жылга бекер VIP</b> берилди! "
                f"(<b>{expires_at_iso[:10]}</b> күнүнө чейин)\n\n"
                f"Эми жарыяларыңыз чектөөсүз жана унааңыздын сүрөтү менен чыгат! 🚗"
            ), 
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не смогли отправить юзеру сообщение: {e}")

@dp.callback_query(F.data.startswith("reject_vip_"))
async def admin_reject_vip(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    
    await state.update_data(reject_user_id=user_id)
    await state.set_state(AdminReject.waiting_for_reason)
    
    await callback.message.answer(
        "✍️ <b>Напиши причину отказа текстом</b>\n"
        "(например: 'Бул унаанын реалдуу сүрөтү эмес' же 'Сүрөт өтө жарык эмес, кайра жибериңиз').\n"
        "<i>Бул текст түз эле айдоочуга барат.</i>", 
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(StateFilter(AdminReject.waiting_for_reason))
async def handle_reject_reason(message: types.Message, state: FSMContext):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
        
    data = await state.get_data()
    user_id = data.get("reject_user_id")
    admin_reason = message.text
    
    await asyncio.to_thread(
        lambda: supabase.table("premium_drivers").delete().eq("user_id", user_id).execute()
    )
    
    user_text = (
        "❌ <b>Кечиресиз, сиздин өтүнүчүңүз четке кагылды.</b>\n\n"
        f"💬 <b>Админдин комментарийи:</b>\n<i>{admin_reason}</i>\n\n"
        "Сураныч, унааңыздын реалдуу сүрөтүн жөнөтүп, кайрадан аракет кылыңыз."
    )
    
    try:
        await bot.send_message(chat_id=user_id, text=user_text, parse_mode="HTML")
        await message.answer(f"✅ Причина отправлена юзеру <code>{user_id}</code>, заявка удалена.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки юзеру (возможно, он заблокировал бота): {e}")
        
    await state.clear()

# --- АДМИН ПАНЕЛЬ: ДОБАВЛЕНИЕ VIP ВОДИТЕЛЕЙ ВРУЧНУЮ ---
@dp.message(F.photo & F.caption.startswith('/addvip'))
async def add_vip_driver(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    try:
        driver_id = int(message.caption.split()[1])
        photo_id = message.photo[-1].file_id
        
        now = datetime.datetime.now(TZ_BISHKEK)
        expires_at = (now + datetime.timedelta(days=365)).isoformat()
        
        await asyncio.to_thread(
            lambda: supabase.table("premium_drivers").upsert({
                "user_id": driver_id,
                "photo_file_id": photo_id,
                "expires_at": expires_at
            }).execute()
        )
        
        await message.reply(
            f"✅ Водитель <code>{driver_id}</code> добавлен в VIP на 1 год (до {expires_at[:10]})!",
            parse_mode="HTML"
        )
    
    except IndexError:
        await message.reply("❌ Формат: отправь фото, в подписи напиши:\n`/addvip ID_ВОДИТЕЛЯ`", parse_mode="Markdown")
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
async def process_and_publish_ad(text_to_analyze: str, message: types.Message):
    user_id = message.from_user.id
    
    prompt = f"""
    Проанализируй текст объявления из кыргызской/русской группы такси: "{text_to_analyze}"
    
    Задача: Разобрать текст и строго вернуть JSON. 
    
    ПРАВИЛА ОПРЕДЕЛЕНИЯ РОЛИ:
    1. "жүргүнчү" (Пассажир - у него НЕТ машины, он хочет уехать):
    - Фразы: "бир адам кетет", "1 адам кетет", "барат", "кетем", "нужна машина".
    - Важно: Если человек пишет маршрут и просто "кетет" без указания машины — он пассажир!
    
    2. "айдоочу" (Водитель - у него ЕСТЬ машина):
    - Фразы: "киши керек", "адам керек", "орун бар", "салон бош", "Кто: Водитель".
    - Наличие ЛЮБОЙ марки авто (K5, Камри, BYD, Grandeur, Малибу, Степ и т.д.) = ВОДИТЕЛЬ.
    
    3. "посылка" (Передача мелких вещей, документов, сумок):
    - Фразы: "передача бар", "посылка", "документ", "передать", "сумка берем".
    
    4. "жүк ташуу" (Грузоперевозки - тяжелый груз, мебель, переезды):
    - Клиент (ищет грузовик): "жүк бар", "портер керек", "газель керек", "көчүш керек".
    - Водитель грузовика (ищет груз): "портер бар", "жүк алам", "бош портер", "газель".

    Верни JSON:
    {{
      "is_ad": boolean,
      "role": string or null,
      "origin": string or null,
      "destination": string or null,
      "time": string or null,
      "price": string or null,
      "passenger_count": string or null,
      "cargo_type": string or null,
      "phone_number": string or null,
      "car_model": string or null
    }}
    """

    try:
        response = await aclient.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        
        parsed_data = json.loads(response.choices[0].message.content)
        
        if not parsed_data.get("is_ad"):
            return "SPAM"

        role = parsed_data.get("role")
        
        text_lower = text_to_analyze.lower()
        if role == "айдоочу" and any(word in text_lower for word in ["адам кетет", "барам", "кетем", "адам барат"]):
            if not parsed_data.get("car_model"): 
                role = "жүргүнчү"

        if not role:
            return "SPAM"

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

        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        now = datetime.datetime.now(TZ_BISHKEK)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        daily_count_res = await asyncio.to_thread(
            lambda: supabase.table(TAXI_TABLE).select("id", count="exact")
            .eq("user_id", user_id).eq("role", role).gte("created_at", start_of_day).execute()
        )
        posts_today = daily_count_res.count or 0

        # Проверяем VIP статус
        is_vip = False
        photo_file_id = None
        
        vip_res = await asyncio.to_thread(
            lambda: supabase.table("premium_drivers").select("photo_file_id, expires_at").eq("user_id", user_id).execute()
        )
        
        if vip_res.data:
            expires_at_str = vip_res.data[0].get("expires_at")
            if expires_at_str:
                expires_at = datetime.datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                if expires_at > now:
                    is_vip = True
                    photo_file_id = vip_res.data[0]["photo_file_id"]

        # Лимит для не-VIP водителей
        daily_limit = 10  # Попутчики — без строгих ограничений

        if not is_vip and role in ["айдоочу", "жүк ташуу"]:
            if posts_today >= daily_limit:
                role_display = "унааңыздын" if role == "айдоочу" else "жүк ташуучу унааңыздын"
                limit_text = (
                    f"🛑 <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>, "
                    f"<b>Сиздин бүгүнкү акысыз лимитиңиз бүттү ({daily_limit}/{daily_limit}).</b>\n\n"
                    f"Жарыяңыз киргизилген жок. Чектөөсүз жарыя жазуу жана "
                    f"<b>{role_display} сүрөтүн</b> кошуу үчүн — "
                    f"унааңыздын сүрөтүн жөнөтсөңүз болот, <b>1 жылга бекер!</b>\n\n"
                    "👇 Төмөнкү баскычты басыңыз:"
                )
                
                limit_builder = InlineKeyboardBuilder()
                limit_builder.row(types.InlineKeyboardButton(
                    text="👑 Сүрөт кошуу (1 Жылга Бекер!)",
                    url=f"{BOT_LINK}?start=buy_vip"
                ))
                
                warning_msg = await bot.send_message(
                    chat_id=message.chat.id, text=limit_text,
                    parse_mode="HTML", reply_markup=limit_builder.as_markup()
                )
                
                async def delete_warning(chat_id, msg_id):
                    await asyncio.sleep(120)
                    try:
                        await bot.delete_message(chat_id, msg_id)
                    except:
                        pass
                asyncio.create_task(delete_warning(warning_msg.chat.id, warning_msg.message_id))
                
                return "LIMIT_REACHED"

        count_res = await asyncio.to_thread(
            lambda: supabase.table(TAXI_TABLE).select("id", count="exact").eq("user_id", user_id).eq("role", role).execute()
        )
        post_count = (count_res.count or 0) + 1

        if role in ["айдоочу", "жүк ташуу"]:
            if is_vip:
                text += "\n\n<i>👑 Сизде VIP-статус (чектөөсүз)</i>"
            else:
                remaining = daily_limit - (posts_today + 1)
                text += f"\n\n<i>⚠️ Бүгүнкү акысыз жарыялар: {remaining}/{daily_limit} калды</i>"

        # Публикуем пост
        if is_vip and role == "айдоочу" and photo_file_id:
            try:
                msg = await bot.send_photo(
                    chat_id=message.chat.id, photo=photo_file_id,
                    caption=text, parse_mode="HTML", reply_markup=get_channel_publish_kb()
                )
            except:
                msg = await bot.send_message(
                    chat_id=message.chat.id, text=text,
                    parse_mode="HTML", reply_markup=get_channel_publish_kb()
                )
        else:
            msg = await bot.send_message(
                chat_id=message.chat.id, text=text,
                parse_mode="HTML", reply_markup=get_channel_publish_kb()
            )
  
        db_payload = {
            "user_id": user_id, "user_name": message.from_user.full_name, "role": role, "origin": origin, "destination": destination,
            "time": time, "passenger_count": str(passenger_count) if role != "жүк ташуу" else cargo_type,
            "phone_num": phone, "car_model": car_model, "price": price,
            "message_id": msg.message_id, "post_count": post_count,
            "created_at": now.isoformat()
        }
        
        insert_res = await asyncio.to_thread(
            lambda: supabase.table(TAXI_TABLE).insert(db_payload).execute()
        )

        # --- МЭТЧИНГ: сразу шлём подходящие объявления + подписка на час ---
        if role in OPPOSITE_ROLE:
            inserted_row = (insert_res.data or [db_payload])[0]

        if not inserted_row.get("user_name"):
                inserted_row["user_name"] = message.from_user.full_name
                
            asyncio.create_task(
                handle_matching(inserted_row, role, destination, time, user_id)
            )

        return "SUCCESS"

    except Exception as e:
        logging.error(f"Ошибка GPT: {e}")
        return "ERROR"

# =====================================================================
# --- ХЭНДЛЕР №1: Ловит ОБЫЧНЫЕ сообщения (и текст, и фото с текстом) ---
# =====================================================================
@dp.message((F.text | F.caption) & ~F.text.startswith('/'), StateFilter(None))
async def handle_new_ad(message: types.Message, state: FSMContext):
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        return
        
    text_to_process = message.text or message.caption
    
    if not text_to_process:
        return
        
    text_lower = text_to_process.lower()
    
    if "http" in text_lower or "t.me" in text_lower or "www." in text_lower or len(text_to_process.split()) < 3:
        try:
            await message.delete()
        except:
            pass
        return 

    status = await process_and_publish_ad(text_to_process, message)

    if status == "SPAM":
        try:
            await message.delete()
        except:
            pass
    elif status == "ERROR":
        logging.error("Объявление не обработано из-за ошибки GPT/БД")


# --- УДАЛЕНИЕ МУСОРА (Стикеры, фото без текста, видео и т.д.) ---
@dp.message()
async def delete_all_other_messages(message: types.Message):
    if message.chat.type == 'private':
        return
        
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        return
        
    try:
        await message.delete()
    except Exception as e:
        logging.warning(f"Не удалось удалить медиа/мусор: {e}")

# --- ЗАПУСК ---
async def main():
    await bot.set_my_commands([types.BotCommand(command="start", description="🚀 Баштоо")])
    await bot.delete_webhook(drop_pending_updates=True)
    
    asyncio.ensure_future(cleanup_old_messages())
    asyncio.ensure_future(weather_and_promo_task(bot, CHANNEL_ID))

    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот выключен")