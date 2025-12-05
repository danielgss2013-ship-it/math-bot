import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command

# --- НАСТРОЙКИ (Берутся из сейфа сервера) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID") 

# Включаем журнал событий, чтобы видеть ошибки
logging.basicConfig(level=logging.INFO)

# Создаем бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 1. Если человек нажал /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Это бот доступа в закрытый математический клуб.\n"
        "Цена: **1500 рублей/месяц**.\n\n"
        "Нажми /buy, чтобы оплатить вход."
    )

# 2. Если человек нажал /buy (Выставляем счет)
@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Доступ в MathClub",
        description="Подписка на 1 месяц. Разборы задач и чат.",
        payload="math_sub_01", 
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        # Цена пишется в КОПЕЙКАХ! 1500 рублей = 150000 копеек
        prices=[LabeledPrice(label="Подписка на месяц", amount=150000)], 
        start_parameter="create_invoice",
    )

# 3. Техническая проверка перед оплатой (Telegram требует ответить "ОК")
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# 4. САМОЕ ГЛАВНОЕ: Оплата прошла успешно
@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    try:
        # Генерируем ссылку, которая сработает только 1 раз для этого человека
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            name=f"Оплата: {message.from_user.full_name}",
            member_limit=1 
        )
        
        await message.answer(
            f"✅ **Оплата прошла успешно!** Добро пожаловать.\n\n"
            f"Вот твоя личная ссылка для входа:\n{invite.invite_link}\n\n"
            f"⚠️ Ссылка одноразовая, нажми её скорее!"
        )
    except Exception as e:
        # Если вдруг бот не админ или что-то сломалось
        await message.answer(f"Оплата прошла, но я не смог создать ссылку. Перешли это сообщение админу: {e}")

# Запуск бота
async def main():
    # Удаляем старые команды и запускаем
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":

    asyncio.run(main())
