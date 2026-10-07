"""Persist durable events until the server confirms receipt."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid


class EventOutbox(asyncio.Queue):
    def __init__(self, server: str, machine_id: str):
        super().__init__(maxsize=1000)
        directory = Path.home() / '.botsdock' / 'outbox'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(f'{server.rstrip("/")}:{machine_id}'.encode()).hexdigest()
        path = directory / f'{key}.sqlite'
        self.database = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.database.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, message TEXT NOT NULL)')
        self.database.commit()

    def put_nowait(self, message):
        if self.full():
            raise asyncio.QueueFull
        if message.get('type') == 'connector.event':
            message = dict(message)
            if not message.get('event_id'):
                message['event_id'] = f'evt_{uuid.uuid4().hex}'
            with self.database:
                self.database.execute('INSERT OR IGNORE INTO events VALUES (?, ?)',
                                      (message['event_id'], json.dumps(message)))
        super().put_nowait(message)

    def pending(self):
        return [json.loads(row[0]) for row in
                self.database.execute('SELECT message FROM events ORDER BY rowid')]

    def acknowledge(self, message):
        event = message.get('event') or {}
        event_id = message.get('event_id') or event.get('id')
        if event_id:
            with self.database:
                self.database.execute('DELETE FROM events WHERE id = ?', (event_id,))

    def close(self):
        self.database.close()
