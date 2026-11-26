import copy
import io
import json
import time
from contextvars import ContextVar
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto
from urllib.parse import urlparse
from openpyxl import Workbook
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K, BufferedInputFile

router = Router()
app = None
pending = {}
active_message = ContextVar('admin_message', default=None)

async def show(message, text, reply_markup=None, photo=None):
    uid = message.chat.id
    if uid not in app.ADMINS:
        return
    clicked = active_message.get()
    message_id = clicked or app.store.data.get('admin_screens', {}).get(str(uid))
    media = InputMediaPhoto(media=photo or BufferedInputFile(app.banner(), filename='admin.png'), caption=text)
    markup = reply_markup or keyboard([('В админ-меню', 'home')])
    if message_id:
        try:
            result = await app.bot.edit_message_media(chat_id=uid, message_id=message_id, media=media, reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' in exc.message.lower():
                with app.store.change() as data:
                    data.setdefault('admin_screens', {})[str(uid)] = message_id
                return
            if clicked or 'message to edit not found' not in exc.message.lower():
                raise
        else:
            with app.store.change() as data:
                data.setdefault('admin_screens', {})[str(uid)] = message_id
            return result
    result = await app.bot.send_photo(uid, photo=media.media, caption=text, reply_markup=markup)
    with app.store.change() as data:
        data.setdefault('admin_screens', {})[str(uid)] = result.message_id
    return result

def config(key, default=None):
    return app.store.data.get('settings', {}).get(key, default)

def audit(data, uid, action):
    data['events'].append(dict(user_id=uid, kind='admin', detail=action, at=time.time()))

def commit(uid, key, value):
    with app.store.change() as data:
        settings = data.setdefault('settings', {})
        data['undo'] = copy.deepcopy(settings)
        settings[key] = value
        audit(data, uid, 'set '+key)

def keys():
    source = __import__('pathlib').Path(app.__file__).read_text(encoding='utf-8')
    import ast
    tree = ast.parse(source)
    texts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'screen':
            candidate = node.args[1] if len(node.args)>1 else next((k.value for k in node.keywords if k.arg=='text'), None)
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value,str): texts.add(candidate.value)
    texts.update(['Призы на 2 000 000 рублей!', 'Новогодний розыгрыш — призы на 2 000 000 рублей!'])
    return sorted(texts)

def keyboard(items):
    if items is not HOME and not any(action == 'home' for _, action in items):
        items = list(items) + [('В админ-меню', 'home')]
    return K(inline_keyboard=[[B(text=label, callback_data='a:'+action)] for label,action in items])

HOME = [('Выдача: включить / выключить','issue'),('Загрузить Excel','import'),('Удалить свободные коды','delete'),('Статистика и Excel','stats'),('Тексты разделов','texts'),('Названия кнопок','buttons'),('Ссылки','links'),('Картинки разделов','images'),('Отложенные приглашения','timers'),('Порог остатка','threshold'),('Текст паузы выдачи','pause'),('Отменить последнее изменение','undo'),('Скачать резервную копию','backup'),('Журнал действий','log')]

async def home(message, notice=""):
    await show(message, f'{notice}Админ-панель {app.BUILD}\nВыдача: {config("enabled",True)}; приглашения: {config("timers",True)}',reply_markup=keyboard(HOME))

@router.message(Command('admin'), F.chat.type=='private')
async def admin(message):
    if message.from_user.id in app.ADMINS:
        pending.pop(message.from_user.id,None)
        await home(message)

@router.callback_query(F.data.startswith('a:'))
async def action(q):
    if q.from_user.id not in app.ADMINS or not q.message or q.message.chat.type != 'private':
        await q.answer('Нет доступа', show_alert=True)
        return
    token = active_message.set(q.message.message_id)
    try:
        await action_current(q)
    finally:
        active_message.reset(token)

