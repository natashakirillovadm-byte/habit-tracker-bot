import datetime
import calendar
import aiosqlite

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils import executor

from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from apscheduler.schedulers.asyncio import AsyncIOScheduler


import os
TOKEN = os.getenv("BOT_TOKEN")
DB = "habits.db"

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)


# ---------- FSM ----------

class AddHabit(StatesGroup):
    waiting_name = State()


# ---------- БАЗА ----------

async def init_db():

    async with aiosqlite.connect(DB) as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS habits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS habit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        habit_id INTEGER,
        date TEXT
        )
        """)

        await db.commit()


# ---------- МЕНЮ ----------

def main_menu():

    kb = ReplyKeyboardMarkup(resize_keyboard=True)

    kb.add("➕ Добавить привычку")
    kb.add("📋 Мои привычки")
    kb.add("✅ Отметить выполнение")
    kb.add("📊 Статистика")
    kb.add("❌ Удалить привычку")

    return kb


# ---------- START ----------

@dp.message_handler(commands="start")
async def start(msg: types.Message):

    async with aiosqlite.connect(DB) as db:

        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
            (msg.from_user.id,)
        )

        await db.commit()

    await msg.answer("Трекер привычек готов", reply_markup=main_menu())


# ---------- ДОБАВИТЬ ПРИВЫЧКУ ----------

@dp.message_handler(lambda m: m.text == "➕ Добавить привычку")
async def add_habit(msg: types.Message):

    await msg.answer("Напиши название привычки")
    await AddHabit.waiting_name.set()


@dp.message_handler(state=AddHabit.waiting_name)
async def save_habit(msg: types.Message, state: FSMContext):

    async with aiosqlite.connect(DB) as db:

        user = await db.execute(
            "SELECT id FROM users WHERE telegram_id=?",
            (msg.from_user.id,)
        )

        user_id = (await user.fetchone())[0]

        await db.execute(
            "INSERT INTO habits (user_id,name) VALUES (?,?)",
            (user_id,msg.text)
        )

        await db.commit()

    await msg.answer("Привычка добавлена", reply_markup=main_menu())

    await state.finish()


# ---------- МОИ ПРИВЫЧКИ ----------

@dp.message_handler(lambda m: m.text == "📋 Мои привычки")
async def my_habits(msg: types.Message):

    async with aiosqlite.connect(DB) as db:

        user = await db.execute(
            "SELECT id FROM users WHERE telegram_id=?",
            (msg.from_user.id,)
        )

        user_id = (await user.fetchone())[0]

        habits = await db.execute(
            "SELECT name FROM habits WHERE user_id=?",
            (user_id,)
        )

        rows = await habits.fetchall()

    if not rows:

        await msg.answer("У тебя пока нет привычек")
        return

    text = "\n".join([f"• {h[0]}" for h in rows])

    await msg.answer(text)


# ---------- ОТМЕТКА ----------

@dp.message_handler(lambda m: m.text == "✅ Отметить выполнение")
async def mark_menu(msg: types.Message):

    today = datetime.date.today().isoformat()

    async with aiosqlite.connect(DB) as db:

        user = await db.execute(
            "SELECT id FROM users WHERE telegram_id=?",
            (msg.from_user.id,)
        )

        user_id = (await user.fetchone())[0]

        habits = await db.execute(
            "SELECT id,name FROM habits WHERE user_id=?",
            (user_id,)
        )

        rows = await habits.fetchall()

        kb = InlineKeyboardMarkup()

        for habit in rows:

            check = await db.execute(
                "SELECT id FROM habit_logs WHERE habit_id=? AND date=?",
                (habit[0],today)
            )

            done = await check.fetchone()

            if not done:

                kb.add(
                    InlineKeyboardButton(
                        habit[1],
                        callback_data=f"mark_{habit[0]}"
                    )
                )

    if kb.inline_keyboard:

        await msg.answer("Выбери привычку", reply_markup=kb)

    else:

        await msg.answer("Сегодня всё выполнено")


@dp.callback_query_handler(lambda c: c.data.startswith("mark_"))
async def mark_done(call: types.CallbackQuery):

    habit_id = int(call.data.split("_")[1])

    today = datetime.date.today().isoformat()

    async with aiosqlite.connect(DB) as db:

        check = await db.execute(
            "SELECT id FROM habit_logs WHERE habit_id=? AND date=?",
            (habit_id,today)
        )

        exists = await check.fetchone()

        if not exists:

            await db.execute(
                "INSERT INTO habit_logs (habit_id,date) VALUES (?,?)",
                (habit_id,today)
            )

            await db.commit()

    await call.answer("Отмечено")


# ---------- СЕРИЯ ----------

async def get_streak(habit_id):

    async with aiosqlite.connect(DB) as db:

        logs = await db.execute(
            "SELECT date FROM habit_logs WHERE habit_id=? ORDER BY date DESC",
            (habit_id,)
        )

        rows = await logs.fetchall()

    days = [datetime.date.fromisoformat(r[0]) for r in rows]

    today = datetime.date.today()

    streak = 0

    for i,day in enumerate(days):

        if day == today - datetime.timedelta(days=i):

            streak += 1

        else:

            break

    return streak


# ---------- СТАТИСТИКА ----------

@dp.message_handler(lambda m: m.text == "📊 Статистика")
async def stats(msg: types.Message):

    today = datetime.date.today()

    week_start = today - datetime.timedelta(days=6)

    month_days = calendar.monthrange(today.year, today.month)[1]

    async with aiosqlite.connect(DB) as db:

        user = await db.execute(
            "SELECT id FROM users WHERE telegram_id=?",
            (msg.from_user.id,)
        )

        user_id = (await user.fetchone())[0]

        habits = await db.execute(
            "SELECT id,name FROM habits WHERE user_id=?",
            (user_id,)
        )

        rows = await habits.fetchall()

        text = ""

        for habit in rows:

            week = await db.execute("""
            SELECT COUNT(*) FROM habit_logs
            WHERE habit_id=? AND date>=?
            """,(habit[0],week_start.isoformat()))

            week_count = (await week.fetchone())[0]

            month = await db.execute("""
            SELECT COUNT(*) FROM habit_logs
            WHERE habit_id=? AND strftime('%m',date)=?
            """,(habit[0],f"{today.month:02d}"))

            month_count = (await month.fetchone())[0]

            streak = await get_streak(habit[0])

            text += f"""
{habit[1]}
Серия: {streak}
7 дней: {week_count}/7
Месяц: {month_count}/{month_days}

