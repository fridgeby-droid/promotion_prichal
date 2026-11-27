import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram import Bot
from aiogram.types import Message, Update, Chat, User, MessageEntity
import bot as application
import admin_panel as panel
from storage import Store

class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.old=(application.store,application.bot,application.ADMINS,panel.app)
        application.store=self.store
        application.ADMINS={1}
        application.bot=SimpleNamespace(send_message=AsyncMock(),edit_message_reply_markup=AsyncMock(),edit_message_media=AsyncMock(return_value=SimpleNamespace(message_id=500)),send_photo=AsyncMock(return_value=SimpleNamespace(message_id=500)))
        panel.app=application
        panel.pending.clear()
        self.telegram=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk')
        self.sequence=0

    async def asyncTearDown(self):
        application.store,application.bot,application.ADMINS,panel.app=self.old
        await self.telegram.session.close()
        self.store.close();self.temp.cleanup()

    async def command(self,text,uid=1):
        self.sequence+=1
        message=Message(message_id=self.sequence,date=datetime.now(timezone.utc),chat=Chat(id=uid,type='private'),from_user=User(id=uid,is_bot=False,first_name='Test'),text=text,entities=[MessageEntity(type='bot_command',offset=0,length=len(text))])
        await application.dp.feed_update(self.telegram,Update(update_id=self.sequence,message=message))

    async def test_start_from_admin_editor_switches_visible_screen(self):
        await self.command('/admin')
        panel.pending[1]={'input':'pause'}
        await self.command('/start')
        self.assertNotIn(1,panel.pending)
        self.assertEqual(application.bot.send_photo.await_count,2)
        call=application.bot.send_photo.call_args.kwargs
        self.assertEqual(call['caption'],'Главное меню')
        self.assertEqual(len(call['reply_markup'].inline_keyboard),7)
        await self.command('/admin')
        self.assertEqual(application.bot.send_photo.await_count,3)
        application.bot.edit_message_media.assert_not_called()
        application.bot.edit_message_reply_markup.assert_awaited()

    async def test_old_split_screen_ids(self):
        with self.store.change() as data:
            data['users']['1']={'screen_message_id':100}
            data['admin_screens']={'1':500}
        await self.command('/start')
        self.assertEqual({c.kwargs['message_id'] for c in application.bot.edit_message_reply_markup.call_args_list},{100,500})
        application.bot.send_photo.assert_awaited_once()

    async def test_normal_user_start(self):
        await self.command('/start',uid=2)
        self.assertIn('2',self.store.data['users'])
        self.assertNotIn('2',self.store.data.get('admin_screens',{}))
        application.bot.send_photo.assert_awaited_once()

    async def test_ended_campaign_hides_old_qr_and_uses_custom_text(self):
        with self.store.change() as data:
            data['users']['2']={}
            data['settings']={'campaign_ended':'Подарки вернутся скоро!'}
        self.store.import_codes(['TEST1234'])
        self.store.assign(2)
        self.store.finish_campaign('legacy',1)
        with patch.object(application,'screen',new_callable=AsyncMock) as screen:
            await application.coupon(2)
            screen.assert_awaited_once()
            self.assertEqual(screen.call_args.args[1],'Подарки вернутся скоро!')
            self.assertNotIn('photo',screen.call_args.kwargs)
        self.assertEqual(self.store.data['jobs'],{})
        self.assertEqual(self.store.data['users']['2']['code'],'TEST1234')

    async def test_new_active_campaign_can_show_new_qr(self):
        with self.store.change() as data:data['users']['2']={}
        self.store.import_codes(['OLD12345']);self.store.assign(2)
        self.store.finish_campaign('legacy',1)
        self.store.new_campaign('Новая','legacy',1)
        self.store.import_codes(['NEW12345'])
        with self.store.change() as data:data['settings']['enabled']=True
        with patch.object(application,'screen',new_callable=AsyncMock) as screen, patch.object(panel,'low_stock',new_callable=AsyncMock):
            await application.coupon(2)
            self.assertIn('photo',screen.call_args.kwargs)
        self.assertEqual(self.store.data['users']['2']['code'],'NEW12345')

    async def test_persistent_keyboard_shortcuts(self):
        for index, text in enumerate(('Главное меню','Админ-панель'), 100):
            message=Message(message_id=index,date=datetime.now(timezone.utc),chat=Chat(id=1,type='private'),from_user=User(id=1,is_bot=False,first_name='Test'),text=text)
            await application.dp.feed_update(self.telegram,Update(update_id=index,message=message))
        self.assertEqual(application.bot.send_photo.await_count,2)
        application.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.data['users']['1']['source'],'')

if __name__=='__main__':unittest.main()
