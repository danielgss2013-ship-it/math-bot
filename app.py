import os
import sqlite3
import datetime
import asyncio
from aiogram import Bot, Dispatcher, types 
from aiogram.utils import executor 
from aiogram.types import Message, LabeledPrice, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, InputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.dispatcher.filters import Command, Text
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from logger import logger 

# --- 1. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN") # Убедись, что тут БОЕВОЙ токен

CHANNEL_ID = -1003328408384
ADMIN_ID = 405491563 
OFFER_FILENAME = 'oferta.pdf' 
DB_PATH = '/data/subscriptions.db'

BASE_PRICE = 150000   # 1500 RUB
PROMO_PRICE = 75000   # 750 RUB
PROMO_CODE = 'FIRST'
ADMIN_TIMEZONE = datetime.timezone(datetime.timedelta(hours=3))
SUPPORT_CONTACT = "@dankurbanoff"

BOT_USERNAME = "tvoya_math_bot"
SUBSCRIPTION_DAYS = 30
REFERRAL_BONUS_DAYS = 14 

# --- МЕНЮ (НИЖНИЕ КНОПКИ) ---
def get_main_menu():
    menu = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    menu.add(KeyboardButton("👤 Мой Аккаунт"))
    menu.add(KeyboardButton("🤝 Реферальная программа"), KeyboardButton("ℹ️ О нас / Помощь"))
    return menu

class PaymentStates(StatesGroup):
    waiting_for_promo_code = State()
    waiting_for_email = State()
    waiting_for_agreement = State()