async def action_current(q):
    uid=q.from_user.id
    if uid not in app.ADMINS or not q.message or q.message.chat.type!='private':
        await q.answer('Нет доступа',show_alert=True); return
    await q.answer()
    cmd=q.data[2:]
    if cmd=='home':
        pending.pop(uid,None); await home(q.message); return
    if cmd=='issue':
        commit(uid,'enabled',not config('enabled',True)); await home(q.message); return
    if cmd=='import':
        pending.pop(uid,None)
        await show(q.message, 'Отправьте .xlsx: первый лист, столбец A, без заголовка первой строки.'); return
    if cmd=='delete':
        batches=app.store.data.get('batches',{})
        await show(q.message, 'Удаляются только НЕвыданные коды. Выберите загрузку или введите коды.',reply_markup=keyboard([('Все свободные','del:all'),('Выборочно','del:select')]+[(b,'del:'+b) for b in list(batches)[-20:]])); return
    if cmd.startswith('del:'):
        target=cmd[4:]
        if target=='select':
            pending[uid]={'input':'delete'}
            await show(q.message, 'Введите коды через пробел.'); return
        codes=list(app.store.data['codes']) if target=='all' else app.store.data.get('batches',{}).get(target,[])
        pending[uid]={'delete':codes}
        await show(q.message, f'Удаление свободных кодов из выбранных {len(codes)} записей. Подтвердить?',reply_markup=keyboard([('Подтвердить','save'),('Отмена','home')])); return
    if cmd in ('texts','buttons','links','images'):
        options = keys() if cmd=='texts' else app.LABELS+['В главное меню','Узнать наши адреса','Оформить карту','Узнать привилегии карты','Узнать условия участия','Написать в Telegram'] if cmd=='buttons' else ['SBIS_URL','CHANNEL_URL','ADDRESSES_URL','CARD_POST_URL','RAFFLE_POST_URL','SUPPORT_URL'] if cmd=='links' else ['Главное меню']+app.LABELS
        pending[uid]={'options':options,'group':cmd}
        await show(q.message, 'Выберите элемент:',reply_markup=keyboard([(s[:60],f'edit:{i}') for i,s in enumerate(options)])); return
    if cmd.startswith('edit:'):
        state=pending.get(uid,{})
        if 'options' not in state: return
        index=int(cmd[5:]); label=state['options'][index]; group=state['group']
        key={'texts':'text:','buttons':'button:','links':'url:','images':'image:'}[group]+label
        pending[uid]={'input':key}
        await show(q.message, f'{label}\nТекущее: {str(config(key,"по умолчанию"))[:600]}\nПришлите новое значение'+(' или фото. Знак - удаляет картинку.' if group=='images' else '. Знак - очищает ссылку.' if group=='links' else '.')); return
    if cmd=='timers':
        await show(q.message, 'При выключении задания остаются в JSON. При включении выберите их судьбу.',reply_markup=keyboard([('Выключить','timer:off'),('Включить, сохранить задания','timer:keep'),('Включить, отменить старые задания','timer:cancel'),('Задержка подарка (сек.)','delay:coupon'),('Задержка карты (сек.)','delay:card')])); return
    if cmd.startswith('timer:'):
        with app.store.change() as data:
            data.setdefault('settings',{})['timers']=cmd!='timer:off'
            if cmd=='timer:cancel':
                for job in data['jobs'].values(): job['cancelled']=True
            audit(data,uid,cmd)
        await home(q.message); return
    if cmd.startswith('delay:') or cmd in ('threshold','pause'):
        pending[uid]={'input':cmd}
        await show(q.message, 'Введите новое значение. Задержка: 1–86400 секунд, порог: 0–50000. Изменение задержки действует на новые задания.'); return
    if cmd=='save':
        state=pending.pop(uid,{})
        notice='Изменения сохранены.\n'
        if 'delete' in state:
            count=0
            with app.store.change() as data:
                for code in state['delete']:
                    if code in data['codes'] and data['codes'][code] is None:
                        del data['codes'][code]; count+=1
                audit(data,uid,f'deleted free codes: {count}')
            notice=f'Удалено: {count}. Выданные коды сохранены.\n'
        elif 'key' in state: commit(uid,state['key'],state['value'])
        await home(q.message,notice); return
    if cmd=='undo':
        with app.store.change() as data:
            if 'undo' in data:
                data['settings'],data['undo']=data['undo'],data.get('settings',{})
                audit(data,uid,'undo settings')
        await home(q.message); return
    if cmd=='backup':
        await q.message.answer_document(BufferedInputFile(json.dumps(app.store.data,ensure_ascii=False).encode(),filename='prichal-backup.json')); await app.event(uid,'admin','backup'); return
    if cmd in ('stats','log'):
        wb=Workbook(); wb.remove(wb.active)
        def sheet(name, rows):
            ws=wb.create_sheet(name)
            for row in rows:
                ws.append([str(v) if v is not None else '' for v in row])
                for cell in ws[ws.max_row]: cell.data_type='s'
        users=app.store.data['users']
        if cmd=='stats':
            header=['Telegram ID','username','Имя','Первый вход (UTC epoch)','Источник','Код','Выдача (UTC epoch)']
            rows=[[uid,u.get('username'),u.get('first_name'),u.get('created_at'),u.get('source'),u.get('code'),u.get('issued_at')] for uid,u in users.items()]
            sheet('Все пользователи',[header]+rows); sheet('Получатели',[header]+[r for r in rows if r[5]])
            free=sum(v is None for v in app.store.data['codes'].values())
            now=time.time()
            await show(q.message, f'Пользователей: {len(users)}; получателей: {sum(bool(u.get("code")) for u in users.values())}; свободных кодов: {free}\nНовых за 24 часа: {sum(u.get("created_at",0)>now-86400 for u in users.values())}; за 7 дней: {sum(u.get("created_at",0)>now-604800 for u in users.values())}.')
        sheet('События',[['ID','Событие','Детали','UTC epoch']]+[[e.get('user_id'),e.get('kind'),e.get('detail'),e.get('at')] for e in app.store.data['events'] if cmd=='stats' or e.get('kind')=='admin'])
        output=io.BytesIO(); wb.save(output); wb.close()
        await q.message.answer_document(BufferedInputFile(output.getvalue(),filename='statistics.xlsx')); await app.event(uid,'admin',cmd)

