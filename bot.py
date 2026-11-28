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
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from PIL import Image, ImageDraw, ImageFont
from aiogram.types import BufferedInputFile, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
BUILD = 'campaigns-5'
clicked_message_id = ContextVar('clicked_message_id', default=None)
import admin_panel as panel
import sys
panel.app = sys.modules[__name__]
dp = Dispatcher()
dp.include_router(panel.router)
store = None
bot = None
bot_name = None
ADMINS = {int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
LABELS = ['Забрать подарок', 'Оформить клубную карту', 'Новогодний розыгрыш', 'Наши магазины', 'Наш канал', 'Связаться с нами', 'Доставка\nВ разработке']

def link(label, key):
    url = panel.config('url:'+key, os.getenv(key, '')).strip()
    label = panel.config('button:'+label,label)
    if urlparse(url).scheme == 'https' and urlparse(url).netloc:
        return Button(text=label, url=url)
    return Button(text=label, callback_data='missing')

def keyboard(*buttons):
    return Keyboard(inline_keyboard=[[b.model_copy(update={'text':panel.config('button:'+b.text,b.text)})] for b in buttons])

BACK = Button(text='В главное меню', callback_data='menu')

def menu():
    labels=[panel.config('button:'+x,x) for x in LABELS]
    return keyboard(*(link(label, 'CHANNEL_URL') if i == 4 else Button(text=label, callback_data=f'page:{i}') for i, label in enumerate(labels)))

def banner():
    image = Image.new('RGB', (800, 180), '#722c41')
    draw = ImageDraw.Draw(image)
    draw.text((400, 90), 'PRICHAL', fill='#fff9ee', anchor='mm', font=ImageFont.load_default(size=52))
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()

async def retire_menu(uid):
    ids = {store.data['users'].get(str(uid), {}).get('screen_message_id'), store.data.get('admin_screens', {}).get(str(uid))}
    for message_id in ids - {None}:
        try:
            await bot.edit_message_reply_markup(chat_id=uid, message_id=message_id, reply_markup=None)
        except TelegramBadRequest as exc:
            if not any(s in exc.message.lower() for s in ('message is not modified', 'message to edit not found', 'message can\'t be edited')):
                raise

async def navigation_keys(uid):
    role = 'admin' if uid in ADMINS else 'user'
    # Stored delivery is not evidence that a Telegram client still shows it.
    # Resend on every explicit menu command, never on inline transitions.
    labels = ['Главное меню'] + (['Админ-панель'] if uid in ADMINS else [])
    await bot.send_message(uid, 'Выберите раздел 👇', reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=s) for s in labels]], resize_keyboard=True, is_persistent=True, one_time_keyboard=False))
    with store.change() as data:
        data.setdefault('navigation_keys', {})[str(uid)] = role

async def screen(uid, text, reply_markup, photo=None, fresh=False, promo_code=None):
    """One durable media message; changing media also replaces its caption/buttons."""
    text = panel.config('text:'+text,text)
    if promo_code is not None:
        text += f'\n\nПромокод: {promo_code}\nЕсли QR не считывается, назовите этот код продавцу.'
    user = store.data['users'][str(uid)]
    clicked = clicked_message_id.get()
    message_id = clicked if clicked is not None else user.get('screen_message_id')
    if fresh:
        await retire_menu(uid)
        message_id = None
    page = user.get('page','Главное меню')
    custom = panel.config('image:'+page,'')
    # A personal QR replaces the cover entirely; covers are for screens without a QR.
    upload = BufferedInputFile(photo,filename='qr.png') if photo is not None else custom or BufferedInputFile(banner(),filename='screen.png')
    if message_id:
        try:
            result = await bot.edit_message_media(
                chat_id=uid, message_id=message_id,
                media=InputMediaPhoto(media=upload, caption=text), reply_markup=reply_markup)
            with store.change() as data:
                data['users'][str(uid)]['screen_message_id'] = message_id
                if uid in ADMINS:
                    data.setdefault('admin_screens', {})[str(uid)] = message_id
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
        if uid in ADMINS:
            data.setdefault('admin_screens', {})[str(uid)] = result.message_id
    return result

async def event(uid, kind, detail=None, campaign_id=None):
    with store.change() as data:
        data['events'].append(dict(user_id=uid, kind=kind, detail=detail, campaign_id=campaign_id or data['campaign']['id'], at=time.time()))

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
    seconds = panel.config('delay:'+branch,seconds)
    with store.change() as data:
        data['jobs'].setdefault(f'{uid}:{branch}', dict(user_id=uid, due_at=time.time()+seconds))

async def invite(uid, manual=False):
    campaign_id = store.data['campaign']['id']
    if not manual and (store.data['campaign']['status']!='active' or not panel.config('timers',True)):
        return
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
        msg = await (screen if manual else bot.send_message)(uid, panel.config('text:Призы на 2 000 000 рублей!','Призы на 2 000 000 рублей!') if manual else panel.config('text:Новогодний розыгрыш — призы на 2 000 000 рублей!','Новогодний розыгрыш — призы на 2 000 000 рублей!'), reply_markup=keyboard(link('Узнать условия участия', 'RAFFLE_POST_URL'), BACK))
    except Exception:
        with store.change() as data:
            target = data if data['campaign']['id']==campaign_id else data['campaign_archives'][campaign_id]
            target['invitations'][str(uid)]['status'] = 'uncertain'
        log.exception('Invitation outcome uncertain for %s', uid)
        return
    with store.change() as data:
        target = data if data['campaign']['id']==campaign_id else data['campaign_archives'][campaign_id]
        target['invitations'][str(uid)].update(status='sent', message_id=msg.message_id)
    await event(uid, 'invitation_sent', 'manual' if manual else 'scheduled', campaign_id=campaign_id)

