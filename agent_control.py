"""Persistent human emergency stop for AgentRelay.

Model intent is not pause/resume authority. In-flight CWA submissions cannot
be revoked, but stale generations must not be published by AgentRelay.
"""
from __future__ import annotations
import json
import os
import threading
from pathlib import Path


class ControlState:
    def __init__(self, filename: str) -> None:
        self.path = Path(filename)
        self._lock = threading.Lock()
        self._paused = False
        self._generation = 0
        if self.path.exists():
            try:
                record = json.loads(self.path.read_text(encoding='utf-8'))
                if (not isinstance(record, dict)
                        or type(record.get('paused')) is not bool
                        or type(record.get('generation')) is not int
                        or record['generation'] < 0):
                    raise ValueError('bad control record')
                self._paused = record['paused']
                self._generation = record['generation']
            except (OSError, UnicodeError, ValueError) as exc:
                # A damaged persistent state must never silently resume.
                raise ValueError('Control state unreadable; refusing to start') from exc

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def permits(self, generation: int) -> bool:
        with self._lock:
            return not self._paused and self._generation == generation

    def _write(self, paused: bool) -> None:
        generation = self._generation + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.tmp')
        with temp.open('w', encoding='utf-8') as stream:
            json.dump({'paused': paused, 'generation': generation}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        self._paused = paused
        self._generation = generation

    def stop(self) -> bool:
        with self._lock:
            if self._paused:
                return False
            self._write(True)
            return True

    def resume(self) -> bool:
        with self._lock:
            if not self._paused:
                return False
            self._write(False)
            return True