async def input_value(message):
    uid=message.from_user.id
    if uid not in app.ADMINS or message.chat.type!='private' or uid not in pending: return False
    state=pending[uid]; key=state.get('input')
    if not key: return False
    value=message.text or ''
    if key=='delete':
        pending[uid]={'delete':value.split()}
    else:
        if key.startswith('image:'):
            if message.photo: value=message.photo[-1].file_id
            elif value=='-': value=''
            else:
                await show(message, 'Пришлите фото или знак -.'); return True
        elif key.startswith('url:'):
            if value=='-': value=''
            if value and (urlparse(value).scheme!='https' or not urlparse(value).netloc):
                await show(message, 'Нужна ссылка https:// или знак -.'); return True
        elif key.startswith('delay:') or key=='threshold':
            if not value.isdigit() or not (0 if key=='threshold' else 1)<=int(value)<=(50000 if key=='threshold' else 86400):
                await show(message, 'Недопустимое число.'); return True
            value=int(value)
        elif not value or len(value)>(64 if key.startswith('button:') else 900):
            await show(message, 'Пустой или слишком длинный текст. Лимит: кнопка 64, описание 900 символов.'); return True
        pending[uid]={'key':key,'value':value}
    photo = value if key.startswith('image:') and value else None
    preview = 'Предпросмотр изображения' if photo else 'Удалить выбранные свободные коды?' if key=='delete' else f'Предпросмотр:\n{value or "(пусто)"}'
    await show(message, preview+'\nСохранить?', photo=photo, reply_markup=keyboard([('Сохранить','save'),('Отмена','home')]))
    return True

async def low_stock():
    free=sum(v is None for v in app.store.data['codes'].values())
    low=free<=config('threshold',50)
    if not low:
        if app.store.data.get('low_alert'):
            with app.store.change() as data: data['low_alert']=False
        return
    if app.store.data.get('low_alert'): return
    for uid in app.ADMINS:
        try: await app.bot.send_message(uid,f'Мало промокодов: осталось {free}. /admin')
        except Exception: continue
    with app.store.change() as data: data['low_alert']=True
