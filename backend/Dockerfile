FROM python:3.10-slim

WORKDIR /app

# 1) get latest pip so it can see google-genai
RUN pip install --upgrade pip

# 2) install your Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) copy in your app
COPY . .

# 4) expose & run
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