# --- 2. БАЗА ДАННЫХ И ФОНОВЫЕ ЗАДАЧИ ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            email TEXT,
            expire_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_subscription_status(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM subscriptions WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if not result: return "Нет подписки"
    expire_date = datetime.datetime.strptime(result[0], '%Y-%m-%d').date()
    return f"Активна до {result[0]}" if expire_date > datetime.datetime.now().date() else "Истекла"

def add_subscription(user_id, username, email, days, is_renewal=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM subscriptions WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    start_date = datetime.datetime.now().date()
    if res:
        current_exp = datetime.datetime.strptime(res[0], '%Y-%m-%d').date()
        if current_exp > start_date: start_date = current_exp
    new_date = (start_date + datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    cursor.execute("""
        INSERT INTO subscriptions (user_id, username, email, expire_date)
        VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET expire_date=?, username=?, email=?
    """, (user_id, username, email, new_date, new_date, username, email))
    conn.commit()
    conn.close()
    return new_date

async def check_expirations(bot: Bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.datetime.now().date().strftime('%Y-%m-%d')
    cursor.execute("SELECT user_id, username FROM subscriptions WHERE expire_date <= ?", (today_str,))
    expired = cursor.fetchall()
    for uid, uname in expired:
        try:
            await bot.ban_chat_member(CHANNEL_ID, uid)
            await bot.send_message(uid, "❌ Срок подписки истек. Вы удалены из канала. Продлите доступ в меню бота.")
            cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (uid,))
            conn.commit()
        except: pass
    conn.close()

# --- 3. ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
scheduler = AsyncIOScheduler()

async def on_startup(dp):
    init_db()
    scheduler.add_job(check_expirations, 'cron', hour=0, minute=1, args=(bot,))
    scheduler.start()

# --- 4. ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: Message, state: FSMContext):
    await state.finish()
    payload = message.get_args()
    if payload and payload.startswith('ref_'):
        try:
            ref_id = int(payload.split('_')[1])
            if ref_id != message.from_user.id and "Активна" in get_subscription_status(ref_id):
                await state.update_data(referrer_id=ref_id)
                await message.answer("🤝 Приятно познакомиться! Вы пришли по рекомендации друга — это круто.")
        except: pass

    welcome_text = (
        "👋 **Добро пожаловать в «Твоя Математика»!**\n\n"
        "Это закрытое комьюнити для тех, кто хочет разбираться в предмете без нервов и зубрежки. "
        "Мы собрали всё самое важное для твоей подготовки в одном месте.\n\n"
        "**Что тебя ждет в канале:**\n"
        "🔹 Ежедневные разборы актуальных задач.\n"
        "🔹 Возможность задать вопрос преподавателю напрямую.\n"
        "🔹 Доступ к базе авторских шпаргалок и гайдов.\n\n"
        "💳 **Стоимость доступа:** 1500₽ за 30 дней.\n"
        "✅ **Безопасность:** Если в течение 24 часов поймешь, что клуб тебе не подходит — мы вернем оплату полностью."
    )
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("💳 Оформить подписку", callback_data="start_payment"))
    await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")
    await message.answer("⬇️ Для управления аккаунтом используй кнопки внизу:", reply_markup=get_main_menu())

@dp.callback_query_handler(lambda c: c.data == 'start_payment', state='*')
async def start_pay(c: types.CallbackQuery):
    await PaymentStates.waiting_for_promo_code.set()
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("Нет промокода", callback_data="skip_promo"))
    await bot.send_message(c.from_user.id, "🎁 **Есть промокод?** Пиши сюда.\nЕсли нет — жми кнопку ниже.", reply_markup=kb, parse_mode="Markdown")

@dp.message_handler(state=PaymentStates.waiting_for_promo_code)
async def promo(m: Message, state: FSMContext):
    price = PROMO_PRICE if m.text.strip().upper() == PROMO_CODE else BASE_PRICE
    await state.update_data(payment_price=price)
    await PaymentStates.waiting_for_email.set()
    await m.answer("✉️ **Напишите ваш Email** для регистрации оплаты.")

@dp.callback_query_handler(lambda c: c.data == 'skip_promo', state=PaymentStates.waiting_for_promo_code)
async def skip_promo(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(payment_price=BASE_PRICE)
    await PaymentStates.waiting_for_email.set()
    await bot.send_message(c.from_user.id, "✉️ **Напишите ваш Email**.")

@dp.message_handler(state=PaymentStates.waiting_for_email)
async def email(m: Message, state: FSMContext):
    if '@' not in m.text: return await m.answer("Пожалуйста, введите корректный Email.")
    await state.update_data(user_email=m.text)
    await PaymentStates.waiting_for_agreement.set()
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("✅ Согласен с офертой", callback_data="agree"))
    await m.answer("📃 Почти готово! Ознакомься с офертой.")
    try: await bot.send_document(m.chat.id, InputFile(OFFER_FILENAME), reply_markup=kb)
    except: await m.answer("Нажимая кнопку, вы соглашаетесь с условиями оферты.", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'agree', state=PaymentStates.waiting_for_agreement)
async def send_invoice(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    await bot.send_invoice(
        c.from_user.id, title="Доступ в клуб", description="Подписка на 1 месяц",
        payload="sub", provider_token=PAYMENT_TOKEN, currency="RUB",
        prices=[LabeledPrice("Клуб 'Твоя Математика'", data['payment_price'])]
    )

@dp.pre_checkout_query_handler(lambda q: True)
async def pre_check(q): await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message_handler(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def success_pay(m: Message, state: FSMContext):
    user_id, username = m.from_user.id, m.from_user.username or "N/A"
    data = await state.get_data()
    expire = add_subscription(user_id, username, data.get('user_email', 'N/A'), SUBSCRIPTION_DAYS)

    if 'referrer_id' in data:
        rid = data['referrer_id']
        add_subscription(rid, "Ref", "Ref", REFERRAL_BONUS_DAYS, True)
        try: await bot.send_message(rid, f"🎁 Бонус! Друг @{username} оплатил подписку. Вам начислено +14 дней!")
        except: pass

    invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
    await m.answer(f"🎉 **Оплата прошла!**\nДоступ до {expire}.\n\n🔗 **Ссылка на вход:**\n{invite.invite_link}", reply_markup=get_main_menu())
    await state.finish()

# --- КНОПКИ МЕНЮ ---

@dp.message_handler(Text(equals="👤 Мой Аккаунт"))
async def my_acc(m: Message):
    status = get_subscription_status(m.from_user.id)
    await m.answer(f"👤 **Аккаунт:** {m.from_user.full_name}\n📊 **Статус:** {status}\n\nПо вопросам: {SUPPORT_CONTACT}", parse_mode="Markdown")

@dp.message_handler(Text(equals="🤝 Реферальная программа"))
async def my_ref(m: Message):
    if "Активна" not in get_subscription_status(m.from_user.id):
        return await m.answer("⚠️ Рефералка доступна только участникам клуба.")
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{m.from_user.id}"
    await m.answer(f"👥 **Зови друзей — учись бесплатно!**\nЗа каждого друга дарим +14 дней.\n\n🔗 **Твоя ссылка:**\n`{link}`", parse_mode="Markdown")

@dp.message_handler(Text(equals="ℹ️ О нас / Помощь"))
async def about(m: Message):
    text = (
        "🧠 **ТВОЯ МАТЕМАТИКА — это твой личный чит-код.**\n\n"
        "Мы здесь, чтобы доказать: математика — это не душные формулы, а база для крутого будущего. 🚀\n\n"
        "**Что ты получаешь внутри:**\n"
        "✅ **Daily Разборы:** Решаем задачи из реальных экзаменов без воды.\n"
        "✅ **Fast Support:** Не понял решение? Пиши преподу, разберемся вместе.\n"
        "✅ **Архив Знаний:** Шпаргалки и гайды доступны сразу в закрепе.\n\n"
        "**Твои гарантии:**\n"
        "Мы уверены в контенте. Если поймешь, что не зашло — в течение 24 часов сделаем возврат. 💸\n\n"
        f"✍️ **Поддержка:** {SUPPORT_CONTACT}"
    )
    await m.answer(text, parse_mode="Markdown")

# --- АДМИНКА ---
@dp.message_handler(Command('admin'))
async def adm(m: Message):
    if m.from_user.id == ADMIN_ID: await m.answer("Админ-панель:\n`/add ID ДНИ` - выдать доступ\n`/remove ID` - удалить")

@dp.message_handler(Command('add'))
async def adm_add(m: Message):
    if m.from_user.id != ADMIN_ID: return
    args = m.get_args().split()
    add_subscription(int(args[0]), "Admin", "Manual", int(args[1]), True)
    await m.answer("Готово.")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
