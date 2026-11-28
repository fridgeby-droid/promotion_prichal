"""Execute the real screen function with a mocked Telegram transport."""
import ast
import contextvars
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from storage import Store

class BadRequest(Exception):
    def __init__(self, message):
        self.message = message

class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        with self.store.change() as data:
            data['users']['1'] = {'screen_message_id': 90}
        self.clicked = contextvars.ContextVar('test_clicked', default=None)
        self.bot = SimpleNamespace(edit_message_media=AsyncMock(return_value=SimpleNamespace(message_id=42)), send_photo=AsyncMock(return_value=SimpleNamespace(message_id=99)))
        tree = ast.parse(Path(__file__).with_name('bot.py').read_text(encoding='utf-8'))
        screen = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'screen')
        namespace = dict(panel=SimpleNamespace(config=lambda key, default=None: default), store=self.store, bot=self.bot, clicked_message_id=self.clicked, banner=lambda: b'banner', BufferedInputFile=lambda *a, **k: a[0], InputMediaPhoto=lambda **k: k, TelegramBadRequest=BadRequest)
        exec(compile(ast.Module(body=[screen], type_ignores=[]), 'bot.py', 'exec'), namespace)
        self.screen = namespace['screen']
        self.panel = namespace['panel']
        namespace['ADMINS'] = set()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_all_transitions_edit_clicked_message(self):
        self.clicked.set(42)
        for text, photo in [('menu', None), ('card', None), ('coupon', b'qr'), ('menu', None), ('raffle', None)]:
            await self.screen(1, text, None, photo)
        self.bot.send_photo.assert_not_called()
        self.assertEqual(self.bot.edit_message_media.await_count, 5)
        self.assertTrue(all(c.kwargs['message_id'] == 42 for c in self.bot.edit_message_media.call_args_list))
        self.assertEqual(self.store.data['users']['1']['screen_message_id'], 42)

    async def test_missing_clicked_message_never_sends(self):
        self.clicked.set(42)
        self.bot.edit_message_media.side_effect = BadRequest('message to edit not found')
        with self.assertRaises(BadRequest):
            await self.screen(1, 'menu', None)
        self.bot.send_photo.assert_not_called()

    async def test_start_can_recover_deleted_menu(self):
        self.bot.edit_message_media.side_effect = BadRequest('message to edit not found')
        await self.screen(1, 'menu', None)
        self.bot.send_photo.assert_awaited_once()

    async def test_unchanged_is_not_resent(self):
        self.clicked.set(42)
        self.bot.edit_message_media.side_effect = BadRequest('message is not modified')
        await self.screen(1, 'menu', None)
        self.bot.send_photo.assert_not_called()

    async def test_qr_replaces_cover_and_cover_returns_without_qr(self):
        self.clicked.set(42)
        self.panel.config = lambda key, default=None: 'gift-cover-file-id' if key.startswith('image:') else default
        await self.screen(1, 'Ваш подарок', None, photo=b'original-qr-bytes')
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs['media']['media'], b'original-qr-bytes')
        await self.screen(1, 'Акция завершена', None)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs['media']['media'], 'gift-cover-file-id')
        self.bot.send_photo.assert_not_called()

    async def test_promo_text_appended_after_admin_description(self):
        self.clicked.set(42)
        self.panel.config=lambda key, default=None: 'Ваш подарок ждёт!' if key.startswith('text:') else default
        await self.screen(1, 'Описание по умолчанию', None, photo=b'qr', promo_code='0Ab12Cd3')
        caption=self.bot.edit_message_media.call_args.kwargs['media']['caption']
        self.assertEqual(caption,'Ваш подарок ждёт!\n\nПромокод: 0Ab12Cd3\nЕсли QR не считывается, назовите этот код продавцу.')

if __name__ == '__main__':
    unittest.main()
