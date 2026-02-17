import asyncio
from datetime import datetime, date, time
from typing import Dict, Tuple, List

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import StateFilter

import os
from dotenv import load_dotenv
import math

load_dotenv()

# ----------------- Настройки -----------------
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')  # либо полная ссылка/ID таблицы

INVENTORY_SHEET_NAME = "Ресурсы лаборатории"  # лист со списком оборудования и количеством
BOOKINGS_SHEET_NAME = "Бронирование ресурсов"    # лист с бронированиями

PAGE_SIZE = 10  # показывать по 10 элементов на страницу

# Формат колонок на листе BOOKINGS (в этом порядке при записи)
BOOKING_COLUMNS = [
    "Дата проведения работ",            # YYYY-MM-DD
    "Имя и фамилия сотрудника(-ков)",
    "Необходимые ресурсы",              # формат: "Oscilloscope:2;Laptop:1"
    "Время начала работ",               # HH:MM
    "Время окончания работ",            # HH:MM
    "Имя руководителя проекта"
]
# --------------------------------------------

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# FSM для процесса бронирования
class BookingStates(StatesGroup):
    waiting_for_date = State()
    waiting_for_start = State()
    waiting_for_end = State()
    waiting_for_name = State()
    waiting_for_resources = State()
    waiting_for_manager = State()
    confirm = State()

# ----------------- Google Sheets helper -----------------
def init_gsheets():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh

async def get_inventory() -> Dict[str, int]:
    def _read():
        sh = init_gsheets()
        try:
            w = sh.worksheet(INVENTORY_SHEET_NAME)
        except Exception:
            w = sh.sheet1

        values = w.get_all_values()
        if not values or len(values) < 2:
            return {}

        headers = [h.strip().lower() for h in values[0]]

        # ищем нужные колонки
        name_col = None
        count_col = None

        for i, h in enumerate(headers):
            if "наимен" in h or "назв" in h or "name" in h:
                name_col = i
            if "кол" in h or "count" in h or "количество" in h:
                count_col = i

        if name_col is None or count_col is None:
            return {}

        inventory = {}

        for row in values[1:]:
            if len(row) <= max(name_col, count_col):
                continue

            name = row[name_col].strip()
            count_raw = row[count_col].strip()

            if not name:
                continue

            try:
                count = int(count_raw)
            except:
                count = 0

            inventory[name] = count

        return inventory

    return await asyncio.to_thread(_read)

async def get_bookings() -> List[Dict]:
    def _read():
        sh = init_gsheets()
        try:
            w = sh.worksheet(BOOKINGS_SHEET_NAME)
        except Exception:
            w = sh.get_worksheet(1)
        rows = w.get_all_records()
        return rows
    return await asyncio.to_thread(_read)

async def append_booking_row(row: List[str]):
    def _append():
        sh = init_gsheets()
        try:
            w = sh.worksheet(BOOKINGS_SHEET_NAME)
        except Exception:
            w = sh.get_worksheet(1)
        w.insert_row(row, index=2, value_input_option='USER_ENTERED')
    await asyncio.to_thread(_append)

# ----------------- Вспомогательные функции -----------------
def parse_resources(text: str) -> Dict[str, int]:
    """
    Ожидает формат: "Oscilloscope:2; Laptop:1" или "Oscilloscope 2, Laptop 1"
    Возвращает словарь {equipment_name: count}
    """
    res = {}
    for sep in [';', ',', '\n']:
        text = text.replace(sep, ';')
    for chunk in text.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ':' in chunk:
            name, cnt = chunk.split(':', 1)
        else:
            tokens = chunk.strip().rsplit(' ', 1)
            if len(tokens) == 2 and tokens[1].isdigit():
                name, cnt = tokens[0], tokens[1]
            else:
                name, cnt = chunk, '1'
        try:
            cnt_i = int(cnt.strip())
        except:
            cnt_i = 1
        res[name.strip()] = cnt_i
    return res

def times_overlap(s1: time, e1: time, s2: time, e2: time) -> bool:
    return (s1 < e2) and (s2 < e1)

