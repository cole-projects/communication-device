FROM python:3.11-slim

RUN apt-get update && apt-get install -y git tini && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# tini runs as PID 1 and reaps orphaned git helper processes (they were exhausting the PID limit)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "tanya_bot.py"]
