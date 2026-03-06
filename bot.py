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


TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise ValueError("BOT_TOKEN not found")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found")


bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

pool = None


class AddHabit(StatesGroup):
    waiting_name = State()


async def init_db():

    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            timezone INTEGER DEFAULT 3
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


def main_menu():

    kb = ReplyKeyboardMarkup(resize_keyboard=True)

    kb.add("Отметить выполнение")
    kb.add("Мои привычки")
    kb.add("Статистика")
    kb.add("Добавить привычку")
    kb.add("Удалить привычку")

    return kb


async def build_today_widget(user_id):

    today = datetime.date.today()

    async with pool.acquire() as conn:

        habits = await conn.fetch("""
        SELECT id,name
        FROM habits
        WHERE user_id=$1
        """, user_id)

        kb = InlineKeyboardMarkup()

        for habit in habits:

            done = await conn.fetchrow("""
            SELECT id FROM habit_logs
            WHERE habit_id=$1 AND date=$2
            """, habit["id"], today)

            if done:

                kb.add(
                    InlineKeyboardButton(
                        f"{habit['name']} | снять",
                        callback_data=f"unmark_{habit['id']}"
                    )
                )

            else:

                kb.add(
                    InlineKeyboardButton(
                        f"{habit['name']} | выполнить",
                        callback_data=f"mark_{habit['id']}"
                    )
                )

    return kb


@dp.message_handler(commands="start")
async def start(msg: types.Message):

    tz = 3

    async with pool.acquire() as conn:

        await conn.execute("""
        INSERT INTO users (telegram_id,timezone)
        VALUES ($1,$2)
        ON CONFLICT (telegram_id)
        DO UPDATE SET timezone=$2
        """, msg.from_user.id, tz)

    await msg.answer("Трекер привычек запущен", reply_markup=main_menu())


@dp.message_handler(lambda m: m.text == "Добавить привычку")
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


@dp.message_handler(lambda m: m.text == "Мои привычки")
async def my_habits(msg: types.Message):

    async with pool.acquire() as conn:

        habits = await conn.fetch("""
        SELECT name
        FROM habits
        JOIN users ON habits.user_id = users.id
        WHERE users.telegram_id=$1
        """, msg.from_user.id)

    if not habits:
        await msg.answer("Привычек пока нет")
        return

    text = "\n".join([f"- {h['name']}" for h in habits])

    await msg.answer(text)


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


@dp.message_handler(lambda m: m.text == "Отметить выполнение")
async def open_widget(msg: types.Message):

    async with pool.acquire() as conn:

        user = await conn.fetchrow(
            "SELECT id FROM users WHERE telegram_id=$1",
            msg.from_user.id
        )

    kb = await build_today_widget(user["id"])

    await msg.answer("Привычки на сегодня", reply_markup=kb)


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

        habit = await conn.fetchrow(
            "SELECT name,user_id FROM habits WHERE id=$1",
            habit_id
        )

    streak = await get_streak(habit_id)

    kb = await build_today_widget(habit["user_id"])

    await call.message.edit_reply_markup(reply_markup=kb)

    await call.answer()

    await call.message.answer(
        f"Отметка добавлена\nСерия: {streak} дней"
    )


@dp.callback_query_handler(lambda c: c.data.startswith("unmark_"))
async def unmark_done(call: types.CallbackQuery):

    habit_id = int(call.data.split("_")[1])
    today = datetime.date.today()

    async with pool.acquire() as conn:

        await conn.execute("""
        DELETE FROM habit_logs
        WHERE habit_id=$1 AND date=$2
        """, habit_id, today)

        habit = await conn.fetchrow(
            "SELECT user_id FROM habits WHERE id=$1",
            habit_id
        )

    kb = await build_today_widget(habit["user_id"])

    await call.message.edit_reply_markup(reply_markup=kb)

    await call.answer()

    await call.message.answer("Снял отметку выполнения")


@dp.message_handler(lambda m: m.text == "Статистика")
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


@dp.message_handler(lambda m: m.text == "Удалить привычку")
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

    await msg.answer("Выбери привычку", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_habit(call: types.CallbackQuery):

    habit_id = int(call.data.split("_")[1])

    async with pool.acquire() as conn:

        await conn.execute("DELETE FROM habits WHERE id=$1", habit_id)
        await conn.execute("DELETE FROM habit_logs WHERE habit_id=$1", habit_id)

    await call.answer("Удалено")


async def reminder():

    utc_now = datetime.datetime.utcnow()

    async with pool.acquire() as conn:

        users = await conn.fetch("SELECT id,telegram_id,timezone FROM users")

        for user in users:

            local_hour = (utc_now.hour + user["timezone"]) % 24

            if local_hour not in [10, 21]:
                continue

            habits = await conn.fetch("""
            SELECT id,name FROM habits
            WHERE user_id=$1
            """, user["id"])

            today = datetime.date.today()

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

                text = (
                    "Давай сделаем этот день чуть лучше\n\nОтметь привычки на сегодня"
                    if local_hour == 10
                    else "Не забудь заполнить трекер привычек"
                )

                await bot.send_message(
                    user["telegram_id"],
                    text,
                    reply_markup=kb
                )


async def on_startup(dp):

    await init_db()

    scheduler = AsyncIOScheduler()

    scheduler.add_job(reminder, "interval", minutes=10)

    scheduler.start()


if __name__ == "__main__":

    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
