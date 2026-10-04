FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers EXTRA_CA_CERT=/app/certs/russian-trusted-root.pem
WORKDIR /app
COPY requirements.txt .
COPY certs ./certs
RUN apt-get update && apt-get install -y --no-install-recommends libnss3-tools ca-certificates
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY monitor ./monitor
RUN useradd --uid 10001 --create-home monitor && mkdir /app/data && chown monitor:monitor /app/data
USER monitor
RUN mkdir -p /home/monitor/.pki/nssdb \
    && certutil -N -d sql:/home/monitor/.pki/nssdb --empty-password \
    && certutil -A -d sql:/home/monitor/.pki/nssdb -n "Russian Trusted Root CA" -t "C,," -i /app/certs/russian-trusted-root.pem
ENTRYPOINT ["python", "-m", "monitor"]
CMD ["run"]