async def check_availability(
    booking_date: date,
    start_time: time,
    end_time: time,
    requested: Dict[str, int]
) -> Tuple[bool, str]:
    inventory = await get_inventory()
    bookings = await get_bookings()

    def find_key(row_keys, substrs):
        for k in row_keys:
            klow = k.lower()
            for s in substrs:
                if s in klow:
                    return k
        return None

    conflicts_counts: Dict[str, int] = {}

    for row in bookings:
        keys = list(row.keys())
        date_key = find_key(keys, ['дата', 'date'])
        start_key = find_key(keys, ['время начала', 'start', 'начало'])
        end_key = find_key(keys, ['время окончания', 'end', 'конец'])
        resources_key = find_key(keys, ['ресурс', 'resource', 'необходим'])
        if not date_key or not start_key or not end_key or not resources_key:
            continue
        try:
            row_date = datetime.strptime(str(row[date_key]).strip(), "%Y-%m-%d").date()
        except:
            continue
        if row_date != booking_date:
            continue
        try:
            row_start = datetime.strptime(str(row[start_key]).strip(), "%H:%M").time()
            row_end = datetime.strptime(str(row[end_key]).strip(), "%H:%M").time()
        except:
            continue
        if not times_overlap(start_time, end_time, row_start, row_end):
            continue
        row_resources = parse_resources(str(row[resources_key]))
        for name, cnt in row_resources.items():
            conflicts_counts[name] = conflicts_counts.get(name, 0) + cnt

    for name, cnt in requested.items():
        inv_cnt = 0
        for inv_name, inv_num in inventory.items():
            if inv_name.strip().lower() == name.strip().lower():
                inv_cnt = inv_num
                break
        if inv_cnt == 0:
            return False, f"Оборудование '{name}' не найдено в инвентаре или его количество равно 0."
        used = conflicts_counts.get(name, 0)
        free = inv_cnt - used
        if free < cnt:
            return False, f"На {booking_date.isoformat()} с {start_time.strftime('%H:%M')} до {end_time.strftime('%H:%M')} свободно только {free} шт '{name}', а запрошено {cnt}."
    return True, "Доступно"

# ----------------- Утилиты для inline выбора (с пагинацией) -----------------
async def build_inventory_keyboard(inventory: Dict[str, int], page: int = 0, page_size: int = PAGE_SIZE) -> Tuple[InlineKeyboardMarkup, Dict[str, str], int]:
    """
    Возвращает (keyboard, callback_map, total_pages).
    callback_map: короткий ключ -> полное имя оборудования (ключи вида i{idx})
    keyboard содержит только slice для page.
    """
    items = list(inventory.items())
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size))
    # нормализуем страницу
    page = max(0, min(page, total_pages - 1))

    # создаём callback_map для всех элементов (ключи привязаны к индексам)
    callback_map: Dict[str, str] = {}
    for idx, (name, _) in enumerate(items):
        callback_map[f"i{idx}"] = name

    # строим видимые кнопки для текущей страницы
    start = page * page_size
    end = start + page_size
    rows = []
    for idx in range(start, min(end, total)):
        name, cnt = items[idx]
        key = f"i{idx}"
        btn = InlineKeyboardButton(text=f"{name} ({cnt})", callback_data=f"select:{key}")
        rows.append([btn])

    # строка корзина/завершить/ручной ввод
    rows.append([
        InlineKeyboardButton(text="Просмотреть корзину", callback_data="view_cart"),
        InlineKeyboardButton(text="Завершить выбор", callback_data="finish_selection")
    ])
    rows.append([InlineKeyboardButton(text="Ввести вручную (текст)", callback_data="manual_input")])

    # Навигация страниц: Prev / Page X/Y / Next
    nav_row = []
    if total_pages > 1:
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page-1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data=f"page:{page}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page+1}"))
        rows.append(nav_row)

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return kb, callback_map, total_pages

