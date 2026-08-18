FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for lxml + Tesseract OCR. The regional mirror
# avoids stalled package-index downloads on this deployment network.
RUN sed -i \
        -e 's|http://deb.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g' \
        -e 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=2 update \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=2 install -y --no-install-recommends \
    gcc libxml2-dev libxslt1-dev curl \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    libtesseract-dev libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir \
    --index-url https://mirrors.cloud.tencent.com/pypi/simple \
    --timeout 60 --retries 3 \
    -r requirements.txt

COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data

EXPOSE 8790

CMD ["gunicorn", "-w", "2", "--preload", "-b", "0.0.0.0:8790", "--timeout", "300", "--access-logfile", "-", "src.app:app"]
