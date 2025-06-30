FROM python:3.10-slim

# Install netcat-openbsd for connectivity testing
RUN apt-get update \
  && apt-get install -y netcat-openbsd \
  && rm -rf /var/lib/apt/lists/*

COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt

CMD ["python", "analytics.py"]
