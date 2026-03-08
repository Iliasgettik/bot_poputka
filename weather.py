import os
import asyncio
import aiohttp
import logging
from aiogram import Bot

# Получаем ключ из окружения
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

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
    url = f"http://api.openweathermap.org/data/2.5/weather?{location['query']}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
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
                # Перевод описания
                raw_desc = data['weather'][0]['description'].lower().strip()
                desc = WEATHER_TRANSLATIONS.get(raw_desc, raw_desc.capitalize())
                
                weather_main = data['weather'][0]['main']
                emoji = get_weather_emoji(weather_main)
                
                # Оставляем только температуру и ветер
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

async def weather_background_task(bot: Bot, channel_id: int):
    while True:
        try:
            logging.info("🌤 Запустилась фоновая задача: собираю погоду...") 
            if WEATHER_API_KEY and channel_id:
                text = await build_weather_message()
                await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
                logging.info("✅ Погода успешно отправлена в канал!")
            else:
                logging.warning("❌ API ключ погоды или ID канала не найдены в Railway.")
        except Exception as e:
            logging.error(f"❌ Ошибка при отправке погоды: {e}")
            
        # Пауза на 2 часа (7200 секунд)
        await asyncio.sleep(7200)