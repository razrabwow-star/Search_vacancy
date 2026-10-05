import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from monitor.db import Store
from monitor.model import Vacancy, classify, geography_match
from monitor.telegram import chunks, flush, report
from monitor.sber import decode_lists, to_vacancy
from monitor.sources import add_page
from monitor.http import FetchError
from monitor.__main__ import run, safe_error
from monitor import sources


def vacancy(key='1', title='Системный аналитик', cities=None, modes=None, conditions=''):
    return Vacancy('alfa', key, title, 'https://job.alfabank.ru/vacancies/test', 'BPMN, функциональные требования', cities or ['Москва'], modes or [], conditions)


class MonitorTests(unittest.TestCase):
    def test_error_diagnostics_do_not_expose_secrets(self):
        self.assertNotIn('secret-token', safe_error(RuntimeError('https://api.telegram.org/botsecret-token/getUpdates')))
        self.assertIn('UID 10001', safe_error(PermissionError('secret-path')))
        self.assertEqual(safe_error(ValueError('Нет TELEGRAM_BOT_TOKEN')), 'Нет TELEGRAM_BOT_TOKEN')

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'db.sqlite3')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def run_source(self, values, status='ok'):
        run = self.store.start()
        matched = sum(self.store.observe(run, v) for v in values)
        self.store.source_done(run, 'alfa', status, len(values), matched, 'timeout' if status != 'ok' else '')
        self.store.finish(run)
        return run

    def test_marks_every_run_and_deduplicates(self):
        values = [vacancy(), vacancy('2', 'Финансовый аналитик')]
        first = self.run_source(values)
        second = self.run_source(values)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM observations').fetchone()[0], 4)
        self.assertEqual(len(self.store.pending_events()), 1)
        self.assertEqual(self.store.conn.execute("SELECT last_processed_run_id FROM vacancies WHERE external_id='2'").fetchone()[0], second)
        self.assertNotEqual(first, second)

    def test_newly_relevant_is_not_lost(self):
        self.run_source([vacancy(title='Финансовый аналитик')])
        self.run_source([vacancy()])
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_processing_error_does_not_create_second_event(self):
        self.run_source([vacancy()])
        v = vacancy()
        v.processing_error = 'Карточка недоступна'
        run = self.run_source([v], 'error')
        self.assertEqual(self.store.conn.execute('SELECT result FROM observations WHERE run_id=?', (run,)).fetchone()[0], 'error')
        self.run_source([vacancy()])
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_missing_only_after_two_successes(self):
        self.run_source([vacancy()])
        self.run_source([], 'error')
        self.assertEqual(self.store.conn.execute('SELECT missing_checks FROM vacancies').fetchone()[0], 0)
        self.run_source([])
        self.assertEqual(self.store.conn.execute('SELECT status FROM vacancies').fetchone()[0], 'active')
        self.run_source([])
        self.assertEqual(self.store.conn.execute('SELECT status FROM vacancies').fetchone()[0], 'missing')

    def test_durable_notification_retry_and_reservation(self):
        run = self.run_source([vacancy()])
        text, ids = report(self.store, run)
        self.store.enqueue_report(run, ['part one', 'part two'], ids)
        self.assertEqual(self.store.pending_events(), [])
        class Sender:
            calls = []
            fail = True
            def send(self, text):
                if text == 'part two' and self.fail:
                    raise RuntimeError('offline')
                self.calls.append(text)
        sender = Sender()
        with self.assertRaises(RuntimeError):
            flush(self.store, sender)
        self.assertIsNone(self.store.conn.execute('SELECT sent_at FROM events').fetchone()[0])
        self.assertEqual(len(self.store.pending_messages()), 1)
        sender.fail = False
        flush(self.store, sender)
        self.assertEqual(sender.calls, ['part one', 'part two'])
        self.assertIsNotNone(self.store.conn.execute('SELECT sent_at FROM events').fetchone()[0])

    def test_geography(self):
        self.assertTrue(classify(vacancy())[0])
        self.assertTrue(classify(vacancy(cities=['Казань'], modes=['Удаленный']))[0])
        self.assertFalse(classify(vacancy(cities=['Казань'], modes=['Удаленный', 'Гибрид']))[0])
        self.assertFalse(classify(vacancy(cities=['Казань'], conditions='Возможность удаленной работы после адаптации'))[0])
        self.assertTrue(classify(vacancy(cities=['Казань'], conditions='Полностью дистанционная работа'))[0])
        self.assertFalse(classify(vacancy(cities=['Казань'], modes=['Remote'], conditions='Проживание в Казани'))[0])
        self.assertFalse(classify(vacancy(cities=['Казань'], conditions='Скидки на дистанционное обучение'))[0])

    def test_roles(self):
        for title in ('Business Analyst', 'Systems Analyst', 'Бизнес‑аналитик', 'Ведущий системный аналитик', 'BPM-аналитик'):
            self.assertTrue(classify(vacancy(title=title))[0], title)
        for title in ('Продуктовый аналитик', 'Data Analyst', 'Финансовый аналитик', 'Разработчик'):
            self.assertFalse(classify(vacancy(title=title))[0], title)
        self.assertTrue(classify(vacancy(title='Аналитик'))[0])

    def test_report_errors_are_not_no_jobs(self):
        run = self.run_source([], 'error')
        text, _ = report(self.store, run)
        self.assertIn('итог неполный', text)
        self.assertNotIn('Подходящих открытых вакансий сейчас нет.', text)

    def test_message_split(self):
        text = ('🚀' * 6000) + '\n' + ('текст\n' * 1500)
        parts = chunks(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3500 for p in parts))

    def test_schema_checks(self):
        with self.assertRaises(FetchError):
            add_page({}, [{'title': 'Нет ID'}], 'id')
        self.assertEqual(decode_lists({'user': {'id': 1, 'name': 'Test'}}), [])
        found = decode_lists({'content': [{'vacancyId': 12, 'vacancyName': 'Системный аналитик', 'city': {'name': 'Москва'}}], 'totalElements': 1})
        self.assertEqual(found[0][1], 1)
        self.assertTrue(classify(to_vacancy(found[0][0][0]))[0])

    def test_partial_pagination_keeps_processed_vacancies(self):
        class Client:
            calls = 0
            def json(self, method, url, **kwargs):
                if url.endswith('/options'):
                    return {'optionLists': {'cities': [{'id': '0100', 'text': 'Москва'}]}}
                self.calls += 1
                if self.calls == 2:
                    raise FetchError('Вторая страница недоступна')
                return {'total': 2, 'items': [{'id': '1', 'name': 'Системный аналитик', 'cityId': '0100', 'slug': '/moskva/test', 'description': ''}]}
        run_id = run(self.store, ['alfa'], Client(), 10, self.directory.name)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()[0], 'partial')
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_yandex_next_uses_only_cursor(self):
        class Client:
            calls = []
            def json(self, method, url, **kwargs):
                self.calls.append((url, kwargs['params']))
                return {'results': [{'id': len(self.calls), 'title': 'Разработчик', 'publication_slug_url': 'dev', 'vacancy': {}}],
                        'next': 'http://internal.invalid/private?cursor=abc' if len(self.calls) == 1 else None}
        client = Client()
        self.assertEqual(len(list(sources.yandex(client, 10))), 2)
        self.assertTrue(all(url.startswith('https://yandex.ru/') for url, _ in client.calls))
        self.assertEqual(client.calls[1][1]['cursor'], 'abc')


if __name__ == '__main__':
    unittest.main()
