from dataclasses import dataclass, field, asdict
import hashlib
import json
import re
import unicodedata
from bs4 import BeautifulSoup


def plain(text):
    if isinstance(text, list):
        return ' '.join(plain(t) for t in text)
    if isinstance(text, dict):
        return plain(text.get('description', text.get('content', text.get('text', ''))))
    return BeautifulSoup(str(text or ''), 'html.parser').get_text(' ', strip=True)


def norm(text):
    text = unicodedata.normalize('NFKC', str(text)).lower().replace('ё', 'е')
    return re.sub(r'\s+', ' ', re.sub('[‐‑‒–—−]', '-', text)).strip()


@dataclass
class Vacancy:
    source: str
    external_id: str
    title: str
    url: str
    description: str = ''
    cities: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    conditions: str = ''
    moscow: bool = False
    processing_error: str = ''

    def digest(self):
        data = asdict(self)
        data['cities'] = sorted(set(data['cities']))
        data['modes'] = sorted(set(data['modes']))
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


ROLE = re.compile(r'бизнес[ -]*аналитик\b|системн\w*[ -]+аналитик\b|business[ -]+analyst\b|systems?[ -]+analyst\b|bpm[ -]*аналитик\b', re.I)
ANALYST = re.compile(r'аналитик|analyst', re.I)
OTHER = re.compile(r'финансов|маркетинг|продуктов|кредитн|data analyst|аналитик данных|bi[ -]+аналитик|комплаенс', re.I)
SIGNALS = [r'сбор\w* (?:и \w+ )?требован', r'бизнес[ -]*процесс', r'функциональн\w* требован', r'нефункциональн\w* требован', r'bpmn|uml', r'проектирован\w* (?:api|интеграц)', r'системн\w* анализ']


def role_match(v):
    title = norm(v.title)
    if ROLE.search(title):
        return True, 'Бизнес-/системный анализ по названию'
    if not ANALYST.search(title) or OTHER.search(title):
        return False, 'Другое направление'
    text = norm(v.description)
    if sum(bool(re.search(s, text)) for s in SIGNALS) >= 2:
        return True, 'Аналитик: обязанности соответствуют бизнес-/системному анализу'
    return False, 'Недостаточно признаков бизнес-/системного анализа'


def geography_match(v):
    if v.moscow or any(norm(c) in ('москва', 'moscow') for c in v.cities):
        return True, 'Москва'
    modes = {norm(m) for m in v.modes}
    remote = any(re.search(r'удален|дистанцион|из дома|remote', m) for m in modes)
    hybrid = any(re.search(r'гибрид|hybrid|mixed', m) for m in modes)
    conditions = norm(v.conditions)
    if re.search(r'только (?:из|в)|проживан\w* в|необходим\w* присутств|посещ\w* офис|после адаптац|после испытательн', conditions):
        return False, 'Вне Москвы: условия удалёнки требуют ручной проверки'
    if remote and not hybrid:
        return True, 'Дистанционно; проверить территориальные ограничения в описании'
    if re.search(r'полностью (?:удален|дистанцион)|(?:работа|работать) (?:из любой точки россии|из любого города россии)', conditions):
        return True, 'Полностью дистанционно по условиям'
    return False, 'Вне Москвы или формат работы не подтверждён'


def classify(v):
    if v.processing_error:
        return False, v.processing_error
    role, reason = role_match(v)
    if not role:
        return False, reason
    geo, geo_reason = geography_match(v)
    return geo, reason + '; ' + geo_reason
