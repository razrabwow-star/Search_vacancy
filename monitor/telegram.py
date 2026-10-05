import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
import ssl

NAMES = {'tbank': 'Т-Банк', 'yandex': 'Яндекс', 'alfa': 'Альфа-Банк', 'sber': 'Сбер', 'vtb': 'ВТБ', 'rwb': 'RWB', 'avito': 'Авито'}


def chunks(text, limit=3500):
    # Count UTF-16 code units as a conservative bound for Telegram's message limit.
    result, current = [], ''
    for line in text.splitlines(keepends=True):
        while len(line.encode('utf-16-le')) // 2 > limit:
            cut = limit // 2
            prefix, line = line[:cut], line[cut:]
            if current:
                result.append(current)
                current = ''
            result.append(prefix)
        if len((current + line).encode('utf-16-le')) // 2 > limit:
            result.append(current)
            current = ''
        current += line
    if current:
        result.append(current)
    return result or ['Нет данных']


def report(store, run_id):
    events = store.pending_events()
    sources = store.conn.execute('SELECT * FROM source_runs WHERE run_id=? ORDER BY source', (run_id,)).fetchall()
    successful = sum(s['status'] == 'ok' for s in sources)
    matches = sum(s['matched'] for s in sources)
    stamp = datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    lines = [f'Вакансии — {stamp}', f'Полностью проверено источников: {successful}/{len(sources)}', f'Новых подходящих вакансий: {len(events)}', '']
    for s in sources:
        name = NAMES[s['source']]
        if s['status'] == 'ok':
            lines.append(f"{name}: проверено {s['count']}, подходит {s['matched']}")
        else:
            lines.append(f"{name}: проверка не завершена — {s['error']}")
    if not events:
        lines += ['', 'Новых подходящих вакансий нет.' if successful == len(sources) else 'На успешно проверенных источниках новых подходящих вакансий нет; итог неполный.']
        if not matches and successful == len(sources):
            lines.append('Подходящих открытых вакансий сейчас нет.')
    for e in events:
        v = json.loads(e['data'])
        location = 'Москва' if v['moscow'] else ', '.join(v['cities']) or 'Город не указан'
        lines += ['', f"{NAMES[v['source']]} — {v['title']}", f"{location} | {', '.join(v['modes']) or 'Формат см. в описании'}", v['url']]
    return '\n'.join(lines), [e['id'] for e in events]


class Sender:
    def __init__(self, token, chat_id):
        if not token or not chat_id:
            raise ValueError('Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env')
        self.url = f'https://api.telegram.org/bot{token}/sendMessage'
        self.chat_id = chat_id

    def send(self, text):
        # Retry in the durable outbox, not inside the request: a timeout can follow a successful send.
        try:
            r = httpx.post(self.url, json={'chat_id': self.chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}, timeout=30, verify=ssl.create_default_context())
            data = r.json()
            if r.status_code != 200 or not data.get('ok'):
                raise RuntimeError('Telegram отклонил отправку; проверьте токен, chat_id и доступ бота к чату')
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Telegram недоступен; сообщения сохранены для повторной отправки') from None


def flush(store, sender):
    for message in store.pending_messages():
        try:
            sender.send(message['text'])
        except RuntimeError:
            store.failed(message['id'])
            raise
        store.sent(message['id'])
        time.sleep(1)


def chat_ids(token):
    if not token:
        raise ValueError('Нет TELEGRAM_BOT_TOKEN')
    try:
        response = httpx.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=30, verify=ssl.create_default_context())
        data = response.json()
        if not data.get('ok'):
            raise RuntimeError('Не удалось получить обновления бота')
        found = set()
        for update in data.get('result', []):
            message = update.get('message') or update.get('channel_post') or {}
            chat = message.get('chat', {})
            if chat.get('id') is not None:
                found.add((chat['id'], chat.get('type', ''), chat.get('title') or chat.get('first_name') or ''))
        return sorted(found)
    except (httpx.HTTPError, ValueError):
        raise RuntimeError('Не удалось получить chat_id') from None
