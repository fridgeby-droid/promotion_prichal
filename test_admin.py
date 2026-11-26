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
        panel.app=SimpleNamespace(store=self.store,ADMINS={1},BUILD='test',event=AsyncMock(),bot=SimpleNamespace(send_message=AsyncMock()))
        panel.pending.clear()
        self.message=SimpleNamespace(chat=SimpleNamespace(type='private'),answer=AsyncMock(),answer_document=AsyncMock())

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

if __name__=='__main__':unittest.main()
