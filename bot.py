import datetime
import calendar
import os
import asyncpg

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils import executor

from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ---------- ENV ----------

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise ValueError("BOT_TOKEN not found")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found")


# ---------- INIT ----------

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

pool = None


# ---------- FSM ----------

class AddHabit(StatesGroup):
    waiting_name = State()


# ---------- DATABASE ----------

async def init_db():

    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS habits(
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            name TEXT
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS habit_logs(
            id SERIAL PRIMARY KEY,
            habit_id INTEGER,
            date DATE,
            UNIQUE(habit_id,date)
        )
        """)


# ---------- MENU ----------

def main_menu():

    kb = ReplyKeyboardMarkup(resize_keyboard=True)

    kb.add("✅ Отметить выполнение")
    kb.add("📋 Мои привычки")
    kb.add("📊 Статистика")
    kb.add("➕ Добавить привычку")
    kb.add("❌ Удалить привычку")

    return kb


# ---------- START ----------

@dp.message_handler(commands="start")
async def start(msg: types.Message):

    async with pool.acquire() as conn:

        await conn.execute(
            "INSERT INTO users (telegram_id) VALUES ($1) ON CONFLICT DO NOTHING",
            msg.from_user.id
        )

    await msg.answer(
        "Трекер привычек готов ✅",
        reply_markup=main_menu()
    )


# ---------- ADD HABIT ----------

@dp.message_handler(lambda m: m.text == "➕ Добавить привычку")
async def add_habit(msg: types.Message):

    await msg.answer("Напиши название привычки")
    await AddHabit.waiting_name.set()


@dp.message_handler(state=AddHabit.waiting_name)
async def save_habit(msg: types.Message, state: FSMContext):

    async with pool.acquire() as conn:

        user = await conn.fetchrow(
            "SELECT id FROM users WHERE telegram_id=$1",
            msg.from_user.id
        )

        await conn.execute(
            "INSERT INTO habits (user_id,name) VALUES ($1,$2)",
            user["id"], msg.text
        )

    await msg.answer("Привычка добавлена", reply_markup=main_menu())
    await state.finish()


# ---------- MY HABITS ----------

@dp.message_handler(lambda m: m.text == "📋 Мои привычки")
async def my_habits(msg: types.Message):

    async with pool.acquire() as conn:

        habits = await conn.fetch("""
        SELECT name
        FROM habits
        JOIN users ON habits.user_id = users.id
        WHERE users.telegram_id=$1
        """, msg.from_user.id)

    if not habits:

        await msg.answer("У тебя пока нет привычек")
        return

    text = "\n".join([f"• {h['name']}" for h in habits])

    await msg.answer(text)


# ---------- STREAK ----------

async def get_streak(habit_id):

    async with pool.acquire() as conn:

        rows = await conn.fetch("""
        SELECT date
        FROM habit_logs
        WHERE habit_id=$1
        ORDER BY date DESC
        """, habit_id)

    days = [r["date"] for r in rows]

    today = datetime.date.today()

    streak = 0

    for i, day in enumerate(days):

        if day == today - datetime.timedelta(days=i):

            streak += 1
        else:
            break

    return streak


# ---------- MARK HABIT ----------

@dp.message_handler(lambda m: m.text == "✅ Отметить выполнение")
async def mark_menu(msg: types.Message):

    today = datetime.date.today()

    async with pool.acquire() as conn:

        habits = await conn.fetch("""
        SELECT habits.id, habits.name
        FROM habits
        JOIN users ON habits.user_id = users.id
        WHERE users.telegram_id=$1
        """, msg.from_user.id)

        kb = InlineKeyboardMarkup()

        for habit in habits:

            done = await conn.fetchrow("""
            SELECT id FROM habit_logs
            WHERE habit_id=$1 AND date=$2
            """, habit["id"], today)

            if not done:

                kb.add(
                    InlineKeyboardButton(
                        habit["name"],
                        callback_data=f"mark_{habit['id']}"
                    )
                )

    if kb.inline_keyboard:

        await msg.answer("Выбери привычку:", reply_markup=kb)

    else:

        await msg.answer("Сегодня всё выполнено 🎉")


@dp.callback_query_handler(lambda c: c.data.startswith("mark_"))
async def mark_done(call: types.CallbackQuery):

    habit_id = int(call.data.split("_")[1])
    today = datetime.date.today()

    async with pool.acquire() as conn:

        await conn.execute("""
        INSERT INTO habit_logs (habit_id,date)
        VALUES ($1,$2)
        ON CONFLICT DO NOTHING
        """, habit_id, today)

    streak = await get_streak(habit_id)

    await call.answer()

    await call.message.answer(
        f"Отмечено ✅\n🔥 Серия: {streak} дней"
    )


# ---------- STATS ----------

@dp.message_handler(lambda m: m.text == "📊 Статистика")
async def stats(msg: types.Message):

    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=6)
    month_days = calendar.monthrange(today.year, today.month)[1]

    async with pool.acquire() as conn:

        habits = await conn.fetch("""
        SELECT habits.id, habits.name
        FROM habits
        JOIN users ON habits.user_id = users.id
        WHERE users.telegram_id=$1
        """, msg.from_user.id)

        text = ""

        for habit in habits:

            week = await conn.fetchval("""
            SELECT COUNT(*)
            FROM habit_logs
            WHERE habit_id=$1 AND date >= $2
            """, habit["id"], week_start)

            month = await conn.fetchval("""
            SELECT COUNT(*)
            FROM habit_logs
            WHERE habit_id=$1
            AND EXTRACT(MONTH FROM date)=$2
            """, habit["id"], today.month)

            text += f"{habit['name']}\n{week}/7 ({month}/{month_days})\n\n"

    await msg.answer(text)


# ---------- DELETE HABIT ----------

@dp.message_handler(lambda m: m.text == "❌ Удалить привычку")
async def delete_menu(msg: types.Message):

    async with pool.acquire() as conn:

        habits = await conn.fetch("""
        SELECT habits.id, habits.name
        FROM habits
        JOIN users ON habits.user_id = users.id
        WHERE users.telegram_id=$1
        """, msg.from_user.id)

    kb = InlineKeyboardMarkup()

    for habit in habits:

        kb.add(
            InlineKeyboardButton(
                habit["name"],
                callback_data=f"del_{habit['id']}"
            )
        )

    await msg.answer("Выбери привычку для удаления", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_habit(call: types.CallbackQuery):

    habit_id = int(call.data.split("_")[1])

    async with pool.acquire() as conn:

        await conn.execute("DELETE FROM habits WHERE id=$1", habit_id)
        await conn.execute("DELETE FROM habit_logs WHERE habit_id=$1", habit_id)

    await call.answer("Удалено")


# ---------- REMINDER ----------

async def reminder():

    today = datetime.date.today()

    async with pool.acquire() as conn:

        users = await conn.fetch("SELECT id,telegram_id FROM users")

        for user in users:

            habits = await conn.fetch("""
            SELECT id,name FROM habits
            WHERE user_id=$1
            """, user["id"])

            kb = InlineKeyboardMarkup()

            for habit in habits:

                done = await conn.fetchrow("""
                SELECT id FROM habit_logs
                WHERE habit_id=$1 AND date=$2
                """, habit["id"], today)

                if not done:

                    kb.add(
                        InlineKeyboardButton(
                            habit["name"],
                            callback_data=f"mark_{habit['id']}"
                        )
                    )

            if kb.inline_keyboard:

                await bot.send_message(
                    user["telegram_id"],
                    "Не забудь отметить привычки за сегодня",
                    reply_markup=kb
                )


# ---------- STARTUP ----------

async def on_startup(dp):

    await init_db()

    scheduler = AsyncIOScheduler()

    scheduler.add_job(reminder, "cron", hour=21, minute=0)

    scheduler.start()


# ---------- RUN ----------

if __name__ == "__main__":

    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