"""

    await msg.answer(text)


# ---------- УДАЛИТЬ ----------

@dp.message_handler(lambda m: m.text == "❌ Удалить привычку")
async def delete_menu(msg: types.Message):

    async with aiosqlite.connect(DB) as db:

        user = await db.execute(
            "SELECT id FROM users WHERE telegram_id=?",
            (msg.from_user.id,)
        )

        user_id = (await user.fetchone())[0]

        habits = await db.execute(
            "SELECT id,name FROM habits WHERE user_id=?",
            (user_id,)
        )

        rows = await habits.fetchall()

    kb = InlineKeyboardMarkup()

    for h in rows:

        kb.add(
            InlineKeyboardButton(
                f"Удалить {h[1]}",
                callback_data=f"del_{h[0]}"
            )
        )

    await msg.answer("Выбери привычку", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_habit(call: types.CallbackQuery):

    habit_id = int(call.data.split("_")[1])

    async with aiosqlite.connect(DB) as db:

        await db.execute("DELETE FROM habits WHERE id=?",(habit_id,))
        await db.execute("DELETE FROM habit_logs WHERE habit_id=?",(habit_id,))

        await db.commit()

    await call.answer("Удалено")


# ---------- НАПОМИНАНИЕ ----------

async def reminder():

    today = datetime.date.today().isoformat()

    async with aiosqlite.connect(DB) as db:

        users = await db.execute("SELECT telegram_id,id FROM users")

        rows = await users.fetchall()

        for tg,user_id in rows:

            habits = await db.execute(
                "SELECT id,name FROM habits WHERE user_id=?",
                (user_id,)
            )

            h = await habits.fetchall()

            kb = InlineKeyboardMarkup()

            for habit in h:

                check = await db.execute(
                    "SELECT id FROM habit_logs WHERE habit_id=? AND date=?",
                    (habit[0],today)
                )

                done = await check.fetchone()

                if not done:

                    kb.add(
                        InlineKeyboardButton(
                            habit[1],
                            callback_data=f"mark_{habit[0]}"
                        )
                    )

            if kb.inline_keyboard:

                await bot.send_message(
                    tg,
                    "Не забудь отметить привычки за сегодня",
                    reply_markup=kb
                )


# ---------- STARTUP ----------

async def on_startup(dp):

    await init_db()

    scheduler = AsyncIOScheduler()

    scheduler.add_job(reminder, "cron", hour=21, minute=0)

    scheduler.start()


if __name__ == "__main__":

    executor.start_polling(dp, on_startup=on_startup)
