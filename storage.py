"""Single-process JSON store. All mutations commit before returning."""
import copy
import json
import os
import re
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager

CODE = re.compile(r'[A-Za-z0-9]{8}\Z')

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'state.json'
        self.lock = open(self.directory / 'state.lock', 'a+b')
        if os.name == 'nt':
            import msvcrt
            self.lock.seek(0)
            self.lock.write(b'0')
            self.lock.flush()
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if self.data.get('version') != 1:
                raise ValueError('Unsupported state version')
            for key in ('codes', 'users', 'jobs', 'invitations', 'events', 'updates'):
                if key not in self.data:
                    raise ValueError('Invalid state: ' + key)
        else:
            self.data = dict(version=1, codes={}, users={}, jobs={}, invitations={}, events=[], updates=[])
        with self.change() as data:
            for invitation in data['invitations'].values():
                if invitation['status'] == 'sending':
                    invitation['status'] = 'uncertain'

    @contextmanager
    def change(self):
        candidate = copy.deepcopy(self.data)
        yield candidate
        fd, name = tempfile.mkstemp(dir=self.directory, prefix='.state-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(candidate, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self.data = candidate
            if os.name != 'nt':
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def import_codes(self, codes):
        if any(not CODE.fullmatch(c) for c in codes):
            raise ValueError('Код должен содержать ровно 8 латинских букв или цифр.')
        added = 0
        with self.change() as data:
            for code in codes:
                if code not in data['codes']:
                    data['codes'][code] = None
                    added += 1
        return added

    def assign(self, uid):
        key = str(uid)
        user = self.data['users'][key]
        if user.get('code'):
            return user['code']
        code = next((c for c, owner in self.data['codes'].items() if owner is None), None)
        if code is None:
            return None
        with self.change() as data:
            data['codes'][code] = key
            data['users'][key]['code'] = code
            data['users'][key]['issued_at'] = time.time()
        return code

    def close(self):
        self.lock.close()
