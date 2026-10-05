FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 EXTRA_CA_CERT=/app/certs/russian-trusted-root.pem
WORKDIR /app
COPY requirements.txt .
COPY certs ./certs
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor ./monitor
RUN useradd --uid 10001 --create-home monitor && mkdir /app/data && chown monitor:monitor /app/data
USER monitor
ENTRYPOINT ["python", "-m", "monitor"]
CMD ["run"]
