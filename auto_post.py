import asyncio
import os
from aiogram import Bot, types
from aiogram.types import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Берем ссылку на бота из настроек Railway
BOT_LINK = os.getenv("BOT_START_LINK")

async def send_instruction_post(bot: Bot, channel_id: int):
    while True:
        try:
            # 1. Текст под фото (инструкция)
            caption_text = (
                "📢 <b>Көңүл буруңуз!</b>\n\n"
                "Жарыя берүү үчүн төмөндөгү <b>\"Жарыя түзүңүз\"</b> баскычын басыңыз 👇"
            )

            # 2. Кнопка под фото
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(
                text="➕ Жарыя түзүңүз", 
                url=BOT_LINK)
            )

            # 3. Отправка фото (файл должен лежать в папке с ботом)
            photo = FSInputFile("instruction.jpg")
            await bot.send_photo(
                chat_id=channel_id,
                photo=photo,
                caption=caption_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            
            print("✅ Пост-инструкция отправлена")
        except Exception as e:
            print(f"❌ Ошибка при отправке инструкции: {e}")

        # Пауза 1 час (3600 секунд)
        await asyncio.sleep(1800)