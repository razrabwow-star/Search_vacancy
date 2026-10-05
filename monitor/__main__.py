import argparse
import json
import logging
import os
import sys
from pathlib import Path
from contextlib import contextmanager
from . import sources, sber
from .db import Store
from .http import Client, FetchError
from .telegram import Sender, flush, report, chunks, NAMES, chat_ids

LOG = logging.getLogger('monitor')


def safe_error(error):
    # Only messages authored by this application may appear in logs.
    known = {
        'Другой прогон уже выполняется',
        'Нет TELEGRAM_BOT_TOKEN',
        'Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env',
        'Telegram отклонил отправку; проверьте токен, chat_id и доступ бота к чату',
        'Telegram недоступен; сообщения сохранены для повторной отправки',
        'Не удалось получить обновления бота',
        'Не удалось получить chat_id',
    }
    if isinstance(error, PermissionError):
        return 'Нет прав на каталог data; назначьте владельца UID 10001'
    if str(error) in known:
        return str(error)
    return f'Ошибка {type(error).__name__}; проверьте настройки и каталог data'


def load_env():
    path = Path('.env')
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


@contextmanager
def lock(path):
    stream = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('Другой прогон уже выполняется') from None
    try:
        yield
    finally:
        stream.close()


def run(store, selected, client, max_pages, directory):
    run_id = store.start()
    for source in selected:
        count = matched = errors = 0
        observed = set()
        LOG.info('Проверка: %s', NAMES[source])
        try:
            vacancies = sber.collect(client, max_pages, directory) if source == 'sber' else getattr(sources, source)(client, max_pages)
            for vacancy in vacancies:
                if vacancy.external_id in observed:
                    continue
                observed.add(vacancy.external_id)
                matched += store.observe(run_id, vacancy)
                errors += bool(vacancy.processing_error)
                count += 1
            store.source_done(run_id, source, 'error' if errors else 'ok', count, matched, f'Не получены описания {errors} вакансий; остальные обработаны' if errors else '')
            LOG.info('%s: проверено %s, подходит %s', NAMES[source], count, matched)
        except Exception as error:
            if isinstance(error, FetchError):
                for vacancy in error.vacancies:
                    if vacancy.external_id not in observed:
                        observed.add(vacancy.external_id)
                        matched += store.observe(run_id, vacancy)
                        count += 1
            message = str(error) if isinstance(error, FetchError) else 'Ошибка сборщика; источник требует проверки'
            store.source_done(run_id, source, 'error', count, matched, message)
            LOG.error('%s: %s', NAMES[source], message)
    store.finish(run_id)
    return run_id


def main():
    load_env()
    parser = argparse.ArgumentParser(description='Мониторинг вакансий бизнес-/системных аналитиков')
    parser.add_argument('command', choices=['run', 'login-sber', 'retry-notifications', 'status', 'chat-id'], nargs='?', default='run')
    parser.add_argument('--dry-run', action='store_true', help='Отдельная тестовая база; без Telegram')
    parser.add_argument('--sources', default=os.getenv('SOURCES', 'tbank,yandex,alfa,sber'))
    parser.add_argument('--data-dir', default=os.getenv('DATA_DIR', 'data'))
    args = parser.parse_args()
    directory = Path(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    selected = list(dict.fromkeys(s.strip() for s in args.sources.split(',') if s.strip()))
    if not selected or any(s not in NAMES for s in selected):
        parser.error('Допустимые источники: tbank,yandex,alfa,sber')
    try:
        with lock(directory / 'monitor.lock'):
            if args.command == 'login-sber':
                sber.login(directory)
                return 0
            if args.command == 'chat-id':
                ids = chat_ids(os.getenv('TELEGRAM_BOT_TOKEN'))
                for chat_id, kind, title in ids:
                    print(f'{chat_id} | {kind} | {title}')
                if not ids:
                    print('Отправьте /start своему боту и повторите команду.')
                return 0
            store = Store(directory / ('dry-run.sqlite3' if args.dry_run else 'monitor.sqlite3'))
            try:
                if args.command == 'status':
                    print(json.dumps([dict(r) for r in store.conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 5')], ensure_ascii=False, indent=2))
                    print(f'Неотправленных сообщений: {len(store.pending_messages())}')
                    return 0
                sender = None if args.dry_run else Sender(os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID'))
                if args.command == 'retry-notifications':
                    if sender is None:
                        parser.error('retry-notifications несовместим с --dry-run')
                    flush(store, sender)
                    return 0
                client = Client(float(os.getenv('REQUEST_TIMEOUT', '35')), float(os.getenv('REQUEST_DELAY', '0.5')))
                try:
                    run_id = run(store, selected, client, int(os.getenv('MAX_PAGES', '150')), directory)
                finally:
                    client.close()
                text, event_ids = report(store, run_id)
                print(text)
                if sender is not None:
                    store.enqueue_report(run_id, chunks(text), event_ids)
                    flush(store, sender)
                else:
                    # Simulate acknowledgement only in the isolated dry-run database.
                    store.enqueue_report(run_id, chunks(text), event_ids)
                    for message in store.pending_messages():
                        store.sent(message['id'])
                    print('\nТестовый прогон: Telegram не вызывался, рабочая база не изменялась.')
                status = store.conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()[0]
                return 0 if status == 'ok' else 2
            finally:
                store.close()
    except Exception as error:
        # Never emit third-party exception strings: Playwright/HTTP errors can embed secrets.
        LOG.error('Запуск не завершён: %s. Существующая очередь сохранена.', safe_error(error))
        return 1


if __name__ == '__main__':
    sys.exit(main())
