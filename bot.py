import asyncio
import contextlib
import io
import logging
import os
import time
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse

from storage import Store
from importer import read_codes
import qrcode
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto
from PIL import Image, ImageDraw, ImageFont
from aiogram.types import BufferedInputFile, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
BUILD = 'single-menu-2'
clicked_message_id = ContextVar('clicked_message_id', default=None)
dp = Dispatcher()
store = None
bot = None
bot_name = None
ADMINS = {int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
LABELS = ['Забрать подарок', 'Оформить клубную карту', 'Новогодний розыгрыш', 'Наши магазины', 'Наш канал', 'Связаться с нами', 'Доставка\nВ разработке']

def link(label, key):
    url = os.getenv(key, '').strip()
    if urlparse(url).scheme == 'https' and urlparse(url).netloc:
        return Button(text=label, url=url)
    return Button(text=label, callback_data='missing')

def keyboard(*buttons):
    return Keyboard(inline_keyboard=[[b] for b in buttons])

BACK = Button(text='В главное меню', callback_data='menu')

def menu():
    return keyboard(*(link(label, 'CHANNEL_URL') if i == 4 else Button(text=label, callback_data=f'page:{i}') for i, label in enumerate(LABELS)))

def banner():
    image = Image.new('RGB', (800, 180), '#722c41')
    draw = ImageDraw.Draw(image)
    draw.text((400, 90), 'PRICHAL', fill='#fff9ee', anchor='mm', font=ImageFont.load_default(size=52))
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()

async def screen(uid, text, reply_markup, photo=None):
    """One durable media message; changing media also replaces its caption/buttons."""
    user = store.data['users'][str(uid)]
    clicked = clicked_message_id.get()
    message_id = clicked if clicked is not None else user.get('screen_message_id')
    upload = BufferedInputFile(photo if photo is not None else banner(), filename='screen.png')
    if message_id:
        try:
            result = await bot.edit_message_media(
                chat_id=uid, message_id=message_id,
                media=InputMediaPhoto(media=upload, caption=text), reply_markup=reply_markup)
            with store.change() as data:
                data['users'][str(uid)]['screen_message_id'] = message_id
            return result
        except TelegramBadRequest as exc:
            if 'message is not modified' in exc.message.lower():
                from types import SimpleNamespace
                return SimpleNamespace(message_id=message_id)
            # A button click must NEVER create a replacement message.
            if clicked is not None or 'message to edit not found' not in exc.message.lower():
                raise
    result = await bot.send_photo(uid, photo=upload, caption=text, reply_markup=reply_markup)
    with store.change() as data:
        data['users'][str(uid)]['screen_message_id'] = result.message_id
    return result

async def event(uid, kind, detail=None):
    with store.change() as data:
        data['events'].append(dict(user_id=uid, kind=kind, detail=detail, at=time.time()))

class Deduplicate(BaseMiddleware):
    async def __call__(self, handler, update, data):
        if update.update_id in store.data['updates']:
            return
        result = await handler(update, data)
        with store.change() as state:
            state['updates'] = (state['updates'] + [update.update_id])[-10000:]
        return result

dp.update.outer_middleware(Deduplicate())

async def schedule(uid, branch, seconds):
    with store.change() as data:
        data['jobs'].setdefault(f'{uid}:{branch}', dict(user_id=uid, due_at=time.time()+seconds))

async def invite(uid, manual=False):
    # Claim persisted before Telegram call: uncertain outcomes never auto-retry.
    claimed = None
    if str(uid) not in store.data['invitations']:
        with store.change() as data:
            data['invitations'][str(uid)] = dict(status='sending', at=time.time())
        claimed = uid
    if claimed is None:
        if manual:
            await screen(uid, 'Призы на 2 000 000 рублей!', reply_markup=keyboard(link('Узнать условия участия', 'RAFFLE_POST_URL'), BACK))
        return
    try:
        msg = await (screen if manual else bot.send_message)(uid, 'Призы на 2 000 000 рублей!' if manual else 'Новогодний розыгрыш — призы на 2 000 000 рублей!', reply_markup=keyboard(link('Узнать условия участия', 'RAFFLE_POST_URL'), BACK))
    except Exception:
        with store.change() as data:
            data['invitations'][str(uid)]['status'] = 'uncertain'
        log.exception('Invitation outcome uncertain for %s', uid)
        return
    with store.change() as data:
        data['invitations'][str(uid)].update(status='sent', message_id=msg.message_id)
    await event(uid, 'invitation_sent', 'manual' if manual else 'scheduled')

async def coupon(uid):
    code = store.assign(uid)
    if code is None:
        await screen(uid, 'Подарочные коды пока закончились. Попробуйте позже.', reply_markup=keyboard(BACK))
        return
    buffer = io.BytesIO()
    qrcode.make(code).save(buffer, format='PNG')
    await screen(uid, photo=buffer.getvalue(), text='Покажи этот QR-код продавцу, чтобы получить подарок', reply_markup=keyboard(link('Узнать наши адреса', 'ADDRESSES_URL'), BACK))
    await schedule(uid, 'coupon', 120)
    await event(uid, 'coupon_shown')

@dp.message(CommandStart(), F.chat.type == 'private')
async def start(message):
    uid = message.from_user.id
    payload = message.text.partition(' ')[2][:64]
    with store.change() as data:
        user = data['users'].setdefault(str(uid), dict(source=payload, created_at=time.time()))
        user.update(username=message.from_user.username, first_name=message.from_user.first_name, last_seen=time.time())
    await event(uid, 'start')
    await screen(uid, 'Главное меню', reply_markup=menu())

@dp.callback_query()
async def navigate(query):
    if not query.message or query.message.chat.type != 'private':
        await query.answer()
        return
    token = clicked_message_id.set(query.message.message_id)
    try:
        await navigate_current(query)
    except TelegramBadRequest:
        log.exception('Cannot edit clicked menu message; no replacement sent')
    finally:
        clicked_message_id.reset(token)

async def navigate_current(query):
    if not query.message or query.message.chat.type != 'private':
        await query.answer()
        return
    uid = query.from_user.id
    if str(uid) not in store.data['users']:
        await query.answer('Сначала нажмите /start', show_alert=True)
        return
    await query.answer()
    if query.data == 'missing':
        await screen(uid, 'Ссылка пока не добавлена. Попробуйте позже.', reply_markup=keyboard(BACK))
    elif query.data == 'menu':
        await screen(uid, 'Главное меню', reply_markup=menu())
    elif query.data.startswith('page:'):
        page = query.data[5:]
        await event(uid, 'menu_open', page)
        if page == '0':
            await coupon(uid)
        elif page == '1':
            await screen(uid, 'Причаливай к своим! Оформи клубную карту Причала', reply_markup=keyboard(link('Оформить карту', 'SBIS_URL'), link('Узнать привилегии карты', 'CARD_POST_URL'), BACK))
            await schedule(uid, 'card', 180)
        elif page == '2':
            await invite(uid, manual=True)
        elif page == '3':
            await screen(uid, 'Найди свой „Причал“! Адреса магазинов, часы работы и ссылки на карты — в нашем канале.', reply_markup=keyboard(link('Узнать наши адреса', 'ADDRESSES_URL'), BACK))
        elif page == '5':
            await screen(uid, 'Есть вопрос? Напиши нам в личные сообщения „Причала“.', reply_markup=keyboard(link('Написать в Telegram', 'SUPPORT_URL'), BACK))
        elif page == '6':
            await screen(uid, 'Раздел доставки пока в разработке. Здесь появятся условия, когда всё будет готово.', reply_markup=keyboard(BACK))

@dp.message(Command('admin'), F.chat.type == 'private')
async def admin(message):
    if message.from_user.id not in ADMINS:
        return
    await message.answer(f'Версия: {BUILD}')
    assigned = sum(owner is not None for owner in store.data['codes'].values())
    uncertain = sum(i['status'] == 'uncertain' for i in store.data['invitations'].values())
    await message.answer(f"Кодов: {len(store.data['codes'])}. Выдано: {assigned}. Осталось: {len(store.data['codes'])-assigned}. Спорных приглашений: {uncertain}.\nДля загрузки отправьте сюда файл .xlsx: первый лист, столбец A, начиная со второй строки.")

@dp.message(F.document, F.chat.type == 'private')
async def upload(message):
    if message.from_user.id not in ADMINS:
        await message.answer('Загрузка доступна только администратору.')
        return
    document = message.document
    if not (document.file_name or '').lower().endswith('.xlsx') or (document.file_size or 0) > 5*1024*1024:
        await message.answer('Нужен файл .xlsx размером до 5 МБ.')
        return
    content = io.BytesIO()
    await bot.download(document, destination=content)
    try:
        codes = read_codes(content.getvalue())
    except Exception as exc:
        log.warning('Invalid workbook: %s', type(exc).__name__)
        await message.answer(str(exc) if isinstance(exc, ValueError) else 'Не удалось прочитать Excel. Проверьте формат .xlsx.')
        return
    added = store.import_codes(codes)
    await event(message.from_user.id, 'codes_imported', str(added))
    await message.answer(f'Добавлено: {added}. Дубликатов пропущено: {len(codes)-added}. Ранее выданные коды сохранены.')

async def worker():
    while True:
        try:
            due = {j['user_id'] for j in store.data['jobs'].values() if j['due_at'] <= time.time() and str(j['user_id']) not in store.data['invitations']}
            for uid in due:
                await invite(uid)
        except Exception:
            log.exception('Scheduler failure')
        await asyncio.sleep(1)

async def main():
    global store, bot
    store = Store(os.getenv('DATA_DIR', '/app/data'))
    log.info('Starting %s', BUILD)
    bot = Bot(os.environ['BOT_TOKEN'])
    task = asyncio.create_task(worker())
    try:
        await dp.start_polling(bot, handle_as_tasks=False)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await bot.session.close()
        store.close()

if __name__ == '__main__':
    asyncio.run(main())
