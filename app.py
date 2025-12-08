import os
import sqlite3
import datetime
import asyncio
from aiogram import Bot, Dispatcher # Bot, Dispatcher импортируем прямо из aiogram
from aiogram.utils import executor # А executor импортируем из aiogram.utils, чтобы не было ошибки!
from aiogram.types import Message, LabeledPrice, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.dispatcher.filters import Command
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- 1. КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ (Хардкод по вашим данным) ---
# Секретные токены берутся из настроек Amvera
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

# Ваши данные
CHANNEL_ID = -1003328408384
ADMIN_ID = 405491563
OFFER_FILENAME = 'oferta.pdf' 
DB_PATH = '/data/subscriptions.db'

# --- FSM: СОСТОЯНИЯ ДЛЯ СБОРА ДАННЫХ ---
class PaymentStates(StatesGroup):
    """Классы состояний для процесса оплаты и сбора email."""
    waiting_for_email = State()
    waiting_for_agreement = State()

# --- 2. ФУНКЦИИ БАЗЫ ДАННЫХ ---

def init_db():
    """Создает таблицу подписок при запуске."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            email TEXT,
            expire_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def add_subscription(user_id, username, email):
    """Добавляет или продлевает подписку на 30 дней."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    expire_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
    
    cursor.execute("""
        INSERT INTO subscriptions (user_id, username, email, expire_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            expire_date = excluded.expire_date,
            email = excluded.email
    """, (user_id, username, email, expire_date))
    conn.commit()
    conn.close()
    return expire_date

def get_subscription_status(user_id):
    """Проверяет статус подписки."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM subscriptions WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()

    if not result:
        return "Нет подписки"

    expire_date_str = result[0]
    expire_date = datetime.datetime.strptime(expire_date_str, '%Y-%m-%d')
    
    if expire_date > datetime.datetime.now():
        return f"Активна до {expire_date_str}"
    else:
        return "Истекла"

# --- 3. ФОНОВАЯ ФУНКЦИЯ ПРОВЕРКИ ИСТЕЧЕНИЯ ---

async def check_expirations(bot: Bot):
    """Проверяет базу данных и удаляет истекшие подписки."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')

    cursor.execute("SELECT user_id, username FROM subscriptions WHERE expire_date <= ?", (today_str,))
    expired_users = cursor.fetchall()

    for user_id, username in expired_users:
        try:
            # 1. Отзываем доступ из канала
            await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            
            # 2. Уведомляем пользователя
            await bot.send_message(user_id, "Ваша подписка на MathClub истекла. Пожалуйста, продлите доступ!")
            
            # 3. Удаляем из таблицы подписок
            cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
            conn.commit()
        
        except Exception as e:
            print(f"Ошибка при обработке истечения подписки для {username}: {e}")

    conn.close()

# --- 4. ИНИЦИАЛИЗАЦИЯ AIOGRAM ---
# MemoryStorage нужен для FSM
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- 5. ОБРАБОТЧИКИ СОБЫТИЙ БОТА ---

@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: Message, state: FSMContext):
    """1. Отправляет информацию и кнопку "Оплатить"."""
    await state.finish() 

    info_text = (
        "🧠 **Добро пожаловать в «Твоя Математика»!**\n\n"
        "Получите полный доступ к закрытому клубу, где вас ждут:\n"
        "🔸 Ежедневные разборы сложных задач.\n"
        "🔸 Прямые консультации с преподавателем.\n"
        "🔸 Архив всех материалов.\n\n"
        "Цена: **1500 рублей/месяц**."
    )
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton(text="💳 Оплатить картой РФ / СБП", callback_data="start_payment"))
    
    await message.answer(info_text, reply_markup=keyboard, parse_mode="Markdown")

# --- 1.2: Обработка нажатия кнопки "Оплатить" ---
@dp.callback_query_handler(lambda c: c.data == 'start_payment', state='*')
async def process_start_payment(callback_query, state: FSMContext):
    """Просит пользователя ввести Email и переводит его в состояние waiting_for_email."""
    await bot.answer_callback_query(callback_query.id)
    
    await PaymentStates.waiting_for_email.set()
    
    await bot.send_message(
        callback_query.from_user.id,
        "✉️ **Для оформления подписки, пожалуйста, напишите ваш Email**.\n"
        "Мы будем использовать его для связи по вопросам оплаты и доступа.",
        parse_mode="Markdown"
    )

