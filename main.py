import os
import logging
import asyncio
import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from supabase import create_client, Client

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
raw_id = os.getenv("CHANNEL_ID")
CHANNEL_ID = int(raw_id) if raw_id else None

# Настройка времени Бишкека для всего кода
TZ_BISHKEK = datetime.timezone(datetime.timedelta(hours=6))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class TaxiStates(StatesGroup):
    choosing_role = State()
    destination = State()
    time = State()
    waiting_for_custom_time = State()
    car_model = State()     
    price = State()         
    passenger_count = State()
    phone_number = State()

# --- ФОНОВАЯ ЗАДАЧА: ОЧИСТКА СТАРЫХ ПОСТОВ (3 СУТОК) ---
async def cleanup_old_messages():
    while True:
        try:
            three_days_ago = (datetime.datetime.now(TZ_BISHKEK) - datetime.timedelta(days=3)).isoformat()
            res = supabase.table("users").select("id", "message_id").lt("created_at", three_days_ago).not_.is_("message_id", "null").execute()
            for record in res.data:
                try: 
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=record["message_id"])
                except: 
                    pass
                supabase.table("users").update({"message_id": None}).eq("id", record["id"]).execute()
        except Exception as e:
            logging.error(f"Ошибка очистки: {e}")
        await asyncio.sleep(3600)

# --- КЛАВИАТУРЫ ---

def get_start_inline_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🚕 Я Водитель", callback_data="set_role_водитель"))
    builder.row(types.InlineKeyboardButton(text="👤 Я Пассажир", callback_data="set_role_пассажир"))
    return builder.as_markup()

def get_cities_kb():
    kb = [[types.KeyboardButton(text="Талас"), types.KeyboardButton(text="Кировка")], [types.KeyboardButton(text="Бишкек")]]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_time_kb():
    builder = ReplyKeyboardBuilder()
    now = datetime.datetime.now(TZ_BISHKEK)
    start_time = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    for i in range(5):
        slot = (start_time + datetime.timedelta(hours=i)).strftime("%H:00")
        builder.add(types.KeyboardButton(text=slot))
    builder.adjust(3)
    builder.row(types.KeyboardButton(text="⏳ Другое время"))
    return builder.as_markup(resize_keyboard=True)

def get_numbers_kb(count):
    builder = ReplyKeyboardBuilder()
    for i in range(1, int(count) + 1):
        builder.add(types.KeyboardButton(text=str(i)))
    builder.adjust(4)
    return builder.as_markup(resize_keyboard=True)

def get_phone_kb():
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text="📱 Отправить мой номер", request_contact=True))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_channel_publish_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="➕ Создать объявление", url="https://t.me/poputka_24_bot?start=go"))
    return builder.as_markup()

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ---
async def proceed_to_next_step(message: types.Message, state: FSMContext, time_value: str):
    await state.update_data(time=time_value)
    data = await state.get_data()
    if data['role'] == "водитель":
        await message.answer("🚗 Введите <b>марку машины</b>:", reply_markup=types.ReplyKeyboardRemove(), parse_mode="HTML")
        await state.set_state(TaxiStates.car_model)
    else:
        await message.answer("👥 Сколько <b>человек</b> поедет?", reply_markup=get_numbers_kb(5), parse_mode="HTML")
        await state.set_state(TaxiStates.passenger_count)

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = "👋 <b>Здравствуйте!</b>\n\nЧтобы подать объявление, выберите вашу роль ниже:"
    await message.answer(welcome_text, reply_markup=get_start_inline_kb(), parse_mode="HTML")
    await state.set_state(TaxiStates.choosing_role)

@dp.callback_query(F.data.startswith("set_role_"))
async def process_role_callback(callback: types.CallbackQuery, state: FSMContext):
    role = callback.data.split("_")[2]
    await state.update_data(role=role)
    await callback.message.answer(f"📍 Вы выбрали: <b>{role}</b>. Куда едем?", reply_markup=get_cities_kb(), parse_mode="HTML")
    await state.set_state(TaxiStates.destination)
    await callback.answer()

@dp.message(TaxiStates.destination)
async def process_dest(message: types.Message, state: FSMContext):
    await state.update_data(destination=message.text)
    await message.answer("🕒 Выберите <b>время</b> выезда:", reply_markup=get_time_kb(), parse_mode="HTML")
    await state.set_state(TaxiStates.time)

