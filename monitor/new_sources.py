"""Public career catalogs: VTB, RWB and Avito. No browser or login needed."""
import json
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .http import FetchError
from .model import Vacancy, plain, ANALYST, OTHER


def analyst(title):
    return bool(ANALYST.search(title) and not OTHER.search(title))


def next_fallback(html):
    script = BeautifulSoup(html, 'html.parser').find('script', id='__NEXT_DATA__')
    try:
        return json.loads(script.string)['props']['pageProps']['fallback']
    except (TypeError, KeyError, ValueError, AttributeError):
        raise FetchError('ВТБ: изменился формат каталога') from None


def named(values):
    if not isinstance(values, list):
        return []
    return [plain(v.get('name') or v.get('title')) for v in values if isinstance(v, dict)]


def vtb(client, max_pages):
    yielded = set()
    # The all-country HH feed caps at 2000 records. Separate relevant scopes
    # avoid declaring that truncated feed complete and include remote jobs elsewhere.
    for filters in ({'area': '1'}, {'schedule': 'remote'}):
        seen = set()
        for page in range(max_pages):
            params = dict(filters, page=page, perPage=100)
            fallback = next_fallback(client.request('GET', 'https://rabota-vtb.ru/career', params=params).text)
            catalogs = [v for k, v in fallback.items() if k.split('?')[0] == '/vtb-federal-campaign/vacancies']
            if len(catalogs) != 1 or not isinstance(catalogs[0], dict):
                raise FetchError('ВТБ: нет выдачи вакансий')
            data = catalogs[0]
            rows, total, pages = data.get('items'), data.get('found'), data.get('pages')
            if not isinstance(rows, list) or type(total) is not int or type(pages) is not int or data.get('page') != page:
                raise FetchError('ВТБ: нет подтверждённой пагинации')
            before = len(seen)
            for row in rows:
                if not isinstance(row, dict) or not row.get('id') or not row.get('name'):
                    raise FetchError('ВТБ: у вакансии нет ID или названия')
                key = str(row['id'])
                seen.add(key)
                if key in yielded:
                    continue
                yielded.add(key)
                url = 'https://rabota-vtb.ru/career/' + key
                d, error = row, ''
                if analyst(row['name']):
                    try:
                        fb = next_fallback(client.request('GET', url).text)
                        d = fb['/vtb-federal-campaign/vacancies/' + key]
                        if str(d.get('id')) != key or not d.get('description'):
                            raise FetchError('Неполная карточка')
                    except (FetchError, KeyError, TypeError, AttributeError):
                        d, error = row, 'ВТБ: не получено описание аналитической вакансии'
                description = plain(d.get('description') or row.get('snippet'))
                # HH's description combines duties and benefits. Only the trailing
                # conditions heading can substantiate a remote format without metadata.
                conditions = condition_tail(description)
                area = d.get('area') or row.get('area') or {}
                yield Vacancy('vtb', key, plain(row['name']), url, description,
                              [plain(area.get('name'))], named(d.get('work_format')),
                              conditions, filters.get('area') == '1', error)
            if page + 1 >= pages:
                if len(seen) != total:
                    raise FetchError('ВТБ: каталог неполный или изменился во время обхода')
                break
            if len(seen) == before:
                raise FetchError('ВТБ: пагинация не продвигается')
        else:
            raise FetchError('ВТБ: достигнут предел страниц')


def condition_tail(description):
    match = re.search(r'(?:условия(?: работы)?|мы предлагаем|что мы предлагаем)\s*[:：]', description, re.I)
    return description[match.end():] if match else ''


