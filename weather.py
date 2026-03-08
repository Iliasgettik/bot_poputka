import os
import asyncio
import aiohttp
import logging
from aiogram import Bot

# Получаем ключ из окружения
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

# Список локаций: города по названиям, перевалы по координатам
LOCATIONS = [
    {
        "icon": "🏙", 
        "name": "Бишкек", 
        "query": "q=Bishkek"
    },
    {
        "icon": "🏘", 
        "name": "Талас", 
        "query": "q=Talas"
    },
    {
        "icon": "⛰", 
        "name": "Перевал Тоо-Ашуу / Тоо-Ашуу ашуусу", 
        "query": "lat=42.318&lon=73.812" # Координаты Тоо-Ашуу
    },
    {
        "icon": "⛰", 
        "name": "Перевал Отмок / Өтмөк ашуусу", 
        "query": "lat=42.288&lon=73.170" # Координаты Отмок
    }
]

# Функция выбора эмодзи для погоды
def get_weather_emoji(weather_main):
    emojis = {
        "Clear": "☀️",
        "Clouds": "☁️",
        "Rain": "🌧",
        "Drizzle": "🌦",
        "Thunderstorm": "⛈",
        "Snow": "❄️",
        "Mist": "🌫",
        "Fog": "🌫"
    }
    return emojis.get(weather_main, "🌤")

async def fetch_weather(session, location):
    url = f"http://api.openweathermap.org/data/2.5/weather?{location['query']}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    async with session.get(url) as response:
        if response.status == 200:
            return await response.json()
        return None

async def build_weather_message():
    message_text = "🌤 <b>Прогноз погоды на сегодня / Бүгүнкү аба ырайы</b>\n\n"
    
    async with aiohttp.ClientSession() as session:
        for loc in LOCATIONS:
            data = await fetch_weather(session, loc)
            
            if data:
                desc = data['weather'][0]['description'].capitalize()
                weather_main = data['weather'][0]['main']
                emoji = get_weather_emoji(weather_main)
                
                temp = round(data['main']['temp'])
                feels_like = round(data['main']['feels_like'])
                humidity = data['main']['humidity']
                wind = round(data['wind']['speed'])
                
                message_text += (
                    f"{loc['icon']} <b>{loc['name']}:</b>\n"
                    f"{emoji} {desc}\n"
                    f"🌡 Температура: {temp}°C\n"
                    f"🌡 Ощущается как: {feels_like}°C\n"
                    f"💧 Влажность: {humidity}%\n"
                    f"💨 Ветер: {wind} м/с\n\n"
                )
            else:
                message_text += f"{loc['icon']} <b>{loc['name']}:</b>\n❌ Ошибка получения данных\n\n"
                
    return message_text

# Главная фоновая задача, которую мы импортируем в бота
async def weather_background_task(bot: Bot, channel_id: int):
    while True:
        try:
            if WEATHER_API_KEY and channel_id:
                text = await build_weather_message()
                await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
            else:
                logging.warning("API ключ погоды или ID канала не найдены.")
        except Exception as e:
            logging.error(f"Ошибка при отправке погоды: {e}")
            
        # Пауза на 2 часа (7200 секунд)
        await asyncio.sleep(7200)