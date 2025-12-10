import os
import sqlite3
import datetime
import asyncio
from aiogram import Bot, Dispatcher, types 
from aiogram.utils import executor 
from aiogram.types import Message, LabeledPrice, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.dispatcher.filters import Command # ИМПОРТИРУЕМ Command
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from logger import logger 

# --- 1. КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

# Ваши данные
CHANNEL_ID = -1003328408384
ADMIN_ID = 405491563
OFFER_FILENAME = 'oferta.pdf' 
DB_PATH = '/data/subscriptions.db'

# Цены в копейках
BASE_PRICE = 150000   # 1500 RUB
PROMO_PRICE = 75000   # 750 RUB (50% скидка)
PROMO_CODE = 'FIRST'

# --- FSM: СОСТОЯНИЯ ДЛЯ СБОРА ДАННЫХ ---
class PaymentStates(StatesGroup):
    waiting_for_promo_code = State()
    waiting_for_email = State()
    waiting_for_agreement = State()

# --- 2. ФУНКЦИИ БАЗЫ ДАННЫХ ---

def init_db():
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
    logger.info("База данных успешно инициализирована.")

def add_subscription(user_id, username, email):
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
    logger.info(f"Подписка для пользователя {user_id} ({username}) продлена до {expire_date}.")
    return expire_date

def get_subscription_status(user_id=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if user_id:
        # Режим: Проверка одного пользователя
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
    
    else:
        # Режим: Получение всех пользователей
        cursor.execute("SELECT user_id, username, email, expire_date FROM subscriptions ORDER BY expire_date DESC")
        results = cursor.fetchall()
        conn.close()
        return results


# --- 3. ФОНОВАЯ ФУНКЦИЯ ПРОВЕРКИ ИСТЕЧЕНИЯ ---

async def check_expirations(bot: Bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')

    cursor.execute("SELECT user_id, username FROM subscriptions WHERE expire_date <= ?", (today_str,))
    expired_users = cursor.fetchall()
    logger.info(f"Найдено {len(expired_users)} просроченных подписок.")

    for user_id, username in expired_users:
        try:
            await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            await bot.send_message(user_id, "Ваша подписка на MathClub истекла. Пожалуйста, продлите доступ!")
            cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
            conn.commit()
            logger.info(f"Пользователь {user_id} ({username}) удален из канала и базы данных.")
        
        except Exception as e:
            logger.error(f"Ошибка при обработке истечения подписки для {username} (ID: {user_id}): {e}")

    conn.close()

# --- 4. ИНИЦИАЛИЗАЦИЯ AIOGRAM ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- 5. ФУНКЦИЯ ЗАПУСКА SCHEDULER'А ---
async def on_startup(dp):
    scheduler.add_job(check_expirations, 'cron', hour=0, minute=1, args=(bot,))
    scheduler.start()
    logger.info("APScheduler успешно запущен и настроен.")

# --- 6. ОБРАБОТЧИКИ СОБЫТИЙ БОТА ---

@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: Message, state: FSMContext):
    await state.finish() 
    logger.info(f"Команда /start от пользователя {message.from_user.id}.")

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

@dp.callback_query_handler(lambda c: c.data == 'start_payment', state='*')
async def process_start_payment(callback_query: types.CallbackQuery, state: FSMContext):
    
    # 1. Пытаемся ответить на Callback Query.
    try:
        await bot.answer_callback_query(callback_query.id)
        logger.debug(f"Callback Query {callback_query.id} успешно отвечен. Переход к FSM.")
    except Exception as e:
        logger.error(f"Критическая ошибка/таймаут при ответе на Callback Query {callback_query.id}. Пользователь: {callback_query.from_user.id}. Ошибка: {e}") 
        await asyncio.sleep(0.5)
        
    # 2. Переводим пользователя в состояние ожидания промокода
    await PaymentStates.waiting_for_promo_code.set()
    
    # Добавляем кнопку "Нет промокода"
    promo_keyboard = InlineKeyboardMarkup(row_width=1)
    promo_keyboard.add(InlineKeyboardButton(text="Нет промокода", callback_data="skip_promo"))

    await bot.send_message(
        callback_query.from_user.id,
        "🎁 **Введите промокод (если есть)**.\n"
        "Например, введите `FIRST` для получения скидки 50% на первый месяц.",
        reply_markup=promo_keyboard, # Прикрепляем новую клавиатуру
        parse_mode="Markdown"
    )

@dp.callback_query_handler(lambda c: c.data == 'skip_promo', state=PaymentStates.waiting_for_promo_code)
async def skip_promo_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id, text="Пропуск. Применяется полная цена.")
    
    # Устанавливаем базовую цену и пропускаем ввод промокода
    await state.update_data(payment_price=BASE_PRICE, promo_applied=False)
    logger.info(f"Пользователь {callback_query.from_user.id} пропустил ввод промокода. Цена: {BASE_PRICE / 100} руб.")
    
    # Переход к следующему шагу (Email)
    await PaymentStates.waiting_for_email.set()
    await bot.send_message(
        callback_query.from_user.id,
        "✉️ **Теперь, пожалуйста, напишите ваш Email**.\n"
        "Мы будем использовать его для связи по вопросам оплаты и доступа.",
        parse_mode="Markdown"
    )


