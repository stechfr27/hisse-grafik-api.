FROM python:3.11-slim

# Playwright Chromium sistem bağımlılıkları (Debian trixie uyumlu)
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    fonts-liberation fonts-dejavu-core fonts-unifont \
    libnss3 libnspr4 \
    libatk1.0-0t64 libatk-bridge2.0-0t64 \
    libcups2t64 libdrm2 libdbus-1-3 \
    libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 \
    libasound2t64 \
    libglib2.0-0t64 libgles2 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --with-deps Debian'da Ubuntu paketleri aradığı için patlar → bağımlılıkları üstte elle kurduk
RUN playwright install chromium

COPY . .

# Tek worker — browser session'ları in-memory tutulduğu için şart
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
