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

class BuyVIP(StatesGroup):
    waiting_for_receipt = State()
    waiting_for_car_photo = State()

class AdminReject(StatesGroup):
    waiting_for_reason = State()


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
    # Добавляем кнопку покупки VIP, которая перекинет юзера в личку с ботом
    builder.row(types.InlineKeyboardButton(text="👑 VIP алуу (Сүрөт кошуу)", url=f"{BOT_LINK}?start=buy_vip"))
    return builder.as_markup()

# --- КОМАНДА /start ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    if message.text and "show_weather" in message.text:
        try:
            status_msg = await message.answer("⏳ Аба ырайы тууралуу маалымат алынууда...")
            weather_text = await build_weather_message()
            await status_msg.edit_text(weather_text, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка при загрузке погоды: {e}")
            
    elif message.text and "buy_vip" in message.text:
        # Юзер нажал кнопку купить VIP
        text = (
            "👑 <b>VIP-статус сатып алуу</b>\n\n"
            "VIP-статус сизге чектөөсүз жарыя киргизүүгө жана <b>унааңыздын сүрөтүн</b> кошууга мүмкүнчүлүк берет!\n\n"
            "💳 <b>Баасы:</b> 200 сом (1 айга)\n"
            "🏦 <b>MBank номери:</b> <code>+996555905044</code> (Аты-жөнү: Ильяс Р.)\n\n"
            "👇 Төлөмдү жүргүзгөндөн кийин, <b>чекдин сүрөтүн ушул жакка жөнөтүңүз</b>."
        )
        # Если есть фото QR-кода, лучше отправлять его (укажи file_id или URL)
        # await message.answer_photo(photo="URL_ИЛИ_ID_QR_КОДА", caption=text, parse_mode="HTML")
        await message.answer(text, parse_mode="HTML")
        await state.set_state(BuyVIP.waiting_for_receipt)
        
    else:
        await message.answer("👋 Саламатсызбы! Мен группадан жарыяларды автоматтык түрдө түзүүчү ботмун.")

# --- ЗАЩИТА ОТ ДУРАКА: Ловим PDF, файлы, текст и стикеры ---
@dp.message(~F.photo, StateFilter(BuyVIP.waiting_for_receipt, BuyVIP.waiting_for_car_photo))
async def handle_invalid_format(message: types.Message):
    await message.answer(
        "⚠️ <b>Кечиресиз, файл, PDF же текст кабыл алынбайт.</b>\n\n"
        "Сураныч, чекти кадимки <b>сүрөт (скриншот)</b> кылып жөнөтүңүз 📸", 
        parse_mode="HTML"
    )

@dp.message(F.photo, StateFilter(BuyVIP.waiting_for_receipt))
async def handle_receipt(message: types.Message, state: FSMContext):
    receipt_photo_id = message.photo[-1].file_id
    await state.update_data(receipt_photo_id=receipt_photo_id)
    
    await message.answer("✅ Чек кабыл алынды!\n\n🚗 Эми <b>унааңыздын сүрөтүн</b> жөнөтүңүз (бул сүрөт жарыяларыңызга кошулат).", parse_mode="HTML")
    await state.set_state(BuyVIP.waiting_for_car_photo)


@dp.message(F.photo, StateFilter(BuyVIP.waiting_for_car_photo))
async def handle_car_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    receipt_photo_id = data.get("receipt_photo_id")
    car_photo_id = message.photo[-1].file_id
    user_id = message.from_user.id
    username = message.from_user.username or "Без юзернейма"
    
    # Клавиатура для админа
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Одобрить (30 дней)", callback_data=f"approve_vip_{user_id}"),
        types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_vip_{user_id}")
    )
    
    admin_text = f"🆕 <b>Заявка на VIP!</b>\n👤 Юзер: @{username} (<code>{user_id}</code>)\n\nВложил 2 фото (чек и авто). Выбери действие:"
    
    # Отправляем админу группу медиа (чек + фото авто)
    media = [
        types.InputMediaPhoto(media=receipt_photo_id, caption="📸 ЧЕК"),
        types.InputMediaPhoto(media=car_photo_id, caption="🚗 ФОТО АВТО")
    ]
    if ADMIN_ID:
        await bot.send_media_group(chat_id=ADMIN_ID, media=media)
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=builder.as_markup())
        
        # Сохраняем file_id авто во временную память админа, чтобы при одобрении записать в БД
        # Простой вариант - передать в callback_data нельзя из-за лимита байтов, поэтому сохраняем через Redis/MemoryStorage админа, 
        # но для MVP мы сохраним авто-фото прямо в Supabase со статусом "pending", либо используем кэш. 
        # Для простоты: запишем в Supabase сразу, но VIP не активен, пока нет expires_at.
        supabase.table("premium_drivers").upsert({
            "user_id": user_id,
            "photo_file_id": car_photo_id
        }).execute()

    await message.answer("⏳ Рахмат! Сиздин маалыматыңыз текшерүүгө жөнөтүлдү. Администратор тастыктагандан кийин сизге билдирүү келет.")
    await state.clear()



