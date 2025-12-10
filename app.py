import os
import sqlite3
import datetime
import asyncio
from aiogram import Bot, Dispatcher, types 
from aiogram.utils import executor 
from aiogram.types import Message, LabeledPrice, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.dispatcher.filters import Command 
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
ADMIN_ID = 405491563 # ВАШ АДМИН ID
OFFER_FILENAME = 'oferta.pdf' 
DB_PATH = '/data/subscriptions.db'

# Цены в копейках
BASE_PRICE = 150000   # 1500 RUB
PROMO_PRICE = 75000   # 750 RUB (50% скидка)
PROMO_CODE = 'FIRST'
ADMIN_TIMEZONE = datetime.timezone(datetime.timedelta(hours=3)) # UTC+3
SUPPORT_CONTACT = "@dankurbanoff" # КОНТАКТ ПОДДЕРЖКИ

# --- FSM: СОСТОЯНИЯ ДЛЯ СБОРА ДАННЫХ ---
class PaymentStates(StatesGroup):
    waiting_for_promo_code = State()
    waiting_for_email = State()
    waiting_for_agreement = State()

# --- 2. ФУНКЦИИ БАЗЫ ДАННЫХ И УВЕДОМЛЕНИЯ ---

async def send_notification(bot: Bot, user_id: int, message_text: str):
    """Отправляет сообщение пользователю, обрабатывая возможные ошибки блокировки."""
    try:
        await bot.send_message(user_id, message_text, parse_mode="Markdown")
        logger.debug(f"Уведомление успешно отправлено пользователю {user_id}")
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение пользователю {user_id}. Вероятно, бот заблокирован: {e}")
        return False


