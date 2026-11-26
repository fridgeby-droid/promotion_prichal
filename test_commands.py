import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
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
        application.bot=SimpleNamespace(edit_message_media=AsyncMock(return_value=SimpleNamespace(message_id=500)),send_photo=AsyncMock(return_value=SimpleNamespace(message_id=500)))
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
        application.bot.send_photo.assert_awaited_once()
        call=application.bot.edit_message_media.call_args.kwargs
        self.assertEqual(call['message_id'],500)
        self.assertEqual(call['media'].caption,'Главное меню')
        self.assertEqual(len(call['reply_markup'].inline_keyboard),7)
        await self.command('/admin')
        application.bot.send_photo.assert_awaited_once()
        self.assertEqual(application.bot.edit_message_media.call_args.kwargs['message_id'],500)

    async def test_old_split_screen_ids(self):
        with self.store.change() as data:
            data['users']['1']={'screen_message_id':100}
            data['admin_screens']={'1':500}
        await self.command('/start')
        self.assertEqual(application.bot.edit_message_media.call_args.kwargs['message_id'],500)
        application.bot.send_photo.assert_not_called()

    async def test_normal_user_start(self):
        await self.command('/start',uid=2)
        self.assertIn('2',self.store.data['users'])
        self.assertNotIn('2',self.store.data.get('admin_screens',{}))
        application.bot.send_photo.assert_awaited_once()

if __name__=='__main__':unittest.main()
