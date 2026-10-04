"""Authenticated Sber adapter. Its schema is detected from vacancy search responses.

Only responses with a recognizable vacancy list and a total are considered complete.
Unknown schema, missing total, expired login, and stalled pagination are explicit errors.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse
from .model import Vacancy, plain
from .http import FetchError

URL = 'https://privet.sber.ru/platform/candidate/vacancy-search/'
TITLE_KEYS = ('vacancyName', 'vacancyTitle', 'positionName', 'jobTitle', 'title', 'name')
ID_KEYS = ('vacancyId', 'requisitionId', 'jobId', 'id', 'uuid')
TOTAL_KEYS = ('totalElements', 'totalCount', 'totalRecords', 'total')


def field(item, keys):
    return next((item[k] for k in keys if item.get(k) not in (None, '')), None)


def labels(value):
    if isinstance(value, list):
        return [x for v in value for x in labels(v)]
    if isinstance(value, dict):
        return labels(field(value, ('name', 'title', 'text', 'label', 'localityName')))
    return [str(value)] if value else []


def decode_lists(data):
    """Extract only candidate lists, never generic account/profile objects."""
    found = []
    def walk(node):
        if isinstance(node, dict):
            total = field(node, TOTAL_KEYS)
            if total is None and isinstance(node.get('page'), dict):
                total = field(node['page'], TOTAL_KEYS)
            for key, value in node.items():
                if isinstance(value, list) and key.lower() in ('vacancies', 'items', 'content', 'results', 'records', 'list'):
                    if value and all(isinstance(r, dict) and field(r, TITLE_KEYS) and field(r, ID_KEYS)
                                     and (key.lower() == 'vacancies' or any(k in r for k in ('vacancyId', 'vacancyName', 'jobTitle', 'positionName', 'description', 'vacancyDescription', 'city', 'cityName', 'cities'))) for r in value):
                        found.append((value, total if isinstance(total, int) and not isinstance(total, bool) else None))
                    elif value == [] and total == 0:
                        found.append(([], 0))
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for n in node:
                walk(n)
    walk(data)
    return found


def to_vacancy(row):
    key = str(field(row, ID_KEYS))
    title = plain(field(row, TITLE_KEYS))
    location = field(row, ('cities', 'city', 'cityName', 'locations', 'location', 'regionName', 'address'))
    modes = labels(field(row, ('workFormat', 'workFormats', 'workMode', 'workModes', 'employmentFormat', 'scheduleName')))
    description = plain(field(row, ('description', 'vacancyDescription', 'jobDescription')))
    description += ' ' + plain(row.get('requirements')) + ' ' + plain(row.get('duties'))
    conditions = plain(field(row, ('conditions', 'workingConditions', 'benefits')))
    url = field(row, ('vacancyUrl', 'url', 'link'))
    url = urljoin(URL, str(url)) if url else URL
    if urlparse(url).hostname != 'privet.sber.ru' or urlparse(url).scheme != 'https':
        url = URL
    # Do not infer Moscow from the description: it can mention an office in passing.
    return Vacancy('sber', key, title, url, description, labels(location), modes, conditions)


def restore(context, directory):
    session_path = Path(directory) / 'sber-session-storage.json'
    if session_path.exists():
        sessions = json.loads(session_path.read_text(encoding='utf-8'))
        context.add_init_script('const saved = ' + json.dumps(sessions) + '; const values=saved[location.origin]; if(values) for(const [key,value] of Object.entries(values)) sessionStorage.setItem(key,value);')


def collect(client, max_pages, directory):
    from playwright.sync_api import sync_playwright
    state_path = Path(directory) / 'sber-state.json'
    if not state_path.exists():
        raise FetchError('Нужно выполнить login-sber и перенести сохранённую сессию на сервер')
    rows, expected, parse_errors = {}, [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        context = browser.new_context(storage_state=str(state_path), locale='ru-RU')
        restore(context, directory)
        page = context.new_page()
        def response_received(response):
            path = urlparse(response.url).path.lower()
            if response.request.method not in ('GET', 'POST') or not re.search(r'vacanc|requisition|job-search', path):
                return
            if response.status in (401, 403):
                parse_errors.append('Сессия Сбера истекла или доступ отклонён')
                return
            if 'json' not in response.headers.get('content-type', ''):
                return
            try:
                for items, total in decode_lists(response.json()):
                    if total is not None:
                        expected.append(total)
                    for item in items:
                        rows[str(field(item, ID_KEYS))] = to_vacancy(item)
            except Exception:
                parse_errors.append('Не удалось прочитать JSON каталога Сбера')
        page.on('response', response_received)
        try:
            page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(2500)
            if '/auth/' in page.url:
                raise FetchError('Сессия Сбера истекла — повторите login-sber')
            for _ in range(max_pages):
                if parse_errors:
                    raise FetchError(parse_errors[0])
                if expected and len(rows) >= max(expected):
                    context.storage_state(path=str(state_path), indexed_db=True)
                    return list(rows.values())
                before = len(rows)
                next_button = page.get_by_role('button', name=re.compile(r'^(Показать (?:ещё|еще)|Загрузить (?:ещё|еще)|Следующая(?: страница)?|Далее)$', re.I))
                available = [b for b in next_button.all() if b.is_visible() and b.is_enabled()]
                if len(available) == 1:
                    available[0].click()
                elif len(available) > 1:
                    raise FetchError('Сбер: несколько кнопок пагинации, требуется уточнение сборщика')
                else:
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(1800)
                if len(rows) == before:
                    # Some lazy lists use a separate scroll container.
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(1800)
                    if len(rows) == before:
                        break
            raise FetchError(f'Сбер: полнота не подтверждена (получено {len(rows)}); требуется проверка каталога после входа', vacancies=list(rows.values()))
        finally:
            context.close()
            browser.close()


def login(directory):
    from playwright.sync_api import sync_playwright
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale='ru-RU')
        page = context.new_page()
        page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        print('Войдите в открывшемся браузере. Откройте каталог вакансий без фильтров, затем нажмите Enter здесь. Коды и пароли вводите только в браузере.')
        input()
        page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(2500)
        if '/auth/' in page.url or not page.url.startswith('https://privet.sber.ru/platform/'):
            browser.close()
            raise FetchError('Каталог не открыт после входа. Сессия не сохранена.')
        state = directory / 'sber-state.json'
        context.storage_state(path=str(state), indexed_db=True)
        sessions = {}
        for tab in context.pages:
            origin = tab.evaluate('location.origin')
            if origin == 'https://privet.sber.ru':
                sessions[origin] = tab.evaluate('Object.fromEntries(Object.entries(sessionStorage))')
        session = directory / 'sber-session-storage.json'
        session.write_text(json.dumps(sessions), encoding='utf-8')
        if os.name != 'nt':
            state.chmod(0o600)
            session.chmod(0o600)
        browser.close()
        print(f'Сессия сохранена в {directory}. Теперь запустите dry-run для Сбера. Сохранение сессии само по себе не подтверждает работу сборщика.')
