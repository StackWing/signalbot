# Use lightweight Python
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Do not copy .env into image. Use env vars in Render/Host
CMD ["python", "ultra_bot.py"]
