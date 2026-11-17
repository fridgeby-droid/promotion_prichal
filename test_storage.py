import tempfile
import unittest
import io
from openpyxl import Workbook
from storage import Store
from importer import read_codes

class Tests(unittest.TestCase):
    def test_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.change() as data:
                data['users'] = {'1': {}, '2': {}, '3': {}}
            self.assertEqual(store.import_codes(['Ab12Cd34', 'Ab12Cd34', '0000ABcd']), 2)
            first = store.assign(1)
            self.assertEqual(store.assign(1), first)
            store.close()
            store = Store(directory)
            self.assertEqual(store.assign(1), first)
            self.assertNotEqual(store.assign(2), first)
            self.assertIsNone(store.assign(3))
            self.assertEqual(store.import_codes([first]), 0)
            with self.assertRaises(ValueError):
                store.import_codes(['ZZZZ9999', 'bad'])
            self.assertNotIn('ZZZZ9999', store.data['codes'])
            with store.change() as data:
                data['invitations']['1'] = {'status': 'sending'}
                data['jobs']['1:card'] = {'due_at': 123}
            store.close()
            store = Store(directory)
            self.assertEqual(store.data['invitations']['1']['status'], 'uncertain')
            self.assertEqual(store.data['jobs']['1:card']['due_at'], 123)
            store.close()

    def excel(self, value):
        workbook = Workbook()
        workbook.active.append(['header', 'ignored'])
        workbook.active.append([value, 'ignored'])
        stream = io.BytesIO()
        workbook.save(stream)
        workbook.close()
        return stream.getvalue()

    def test_excel(self):
        self.assertEqual(read_codes(self.excel('0000ABcd')), ['0000ABcd'])
        for value in (12345678, '=12345678', ' ABCD1234', 'АБВГ1234'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_codes(self.excel(value))

if __name__ == '__main__':
    unittest.main()
