import os
import json
import datetime
from io import BytesIO

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request
from fastapi.security import OAuth2PasswordBearer
import httpx
import redis
from confluent_kafka import Producer, Consumer, KafkaException
from jose import JWTError
from minio import Minio

# Gemini client
from google import genai
from google.genai import types

# Database & metadata from your database.py
from sqlalchemy import insert, create_engine
from databases import Database
from database import llm_logs, user_logs, metadata, DATABASE_URL, database

from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI()

# --- Environment-based configuration ---
KEYCLOAK_URL = os.getenv(
    "KEYCLOAK_URL", "http://keycloak:8080/realms/ai-bootcamp"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS = os.getenv("MINIO_ROOT_USER", "minio")
MINIO_SECRET = os.getenv("MINIO_ROOT_PASSWORD", "minio123")
MINIO_SECURE = os.getenv("MINIO_SECURE", "False").lower() in ("true", "1", "yes")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "uploads")

# --- Clients initialization ---
# Redis
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Kafka
producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})
consumer_config = {
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "group.id": "fastapi-group",
    "auto.offset.reset": "earliest",
}

# Gemini
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is required")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Keycloak OAuth2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# MinIO (initialized on startup)
minio_client: Minio

# --- Startup / Shutdown events ---
@app.on_event("startup")
async def startup():
    global minio_client
    # Connect to the database
    await database.connect()
    # Ensure tables exist
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)
    # Initialize MinIO client
    minio_client = Minio(
        MINIO_URL,
        access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET,
        secure=MINIO_SECURE,
    )

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# --- Helper for Keycloak token verification ---
async def verify_token(token: str = Depends(oauth2_scheme)):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{KEYCLOAK_URL}/protocol/openid-connect/userinfo", headers=headers)
        if res.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token error")

# --- Routes ---

@app.post("/ask-llm")
async def ask_llm(request: Request):
    data = await request.json()
    prompt = data.get("prompt")
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # 1) Check cache
    cached = redis_client.get(prompt)
    if cached:
        source, reply = "cache", cached
    else:
        # 2) Stream from Gemini
        source, reply = "gemini", ""
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
            response_mime_type="text/plain",
        )
        try:
            for chunk in gemini_client.models.generate_content_stream(
                model=GEMINI_MODEL, contents=contents, config=config
            ):
                reply += chunk.text
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Gemini Error: {e}")
        redis_client.set(prompt, reply)

    # 3) Log to PostgreSQL
    await database.execute(
        insert(llm_logs).values(
            prompt=prompt,
            response=reply,
            source=source,
            created_at=datetime.datetime.utcnow(),
        )
    )

    # 4) Emit Kafka event
    event = {
        "prompt": prompt,
        "response": reply,
        "source": source,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
    try:
        producer.produce("llm-events", value=json.dumps(event).encode("utf-8"))
        producer.flush()
    except Exception as e:
        print(f"[Kafka Error] Failed to send event: {e}")

    return {"source": source, "response": reply}

@app.get("/secure-data")
async def secure_data(token: str = Depends(verify_token)):
    return {"message": "Protected route access granted"}

@app.post("/log-action")
async def log_action(username: str, action: str):
    await database.execute(
        insert(user_logs).values(username=username, action=action, timestamp=datetime.datetime.utcnow())
    )
    return {"status": "logged"}

@app.get("/logs")
async def get_logs():
    rows = await database.fetch_all(user_logs.select().limit(10))
    return rows

@app.post("/send-message")
def send_message(message: str):
    try:
        producer.produce("ai-topic", message.encode("utf-8"))
        producer.flush()
        return {"status": "message sent"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/consume-messages")
async def consume_messages():
    consumer = Consumer(consumer_config)
    consumer.subscribe(["ai-topic"])
    messages = []
    try:
        for _ in range(5):
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())
            messages.append(msg.value().decode("utf-8"))
    finally:
        consumer.close()
    return {"messages": messages}

@app.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    contents = await file.read()
    minio_client.put_object(
        MINIO_BUCKET,
        file.filename,
        data=BytesIO(contents),
        length=len(contents),
        content_type=file.content_type,
    )
    return {"filename": file.filename, "status": "uploaded"}

@app.get("/list-files")
def list_files():
    return [obj.object_name for obj in minio_client.list_objects(MINIO_BUCKET)]

@app.get("/llm-history")
async def get_llm_history():
    rows = await database.fetch_all(
        llm_logs.select().order_by(llm_logs.c.created_at.desc()).limit(50)
    )
    return [dict(row) for row in rows]