# ----------------- Команды бота -----------------
@dp.message(Command(commands=["start"]))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для бронирования оборудования.\n"
        "Команды:\n"
        "/list - показать оборудование и количество\n"
        "/book - забронировать оборудование\n"
        "/help - помощь"
    )

@dp.message(Command(commands=["help"]))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться:\n"
        "1) /list - посмотреть инвентарь\n"
        "2) /book - начать диалог бронирования\n"
        "   Можно выбрать ресурсы через кнопки или ввести текстом.\n"
        "3) Администратор может править таблицу напрямую в Google Sheets"
    )

@dp.message(Command(commands=["list"]))
async def cmd_list(message: Message):
    inv = await get_inventory()
    if not inv:
        await message.answer("Инвентарь пустой или не удалось прочитать лист.")
        return
    text = "Инвентарь:\n"
    for name, cnt in inv.items():
        text += f"- {name}: {cnt}\n"
    await message.answer(text)

# -------------- Процесс бронирования --------------
@dp.message(Command(commands=["book"]))
async def cmd_book(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Начинаем бронирование.\nВведите дату работ в формате YYYY-MM-DD (например 2026-03-10):")
    await state.set_state(BookingStates.waiting_for_date)

@dp.message(StateFilter(BookingStates.waiting_for_date))
async def process_date(message: Message, state: FSMContext):
    txt = message.text.strip()
    try:
        dt = datetime.strptime(txt, "%Y-%m-%d").date()
    except:
        await message.answer("Неправильный формат даты. Введите YYYY-MM-DD.")
        return
    await state.update_data(booking_date=dt.isoformat())
    await message.answer("Введите время начала в формате HH:MM (например 09:00):")
    await state.set_state(BookingStates.waiting_for_start)

@dp.message(StateFilter(BookingStates.waiting_for_start))
async def process_start(message: Message, state: FSMContext):
    txt = message.text.strip()
    try:
        t = datetime.strptime(txt, "%H:%M").time()
    except:
        await message.answer("Неправильный формат времени. Введите HH:MM.")
        return
    await state.update_data(start_time=t.strftime("%H:%M"))
    await message.answer("Введите время окончания в формате HH:MM (например 12:30):")
    await state.set_state(BookingStates.waiting_for_end)

@dp.message(StateFilter(BookingStates.waiting_for_end))
async def process_end(message: Message, state: FSMContext):
    txt = message.text.strip()
    try:
        t = datetime.strptime(txt, "%H:%M").time()
    except:
        await message.answer("Неправильный формат времени. Введите HH:MM.")
        return
    data = await state.get_data()
    start = datetime.strptime(data['start_time'], "%H:%M").time()
    if t <= start:
        await message.answer("Время окончания должно быть позже времени начала.")
        return
    await state.update_data(end_time=t.strftime("%H:%M"))
    await message.answer("Введите имя и фамилию сотрудника(-ков), кто будет работать:")
    await state.set_state(BookingStates.waiting_for_name)

@dp.message(StateFilter(BookingStates.waiting_for_name))
async def process_name(message: Message, state: FSMContext):
    txt = message.text.strip()
    if not txt:
        await message.answer("Напишите имя и фамилию.")
        return
    await state.update_data(employee_name=txt)

    # Переходим к выбору ресурсов — отправляем inline-кнопки (страница 0)
    inv = await get_inventory()
    kb, cmap, total_pages = await build_inventory_keyboard(inv, page=0)
    # Инициализируем пустую корзину и callback_map и текущую страницу
    await state.update_data(cart={}, callback_map=cmap, inv_page=0)
    await message.answer("Выберите необходимые ресурсы через кнопки. Можно добавлять несколько позиций.", reply_markup=kb)
    await state.set_state(BookingStates.waiting_for_resources)

# Обработчики callback'ов для выбора ресурсов (select/qty/page/view_cart/etc.)
@dp.callback_query(lambda c: c.data and c.data.startswith('select:'))
async def select_equipment(call: types.CallbackQuery, state: FSMContext):
    key = call.data.split(':', 1)[1]
    data = await state.get_data()
    callback_map = data.get('callback_map', {})
    name = callback_map.get(key)
    if not name:
        # Кнопка устарела — предложим обновить клавиатуру
        inv = await get_inventory()
        kb, cmap, _ = await build_inventory_keyboard(inv, page=0)
        await state.update_data(callback_map=cmap, inv_page=0)
        await call.message.answer("Эта кнопка устарела. Обновляю список — выберите снова:", reply_markup=kb)
        await call.answer()
        return

    inv = await get_inventory()
    max_cnt = inv.get(name, 0)
    await state.update_data(temp_select={'key': key, 'name': name, 'count': 1})

    kb_rows = [
        [
            InlineKeyboardButton(text='➖', callback_data=f'qty:{key}:dec'),
            InlineKeyboardButton(text='1', callback_data=f'qty:{key}:noop'),
            InlineKeyboardButton(text='➕', callback_data=f'qty:{key}:inc')
        ],
        [
            InlineKeyboardButton(text='Добавить в корзину', callback_data=f'qty:{key}:add'),
            InlineKeyboardButton(text='Отмена', callback_data='qty:cancel')
        ]
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await call.message.answer(f"Выбрано: {name}\nДоступно: {max_cnt}\nКоличество: 1", reply_markup=kb)
    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith('qty:'))
async def qty_handler(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(':')
    if len(parts) < 2:
        await call.answer()
        return
    if parts[1] == 'cancel':
        await call.message.answer('Выбор отменён.')
        await call.answer()
        inv = await get_inventory()
        kb, cmap, _ = await build_inventory_keyboard(inv, page=0)
        await state.update_data(callback_map=cmap, inv_page=0)
        await call.message.answer('Выберите ещё:', reply_markup=kb)
        return
    if len(parts) != 3:
        await call.answer()
        return
    _, key, action = parts[0], parts[1], parts[2]

    data = await state.get_data()
    callback_map = data.get('callback_map', {})
    name = callback_map.get(key)
    if not name:
        inv = await get_inventory()
        kb, cmap, _ = await build_inventory_keyboard(inv, page=0)
        await state.update_data(callback_map=cmap, inv_page=0)
        await call.message.answer("Кнопка устарела. Обновляю список — выберите снова:", reply_markup=kb)
        await call.answer()
        return

    temp = data.get('temp_select') or {}
    if temp.get('key') != key:
        count = 1
    else:
        count = temp.get('count', 1)

    inv = await get_inventory()
    max_cnt = inv.get(name, 0)

    if action == 'inc':
        if count < max_cnt:
            count += 1
    elif action == 'dec':
        if count > 1:
            count -= 1
    elif action == 'add':
        cart = data.get('cart', {})
        cart[name] = cart.get(name, 0) + count
        await state.update_data(cart=cart)
        await call.message.answer(f"Добавлено: {count} x {name} в корзину.")
        await call.answer()
        inv = await get_inventory()
        # оставляем пользователя на той же странице
        page = data.get('inv_page', 0)
        kb, cmap, _ = await build_inventory_keyboard(inv, page=page)
        await state.update_data(callback_map=cmap)
        await call.message.answer('Выберите ещё или завершите выбор:', reply_markup=kb)
        return

    await state.update_data(temp_select={'key': key, 'name': name, 'count': count})
    kb_rows = [
        [
            InlineKeyboardButton(text='➖', callback_data=f'qty:{key}:dec'),
            InlineKeyboardButton(text=str(count), callback_data=f'qty:{key}:noop'),
            InlineKeyboardButton(text='➕', callback_data=f'qty:{key}:inc')
        ],
        [
            InlineKeyboardButton(text='Добавить в корзину', callback_data=f'qty:{key}:add'),
            InlineKeyboardButton(text='Отмена', callback_data='qty:cancel')
        ]
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await call.message.edit_text(f"Выбрано: {name}\nДоступно: {max_cnt}\nКоличество: {count}", reply_markup=kb)
    except Exception:
        await call.message.answer(f"Выбрано: {name}\nДоступно: {max_cnt}\nКоличество: {count}", reply_markup=kb)
    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith('page:'))
async def page_handler(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(':', 1)
    if len(parts) != 2:
        await call.answer()
        return
    try:
        page = int(parts[1])
    except:
        await call.answer()
        return
    inv = await get_inventory()
    kb, cmap, total_pages = await build_inventory_keyboard(inv, page=page)
    await state.update_data(callback_map=cmap, inv_page=page)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        await call.message.answer("Обновляю список:", reply_markup=kb)
    await call.answer()

# ----------------------------------------------
# Обновлённый view_cart и cart_actions с удалением одной единицы
# ----------------------------------------------
@dp.callback_query(lambda c: c.data == 'view_cart')
async def view_cart(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get('cart', {})
    if not cart:
        await call.message.answer('Корзина пуста.')
        await call.answer()
        return

    # Формируем текст корзины и создаём карту индексов для безопасных callback'ов
    text_lines = ['Ваша корзина:']
    cart_map: Dict[str, str] = {}  # c{idx} -> name
    rows = []
    for idx, (name, qty) in enumerate(cart.items()):
        text_lines.append(f"{idx+1}. {name}: {qty}")
        key = f"c{idx}"
        cart_map[key] = name
        # Для каждой позиции добавим строку с кнопками: -1 и Удалить строку
        rows.append([
            InlineKeyboardButton(text=f"−1 ({idx+1})", callback_data=f"cart:dec:{key}"),
            InlineKeyboardButton(text="Удалить", callback_data=f"cart:remove:{key}")
        ])

    # Добавляем общие кнопки (очистить/закрыть)
    rows.append([
        InlineKeyboardButton(text='Очистить корзину', callback_data='cart:clear'),
        InlineKeyboardButton(text='Закрыть', callback_data='cart:close')
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    # Сохраняем карту в состоянии, чтобы callback'ы могли ссылаться на реальные имена
    await state.update_data(cart_map=cart_map)
    await call.message.answer('\n'.join(text_lines), reply_markup=kb)
    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith('cart:'))
async def cart_actions(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(':')
    # форматы:
    # cart:clear
    # cart:close
    # cart:dec:c{idx}
    # cart:remove:c{idx}
    if len(parts) < 2:
        await call.answer()
        return
    action = parts[1]
    data = await state.get_data()
    cart = data.get('cart', {})
    cart_map = data.get('cart_map', {})

    if action == 'clear':
        await state.update_data(cart={})
        await call.message.answer('Корзина очищена.')
        await call.answer()
        return

    if action == 'close':
        # отправляем обратно меню инвентаря — на той же странице, где был пользователь
        inv = await get_inventory()
        page = data.get('inv_page', 0)
        kb, cmap, _ = await build_inventory_keyboard(inv, page=page)
        # обновляем callback_map в состоянии
        await state.update_data(callback_map=cmap)
        # отвечаем пользователю клавиатурой выбора
        await call.message.answer('Выберите ещё или завершите выбор:', reply_markup=kb)
        await call.answer()
        return

    if action in ('dec', 'remove'):
        if len(parts) != 3:
            await call.answer()
            return
        key = parts[2]
        name = cart_map.get(key)
        if not name:
            await call.message.answer("Кнопка устарела — откройте корзину ещё раз.")
            await call.answer()
            return

        if action == 'dec':
            current = cart.get(name, 0)
            if current <= 1:
                cart.pop(name, None)
                await state.update_data(cart=cart)
                await call.message.answer(f"Позиция '{name}' удалена из корзины.")
            else:
                cart[name] = current - 1
                await state.update_data(cart=cart)
                await call.message.answer(f"Уменьшено: {name} на 1. Теперь: {cart[name]}")
            await call.answer()
            # пересобираем и показываем обновлённую корзину
            data2 = await state.get_data()
            cart2 = data2.get('cart', {})
            if not cart2:
                await call.message.answer('Корзина пуста.')
                return
            text_lines = ['Ваша корзина:']
            new_rows = []
            new_cart_map: Dict[str, str] = {}
            for idx, (n, q) in enumerate(cart2.items()):
                text_lines.append(f"{idx+1}. {n}: {q}")
                k = f"c{idx}"
                new_cart_map[k] = n
                new_rows.append([
                    InlineKeyboardButton(text=f"−1 ({idx+1})", callback_data=f"cart:dec:{k}"),
                    InlineKeyboardButton(text="Удалить", callback_data=f"cart:remove:{k}")
                ])
            new_rows.append([
                InlineKeyboardButton(text='Очистить корзину', callback_data='cart:clear'),
                InlineKeyboardButton(text='Закрыть', callback_data='cart:close')
            ])
            new_kb = InlineKeyboardMarkup(inline_keyboard=new_rows)
            await state.update_data(cart_map=new_cart_map)
            await call.message.answer('\n'.join(text_lines), reply_markup=new_kb)
            return

        if action == 'remove':
            if name in cart:
                cart.pop(name, None)
                await state.update_data(cart=cart)
                await call.message.answer(f"Позиция '{name}' полностью удалена из корзины.")
            else:
                await call.message.answer("Позиция уже отсутствует в корзине.")
            await call.answer()
            # обновляем отображение корзины
            data2 = await state.get_data()
            cart2 = data2.get('cart', {})
            if not cart2:
                await call.message.answer('Корзина пуста.')
                return
            text_lines = ['Ваша корзина:']
            new_rows = []
            new_cart_map: Dict[str, str] = {}
            for idx, (n, q) in enumerate(cart2.items()):
                text_lines.append(f"{idx+1}. {n}: {q}")
                k = f"c{idx}"
                new_cart_map[k] = n
                new_rows.append([
                    InlineKeyboardButton(text=f"−1 ({idx+1})", callback_data=f"cart:dec:{k}"),
                    InlineKeyboardButton(text="Удалить", callback_data=f"cart:remove:{k}")
                ])
            new_rows.append([
                InlineKeyboardButton(text='Очистить корзину', callback_data='cart:clear'),
                InlineKeyboardButton(text='Закрыть', callback_data='cart:close')
            ])
            new_kb = InlineKeyboardMarkup(inline_keyboard=new_rows)
            await state.update_data(cart_map=new_cart_map)
            await call.message.answer('\n'.join(text_lines), reply_markup=new_kb)
            return

    # fallback
    await call.answer()

# ----------------------------------------------

@dp.callback_query(lambda c: c.data == 'manual_input')
async def manual_input(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(cart={})
    await call.message.answer(
        "Введите необходимые ресурсы текстом в формате:\nOscilloscope:2; Laptop:1\n(или как раньше — без чисел считается 1)")
    await call.answer()

@dp.callback_query(lambda c: c.data == 'finish_selection')
async def finish_selection(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get('cart', {})
    if not cart:
        await call.message.answer('Корзина пуста — выберите хотя бы один ресурс или введите вручную.')
        await call.answer()
        return
    resources_text = '; '.join([f"{k}:{v}" for k, v in cart.items()])
    requested_parsed = {k: v for k, v in cart.items()}
    await state.update_data(resources=resources_text, requested_parsed=requested_parsed)
    await call.message.answer(f"Вы выбрали:\n{resources_text}\n\nВведите имя руководителя проекта:")
    await state.set_state(BookingStates.waiting_for_manager)
    await call.answer()

# Обработка текстового ввода ресурсов (если пользователь выбрал ввод вручную)
@dp.message(StateFilter(BookingStates.waiting_for_resources))
async def process_resources(message: Message, state: FSMContext):
    txt = message.text.strip()
    if not txt:
        await message.answer("Нужно указать хотя бы один ресурс.")
        return
    requested = parse_resources(txt)
    await state.update_data(resources=txt, requested_parsed=requested)
    await message.answer("Введите имя руководителя проекта:")
    await state.set_state(BookingStates.waiting_for_manager)

# ----------------- Новая логика: отправляем inline подтверждение -----------------
@dp.message(StateFilter(BookingStates.waiting_for_manager))
async def process_manager(message: Message, state: FSMContext):
    txt = message.text.strip()
    if not txt:
        await message.answer("Укажите имя руководителя проекта.")
        return
    await state.update_data(manager_name=txt)
    data = await state.get_data()

    # проверка доступности перед показом подтверждения
    booking_date = datetime.strptime(data['booking_date'], "%Y-%m-%d").date()
    start_time = datetime.strptime(data['start_time'], "%H:%M").time()
    end_time = datetime.strptime(data['end_time'], "%H:%M").time()
    requested = data.get('requested_parsed', {})

    ok, msg = await check_availability(booking_date, start_time, end_time, requested)
    if not ok:
        await message.answer(f"К сожалению, бронирование невозможно: {msg}")
        await state.clear()
        return

    # Формируем сводку и inline-кнопки подтверждения
    summary = (
        f"Подтвердите бронирование:\n"
        f"Дата: {booking_date.isoformat()}\n"
        f"Время: {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}\n"
        f"Сотрудник(-и): {data['employee_name']}\n"
        f"Ресурсы: {data.get('resources', '')}\n"
        f"Руководитель проекта: {data['manager_name']}\n\n"
    )
    kb_rows = [
        [
            InlineKeyboardButton(text="Подтвердить ✅", callback_data="confirm:yes"),
            InlineKeyboardButton(text="Отмена ❌", callback_data="confirm:cancel")
        ]
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer(summary, reply_markup=kb)
    await state.set_state(BookingStates.confirm)

@dp.callback_query(lambda c: c.data and c.data.startswith('confirm:'))
async def confirm_handler(call: types.CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    if action == "cancel":
        await call.message.answer("Бронирование отменено.")
        await state.clear()
        await call.answer()
        return

    # action == "yes"
    data = await state.get_data()
    booking_date = datetime.strptime(data['booking_date'], "%Y-%m-%d").date()
    start_time = datetime.strptime(data['start_time'], "%H:%M").time()
    end_time = datetime.strptime(data['end_time'], "%H:%M").time()
    requested = data.get('requested_parsed', {})

    ok, msg = await check_availability(booking_date, start_time, end_time, requested)
    if not ok:
        await call.message.answer(f"К сожалению, бронирование невозможно: {msg}")
        await state.clear()
        await call.answer()
        return

    row = [
        data['booking_date'],
        data['employee_name'],
        data['resources'],
        data['start_time'],
        data['end_time'],
        data['manager_name']
    ]
    await append_booking_row(row)
    await call.message.answer("Бронирование успешно записано в таблицу.")
    await state.clear()
    await call.answer()

@dp.message(StateFilter(BookingStates.confirm))
async def process_confirm_text(message: Message, state: FSMContext):
    txt = message.text.strip().lower()
    if txt in ("отмена", "cancel", "нет", "no"):
        await message.answer("Бронирование отменено.")
        await state.clear()
        return
    if txt in ("да", "ok", "yes"):
        data = await state.get_data()
        booking_date = datetime.strptime(data['booking_date'], "%Y-%m-%d").date()
        start_time = datetime.strptime(data['start_time'], "%H:%M").time()
        end_time = datetime.strptime(data['end_time'], "%H:%M").time()
        requested = data.get('requested_parsed', {})

        ok, msg = await check_availability(booking_date, start_time, end_time, requested)
        if not ok:
            await message.answer(f"К сожалению, бронирование невозможно: {msg}")
            await state.clear()
            return
        row = [
            data['booking_date'],
            data['employee_name'],
            data['resources'],
            data['start_time'],
            data['end_time'],
            data['manager_name']
        ]
        await append_booking_row(row)
        await message.answer("Бронирование успешно записано в таблицу.")
        await state.clear()
        return
    await message.answer("Нажмите кнопку 'Подтвердить' или 'Отмена' внизу, или напишите 'да'/'отмена'.")

# ----------------- Запуск бота -----------------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    print("Bot started")
    dp.run_polling(bot)