@dp.message_handler(state=PaymentStates.waiting_for_promo_code)
async def process_promo_code(message: Message, state: FSMContext):
    promo_code = message.text.strip().upper()
    
    # Проверяем промокод
    if promo_code == PROMO_CODE:
        final_price = PROMO_PRICE
        await state.update_data(payment_price=final_price, promo_applied=True)
        await message.answer(f"✅ Промокод **{PROMO_CODE}** активирован! Стоимость подписки на первый месяц составит **{final_price / 100:.0f} рублей**.")
        logger.info(f"Пользователь {message.from_user.id} применил промокод '{PROMO_CODE}'. Цена: {final_price / 100} руб.")
    else:
        final_price = BASE_PRICE
        await state.update_data(payment_price=final_price, promo_applied=False)
        await message.answer(f"❌ Промокод не найден или недействителен. Применяется полная цена.")
        logger.info(f"Пользователь {message.from_user.id} ввел неверный промокод. Цена: {final_price / 100} руб.")

    # Переход к следующему шагу (Email)
    await PaymentStates.waiting_for_email.set()
    await message.answer(
        "✉️ **Теперь, пожалуйста, напишите ваш Email**.\n"
        "Мы будем использовать его для связи по вопросам оплаты и доступа.",
        parse_mode="Markdown"
    )

@dp.message_handler(state=PaymentStates.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    user_email = message.text.strip()
    
    if '@' not in user_email or '.' not in user_email or len(user_email) < 5:
        await message.answer("Кажется, это неверный формат Email. Попробуйте еще раз.")
        return

    await state.update_data(user_email=user_email)
    await PaymentStates.waiting_for_agreement.set()
    logger.debug(f"Email '{user_email}' сохранен. Переход к соглашению.")

    agreement_keyboard = InlineKeyboardMarkup(row_width=1)
    agreement_keyboard.add(InlineKeyboardButton(text="✅ Я согласен с офертой", callback_data="agree_offer"))

    await bot.send_message(
        message.chat.id,
        "📃 **Перед оплатой ознакомьтесь с Офертой и ПОПД**.\n\n"
        "Нажимая «Я согласен», вы подтверждаете свое согласие с условиями оказания услуг.",
        parse_mode="Markdown"
    )
    
    try:
       await bot.send_document(message.chat.id, InputFile(OFFER_FILENAME), reply_markup=agreement_keyboard)
    except Exception as e:
       logger.error(f"Ошибка при отправке файла оферты {OFFER_FILENAME}: {e}")
       await message.answer(f"Ошибка при отправке файла оферты. Пожалуйста, подтвердите согласие ниже:", reply_markup=agreement_keyboard)


@dp.callback_query_handler(lambda c: c.data == 'agree_offer', state=PaymentStates.waiting_for_agreement)
async def process_agreement(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    
    user_data = await state.get_data()
    # Получаем цену из FSM, если ее нет, используем базовую цену
    payment_price = user_data.get('payment_price', BASE_PRICE) 
    is_promo = user_data.get('promo_applied', False)

    await state.set_state(None)
    logger.info(f"Пользователь {callback_query.from_user.id} согласился с офертой. Выставление счета. Цена: {payment_price / 100} RUB.")
    
    title_text = "Доступ в MathClub (со скидкой)" if is_promo else "Доступ в MathClub"
    price_label = f"Подписка на 1 месяц ({payment_price / 100:.0f} RUB)"

    await bot.send_invoice(
        chat_id=callback_query.from_user.id,
        title=title_text,
        description="Подписка на 1 месяц. Разборы задач и чат.",
        payload="math_sub_01", 
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=price_label, amount=payment_price)], # Используем динамическую цену
        is_flexible=False
    )

