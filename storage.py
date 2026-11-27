"""Single-process JSON store. All mutations commit before returning."""
import copy
import json
import os
import re
import tempfile
import time
import uuid
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
            data.setdefault('campaign', dict(id='legacy', name='Первая акция (перенесена)', status='active', created_at=time.time()))
            data.setdefault('campaign_archives', {})
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
        historical = {code for archive in self.data['campaign_archives'].values() for code, owner in archive['codes'].items() if owner is not None}
        with self.change() as data:
            for code in codes:
                if code not in data['codes'] and code not in historical:
                    data['codes'][code] = None
                    added += 1
        return added

    def assign(self, uid):
        key = str(uid)
        user = self.data['users'][key]
        if self.data['campaign']['status'] != 'active':
            return None
        if user.get('code'):
            return user['code']
        if self.data['campaign']['status'] != 'active' or not self.data.get('settings', {}).get('enabled', True):
            return None
        code = next((c for c, owner in self.data['codes'].items() if owner is None), None)
        if code is None:
            return None
        with self.change() as data:
            data['codes'][code] = key
            data['users'][key]['code'] = code
            data['users'][key]['issued_at'] = time.time()
        return code

    def finish_campaign(self, expected_id, actor):
        if self.data['campaign']['id'] != expected_id:
            raise ValueError('Акция уже изменилась. Откройте раздел заново.')
        with self.change() as data:
            data['campaign'].update(status='ended', ended_at=time.time())
            data.setdefault('settings', {})['enabled'] = False
            data['settings']['timers'] = False
            data['events'].append(dict(user_id=actor, kind='admin', detail='campaign ended', campaign_id=expected_id, at=time.time()))

    def new_campaign(self, name, expected_id, actor):
        if not name.strip() or len(name)>100:
            raise ValueError('Название акции: от 1 до 100 символов.')
        if self.data['campaign']['id'] != expected_id:
            raise ValueError('Акция уже изменилась. Откройте раздел заново.')
        new_id = uuid.uuid4().hex
        with self.change() as data:
            metadata = copy.deepcopy(data['campaign'])
            metadata.update(status='ended', ended_at=metadata.get('ended_at',time.time()))
            archive = dict(metadata=metadata, users=copy.deepcopy(data['users']), settings=copy.deepcopy(data.get('settings',{})))
            for key in ('codes','jobs','invitations','batches'):
                archive[key] = copy.deepcopy(data.get(key,{}))
                data[key] = {}
            data['campaign_archives'][expected_id] = archive
            for user in data['users'].values():
                user.pop('code',None)
                user.pop('issued_at',None)
            data['campaign'] = dict(id=new_id, name=name.strip(), status='active', created_at=time.time())
            data.setdefault('settings',{}).update(enabled=False,timers=False)
            data.pop('undo',None)
            data['low_alert'] = False
            data['events'].append(dict(user_id=actor,kind='admin',detail='campaign created: '+name,campaign_id=new_id,at=time.time()))
        return new_id

    def close(self):
        self.lock.close()