# --- 2.1: Обработка ввода Email и отправка Оферты ---
@dp.message_handler(state=PaymentStates.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    """Проверяет Email, сохраняет его и предлагает ознакомиться с офертой."""
    user_email = message.text.strip()
    
    if '@' not in user_email or '.' not in user_email or len(user_email) < 5:
        await message.answer("Кажется, это неверный формат Email. Попробуйте еще раз.")
        return

    await state.update_data(user_email=user_email)
    await PaymentStates.waiting_for_agreement.set()

    agreement_keyboard = InlineKeyboardMarkup(row_width=1)
    agreement_keyboard.add(InlineKeyboardButton(text="✅ Я согласен с офертой", callback_data="agree_offer"))

    await bot.send_message(
        message.chat.id,
        "📃 **Перед оплатой ознакомьтесь с Офертой и ПОПД**.\n\n"
        "Нажимая «Я согласен», вы подтверждаете свое согласие с условиями оказания услуг.",
        parse_mode="Markdown"
    )
    
    # Отправка файла PDF (OFFER_FILENAME = 'oferta.pdf')
    try:
       await bot.send_document(message.chat.id, InputFile(OFFER_FILENAME), reply_markup=agreement_keyboard)
    except Exception:
       # Если файл не найден (например, не загружен на GitHub), отправляем просто кнопку
       await message.answer(f"Ошибка при отправке файла оферты. Пожалуйста, подтвердите согласие ниже:", reply_markup=agreement_keyboard)


# --- 2.2 и 2.3: Обработка согласия и выставление счета ---
@dp.callback_query_handler(lambda c: c.data == 'agree_offer', state=PaymentStates.waiting_for_agreement)
async def process_agreement(callback_query, state: FSMContext):
    """Снятие состояния, выставление счета через ЮKassa."""
    await bot.answer_callback_query(callback_query.id)
    
    await state.set_state(None) # Выходим из FSM

    # Выставляем счет через ЮKassa
    await bot.send_invoice(
        chat_id=callback_query.from_user.id,
        title="Доступ в MathClub",
        description="Подписка на 1 месяц. Разборы задач и чат.",
        payload="math_sub_01", 
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label="Подписка на 1 месяц", amount=150000)],
        is_flexible=False
    )

@dp.pre_checkout_query_handler(lambda query: True)
async def process_pre_checkout_query(pre_checkout_query):
    """Техническая проверка перед оплатой."""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


# --- 3. Успешная оплата ---
@dp.message_handler(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message, state: FSMContext):
    """Обработка успешной оплаты и выдача доступа."""
    user_id = message.from_user.id
    username = message.from_user.username or 'N/A'
    
    # Получаем Email, который пользователь вводил ранее
    user_data = await state.get_data()
    user_email = user_data.get('user_email', 'Email not collected') 

    # 1. Добавляем/продлеваем подписку в БД
    expire_date = add_subscription(user_id, username, user_email)

    # 2. Генерируем ОДНОРАЗОВУЮ ссылку для доступа (на 30 дней)
    invite = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1,
        name=f"Оплата: {message.from_user.full_name}",
        expire_date=datetime.datetime.now() + datetime.timedelta(days=30)
    )

    # 3. Уведомляем пользователя
    await bot.send_message(
        message.chat.id,
        f"🎉 **Оплата успешно произведена, добро пожаловать в клуб «Твоя Математика»!**\n\n"
        f"Ваша подписка активна до {expire_date}.\n"
        f"Вот ваша **одноразовая** ссылка для входа: {invite.invite_link}\n\n"
        f"Если есть вопросы — пишите в поддержку.",
        parse_mode="Markdown"
    )

# --- 6. АДМИН-ПАНЕЛЬ ---
@dp.message_handler(commands=['admin'])
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return

    user_id_to_check = message.text.split()[-1]

    if message.text == '/admin':
        await message.answer(
            f"Добро пожаловать, Администратор!\n\n"
            f"Текущее время: {datetime.datetime.now().strftime('%H:%M:%S')}\n"
            f"Для проверки статуса пользователя введите: /admin [ID пользователя]"
        )
    else:
        try:
            user_id = int(user_id_to_check)
            status = get_subscription_status(user_id)
            await message.answer(f"Статус подписки для ID {user_id}: **{status}**", parse_mode="Markdown")
        except ValueError:
            await message.answer("Неверный ID пользователя. Попробуйте: /admin 12345678")


# --- 7. ЗАПУСК БОТА ---
if __name__ == '__main__':
    init_db()
    
    scheduler.add_job(check_expirations, 'cron', hour=0, minute=1, args=(bot,))
    scheduler.start()

    executor.start_polling(dp, skip_updates=True)
