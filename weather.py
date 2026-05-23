import os
import asyncio
import aiohttp
import logging
from aiogram import Bot
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder

weather_click_count = 0

# Список локаций
LOCATIONS = [
    {"icon": "🏙", "name": "Бишкек", "query": "q=Bishkek"},
    {"icon": "🏘", "name": "Талас", "query": "q=Talas"},
    {"icon": "⛰", "name": "Тоо-Ашуу ашуусу", "query": "lat=42.318&lon=73.812"},
    {"icon": "⛰", "name": "Өтмөк ашуусу", "query": "lat=42.288&lon=73.170"}
]

# Словарь для перевода описания погоды с русского на кыргызский
WEATHER_TRANSLATIONS = {
    "ясно": "Ачык",
    "пасмурно": "Булуттуу",
    "облачно с прояснениями": "Ала булуттуу",
    "небольшая облачность": "Бир аз булуттуу",
    "переменная облачность": "Өзгөрүлмө булуттуу",
    "небольшой дождь": "Бир аз жамгыр",
    "дождь": "Жамгыр",
    "сильный дождь": "Катуу жамгыр",
    "снег": "Кар",
    "небольшой снег": "Бир аз кар",
    "мокрый снег": "Жамгыр аралаш кар",
    "гроза": "Күн күркүрөйт",
    "туман": "Туман",
    "дымка": "Мунар"
}

# Функция выбора эмодзи
def get_weather_emoji(weather_main):
    emojis = {
        "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧", "Drizzle": "🌦",
        "Thunderstorm": "⛈", "Snow": "❄️", "Mist": "🌫", "Fog": "🌫"
    }
    return emojis.get(weather_main, "🌤")

async def fetch_weather(session, location):
    api_key = os.getenv("WEATHER_API_KEY")
    url = f"http://api.openweathermap.org/data/2.5/weather?{location['query']}&appid={api_key}&units=metric&lang=ru"
    async with session.get(url) as response:
        if response.status == 200:
            return await response.json()
        return None

async def build_weather_message():
    message_text = "🌤 <b>Бүгүнкү аба ырайы</b>\n\n"
    
    async with aiohttp.ClientSession() as session:
        for loc in LOCATIONS:
            data = await fetch_weather(session, loc)
            
            if data:
                raw_desc = data['weather'][0]['description'].lower().strip()
                desc = WEATHER_TRANSLATIONS.get(raw_desc, raw_desc.capitalize())
                
                weather_main = data['weather'][0]['main']
                emoji = get_weather_emoji(weather_main)
                
                temp = round(data['main']['temp'])
                wind = round(data['wind']['speed'])
                
                message_text += (
                    f"{loc['icon']} <b>{loc['name']}:</b>\n"
                    f"{emoji} {desc}\n"
                    f"🌡 Температура: {temp}°C\n"
                    f"💨 Шамал: {wind} м/с\n\n"
                )
            else:
                message_text += f"{loc['icon']} <b>{loc['name']}:</b>\n❌ Маалымат алууда ката кетти\n\n"
                
    return message_text

def get_and_increment_weather_count():
    global weather_click_count
    weather_click_count += 1
    return weather_click_count

async def weather_and_promo_task(bot: Bot, channel_id: int):
    send_weather = True  # Начинаем с погоды

    while True:
        try:
            if send_weather:
                api_key = os.getenv("WEATHER_API_KEY")
                if api_key and channel_id:
                    text = await build_weather_message()
                    await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
                    logging.info("✅ Погода отправлена!")
                else:
                    logging.warning("❌ API ключ погоды или ID канала не найдены.")
            else:
                if channel_id:
                    promo_text = (
                        "🚕 <b>АЙДООЧУ</b>\n\n"
                        "🎁 <b><i>БЕКЕР акция!!!!</i></b>\n"
                        "<i>Жарыяларыңыз чектөөсүз жана унааңыздын сүрөтү менен чыгат).</i>\n\n"
                        "Кантип алса болот? Шарттары өтө жөнөкөй:\n\n"
                        "1️⃣ Төмөнкү «💳 Тариф тандоо» баскычын басып, ботко кириңиз.\n"
                        "2️⃣ Бот сизден төлөмдүн чегин (чек) сураганда — унааңыздын сүрөтүн эле жибере бериңиз 🚗\n"
                        "3️⃣ Андан кийин бот \"Унааңыздын сүрөтүн жибериңиз\" деп кайра сураганда — "
                        "кайра эле ошол унааңыздын сүрөтүн экинчи жолу жибериңиз 🚗\n"
                        "4️⃣ Даяр! Админ текшерип, сизге даро 1 айга бекер VIP кошуп берет ✅"
                    )

                    builder = InlineKeyboardBuilder()
                    builder.row(types.InlineKeyboardButton(
                        text="💳 Тариф тандоо (Сүрөт кошуу)",
                        url=f"{os.getenv('BOT_START_LINK')}?start=buy_vip"
                    ))

                    photo = types.FSInputFile("promo.png")
                    await bot.send_photo(
                        chat_id=channel_id,
                        photo=photo,
                        caption=promo_text,
                        parse_mode="HTML",
                        reply_markup=builder.as_markup()
)
                    logging.info("✅ Акция отправлена!")

        except Exception as e:
            logging.error(f"❌ Ошибка: {e}")

        send_weather = not send_weather  # Переключаем
        await asyncio.sleep(60)  # 30 минут