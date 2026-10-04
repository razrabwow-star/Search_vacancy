from urllib.parse import urlparse, parse_qs, quote
from bs4 import BeautifulSoup
from .http import FetchError
from .model import Vacancy, plain, ANALYST, OTHER

MOSCOW_FIAS = '0c5b2444-70a0-4932-980c-b4dc0d3f02b5'
TB_API = 'https://www.tbank.ru/pfpjobs/papi/'
YA_API = 'https://yandex.ru/jobs/api/jobs/publications/'
ALFA_API = 'https://job.alfabank.ru/api/vacancies'


def add_page(seen, items, id_key):
    if not isinstance(items, list):
        raise FetchError('Нет списка вакансий в ответе')
    new = 0
    for item in items:
        if not isinstance(item, dict) or not item.get(id_key):
            raise FetchError('У вакансии отсутствует стабильный ID')
        key = str(item[id_key])
        if key not in seen:
            new += 1
        seen[key] = item
    return new


def tbank(client, max_pages):
    seen = set()
    # A Moscow pass supplies exact geography; a country-wide pass covers remote jobs.
    for fias in (MOSCOW_FIAS, None):
        offset = 0
        for _ in range(max_pages):
            filters = {'type': 'T_CAREER', 'status': 'ACTIVE', 'includeSeoAndPcPublications': False, 'includeInternshipPublications': True,
                       'or': [{'category': c} for c in ('tcareer_it', 'tcareer_back_office', 'tcareer_work_with_clients')]}
            if fias:
                filters['searchFiasIds'] = [fias]
            raw = client.json('POST', TB_API + 'getVacancies', json={'filters': {'generatedGraphQL': filters}, 'pagination': {'offset': offset}, 'limit': 100})
            if raw.get('resultCode') != 'OK' or not isinstance(raw.get('payload'), dict):
                raise FetchError('Т-Банк: запрос отклонён')
            payload = raw['payload']
            items = payload.get('vacancies')
            page_ids = {}
            add_page(page_ids, items, 'urlSlug')
            for key, row in page_ids.items():
                if key not in seen:
                    seen.add(key)
                    yield tbank_vacancy(client, key, row, bool(fias))
            p = payload.get('nextPagination', {})
            if p.get('isFinished') is True:
                if not items and offset == 0:
                    raise FetchError('Т-Банк: неожиданно пустой каталог, проверка неполная')
                break
            next_offset = p.get('offset')
            if not isinstance(next_offset, int) or next_offset <= offset or not items:
                raise FetchError('Т-Банк: пагинация не продвигается')
            offset = next_offset
        else:
            raise FetchError('Т-Банк: достигнут предел страниц')


def tbank_vacancy(client, key, row, moscow):
    categories = {'tcareer_it': 'it', 'tcareer_back_office': 'back-office', 'tcareer_work_with_clients': 'service'}
    category = categories[row['category']]
    url = f"https://www.tbank.ru/career/{category}/vacancy/moscow/{quote(row.get('seoSlug') or '_')}/{quote(key)}/"
    description = plain(row.get('shortDescription'))
    cities = [row['subtitle']] if row.get('subtitle') else []
    modes = row.get('tags', [])
    conditions = processing_error = ''
    if ANALYST.search(row['title']) and not OTHER.search(row['title']) and not row.get('redirectUrl'):
        try:
            detail = client.json('POST', TB_API + 'getVacancyDescription', json={'urlSlug': key, 'options': {'category': row['category']}})
        except FetchError:
            detail = {'resultCode': 'ERROR'}
        if detail.get('resultCode') != 'OK' or not isinstance(detail.get('payload'), dict):
            processing_error = 'Т-Банк: не получено описание аналитической вакансии'
        d = detail.get('payload', {})
        if d.get('status') == 'not-found':
            processing_error = 'Вакансия исчезла между чтением списка и карточки'
        sections = d.get('description', [])
        if isinstance(sections, list):
            description = ' '.join(plain(s.get('content', '')) for s in sections if isinstance(s, dict)) or description
            conditions = ' '.join(plain(s.get('content', '')) for s in sections if isinstance(s, dict) and any(x in str(s.get('title', '')).lower() for x in ('услов', 'предлага', 'работа')))
        cities += [d['subtitle']] if d.get('subtitle') else []
        modes = [t.get('text', '') if isinstance(t, dict) else str(t) for t in d.get('tags', modes)]
    return Vacancy('tbank', key, plain(row['title']), url, description, cities, modes, conditions, moscow, processing_error)


