import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from storage import Store
import admin_panel as panel

class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        with self.store.change() as data:
            data['users']['2']={}
        self.store.import_codes(['ABCD1234','ABCD5678'])
        self.store.assign(2)
        panel.app=SimpleNamespace(store=self.store,ADMINS={1},BUILD='test',event=AsyncMock(),retire_menu=AsyncMock(),navigation_keys=AsyncMock(),banner=lambda:b'image',bot=SimpleNamespace(send_message=AsyncMock(),edit_message_media=AsyncMock(),send_photo=AsyncMock(return_value=SimpleNamespace(message_id=101))))
        panel.pending.clear()
        self.message=SimpleNamespace(message_id=100,chat=SimpleNamespace(id=1,type='private'),answer=AsyncMock(),answer_document=AsyncMock())

    async def asyncTearDown(self):
        self.store.close();self.temp.cleanup()

    async def click(self,cmd,uid=1):
        q=SimpleNamespace(from_user=SimpleNamespace(id=uid),message=self.message,data='a:'+cmd,answer=AsyncMock())
        await panel.action(q)

    async def test_authorization(self):
        await self.click('issue',uid=2)
        self.assertTrue(panel.config('enabled',True))
        self.message.answer.assert_not_called()

    async def test_delete_requires_confirmation_preserves_issued(self):
        await self.click('del:all')
        self.assertEqual(len(self.store.data['codes']),2)
        await self.click('save')
        self.assertEqual(self.store.data['codes'],{'ABCD1234':'2'})
        self.assertEqual(self.store.assign(2),'ABCD1234')

    async def test_settings_undo_restart(self):
        panel.commit(1,'button:hello','new')
        await self.click('undo')
        self.assertIsNone(panel.config('button:hello'))
        await self.click('issue')
        self.store.close();self.store=Store(self.temp.name);panel.app.store=self.store
        self.assertFalse(panel.config('enabled'))

    async def test_timer_cancel_retains_dedupe(self):
        with self.store.change() as data:data['jobs']['2:card']={'user_id':2,'due_at':1}
        await self.click('timer:cancel')
        self.assertTrue(self.store.data['jobs']['2:card']['cancelled'])

    async def test_low_stock_only_once(self):
        await panel.low_stock();await panel.low_stock()
        panel.app.bot.send_message.assert_awaited_once()

    async def test_export(self):
        await self.click('stats')
        self.message.answer_document.assert_awaited_once()

    async def test_create_campaign_confirmation_and_archive_export(self):
        await self.click('campaign:new')
        self.message.from_user=SimpleNamespace(id=1)
        self.message.text='Новая акция'
        await panel.input_value(self.message)
        self.assertEqual(self.store.data['campaign']['id'],'legacy')
        await self.click('save')
        self.assertEqual(self.store.data['campaign']['name'],'Новая акция')
        self.assertIn('legacy',self.store.data['campaign_archives'])
        await self.click('history')
        self.message.answer_document.assert_awaited_once()

    async def test_admin_transitions_edit_same_message(self):
        for cmd in ('home','delete','del:select','home','timers','threshold','home'):
            await self.click(cmd)
        panel.app.bot.send_photo.assert_not_called()
        self.assertEqual(panel.app.bot.edit_message_media.await_count,7)
        self.assertTrue(all(call.kwargs['message_id']==100 for call in panel.app.bot.edit_message_media.call_args_list))

    async def test_admin_command_and_input_create_new_panel(self):
        self.message.from_user=SimpleNamespace(id=1)
        await panel.admin(self.message)
        panel.app.bot.send_photo.assert_awaited_once()
        await panel.admin(self.message)
        panel.pending[1]={'input':'threshold'}
        self.message.text='25'
        await panel.input_value(self.message)
        panel.app.retire_menu.assert_awaited()
        self.assertEqual(panel.app.bot.send_photo.await_count,3)
        self.assertEqual(panel.pending[1],{'key':'threshold','value':25})

    async def test_image_preview_returns_panel_to_bottom(self):
        with self.store.change() as data:data['admin_screens']={'1':100}
        self.message.from_user=SimpleNamespace(id=1)
        self.message.text=None
        self.message.photo=[SimpleNamespace(file_id='image-file-id')]
        panel.pending[1]={'input':'image:Главное меню'}
        await panel.input_value(self.message)
        panel.app.bot.send_photo.assert_awaited_once()
        self.assertEqual(panel.app.bot.send_photo.call_args.kwargs['photo'],'image-file-id')

if __name__=='__main__':unittest.main()