@dp.message(TaxiStates.time)
async def process_time(message: types.Message, state: FSMContext):
    if message.text == "⏳ Другое время":
        await message.answer("📝 Введите время (например: 15:30, 'через час' или азыр):", reply_markup=types.ReplyKeyboardRemove(), parse_mode="HTML")
        await state.set_state(TaxiStates.waiting_for_custom_time)
    else:
        await proceed_to_next_step(message, state, message.text)

@dp.message(TaxiStates.waiting_for_custom_time)
async def process_custom_time(message: types.Message, state: FSMContext):
    await proceed_to_next_step(message, state, message.text)

@dp.message(TaxiStates.car_model)
async def process_car(message: types.Message, state: FSMContext):
    await state.update_data(car_model=message.text)
    await message.answer("💰 Укажите <b>цену</b> (сом):", parse_mode="HTML")
    await state.set_state(TaxiStates.price)

@dp.message(TaxiStates.price)
async def process_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    await message.answer("💺 Сколько у вас <b>свободных мест</b>?", reply_markup=get_numbers_kb(7), parse_mode="HTML")
    await state.set_state(TaxiStates.passenger_count)

@dp.message(TaxiStates.passenger_count)
async def process_p_count(message: types.Message, state: FSMContext):
    await state.update_data(passenger_count=message.text)
    await message.answer("📱 Нажмите <b>«Отправить номер или введите в ручную»</b>:", reply_markup=get_phone_kb(), parse_mode="HTML")
    await state.set_state(TaxiStates.phone_number)

@dp.message(TaxiStates.phone_number)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone_number=phone)
    data = await state.get_data()
    user = message.from_user
    
    clean_phone = phone.replace(" ", "").replace("-", "")
    if not clean_phone.startswith('+'): clean_phone = '+' + clean_phone
    
    role_name = "ВОДИТЕЛЬ" if data['role'] == "водитель" else "ПАССАЖИР"
    icon = "🚕" if data['role'] == "водитель" else "👤"
    
    # Текст без фразы "НОВАЯ ЗАЯВКА"
    text = (f"{icon} <b>{role_name}</b>\n\n"
            f"📍 <b>Куда</b>: {data['destination']}\n"
            f"🕒 <b>Время</b>: {data['time']}\n")
    
    if data['role'] == "водитель":
        text += f"🚗 <b>Авто</b>: {data.get('car_model')}\n💰 <b>Цена</b>: {data.get('price')} сом\n"
    
    text += (f"👥 <b>{'Мест' if data['role'] == 'водитель' else 'Человек'}</b>: {data['passenger_count']}\n"
             f"📞 <b>Тел.</b>: <a href='tel:{clean_phone}'><code>{phone}</code></a>\n\n"
             f"👤 <b>{role_name.capitalize()}</b>: <a href='tg://user?id={user.id}'>{user.full_name}</a>")

    try:
        # Считаем посты для счетчика
        count_res = supabase.table("users").select("id", count="exact").eq("user_id", user.id).eq("role", data['role']).execute()
        post_count = (count_res.count or 0) + 1

        # Отправляем новое сообщение (БЕЗ УДАЛЕНИЯ старых)
        msg = await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML", reply_markup=get_channel_publish_kb())

        # ВСЕГДА INSERT новой строки
        db_payload = {
            "user_id": user.id, "role": data['role'], "destination": data['destination'],
            "time": data['time'], "passenger_count": data['passenger_count'], 
            "phone_num": phone, "car_model": data.get("car_model"), 
            "price": data.get("price"), "message_id": msg.message_id,
            "post_count": post_count, "created_at": datetime.datetime.now(TZ_BISHKEK).isoformat()
        }
        supabase.table("users").insert(db_payload).execute()

        await message.answer(f"✅ <b>Опубликовано!</b>\nОбъявление №{post_count}", parse_mode="HTML", reply_markup=get_start_inline_kb())
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

async def main():
    await bot.set_my_commands([types.BotCommand(command="start", description="🚀 Начать")])
    asyncio.create_task(cleanup_old_messages())
    await dp.start_polling(bot)

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nБот выключен")