def rwb(client, max_pages):
    root = 'https://career.rwb.ru'
    seen, offset = set(), 0
    for _ in range(max_pages):
        raw = client.json('GET', root + '/hr-crm-api/api/v2/pub/vacancies', params={'limit': 100, 'offset': offset})
        data = raw.get('data')
        if not isinstance(data, dict):
            raise FetchError('RWB: нет данных каталога')
        rows, paging = data.get('items'), data.get('range', {})
        total = paging.get('count')
        if not isinstance(rows, list) or type(total) is not int or paging.get('offset') != offset:
            raise FetchError('RWB: нет подтверждённой пагинации')
        before = len(seen)
        for row in rows:
            if not isinstance(row, dict) or not row.get('id') or not row.get('name'):
                raise FetchError('RWB: у вакансии нет ID или названия')
            key = str(row['id'])
            if key in seen:
                continue
            seen.add(key)
            url = root + '/vacancies/' + key
            d, error = {}, ''
            if analyst(row['name']):
                try:
                    raw_detail = client.json('GET', root + '/crm-api/api/v1/pub/vacancies/' + key,
                                             headers={'Referer': url, 'Origin': root})
                    d = raw_detail.get('data')
                    if not isinstance(d, dict) or str(d.get('id')) != key or not d.get('description'):
                        raise FetchError('Неполная карточка')
                except FetchError:
                    d, error = {}, 'RWB: не получено описание аналитической вакансии'
            description = ' '.join(plain(d.get(k)) for k in ('description', 'requirements_arr', 'duties_arr'))
            city = d.get('office_location_city_title') or row.get('city_title')
            modes = named(d.get('employment_types_list') or row.get('employment_types'))
            yield Vacancy('rwb', key, plain(row['name']), url, description,
                          [plain(city)] if city else [], modes, plain(d.get('conditions_arr')), False, error)
        offset += len(rows)
        if offset >= total:
            if len(seen) != total:
                raise FetchError('RWB: каталог неполный или содержит повторные ID')
            break
        if len(seen) == before:
            raise FetchError('RWB: пагинация не продвигается')
    else:
        raise FetchError('RWB: достигнут предел страниц')


def avito(client, max_pages):
    root = 'https://career.avito.com'
    # The initial HTML is a preview of four jobs per direction. The public
    # filter response supplies the complete list and counts per direction.
    data = client.json('GET', root + '/vacancies/', params={'action': 'filter'},
                       headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': root + '/vacancies/'})
    if not isinstance(data.get('html'), str):
        raise FetchError('Авито: нет HTML выдачи')
    counts = data.get('counts', {}).get('DIRECTION')
    if not isinstance(counts, dict) or not counts or any(type(n) is not int or n < 0 for n in counts.values()):
        raise FetchError('Авито: нет общего количества вакансий по направлениям')
    soup = BeautifulSoup(data['html'], 'html.parser')
    cards = soup.select('.vacancies-section__item')
    seen = set()
    for card in cards:
        link = card.select_one('a.vacancies-section__item-name[href]')
        key = card.get('data-vacancy-id')
        if not key or not link or not link.get_text(strip=True):
            raise FetchError('Авито: у вакансии нет ID, названия или ссылки')
        url = urljoin(root, link['href'])
        if urlparse(url).hostname != 'career.avito.com' or not re.fullmatch(r'/vacancies/[^/]+/\d+/', urlparse(url).path):
            raise FetchError('Авито: неизвестный адрес карточки')
        if key in seen:
            continue
        seen.add(key)
        title = link.get_text(' ', strip=True)
        city_node, mode_node = card.select_one('.vacancies-section__item-cities'), card.select_one('.vacancies-section__item-format')
        cities = [s.strip() for s in city_node.get_text(' ', strip=True).split(',')] if city_node else []
        modes = [mode_node.get_text(' ', strip=True)] if mode_node else []
        description = conditions = error = ''
        if analyst(title):
            try:
                detail = BeautifulSoup(client.request('GET', url).text, 'html.parser')
                header = detail.select_one('[data-detail-vacancy]')
                sections = detail.select('.vacancies-detail__description')
                if not header or str(header.get('data-detail-vacancy-id')) != key or not sections:
                    raise FetchError('Неполная карточка')
                description = ' '.join(s.get_text(' ', strip=True) for s in sections)
                conditions = ' '.join(s.get_text(' ', strip=True) for s in sections
                                      if s.find(re.compile('^h[1-6]$')) and re.search(r'условия|предлагаем|у нас|получите', s.find(re.compile('^h[1-6]$')).get_text(), re.I))
            except FetchError:
                error = 'Авито: не получено описание аналитической вакансии'
        yield Vacancy('avito', key, title, url, description, cities, modes, conditions, False, error)
    if len(seen) != sum(counts.values()):
        raise FetchError('Авито: число карточек не совпадает с полным каталогом')