def get_current_subscription(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM subscriptions WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        # Возвращаем объект datetime.date для удобного сравнения
        return datetime.datetime.strptime(result[0], '%Y-%m-%d').date()
    return None

def add_subscription(user_id, username, email, days=30, is_renewal=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    current_expiry = get_current_subscription(user_id)
    
    # Дата начала подписки: либо сегодня, либо дата сразу после истечения текущей
    start_date = datetime.datetime.now().date()
    if current_expiry and current_expiry > start_date:
        start_date = current_expiry
        
    new_expire_date = (start_date + datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    
    cursor.execute("""
        INSERT INTO subscriptions (user_id, username, email, expire_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            expire_date = ?,
            email = ?
    """, (user_id, username, email, new_expire_date, new_expire_date, email))
    conn.commit()
    conn.close()
    
    action = "продлена" if is_renewal else "добавлена"
    logger.info(f"Подписка для пользователя {user_id} ({username}) {action} до {new_expire_date}.")
    
    return new_expire_date


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
        expire_date = datetime.datetime.strptime(expire_date_str, '%Y-%m-%d').date()
        
        if expire_date > datetime.datetime.now().date():
            return f"Активна до {expire_date_str}"
        else:
            return "Истекла"
    
    else:
        # Режим: Получение всех пользователей
        cursor.execute("SELECT user_id, username, email, expire_date FROM subscriptions ORDER BY expire_date DESC")
        results = cursor.fetchall()
        conn.close()
        return results

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


# --- 3. ФОНОВАЯ ФУНКЦИЯ ПРОВЕРКИ ИСТЕЧЕНИЯ ---

async def check_expirations(bot: Bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = datetime.datetime.now().date()
    
    # 1. ПОИСК ПОДПИСОК ДЛЯ УВЕДОМЛЕНИЯ ЗА 3 ДНЯ
    future_date_str = (datetime.datetime.now().date() + datetime.timedelta(days=3)).strftime('%Y-%m-%d')
    
    # Ищем подписки, которые истекают через 3 дня
    cursor.execute("SELECT user_id FROM subscriptions WHERE expire_date = ?", (future_date_str,))
    users_to_notify = cursor.fetchall()
    
    for user_id_tuple in users_to_notify:
        user_id = user_id_tuple[0]
        message = (
            "⏳ **ВНИМАНИЕ! Ваша подписка на Твоя Математика истекает через 3 дня** "
            f"({future_date_str}).\n\n"
            "Пожалуйста, убедитесь, что на вашей карте достаточно средств для автоматического "
            "продления (1500 ₽). Чтобы проверить статус, отправьте `/status`."
        )
        await send_notification(bot, user_id, message)


    # 2. ПОИСК ИСТЕКШИХ ПОДПИСОК (СЕГОДНЯ ИЛИ РАНЬШЕ)
    today_str = today.strftime('%Y-%m-%d')

    cursor.execute("SELECT user_id, username FROM subscriptions WHERE expire_date <= ?", (today_str,))
    expired_users = cursor.fetchall()
    logger.info(f"Найдено {len(expired_users)} просроченных подписок, подлежащих удалению.")

    for user_id, username in expired_users:
        try:
            await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            await send_notification(bot, user_id, "❌ **Ваша подписка на Твоя Математика истекла.** "
                                                  "Вы были удалены из канала. "
                                                  "Пожалуйста, продлите доступ, нажав `/start`.")
            
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
        "🔸 Регулярные разборы сложных задач.\n"
        "🔸 Прямые консультации с преподавателем.\n"
        "🔸 Архив всех материалов.\n\n"
        "Цена: **1500 рублей/месяц**."
    )
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton(text="💳 Оплатить картой РФ / СБП", callback_data="start_payment"))
    
    await message.answer(info_text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query_handler(lambda c: c.data == 'start_payment', state='*')
async def process_start_payment(callback_query: types.CallbackQuery, state: FSMContext):
    
    try:
        await bot.answer_callback_query(callback_query.id)
    except Exception as e:
        logger.error(f"Ошибка при ответе на Callback Query {callback_query.id}: {e}") 
        await asyncio.sleep(0.5)
        
    await PaymentStates.waiting_for_promo_code.set()
    
    promo_keyboard = InlineKeyboardMarkup(row_width=1)
    promo_keyboard.add(InlineKeyboardButton(text="Нет промокода", callback_data="skip_promo"))

    await bot.send_message(
        callback_query.from_user.id,
        "🎁 **Введите промокод (если есть)**.\n"
        "Регистр не важен☺️.",
        reply_markup=promo_keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query_handler(lambda c: c.data == 'skip_promo', state=PaymentStates.waiting_for_promo_code)
async def skip_promo_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id, text="Пропуск. Применяется полная цена.")
    
    await state.update_data(payment_price=BASE_PRICE, promo_applied=False)
    logger.info(f"Пользователь {callback_query.from_user.id} пропустил ввод промокода. Цена: {BASE_PRICE / 100} руб.")
    
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
        "📃 **Перед оплатой, пожалуйста, ознакомьтесь с Офертой и Политикой конфидециальности**.\n\n"
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
    payment_price = user_data.get('payment_price', BASE_PRICE) 
    is_promo = user_data.get('promo_applied', False)

    await state.set_state(None)
    logger.info(f"Пользователь {callback_query.from_user.id} согласился с офертой. Выставление счета. Цена: {payment_price / 100} RUB.")
    
    title_text = "Доступ в Твоя Математика (со скидкой)" if is_promo else "Доступ в Твоя Математика"
    price_label = f"Подписка на 1 месяц ({payment_price / 100:.0f} RUB)"

    await bot.send_invoice(
        chat_id=callback_query.from_user.id,
        title=title_text,
        description="Подписка на 1 месяц. Разборы задач и чат.",
        payload="math_sub_01", 
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=price_label, amount=payment_price)],
        is_flexible=False
    )

@dp.pre_checkout_query_handler(lambda query: True)
async def process_pre_checkout_query(pre_checkout_query):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message_handler(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or 'N/A'
    
    logger.info(f"Получено сообщение об успешной оплате от пользователя {user_id}.")

    try:
        user_data = await state.get_data()
        user_email = user_data.get('user_email', 'Email not collected') 
        
        expire_date = add_subscription(user_id, username, user_email, days=30, is_renewal=False) 

        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            name=f"Оплата: {message.from_user.full_name}",
            expire_date=datetime.datetime.now() + datetime.timedelta(days=30)
        )
        logger.info(f"Создана одноразовая ссылка для {user_id}: {invite.invite_link}")

        # УЛУЧШЕННОЕ СООБЩЕНИЕ ОБ УСПЕШНОЙ ОПЛАТЕ
        await bot.send_message(
            message.chat.id,
            f"🎉 **Оплата успешно произведена, добро пожаловать в клуб «Твоя Математика»!**\n\n"
            f"Ваша подписка активна до **{expire_date}**.\n"
            f"Для проверки статуса используйте команду `/status`.\n\n"
            f"Вот ваша **одноразовая** ссылка для входа: {invite.invite_link}\n\n"
            f"Если есть вопросы — пишите в поддержку **{SUPPORT_CONTACT}**.",
            parse_mode="Markdown"
        )
        await state.finish() 

    except Exception as e:
        logger.error(f"КРИТИЧЕСКАЯ ОШИБКА при обработке успешной оплаты для {user_id}: {e}")
        await bot.send_message(user_id, f"⚠️ **Критическая ошибка!** Оплата прошла, но бот не смог выдать ссылку. Пожалуйста, обратитесь в поддержку {SUPPORT_CONTACT}.", parse_mode="Markdown")

# --- ОБРАБОТЧИК АВТОМАТИЧЕСКОГО ПРОДЛЕНИЯ ---
@dp.message_handler(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def auto_renewal_payment(message: Message, state: FSMContext):
    
    user_id = message.from_user.id
    username = message.from_user.username or 'N/A'
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM subscriptions WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    user_email = result[0] if result else 'Email not found'

    expire_date = add_subscription(user_id, username, user_email, days=30, is_renewal=True) 
    
    await send_notification(
        bot, user_id, 
        f"✅ **Ваша подписка на Твоя Математика успешно продлена!**\n"
        f"Новая дата истечения: **{expire_date}**.\n"
        f"Статус всегда можно проверить командой `/status`."
    )

# --- НОВАЯ КОМАНДА /STATUS ---

@dp.message_handler(Command('status'))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    
    # Получаем статус и дату истечения
    status_text = get_subscription_status(user_id)
    
    if status_text == "Нет подписки":
        response = (
            "❌ **У вас нет активной подписки на Твоя Математика.**\n\n"
            "Чтобы получить доступ, нажмите `/start`."
        )
    elif status_text == "Истекла":
        response = (
            "⚠️ **Ваша подписка истекла.**\n\n"
            "Пожалуйста, продлите доступ, нажав `/start`."
        )
    else: # Активна до [дата]
        expire_date_str = status_text.split()[-1]
        
        # Генерация новой одноразовой ссылки на случай, если старая была утеряна
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                member_limit=1,
                name=f"Статус: {message.from_user.full_name}",
                expire_date=datetime.datetime.now() + datetime.timedelta(minutes=5) # ссылка действует 5 минут
            )
            invite_link = invite.invite_link
        except Exception as e:
             logger.error(f"Ошибка при создании ссылки для статуса {user_id}: {e}")
             invite_link = "*(Не удалось создать ссылку. Обратитесь в поддержку.)*"

        response = (
            "✅ **Ваша подписка на Твоя Математика активна!**\n\n"
            f"Срок действия: **до {expire_date_str}**.\n\n"
            f"По всем вопросам: {SUPPORT_CONTACT}"
        )
        
    await message.answer(response, parse_mode="Markdown")

# --- АДМИН-КОМАНДЫ (БЕЗ ИЗМЕНЕНИЙ В ЛОГИКЕ) ---

@dp.message_handler(Command('admin'))
async def cmd_admin(message: Message):
    
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return

    current_time_utc3 = datetime.datetime.now(ADMIN_TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')

    all_subs = get_subscription_status() 
    
    header = (
        f"👋 **Добро пожаловать, Администратор!**\n"
        f"Текущее время (UTC+3): **{current_time_utc3}**\n\n"
        f"**--- СПИСОК ПОДПИСЧИКОВ В БАЗЕ ---**"
    )
    
    response = [header]
    active_count = 0
    
    for user_id_db, username, email, expire_date_str in all_subs:
        
        try:
            expire_date = datetime.datetime.strptime(expire_date_str, '%Y-%m-%d')
            is_active = expire_date.date() > datetime.datetime.now().date()
        except ValueError:
            is_active = False
            expire_date_str = "Ошибка даты"

        if is_active:
            active_count += 1
        
        status_icon = "🟢" if is_active else "🔴"
        
        response.append(
            f"{status_icon} **{username}** (ID: {user_id_db})\n"
            f"   Email: `{email}`\n"
            f"   До: {expire_date_str}"
        )
        
    summary = (
        f"\n--- СТАТИСТИКА ---\n"
        f"✅ **Активных подписок:** {active_count}\n"
        f"📋 **Всего записей в базе:** {len(all_subs)}\n\n"
        f"Используйте: `/add [ID] [дни]` или `/remove [ID]`"
    )
    
    response.append(summary)

    if len(all_subs) == 0:
         await message.answer(f"{header}\n\nВ базе нет записей о подписчиках.", parse_mode="Markdown")
         return
    
    chunk_size = 4000
    full_response = "\n".join(response)

    for i in range(0, len(full_response), chunk_size):
        await message.answer(full_response[i:i + chunk_size], parse_mode="Markdown")


@dp.message_handler(Command('add'))
async def cmd_add(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("Нет доступа.")
    
    args = message.get_args().split()
    
    if len(args) != 2:
        return await message.answer("❌ **Неверный формат**. Использование: `/add [USER_ID] [ДНИ]`\n"
                                   "Пример: `/add 123456789 30`")

    try:
        user_id = int(args[0])
        days = int(args[1])
    except ValueError:
        return await message.answer("❌ **USER_ID и ДНИ** должны быть числовыми значениями.")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT username, email FROM subscriptions WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        username = f"ID_{user_id}_(New)"
        email = "Manual_Addition"
    else:
        username, email = result
        
    
    new_expire_date = add_subscription(user_id, username, email, days=days, is_renewal=True)

    await message.answer(f"✅ Подписка для пользователя **{user_id} ({username})** успешно **продлена** на **{days}** дней.\n"
                         f"Новая дата истечения: **{new_expire_date}**", parse_mode="Markdown")
    
    await send_notification(
        bot, user_id, 
        f"🎉 **Ваш доступ к Твоя Математика был вручную продлен Администратором!**\n"
        f"Срок действия продлен на **{days}** дней. Новая дата истечения: **{new_expire_date}**.\n"
        f"Проверить статус: `/status`."
    )


@dp.message_handler(Command('remove'))
async def cmd_remove(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("Нет доступа.")
    
    args = message.get_args().split()
    
    if len(args) != 1:
        return await message.answer("❌ **Неверный формат**. Использование: `/remove [USER_ID]`\n"
                                   "Пример: `/remove 123456789`")

    try:
        user_id = int(args[0])
    except ValueError:
        return await message.answer("❌ **USER_ID** должен быть числовым значением.")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM subscriptions WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()
    
    if not user_info:
        conn.close()
        return await message.answer(f"⚠️ Пользователь с ID **{user_id}** не найден в базе данных.", parse_mode="Markdown")

    username = user_info[0]

    # 1. Удаление из канала
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        channel_status = "Успешно удален/забанен в канале."
        await send_notification(bot, user_id, f"🚫 **Ваш доступ к Твоя Математика был аннулирован Администратором.** Вы были удалены из канала. По всем вопросам: {SUPPORT_CONTACT}")
    except Exception as e:
        channel_status = f"Ошибка при удалении/бане в канале: {e}"
        logger.error(f"Ошибка бана пользователя {user_id}: {e}")

    # 2. Удаление из базы данных
    cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    # Уведомление администратора
    await message.answer(
        f"✅ Пользователь **{user_id} ({username})** **удален**:\n"
        f"1. Удален из базы подписок.\n"
        f"2. {channel_status}",
        parse_mode="Markdown"
    )

# --- 7. ЗАПУСК БОТА ---
if __name__ == '__main__':
    init_db()
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)