async def coupon(uid):
    if store.data['campaign']['status']!='active':
        await screen(uid,panel.config('campaign_ended','Акция завершена. Сейчас активных акций нет. Следите за новыми предложениями!'),reply_markup=keyboard(BACK))
        return
    if not panel.config('enabled',True) and not store.data['users'][str(uid)].get('code'):
        await screen(uid,panel.config('pause','Выдача подарков временно приостановлена.'),reply_markup=keyboard(BACK))
        return
    code = store.assign(uid)
    if code is None:
        await screen(uid, 'Подарочные коды пока закончились. Попробуйте позже.', reply_markup=keyboard(BACK))
        return
    buffer = io.BytesIO()
    qrcode.make(code).save(buffer, format='PNG')
    await screen(uid, photo=buffer.getvalue(), text='Покажи этот QR-код продавцу, чтобы получить подарок', promo_code=code, reply_markup=keyboard(link('Узнать наши адреса', 'ADDRESSES_URL'), BACK))
    await schedule(uid, 'coupon', 120)
    await event(uid, 'coupon_shown')
    await panel.low_stock()

@dp.message(CommandStart(), F.chat.type == 'private')
@dp.message(F.text == 'Главное меню', F.chat.type == 'private')
async def start(message):
    uid = message.from_user.id
    # Leave editor mode and switch the visible admin screen to the client menu.
    panel.pending.pop(uid, None)
    payload = message.text.partition(' ')[2][:64] if message.text.startswith('/start') else ''
    with store.change() as data:
        user = data['users'].setdefault(str(uid), dict(source=payload, created_at=time.time()))
        user.update(username=message.from_user.username, first_name=message.from_user.first_name, last_seen=time.time())
    with store.change() as data: data['users'][str(uid)]['page']='Главное меню'
    await event(uid, 'start')
    await navigation_keys(uid)
    await screen(uid, 'Главное меню', reply_markup=menu(), fresh=True)

@dp.callback_query()
async def navigate(query):
    if (query.data or '').startswith('a:'):
        await panel.action(query)
        return
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
        with store.change() as data: data['users'][str(uid)]['page']='Главное меню'
        await screen(uid, 'Главное меню', reply_markup=menu())
    elif query.data.startswith('page:'):
        page = query.data[5:]
        if page.isdigit() and int(page)<len(LABELS):
            with store.change() as data: data['users'][str(uid)]['page']=LABELS[int(page)]
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
@dp.message(F.text == 'Админ-панель', F.chat.type == 'private')
async def admin(message):
    await panel.admin(message)

@dp.message(F.document, F.chat.type == 'private')
async def upload(message):
    if message.from_user.id not in ADMINS:
        await panel.show(message, 'Загрузка доступна только администратору.')
        return
    document = message.document
    if not (document.file_name or '').lower().endswith('.xlsx') or (document.file_size or 0) > 5*1024*1024:
        await panel.show(message, 'Нужен файл .xlsx размером до 5 МБ.')
        return
    campaign_id = store.data['campaign']['id']
    content = io.BytesIO()
    await bot.download(document, destination=content)
    try:
        codes = read_codes(content.getvalue())
    except Exception as exc:
        log.warning('Invalid workbook: %s', type(exc).__name__)
        await panel.show(message, str(exc) if isinstance(exc, ValueError) else 'Не удалось прочитать Excel. Проверьте формат .xlsx.')
        return
    if store.data['campaign']['id']!=campaign_id:
        await panel.show(message,'Акция сменилась во время загрузки. Отправьте Excel заново.'); return
    before=set(store.data['codes'])
    added = store.import_codes(codes)
    with store.change() as data:
        batch=str(message.message_id)+'-'+str(message.from_user.id)
        data.setdefault('batches',{})[batch]=[c for c in codes if c not in before]
        panel.audit(data,message.from_user.id,'import '+batch+': '+str(added))
    await panel.low_stock()
    await event(message.from_user.id, 'codes_imported', str(added))
    await panel.show(message, f'Добавлено: {added}. Дубликатов и ранее выданных кодов пропущено: {len(codes)-added}. Ранее выданные коды сохранены.')

@dp.message(F.chat.type=='private')
async def admin_input(message):
    await panel.input_value(message)

async def worker():
    while True:
        try:
            await panel.low_stock()
            campaign_id=store.data['campaign']['id']
            due = {j['user_id'] for j in store.data['jobs'].values() if store.data['campaign']['status']=='active' and panel.config('timers',True) and not j.get('cancelled') and j['due_at'] <= time.time() and str(j['user_id']) not in store.data['invitations']}
            for uid in due:
                if store.data['campaign']['id']!=campaign_id: break
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
