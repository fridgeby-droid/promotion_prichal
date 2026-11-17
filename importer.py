import io
import zipfile
from openpyxl import load_workbook
from storage import CODE

def read_codes(content):
    if len(content) > 5 * 1024 * 1024:
        raise ValueError('Максимальный размер файла — 5 МБ.')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(x.file_size for x in archive.infolist()) > 30 * 1024 * 1024:
            raise ValueError('Слишком большой распакованный Excel-файл.')
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    codes = []
    try:
        sheet = workbook.worksheets[0]
        for row_number, row in enumerate(sheet.iter_rows(min_row=2, max_col=1), 2):
            if row_number > 50001:
                raise ValueError('Не более 50 000 строк за загрузку.')
            cell = row[0]
            if cell.value is None or cell.value == '':
                continue
            if cell.data_type == 'f' or not isinstance(cell.value, str) or not CODE.fullmatch(cell.value):
                raise ValueError(f'Строка {row_number}: нужен текст из 8 латинских букв/цифр, без пробелов и формул.')
            codes.append(cell.value)
    finally:
        workbook.close()
    if not codes:
        raise ValueError('В первом столбце после заголовка нет кодов.')
    return codes