@dp.pre_checkout_query_handler(lambda query: True)
async def process_pre_checkout_query(pre_checkout_query):
    logger.debug(f"Pre-checkout query ID: {pre_checkout_query.id}. Пользователь: {pre_checkout_query.from_user.id}.")
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message_handler(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or 'N/A'
    
    logger.info(f"Получено сообщение об успешной оплате от пользователя {user_id}.")

    try:
        user_data = await state.get_data()
        user_email = user_data.get('user_email', 'Email not collected') 
        logger.info(f"Обработка успешной оплаты. Email: {user_email}.")

        expire_date = add_subscription(user_id, username, user_email)

        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            name=f"Оплата: {message.from_user.full_name}",
            expire_date=datetime.datetime.now() + datetime.timedelta(days=30)
        )
        logger.info(f"Создана одноразовая ссылка для {user_id}: {invite.invite_link}")

        await bot.send_message(
            message.chat.id,
            f"🎉 **Оплата успешно произведена, добро пожаловать в клуб «Твоя Математика»!**\n\n"
            f"Ваша подписка активна до {expire_date}.\n"
            f"Вот ваша **одноразовая** ссылка для входа: {invite.invite_link}\n\n"
            f"Если есть вопросы — пишите в поддержку.",
            parse_mode="Markdown"
        )
        await state.finish() 

    except Exception as e:
        logger.error(f"КРИТИЧЕСКАЯ ОШИБКА при обработке успешной оплаты для {user_id}. Состояние FSM: {await state.get_state()}. Ошибка: {e}")
        await bot.send_message(user_id, "⚠️ **Критическая ошибка!** Оплата прошла, но бот не смог выдать ссылку. Пожалуйста, обратитесь в поддержку @dankurbanoff.", parse_mode="Markdown")


@dp.message_handler(Command('admin')) # ИСПОЛЬЗУЕМ Command
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return

    # Получаем аргументы команды, убираем пробелы и приводим к нижнему регистру для надежности
    # Используем message.get_args() для надежного извлечения аргументов
    arg = message.get_args().strip().lower()

    # Определяем режим
    if not arg:
        # Если аргументов нет (просто /admin), по умолчанию режим 'all'
        mode = 'all'
    elif arg == 'active' or arg == 'all':
        # Если указано 'active' или 'all'
        mode = arg
    else:
        # Пытаемся обработать как ID пользователя
        try:
            user_id = int(arg) 
            status = get_subscription_status(user_id)
            await message.answer(f"Статус подписки для ID {user_id}: **{status}**", parse_mode="Markdown")
        except ValueError:
            await message.answer(
                f"Неверный ID пользователя или команда.\n\n"
                f"Используйте:\n"
                f"• `/admin` (Показать всех)\n"
                f"• `/admin active` (Только активные)\n"
                f"• `/admin [числовой ID]`", 
                parse_mode="Markdown"
            )
        return
        
    # --- Режим: Вывод списка подписчиков (mode = 'all' или 'active') ---

    all_subs = get_subscription_status() # Получаем все записи
    response = ["**--- СПИСОК ПОДПИСЧИКОВ ---**"]
    active_count = 0
    
    for user_id, username, email, expire_date_str in all_subs:
        expire_date = datetime.datetime.strptime(expire_date_str, '%Y-%m-%d')
        is_active = expire_date > datetime.datetime.now()
        
        # Фильтрация по режиму
        if mode == 'active' and not is_active:
            continue
            
        if is_active:
            active_count += 1
        
        status_icon = "🟢" if is_active else "🔴"
        
        # Email включен в вывод
        response.append(
            f"{status_icon} **{username}** (ID: {user_id})\n"
            f"   Email: {email}\n"
            f"   До: {expire_date_str}\n"
        )
        
    if len(response) == 1: # Только заголовок, нет записей
         await message.answer("В базе нет записей о подписчиках.")
         return

    # Разбиваем длинное сообщение на части, чтобы Telegram его принял
    chunk_size = 4000
    full_response = "\n".join(response)
    
    # Добавляем общую статистику
    if mode == 'active':
         # При показе 'active' считаем только активные записи
         header = f"✅ **ВСЕГО АКТИВНЫХ ПОДПИСЧИКОВ: {active_count}**\n\n"
    else: # mode == 'all'
         # При показе 'all' считаем все записи в базе
         header = f"📋 **ВСЕГО ЗАПИСЕЙ В БАЗЕ: {len(all_subs)}** (Активных: {active_count})\n\n"
    
    full_response = header + full_response

    # Отправка сообщений по частям
    for i in range(0, len(full_response), chunk_size):
        await message.answer(full_response[i:i + chunk_size], parse_mode="Markdown")


# --- 7. ЗАПУСК БОТА ---
if __name__ == '__main__':
    init_db()
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
