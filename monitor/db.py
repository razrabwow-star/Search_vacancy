import sqlite3
from datetime import datetime, timezone
from dataclasses import asdict
import json
from .model import classify


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_runs(run_id INTEGER, source TEXT, status TEXT, count INTEGER, matched INTEGER, error TEXT, PRIMARY KEY(run_id,source));
            CREATE TABLE IF NOT EXISTS vacancies(source TEXT, external_id TEXT, data TEXT NOT NULL, hash TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_processed_run_id INTEGER NOT NULL,
                relevant INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', missing_checks INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source,external_id));
            CREATE TABLE IF NOT EXISTS observations(run_id INTEGER, source TEXT, external_id TEXT, processed_at TEXT, result TEXT, reason TEXT,
                PRIMARY KEY(run_id,source,external_id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run_id INTEGER, source TEXT, external_id TEXT, data TEXT, report_id INTEGER, sent_at TEXT);
            CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, run_id INTEGER, created TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, report_id INTEGER, part INTEGER, text TEXT NOT NULL, sent_at TEXT, attempts INTEGER DEFAULT 0,
                UNIQUE(report_id,part));
        ''')
        self.conn.commit()

    def start(self):
        # A terminated previous run remains visible, rather than appearing successful.
        with self.conn:
            self.conn.execute("UPDATE runs SET status='interrupted',finished=? WHERE status='running'", (now(),))
            cur = self.conn.execute("INSERT INTO runs(started,status) VALUES(?,'running')", (now(),))
        return cur.lastrowid

    def observe(self, run_id, vacancy):
        relevant, reason = classify(vacancy)
        key = (vacancy.source, vacancy.external_id)
        timestamp = now()
        data = json.dumps(asdict(vacancy), ensure_ascii=False)
        with self.conn:
            old = self.conn.execute('SELECT relevant,data,hash FROM vacancies WHERE source=? AND external_id=?', key).fetchone()
            stored_relevant = old['relevant'] if old and vacancy.processing_error else relevant
            stored_data = old['data'] if old and vacancy.processing_error else data
            stored_hash = old['hash'] if old and vacancy.processing_error else vacancy.digest()
            self.conn.execute('''INSERT INTO vacancies(source,external_id,data,hash,first_seen,last_seen,last_processed_run_id,relevant,reason)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET data=excluded.data,hash=excluded.hash,
                last_seen=excluded.last_seen,last_processed_run_id=excluded.last_processed_run_id,relevant=excluded.relevant,reason=excluded.reason,
                status='active',missing_checks=0''', (*key, stored_data, stored_hash, timestamp, timestamp, run_id, stored_relevant, reason))
            cur = self.conn.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)', (run_id, *key, timestamp, 'error' if vacancy.processing_error else 'matched' if relevant else 'rejected', reason))
            if cur.rowcount and relevant and (old is None or not old['relevant']):
                self.conn.execute('INSERT INTO events(run_id,source,external_id,data) VALUES(?,?,?,?)', (run_id, *key, data))
        return relevant

    def source_done(self, run_id, source, status, count, matched, error=''):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO source_runs VALUES(?,?,?,?,?,?)', (run_id, source, status, count, matched, error))
            if status == 'ok':
                self.conn.execute('''UPDATE vacancies SET missing_checks=missing_checks+1 WHERE source=? AND last_processed_run_id<>? AND status='active' ''', (source, run_id))
                self.conn.execute("UPDATE vacancies SET status='missing' WHERE source=? AND missing_checks>=2", (source,))

    def finish(self, run_id):
        with self.conn:
            errors = self.conn.execute("SELECT count(*) FROM source_runs WHERE run_id=? AND status<>'ok'", (run_id,)).fetchone()[0]
            self.conn.execute('UPDATE runs SET finished=?,status=? WHERE id=?', (now(), 'partial' if errors else 'ok', run_id))

    def pending_events(self):
        return self.conn.execute('SELECT * FROM events WHERE report_id IS NULL ORDER BY id').fetchall()

    def enqueue_report(self, run_id, chunks, event_ids):
        with self.conn:
            cur = self.conn.execute('INSERT INTO reports(run_id,created) VALUES(?,?)', (run_id, now()))
            report_id = cur.lastrowid
            for i, chunk in enumerate(chunks):
                self.conn.execute('INSERT INTO outbox(report_id,part,text) VALUES(?,?,?)', (report_id, i, chunk))
            self.conn.executemany('UPDATE events SET report_id=? WHERE id=? AND report_id IS NULL', [(report_id, e) for e in event_ids])
        return report_id

    def pending_messages(self):
        return self.conn.execute('SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id').fetchall()

    def sent(self, message_id):
        with self.conn:
            row = self.conn.execute('SELECT report_id FROM outbox WHERE id=?', (message_id,)).fetchone()
            self.conn.execute('UPDATE outbox SET sent_at=? WHERE id=?', (now(), message_id))
            if not self.conn.execute('SELECT 1 FROM outbox WHERE report_id=? AND sent_at IS NULL', (row['report_id'],)).fetchone():
                self.conn.execute('UPDATE events SET sent_at=? WHERE report_id=?', (now(), row['report_id']))

    def failed(self, message_id):
        with self.conn:
            self.conn.execute('UPDATE outbox SET attempts=attempts+1 WHERE id=?', (message_id,))

    def close(self):
        self.conn.close()
