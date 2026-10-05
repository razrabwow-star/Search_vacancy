"""Print GraphQL response structure only, without credentials or field values."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def shape(value, depth=0):
    if depth >= 10:
        return type(value).__name__
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:60]:
            # Unusual dynamic keys may be identifiers: never print those.
            if not key.isidentifier() or len(key) > 60:
                key = '[dynamic-key]'
            result[key] = shape(item, depth + 1)
        return result
    if isinstance(value, list):
        return {'array_length': len(value), 'first_item': shape(value[0], depth + 1) if value else None}
    return type(value).__name__


def main():
    from playwright.sync_api import sync_playwright
    from .sber import URL, restore, browser_options
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='data')
    args = parser.parse_args()
    directory = Path(args.data_dir)
    state = directory / 'sber-state.json'
    if not state.exists():
        print('Сессия не найдена: сначала выполните login-sber.')
        return 1
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, **browser_options())
        context = browser.new_context(storage_state=str(state), locale='ru-RU')
        restore(context, directory)
        page = context.new_page()
        def inspect(response):
            url = urlparse(response.url)
            if url.hostname != 'privet.sber.ru' or url.path != '/api-web/app-external-candidate-bff/graphql':
                return
            print('GraphQL HTTP', response.status)
            try:
                structure = json.dumps(shape(response.json()), ensure_ascii=False, indent=2)
                if structure not in seen:
                    seen.add(structure)
                    print(structure, flush=True)
            except Exception as error:
                print('Ответ не прочитан:', type(error).__name__)
        page.on('response', inspect)
        try:
            page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(20000)
            print('Откройте каталог без фильтров, дождитесь списка и нажмите Enter здесь. Значения полей, cookies и токены не печатаются.', flush=True)
            input()
            page.wait_for_timeout(1000)
            if not seen:
                print('JSON-ответы каталога не получены.')
        finally:
            context.close()
            browser.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
