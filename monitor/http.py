import time
import ssl
import os
import httpx


class FetchError(RuntimeError):
    def __init__(self, message, vacancies=None):
        super().__init__(message)
        self.vacancies = vacancies or []


class Client:
    def __init__(self, timeout=35, delay=0.5):
        self.delay = delay
        context = ssl.create_default_context()
        if os.getenv('EXTRA_CA_CERT'):
            context.load_verify_locations(os.environ['EXTRA_CA_CERT'])
        self.client = httpx.Client(verify=context, timeout=timeout, follow_redirects=True, headers={'User-Agent': 'Mozilla/5.0 (compatible; PersonalVacancyMonitor/1.0)', 'Accept': 'application/json,text/html'})

    def close(self):
        self.client.close()

    def request(self, method, url, **kwargs):
        for attempt in range(3):
            time.sleep(self.delay)
            try:
                response = self.client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise FetchError(f'HTTP {response.status_code}')
                response.raise_for_status()
                return response
            except (httpx.HTTPError, FetchError) as error:
                if attempt == 2:
                    # Do not log response bodies, tokens or authenticated URLs.
                    reason = f'HTTP {error.response.status_code}' if isinstance(error, httpx.HTTPStatusError) else str(error) if isinstance(error, FetchError) else type(error).__name__
                    if 'CERTIFICATE_VERIFY_FAILED' in str(error):
                        reason = 'недоверенный TLS-сертификат; настройте EXTRA_CA_CERT'
                    raise FetchError('Источник недоступен после трёх попыток: ' + reason) from None
                time.sleep(2 ** attempt)

    def json(self, method, url, **kwargs):
        try:
            data = self.request(method, url, **kwargs).json()
        except ValueError:
            raise FetchError('Источник вернул ответ не в JSON') from None
        if not isinstance(data, dict):
            raise FetchError('Изменилась структура ответа источника')
        return data