@dp.callback_query(F.data.startswith("approve_vip_"))
async def admin_approve_vip(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    now = datetime.datetime.now(TZ_BISHKEK)
    
    # 1. Получаем текущие данные водителя из базы
    user_data = supabase.table("premium_drivers").select("expires_at").eq("user_id", user_id).execute()
    
    # 2. Логика продления (суммируем дни, если VIP еще активен)
    new_expires_at = now + datetime.timedelta(days=30) # По умолчанию: +30 дней от сегодня
    
    if user_data.data and user_data.data[0].get("expires_at"):
        old_expires_str = user_data.data[0]["expires_at"]
        old_expires_date = datetime.datetime.fromisoformat(old_expires_str.replace('Z', '+00:00'))
        
        # Если старый VIP еще НЕ истек, прибавляем 7 дней к ОСТАТКУ
        if old_expires_date > now:
            new_expires_at = old_expires_date + datetime.timedelta(days=30)
            
    expires_at_iso = new_expires_at.isoformat()
    
    # 3. Обновляем базу данных новой датой
    supabase.table("premium_drivers").update({
        "expires_at": expires_at_iso
    }).eq("user_id", user_id).execute()
    
    await callback.message.edit_text(f"✅ Водитель {user_id} успешно получил/продлил VIP до {expires_at_iso[:10]}!")
    
    # 4. Отправляем уведомление с новой датой
    try:
        await bot.send_message(
            chat_id=user_id, 
            text=f"🎉 <b>Куттуктайбыз!</b> Сиздин төлөмүңүз тастыкталды.\n\nСиздин VIP-статусуңуз <b>{expires_at_iso[:10]}</b> күнүнө чейин узартылды/берилди. Эми группага жазган жарыяларыңыз чектөөсүз жана унааңыздын сүрөтү менен чыгат!", 
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не смогли отправить юзеру сообщение: {e}")

@dp.callback_query(F.data.startswith("reject_vip_"))
async def admin_reject_vip(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    
    # Запоминаем, кого именно мы отклоняем
    await state.update_data(reject_user_id=user_id)
    await state.set_state(AdminReject.waiting_for_reason)
    
    # Просим админа написать причину
    await callback.message.answer(
        "✍️ <b>Напиши причину отказа текстом</b> (например: 'Чек эски' или 'Машинанын реалдуу сүрөтүн киргизиңиз').\n"
        "<i>Бул текст түз эле айдоочуга барат.</i>", 
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(StateFilter(AdminReject.waiting_for_reason))
async def handle_reject_reason(message: types.Message, state: FSMContext):
    # Убеждаемся, что пишет админ
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
        
    data = await state.get_data()
    user_id = data.get("reject_user_id")
    admin_reason = message.text
    
    # Удаляем заявку из базы
    supabase.table("premium_drivers").delete().eq("user_id", user_id).execute()
    
    # Отправляем сообщение водителю с твоей причиной
    user_text = (
        "❌ <b>Кечиресиз, сиздин VIP өтүнүчүңүз четке кагылды.</b>\n\n"
        f"💬 <b>Админдин комментарийи:</b>\n<i>{admin_reason}</i>\n\n"
        "Сураныч, маалыматты тууралап, кайрадан /start аркылуу жөнөтүңүз."
    )
    
    try:
        await bot.send_message(chat_id=user_id, text=user_text, parse_mode="HTML")
        await message.answer(f"✅ Причина отправлена юзеру <code>{user_id}</code>, заявка удалена.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки юзеру (возможно, он заблокировал бота): {e}")
        
    # Очищаем состояние админа
    await state.clear()

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
    Проанализируй текст объявления из кыргызской группы попутчиков: "{text_to_analyze}"
    
    Задача: Разобрать текст и строго вернуть JSON. 
    
    1. is_ad (boolean): 
    Если текст про поиск машины, пассажиров, такси, передачу посылки или груза — ставь true. 

    2. role (string: "айдоочу", "жүргүнчү", "посылка", "жүк ташуу" или null):
    КАК ОПРЕДЕЛИТЬ РОЛЬ (ОЧЕНЬ ВАЖНО):
    
    ▶ Маркеры "жүргүнчү" (Пассажир - у него НЕТ машины):
    - Ищет авто: "такси керек", "машина керек", "минивэн керек", "машиналар керек".
    - Указывает количество себя: "2 адамбыз", "3 киши", "1 киши бар", "7 адам кетет".
    - ПРАВИЛО: Если в тексте есть фраза вроде "машина керек 2 адамбыз" или "3 киши такси керек" — это СТРОГО "жүргүнчү"!

    ▶ Маркеры "айдоочу" (Водитель - у него ЕСТЬ машина):
    - Прямо пишет: "Водитель", "Айдоочу".
    - Ищет людей: "киши керек", "адам керек", "пассажир ала кетем", "кетчулор чалгыла".
    - Указывает на места: "салон бош", "орун бар", "места бар", "1 место", "алдынкы оорун".
    - Указывает свое авто: "женил машина", "Киа", "Соната", "Степ", "комфорт машина".

    ▶ Маркеры "посылка" (Мелкие вещи и документы):
    - Слова: "передача", "посылка", "документ", "сумка", "пакет".
    - ПРАВИЛО: Если пишут "передача алып кетчулор барбы", "посылка берип жиберем" — это СТРОГО "посылка".

    ▶ Маркеры "жүк ташуу" (Крупные грузы и переезды):
    - Слова: "грузовой спринтер", "уй коч", "мебель", "жук ташуу", "тонна".
    - ВАЖНО: Не путай мелкую "передача" с крупным "жүк ташуу"!

    3. ОСОБЕННОСТИ КЫРГЫЗСКОГО:
    Слова "чыгам", "чыгабыз", "барам", "кетем" используют ВСЕ. Не определяй роль только по ним! Смотри на контекст. Если просто написано "Бишкектен Таласка чыгабыз 2 киши" и непонятно, машина это или пассажиры — ставь role: null.

    Верни JSON в формате:
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

        # ==========================================
        # 👑 БИЗНЕС-ЛОГИКА: ЛИМИТЫ И VIP
        # ==========================================
        now = datetime.datetime.now(TZ_BISHKEK)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        # 1. Считаем, сколько постов юзер УЖЕ сделал сегодня
        daily_count_res = supabase.table(TAXI_TABLE).select("id", count="exact") \
            .eq("user_id", user_id).eq("role", role).gte("created_at", start_of_day).execute()
        posts_today = daily_count_res.count or 0

        # 2. Проверяем статус VIP и срок его действия
        is_vip = False
        photo_file_id = None
        
        vip_res = supabase.table("premium_drivers").select("photo_file_id, expires_at").eq("user_id", user_id).execute()
        
        if vip_res.data:
            expires_at_str = vip_res.data[0].get("expires_at")
            if expires_at_str:
                # Парсим время из БД (Supabase отдает формат ISO 8601)
                expires_at = datetime.datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                if expires_at > now:
                    is_vip = True
                    photo_file_id = vip_res.data[0]["photo_file_id"]

        # 3. ПРОВЕРКА ЛИМИТА: Если не VIP и это уже 4-й пост (лимит 3)
        # Ограничиваем ТОЛЬКО водителей ("айдоочу") и грузоперевозки ("жүк ташуу")
        if not is_vip and posts_today >= 3 and role in ["айдоочу", "жүк ташуу"]:
            
            # Меняем текст, чтобы он подходил и таксистам, и грузовикам
            role_display = "унааңыздын" if role == "айдоочу" else "жүк ташуучу унааңыздын"
            
            limit_text = (
                f"🛑 <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>, <b>Сиздин бүгүнкү акысыз лимитиңиз бүттү (3/3).</b>\n\n"
                f"Жарыяңыз киргизилген жок. Чектөөсүз жарыя жазуу жана <b>{role_display} сүрөтүн</b> кошуу үчүн <b>VIP-статус</b> сатып алыңыз!\n\n"
                "👇 VIP алуу үчүн төмөнкү баскычты басыңыз:"
            )
            
            # Отправляем сообщение-уведомление прямо в группу
            limit_builder = InlineKeyboardBuilder()
            limit_builder.row(types.InlineKeyboardButton(text="👑 VIP алуу", url=f"{BOT_LINK}?start=buy_vip"))
            
            warning_msg = await bot.send_message(chat_id=message.chat.id, text=limit_text, parse_mode="HTML", reply_markup=limit_builder.as_markup())
            
            # Удаляем предупреждение через 2 минуты, чтобы не засорять чат
            async def delete_warning(chat_id, msg_id):
                await asyncio.sleep(120)
                try:
                    await bot.delete_message(chat_id, msg_id)
                except:
                    pass
            asyncio.create_task(delete_warning(warning_msg.chat.id, warning_msg.message_id))
            
            return "LIMIT_REACHED"

        # ==========================================
        # 📤 ПУБЛИКАЦИЯ ПОСТА И СОХРАНЕНИЕ
        # ==========================================
        
        # Общая аналитика постов юзера (за все время)
        count_res = supabase.table(TAXI_TABLE).select("id", count="exact").eq("user_id", user_id).eq("role", role).execute()
        post_count = (count_res.count or 0) + 1

        # --- НОВЫЙ БЛОК: ДОБАВЛЯЕМ СЧЕТЧИК ЛИМИТОВ В КОНЕЦ ТЕКСТА ---
        if role in ["айдоочу", "жүк ташуу"]:
            if is_vip:
                text += "\n\n<i>👑 Сизде VIP-статус (чектөөсүз)</i>"
            else:
                remaining = 3 - (posts_today + 1)
                text += f"\n\n<i>⚠️ Бүгүнкү акысыз жарыялар: {remaining}/3 калды</i>"
        # -----------------------------------------------------------

        # Отправка самого поста в группу (с фото, если VIP)
        if is_vip and role == "айдоочу" and photo_file_id:
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
            "created_at": now.isoformat()
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