from fastapi import FastAPI, Depends, HTTPException, UploadFile, File,Request
from fastapi.security import OAuth2PasswordBearer

from jose import JWTError
import httpx
import datetime
import openai
import redis
import os
import redis
import os
import json

from minio import Minio
from database import database, user_logs

from confluent_kafka import Producer, Consumer, KafkaException
import time

from io import BytesIO

from database import metadata, DATABASE_URL
from sqlalchemy import create_engine

app = FastAPI()

# OAuth2 setup for Keycloak
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
KEYCLOAK_REALM = "ai-bootcamp"
KEYCLOAK_URL = "http://keycloak:8080/realms/ai-bootcamp"


# Set up OpenRouter config
openai.api_base = "https://openrouter.ai/api/v1"
openai.api_key = "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" 

# Redis client
redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)

async def verify_token(token: str = Depends(oauth2_scheme)):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{KEYCLOAK_URL}/protocol/openid-connect/userinfo", headers=headers)
            if res.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token error")

# Globals for Kafka and MinIO
producer = Producer({'bootstrap.servers': 'kafka:9092'})
consumer_config = {
    'bootstrap.servers': 'kafka:9092',
    'group.id': 'fastapi-group',
    'auto.offset.reset': 'earliest'
}

minio_client = None
BUCKET_NAME = "uploads"

@app.on_event("startup")
async def startup():
    global minio_client
    await database.connect()

    # Create tables if not exist
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

    # Initialize MinIO
    minio_client = Minio(
        "minio:9000",
        access_key="minio",
        secret_key="minio123",
        secure=False
    )

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# -------------------
# Secure Test Route
# -------------------
@app.get("/secure-data")
async def secure_data(token: str = Depends(verify_token)):
    return {"message": "Protected route access granted"}

# -------------------
# PostgreSQL Logging
# -------------------
@app.post("/log-action")
async def log_action(username: str, action: str):
    query = user_logs.insert().values(username=username, action=action, timestamp=datetime.datetime.utcnow())
    await database.execute(query)
    return {"status": "logged"}

@app.get("/logs")
async def get_logs():
    query = user_logs.select().limit(10)
    return await database.fetch_all(query)

# -------------------
# Kafka Messaging
# -------------------
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

# -------------------
# MinIO File Upload
# -------------------
@app.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    contents = await file.read()
    
    minio_client.put_object(
        BUCKET_NAME,
        file.filename,
        data=BytesIO(contents),  # ✅ FIX: stream not raw bytes
        length=len(contents),
        content_type=file.content_type,
    )
    return {"filename": file.filename, "status": "uploaded"}

@app.get("/list-files")
def list_files():
    global minio_client
    objects = minio_client.list_objects(BUCKET_NAME)
    return [obj.object_name for obj in objects]




from database import llm_logs

@app.post("/ask-llm")
async def ask_llm(request: Request):
    data = await request.json()
    prompt = data.get("prompt")

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    cached = redis_client.get(prompt)
    source = "cache"
    reply = cached

    if not cached:
        try:
            response = openai.ChatCompletion.create(
                model="openai/gpt-4o-mini",  # or any supported model
                messages=[{"role": "user", "content": prompt}]
            )
            reply = response['choices'][0]['message']['content']
            redis_client.set(prompt, reply)
            source = "openrouter"
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"OpenRouter Error: {str(e)}")

    # Log to PostgreSQL
    await database.execute(
        llm_logs.insert().values(
            prompt=prompt,
            response=reply,
            source=source
        )
    )

    # Send Kafka event
    event = {
        "prompt": prompt,
        "response": reply,
        "source": source,
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

    try:
        producer.produce("llm-events", value=json.dumps(event).encode("utf-8"))
        producer.flush()
    except Exception as e:
        print(f"[Kafka Error] Failed to send event: {e}")

    return {"source": source, "response": reply}

from database import llm_logs

@app.get("/llm-history")
async def get_llm_history():
    query = llm_logs.select().order_by(llm_logs.c.created_at.desc()).limit(50)
    rows = await database.fetch_all(query)
    return [dict(row) for row in rows]

@app.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    contents = await file.read()


    minio_client.put_object(
        BUCKET_NAME,
        file.filename,
        data=BytesIO(contents),  # <--- THIS is the fix
        length=len(contents),
        content_type=file.content_type,
    )

    return {"filename": file.filename, "status": "uploaded"}

@app.get("/list-files")
def list_files():
    objects = minio_client.list_objects(BUCKET_NAME)
    return [obj.object_name for obj in objects]