def yandex(client, max_pages):
    seen = {}
    yielded = set()
    cursor = None
    cursors = set()
    for _ in range(max_pages):
        params = {'page_size': 100}
        if cursor:
            params['cursor'] = cursor
        data = client.json('GET', YA_API, params=params)
        new = add_page(seen, data.get('results'), 'id')
        for row in data['results']:
            key = str(row['id'])
            if key not in yielded:
                yielded.add(key)
                yield yandex_vacancy(client, key, row)
        if not data.get('next'):
            if not seen and data.get('count') != 0:
                raise FetchError('Яндекс: неожиданно пустая выдача')
            break
        # The next URL can contain an internal hostname. Reuse only its cursor.
        cursor = parse_qs(urlparse(data['next']).query).get('cursor', [None])[0]
        if not cursor or cursor in cursors or not new:
            raise FetchError('Яндекс: пагинация не продвигается')
        cursors.add(cursor)
    else:
        raise FetchError('Яндекс: достигнут предел страниц')


def yandex_vacancy(client, key, row):
    vacancy = row.get('vacancy', {})
    url = 'https://yandex.ru/jobs/vacancies/' + quote(row['publication_slug_url'])
    description = plain(row.get('short_summary'))
    conditions = processing_error = ''
    if ANALYST.search(row['title']) and not OTHER.search(row['title']):
        for _ in range(2):
            try:
                detail_html = client.request('GET', url).text
            except FetchError:
                detail_html = ''
                processing_error = 'Яндекс: не получено описание аналитической вакансии'
            soup = BeautifulSoup(detail_html, 'html.parser')
            main = soup.find('main')
            if not main or not main.find('h1'):
                if row.get('redirect_url'):
                    # External publications may have a different layout; use the public summary.
                    main = None
                else:
                    processing_error = 'Яндекс: не получена карточка аналитической вакансии'
            if main:
                description = main.get_text(' ', strip=True)
                processing_error = ''
                break
            if row.get('redirect_url') and detail_html:
                processing_error = ''
                break
        # Work mode is authoritative metadata; arbitrary mentions in responsibilities aren't.
    return Vacancy('yandex', key, plain(row['title']), url, description,
                   [c['name'] for c in vacancy.get('cities', [])],
                   [m['name'] for m in vacancy.get('work_modes', [])], conditions, False, processing_error)


def alfa(client, max_pages):
    data = client.json('GET', ALFA_API + '/options', params=[('listId', 'cities'), ('take', 2000)])
    cities = {str(c['id']): c['text'] for c in data.get('optionLists', {}).get('cities', [])}
    seen = {}
    yielded = set()
    skip = 0
    for _ in range(max_pages):
        data = client.json('GET', ALFA_API, params={'take': 100, 'skip': skip})
        items = data.get('items')
        new = add_page(seen, items, 'id')
        for r in items:
            key = str(r['id'])
            if key not in yielded and not r.get('isInternal') and not r.get('isHidden'):
                yielded.add(key)
                yield Vacancy('alfa', key, plain(r['name']), 'https://job.alfabank.ru/vacancies' + r['slug'],
                              plain(r.get('description')), [cities.get(str(r.get('cityId')), str(r.get('cityId', '')))], [],
                              plain(r.get('conditions')), str(r.get('cityId')) == '0100' or '0100' in r.get('cityIds', []))
        total = data.get('total')
        if not isinstance(total, int):
            raise FetchError('Альфа-Банк: нет общего количества вакансий')
        skip += len(items)
        if skip >= total:
            break
        if not new or not items:
            raise FetchError('Альфа-Банк: пагинация не продвигается')
    else:
        raise FetchError('Альфа-Банк: достигнут предел страниц')
