import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from monitor.__main__ import run
from monitor.db import Store
from monitor.http import FetchError
from monitor.model import Vacancy, classify
from monitor.new_sources import vtb, avito


def vtb_html(items, total):
    data = {'props': {'pageProps': {'fallback': {'/vtb-federal-campaign/vacancies':
            {'items': items, 'found': total, 'pages': 1, 'page': 0}}}}}
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'


class NewSourcesTests(unittest.TestCase):
    def test_vtb_checks_moscow_and_remote_without_duplicate_ids(self):
        class Client:
            calls = []
            def request(self, method, url, **kwargs):
                self.calls.append(kwargs['params'])
                return SimpleNamespace(text=vtb_html([{'id': 1, 'name': 'Разработчик', 'area': {'name': 'Москва'}}], 1))
        client = Client()
        self.assertEqual(len(list(vtb(client, 3))), 1)
        self.assertEqual(client.calls[0]['area'], '1')
        self.assertEqual(client.calls[1]['schedule'], 'remote')

    def test_vtb_rejects_capped_catalog(self):
        class Client:
            def request(self, *args, **kwargs):
                return SimpleNamespace(text=vtb_html([{'id': 1, 'name': 'Разработчик'}], 2))
        with self.assertRaises(FetchError):
            list(vtb(Client(), 3))

    def test_rwb_partial_page_keeps_observation(self):
        class Client:
            def json(self, method, url, **kwargs):
                if kwargs['params']['offset']:
                    raise FetchError('Вторая страница недоступна')
                return {'data': {'range': {'count': 2, 'offset': 0}, 'items': [{'id': 1, 'name': 'Разработчик'}]}}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db.sqlite3')
            try:
                run_id = run(store, ['rwb'], Client(), 3, directory)
                self.assertEqual(store.conn.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
                self.assertEqual(store.conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()[0], 'partial')
            finally:
                store.close()

    def test_avito_requires_full_count_and_real_work_format(self):
        class Client:
            total = 1
            def json(self, *args, **kwargs):
                return {'counts': {'DIRECTION': {'it': self.total}}, 'html': '''
                    <div class="vacancies-section__item" data-vacancy-id="11" data-vacancy-remote="Да">
                    <a class="vacancies-section__item-name" href="/vacancies/it/22/">Разработчик</a>
                    <div class="vacancies-section__item-cities">Казань</div>
                    <span class="vacancies-section__item-format">Гибрид</span></div>'''}
        client = Client()
        item = list(avito(client, 3))[0]
        self.assertEqual(item.external_id, '11')
        self.assertEqual(item.modes, ['Гибрид'])
        client.total = 2
        with self.assertRaises(FetchError):
            list(avito(client, 3))

    def test_data_analyst_in_business_analytics_department_is_excluded(self):
        v = Vacancy('vtb', '1', 'Fullstack аналитик данных в службу бизнес аналитики', 'https://example.com', cities=['Москва'])
        self.assertFalse(classify(v)[0])
        v.title = 'Бизнес-аналитик'
        self.assertTrue(classify(v)[0])


if __name__ == '__main__':
    unittest.main()
