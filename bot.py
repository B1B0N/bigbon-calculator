import asyncio
import os
import re
import sqlite3
from datetime import date

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# В панели хостинга создай переменную окружения BOT_TOKEN
# и вставь туда токен, полученный у BotFather.
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавь переменную окружения BOT_TOKEN "
        "в настройках хостинга и вставь туда токен BotFather."
    )

bot = Bot(token=TOKEN)
dp = Dispatcher()

# База данных хранит завершённые тоталы.
db = sqlite3.connect("calculator.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS daily_totals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    total INTEGER NOT NULL,
    count INTEGER NOT NULL
)
""")
db.commit()

# Текущее состояние хранится в памяти процесса.
# Если хостинг перезапустит бота, незавершённый подсчёт сбросится.
totals = {}
counts = {}
active = set()
ambiguous = {}
batches = {}
batch_counter = {}


def format_number(number: int) -> str:
    return f"{number:,}".replace(",", " ")


def find_amounts(text: str) -> list[int]:
    """Находит числа-кандидаты и предварительно исключает похожие на ID/телефоны/карты."""
    text = re.sub(r"@\w+", " ", text)                 # username
    text = re.sub(r"[№#]\s*\d+", " ", text)           # номера с № или #

    # Номера карт из 13–19 цифр, в том числе с пробелами/дефисами.
    text = re.sub(
        r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)",
        " ",
        text
    )

    # Телефоны с +, пробелами, скобками и дефисами.
    text = re.sub(
        r"(?<!\w)\+?\d(?:[\d\s()\-]{6,}\d)(?!\w)",
        " ",
        text
    )

    # Числа из 3–5 цифр. Даты с точками/слэшами обычно не проходят
    # из-за границ слова, а числа внутри длинных идентификаторов исключены.
    matches = re.findall(r"(?<![\w\d])\d{3,5}(?![\w\d])", text)
    return [int(value) for value in matches]


@dp.message(Command("start"))
async def start(message: types.Message):
    chat_id = message.chat.id
    totals[chat_id] = 0
    counts[chat_id] = 0
    ambiguous[chat_id] = []
    active.add(chat_id)
    batches.pop(chat_id, None)

    await message.answer(
        "🧮 Новый подсчёт начат!\n\n"
        "Пересылай сообщения с суммами.\n"
        "Когда закончишь, напиши /total.\n\n"
        "Для просмотра итогов за день: /day"
    )


@dp.message(Command("day"))
async def day(message: types.Message):
    chat_id = message.chat.id
    today = date.today().isoformat()

    cursor = db.execute(
        """
        SELECT total FROM daily_totals
        WHERE chat_id = ? AND day = ?
        ORDER BY id ASC
        """,
        (chat_id, today)
    )
    results = cursor.fetchall()

    if not results:
        await message.answer("📅 За сегодня ещё нет завершённых тоталов.")
        return

    total_sum = sum(row[0] for row in results)
    text = f"📅 ИТОГИ ЗА {date.today().strftime('%d.%m.%Y')}\n\n"

    for index, row in enumerate(results, 1):
        text += f"🔹 Тотал №{index}: {format_number(row[0])}\n"

    text += (
        "\n━━━━━━━━━━━━━━\n"
        f"📊 Всего тоталов: {len(results)}\n"
        f"💰 Общая сумма: {format_number(total_sum)}"
    )
    await message.answer(text)


@dp.message(Command("total"))
async def total(message: types.Message):
    chat_id = message.chat.id

    if chat_id not in active:
        await message.answer("Сначала начни подсчёт командой /start.")
        return

    active.remove(chat_id)
    items = ambiguous.get(chat_id, [])

    batch_id = batch_counter.get(chat_id, 0) + 1
    batch_counter[chat_id] = batch_id

    batches[chat_id] = {
        "id": batch_id,
        "user_id": message.from_user.id,
        "items": items,
        "selected": {},
        "base_total": totals.get(chat_id, 0),
        "base_count": counts.get(chat_id, 0),
    }

    await message.answer(
        "📊 Сбор сообщений завершён!\n\n"
        f"✅ Обычных сумм: {counts.get(chat_id, 0)}\n"
        f"💰 Предварительная сумма: "
        f"{format_number(totals.get(chat_id, 0))}\n"
        f"⚠️ Требуют выбора: {len(items)}"
    )

    if not items:
        await finish_batch(chat_id)
        return

    for index, item in enumerate(items):
        buttons = [
            InlineKeyboardButton(
                text=f"➕ {format_number(number)}",
                callback_data=f"pick:{batch_id}:{index}:{choice}"
            )
            for choice, number in enumerate(item["numbers"])
        ]

        # Telegram ограничивает callback_data 64 байтами; в кнопках
        # используются только короткие числовые значения.
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[button] for button in buttons]
        )

        await message.answer(
            f"📩 Сообщение №{index + 1} из {len(items)}\n\n"
            f"Исходный текст:\n{item['text'][:2500]}\n\n"
            "👇 Выбери нужную сумму:",
            reply_markup=keyboard
        )


@dp.callback_query(F.data.startswith("pick:"))
async def choose_amount(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id

    try:
        _, batch_str, index_str, choice_str = callback.data.split(":")
        batch_id = int(batch_str)
        index = int(index_str)
        choice = int(choice_str)
    except (ValueError, AttributeError):
        await callback.answer("Не удалось обработать выбор.", show_alert=True)
        return

    batch = batches.get(chat_id)

    if not batch or batch["id"] != batch_id:
        await callback.answer("Этот подсчёт уже неактуален.")
        return

    if callback.from_user.id != batch["user_id"]:
        await callback.answer(
            "Выбрать сумму может только автор подсчёта.",
            show_alert=True
        )
        return

    if index < 0 or index >= len(batch["items"]):
        await callback.answer("Ошибка выбора.")
        return

    item = batch["items"][index]
    if choice < 0 or choice >= len(item["numbers"]):
        await callback.answer("Ошибка выбора.")
        return

    if index in batch["selected"]:
        await callback.answer("Для этого сообщения сумма уже выбрана.")
        return

    number = item["numbers"][choice]
    batch["selected"][index] = number

    await callback.message.edit_text(
        f"✅ Сообщение №{index + 1}\n"
        f"Выбрана сумма: {format_number(number)}"
    )
    await callback.answer("Сумма добавлена!")

    if len(batch["selected"]) == len(batch["items"]):
        await finish_batch(chat_id)


async def finish_batch(chat_id: int):
    batch = batches.get(chat_id)
    if not batch:
        return

    extra_total = sum(batch["selected"].values())
    extra_count = len(batch["selected"])
    final_total = batch["base_total"] + extra_total
    final_count = batch["base_count"] + extra_count
    today = date.today().isoformat()

    db.execute(
        """
        INSERT INTO daily_totals (chat_id, day, total, count)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, today, final_total, final_count)
    )
    db.commit()

    totals[chat_id] = final_total
    counts[chat_id] = final_count

    await bot.send_message(
        chat_id,
        "🏁 ТОТАЛ ЗАВЕРШЁН!\n\n"
        f"📊 Обычных чисел: {batch['base_count']}\n"
        f"📊 Выбрано вручную: {extra_count}\n\n"
        f"💰 ИТОГОВАЯ СУММА: {format_number(final_total)}\n"
        f"🔢 ВСЕГО ЧИСЕЛ: {final_count}\n\n"
        "📅 Посмотреть итоги за день: /day"
    )

    batches.pop(chat_id, None)
    ambiguous[chat_id] = []


@dp.message()
async def collect_messages(message: types.Message):
    chat_id = message.chat.id
    if chat_id not in active:
        return

    # Учитываем обычный текст и подпись к фото/документу.
    text = message.text or message.caption
    if not text:
        return

    numbers = find_amounts(text)
    if not numbers:
        return

    if len(numbers) == 1:
        totals[chat_id] = totals.get(chat_id, 0) + numbers[0]
        counts[chat_id] = counts.get(chat_id, 0) + 1
    else:
        ambiguous.setdefault(chat_id, []).append({
            "text": text,
            "numbers": numbers
        })


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
