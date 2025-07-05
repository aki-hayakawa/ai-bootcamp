import os
import json
import datetime
from io import BytesIO


from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request
from fastapi.security import OAuth2PasswordBearer
import httpx
import redis
from jose import JWTError, jwt
import pika
import json
from minio import Minio
from sqlalchemy import insert, create_engine
from databases import Database
from dotenv import load_dotenv

from sqlalchemy.exc import OperationalError
import time

# Gemini client
from google import genai
from google.genai import types

# Your database metadata
from database import llm_logs, user_logs, metadata, DATABASE_URL, database

# ─── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv()

# ─── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI()

# ─── Environment configuration ─────────────────────────────────────────────────
KEYCLOAK_URL             = os.getenv("KEYCLOAK_URL")
CLIENT_ID                = os.getenv("KEYCLOAK_CLIENT_ID")
CLIENT_SECRET            = os.getenv("KEYCLOAK_CLIENT_SECRET")
OAUTH2_TOKEN_URL         = f"{KEYCLOAK_URL}/protocol/openid-connect/token"
OAUTH2_USERINFO_URL      = f"{KEYCLOAK_URL}/protocol/openid-connect/userinfo"

REDIS_URL                = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
GEMINI_API_KEY           = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL             = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

# Available Gemini models
AVAILABLE_MODELS = [
    "gemini-2.5-pro",
    "gemini-1.5-pro", 
    "gemini-1.5-flash",
    "gemini-1.0-pro"
]

MINIO_URL                = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS             = os.getenv("MINIO_ROOT_USER", "minio")
MINIO_SECRET             = os.getenv("MINIO_ROOT_PASSWORD", "minio123")
MINIO_SECURE             = os.getenv("MINIO_SECURE", "False").lower() in ("true","1","yes")
MINIO_BUCKET             = os.getenv("MINIO_BUCKET", "uploads")

# ─── Clients initialization ───────────────────────────────────────────────────
redis_client = redis.from_url(REDIS_URL, decode_responses=True)


# RabbitMQ connection will be established in startup
rabbitmq_connection = None
rabbitmq_channel = None

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is required")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=OAUTH2_TOKEN_URL)

minio_client: Minio
jwks = None  # will hold Keycloak’s signing keys



# after your other imports & load_dotenv…
redis_client = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)

@app.get("/analytics/summary")
async def analytics_summary():
    total   = int(redis_client.get("analytics:llm:total") or 0)
    by_src  = redis_client.hgetall("analytics:llm:by_source")
    latency = redis_client.hgetall("analytics:llm:latency_sum")
    sum_    = float(latency.get("sum",0))
    cnt     = int(latency.get("count",0))
    avg_lat = sum_/cnt if cnt>0 else None

    return {"total_calls": total, "by_source": by_src, "avg_latency": avg_lat}

@app.get("/analytics/top-prompts")
async def analytics_top_prompts(limit: int = 10):
    # Returns top N prompts by usage count
    raw = redis_client.zrevrange("analytics:llm:top_prompts", 0, limit-1, withscores=True)
    return [{"prompt": p, "count": int(s)} for p,s in raw]

@app.get("/analytics/files")
async def analytics_files():
    total_files = int(redis_client.get("analytics:files:total") or 0)
    total_size = int(redis_client.get("analytics:files:total_size") or 0)
    by_type = redis_client.hgetall("analytics:files:by_type")
    
    return {
        "total_files": total_files,
        "total_size": total_size,
        "by_type": by_type
    }

@app.get("/analytics/file-ai")
async def analytics_file_ai():
    total_requests = int(redis_client.get("analytics:file_ai:total") or 0)
    by_type = redis_client.hgetall("analytics:file_ai:by_type")
    by_model = redis_client.hgetall("analytics:file_ai:by_model")
    top_files = redis_client.zrevrange("analytics:file_ai:top_files", 0, 9, withscores=True)
    
    return {
        "total_requests": total_requests,
        "by_type": by_type,
        "by_model": by_model,
        "top_files": [{"filename": f, "count": int(s)} for f, s in top_files]
    }

@app.get("/analytics/deleted-files")
async def get_deleted_files_analytics():
    try:
        print(f"[DEBUG] Fetching deleted files from Redis...", flush=True)
        
        # Get deleted files from Redis (most recent 50)
        deleted_files_data = redis_client.zrevrange("analytics:files:deleted", 0, 49, withscores=True)
        print(f"[DEBUG] Found {len(deleted_files_data)} deleted file entries in Redis", flush=True)
        
        deleted_files = []
        for file_data_str, timestamp in deleted_files_data:
            try:
                # With decode_responses=True, Redis returns strings not bytes
                # file_data_str should already be a string
                print(f"[DEBUG] Processing deleted file data: type={type(file_data_str)}, value={file_data_str[:100] if len(str(file_data_str)) > 100 else file_data_str}", flush=True)
                
                file_data = json.loads(file_data_str)
                deleted_files.append({
                    "filename": file_data.get("filename", "unknown"),
                    "content_type": file_data.get("content_type", "unknown"),
                    "file_size": file_data.get("file_size", 0),
                    "deleted_at": file_data.get("deleted_at", timestamp),
                    "status": "Deleted"
                })
                print(f"[DEBUG] Added deleted file: {file_data.get('filename')}", flush=True)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
                print(f"[DEBUG] Failed to parse deleted file data: {e} | Data: {file_data_str}", flush=True)
                continue
        
        print(f"[DEBUG] Returning {len(deleted_files)} deleted files", flush=True)
        return {
            "deleted_files": deleted_files,
            "total_deleted": len(deleted_files)
        }
        
    except Exception as e:
        print(f"[ERROR] Error getting deleted files analytics: {e}", flush=True)
        return {"deleted_files": [], "total_deleted": 0}

@app.post("/test/add-deleted-file")
async def test_add_deleted_file():
    """Test endpoint to manually add a deleted file for testing"""
    try:
        test_data = {
            "filename": "test-deleted-file.txt",
            "content_type": "text/plain",
            "file_size": 1024,
            "deleted_at": time.time()
        }
        
        # Add directly to Redis
        redis_client.zadd("analytics:files:deleted", {json.dumps(test_data): test_data["deleted_at"]})
        
        print(f"[TEST] Added test deleted file: {test_data}", flush=True)
        return {"message": "Test deleted file added", "data": test_data}
        
    except Exception as e:
        print(f"[ERROR] Failed to add test deleted file: {e}", flush=True)
        return {"error": str(e)}

# ─── Startup / Shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global jwks, minio_client

    # 1) Connect the databases.Client
    await database.connect()
    engine = create_engine(DATABASE_URL)

    # 2) Wait for Postgres to accept connections
    for i in range(10):
        try:
            conn = engine.connect()
            conn.close()
            break
        except OperationalError:
            print(f"⏳ Waiting for Postgres ({i+1}/10)…", flush=True)
            time.sleep(1)

    # 3) Create tables
    try:
        metadata.create_all(engine)
        print("✅ Tables created (or already existed)", flush=True)
    except Exception as e:
        print("❌ metadata.create_all() failed:", e, flush=True)

    # 4) Initialize RabbitMQ connection
    global rabbitmq_connection, rabbitmq_channel
    try:
        rabbitmq_connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        rabbitmq_channel = rabbitmq_connection.channel()
        
        # Declare queues
        rabbitmq_channel.queue_declare(queue='llm-events', durable=True)
        rabbitmq_channel.queue_declare(queue='ai-topic', durable=True)
        rabbitmq_channel.queue_declare(queue='file-events', durable=True)
        rabbitmq_channel.queue_declare(queue='file-ai-events', durable=True)
        
        print("✅ RabbitMQ connection established", flush=True)
    except Exception as e:
        print(f"❌ RabbitMQ connection failed: {e}", flush=True)

    # 5) Fetch Keycloak JWKS …
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{KEYCLOAK_URL}/protocol/openid-connect/certs")
        resp.raise_for_status()
        jwks = resp.json()

    # 6) Initialize MinIO …
    minio_client = Minio(
        MINIO_URL, access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET, secure=MINIO_SECURE
    )
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()
    
    # Close RabbitMQ connection
    global rabbitmq_connection
    if rabbitmq_connection and not rabbitmq_connection.is_closed:
        rabbitmq_connection.close()

# ─── Token verification ────────────────────────────────────────────────────────
async def verify_token(token: str = Depends(oauth2_scheme)):
    try:
        # 1) Find the key by `kid`
        unhdr = jwt.get_unverified_header(token)
        key = next(k for k in jwks["keys"] if k["kid"] == unhdr["kid"])
        pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))

        # 2) Decode & verify
        payload = jwt.decode(
            token,
            pub,
            audience=CLIENT_ID,
            issuer=f"{KEYCLOAK_URL}/",
            algorithms=[unhdr["alg"]],
        )
        return payload
    except (StopIteration, JWTError) as e:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# --- Routes ---

@app.get("/models")
async def get_available_models():
    return {"models": AVAILABLE_MODELS, "default": GEMINI_MODEL}

@app.post("/ask-llm")
async def ask_llm(request: Request):
    data = await request.json()
    prompt = data.get("prompt")
    model = data.get("model", GEMINI_MODEL)
    
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    
    # Validate model
    if model not in AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail=f"Invalid model. Available models: {AVAILABLE_MODELS}")

    # Create cache key that includes model
    cache_key = f"{model}:{prompt}"
    
    # 1) Check cache
    cached = redis_client.get(cache_key)
    if cached:
        source, reply, duration = "cache", cached, 0.0
    else:
        # 2) Stream from Gemini and measure duration
        source, reply = "gemini", ""
        start = time.time()
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)]
            )
        ]
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
            response_mime_type="text/plain",
        )
        try:
            for chunk in gemini_client.models.generate_content_stream(
                model=model, contents=contents, config=config
            ):
                reply += chunk.text
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Gemini Error: {e}")
        duration = time.time() - start

        # cache the full reply with model-specific key
        redis_client.set(cache_key, reply)

    # 3) Log to PostgreSQL
    await database.execute(
        insert(llm_logs).values(
            prompt=prompt,
            response=reply,
            source=source,
            created_at=datetime.datetime.utcnow(),
        )
    )

    # 4) Emit RabbitMQ event (with duration and model)
    # assign an epoch‐seconds timestamp
    ts = time.time()
    event = {
        "prompt":    prompt,
        "response":  reply,
        "source":    source,
        "model":     model,
        "duration":  duration,  # seconds
        "timestamp": ts,
    }
    try:
        rabbitmq_channel.basic_publish(
            exchange='',
            routing_key='llm-events',
            body=json.dumps(event),
            properties=pika.BasicProperties(delivery_mode=2)  # Make message persistent
        )
        print(f"[RabbitMQ] Published to llm-events: {event}", flush=True)
    except Exception as e:
        print(f"[RabbitMQ Error] Failed to send event: {e}", flush=True)

    return {"source": source, "response": reply, "model": model}

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
        rabbitmq_channel.basic_publish(
            exchange='',
            routing_key='ai-topic',
            body=message,
            properties=pika.BasicProperties(delivery_mode=2)
        )
        return {"status": "message sent"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/consume-messages")
def consume_messages():
    messages = []
    try:
        # Get up to 5 messages from the queue
        for _ in range(5):
            method_frame, header_frame, body = rabbitmq_channel.basic_get(queue='ai-topic', auto_ack=True)
            if method_frame:
                messages.append(body.decode('utf-8'))
            else:
                break  # No more messages
    except Exception as e:
        return {"error": str(e)}
    return {"messages": messages}

@app.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    contents = await file.read()
    file_size = len(contents)
    
    # Upload to MinIO
    minio_client.put_object(
        MINIO_BUCKET,
        file.filename,
        data=BytesIO(contents),
        length=file_size,
        content_type=file.content_type,
    )
    
    # Emit file upload event to RabbitMQ
    ts = time.time()
    event = {
        "event_type": "file_upload",
        "filename": file.filename,
        "file_size": file_size,
        "content_type": file.content_type,
        "timestamp": ts,
    }
    try:
        rabbitmq_channel.basic_publish(
            exchange='',
            routing_key='file-events',
            body=json.dumps(event),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        print(f"[RabbitMQ] Published file event: {event}", flush=True)
    except Exception as e:
        print(f"[RabbitMQ Error] Failed to send file event: {e}", flush=True)
    
    return {"filename": file.filename, "status": "uploaded", "size": file_size}

@app.get("/list-files")
def list_files():
    files = []
    for obj in minio_client.list_objects(MINIO_BUCKET):
        # Get file info
        try:
            stat = minio_client.stat_object(MINIO_BUCKET, obj.object_name)
            files.append({
                "name": obj.object_name,
                "size": stat.size,
                "content_type": stat.content_type,
                "last_modified": stat.last_modified.isoformat() if stat.last_modified else None
            })
        except Exception as e:
            # Fallback to basic info if stat fails
            files.append({
                "name": obj.object_name,
                "size": obj.size,
                "content_type": None,
                "last_modified": obj.last_modified.isoformat() if obj.last_modified else None
            })
    return files

@app.get("/download-file/{filename}")
def download_file(filename: str):
    try:
        # Get the file from MinIO
        response = minio_client.get_object(MINIO_BUCKET, filename)
        
        # Get file info for content type
        try:
            stat = minio_client.stat_object(MINIO_BUCKET, filename)
            content_type = stat.content_type or "application/octet-stream"
        except:
            content_type = "application/octet-stream"
        
        # Read the file content
        file_content = response.read()
        response.close()
        
        from fastapi.responses import Response
        return Response(
            content=file_content,
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Length": str(len(file_content))
            }
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {str(e)}")

@app.get("/view-file/{filename}")
def view_file(filename: str):
    try:
        # Get the file from MinIO
        response = minio_client.get_object(MINIO_BUCKET, filename)
        
        # Get file info for content type
        try:
            stat = minio_client.stat_object(MINIO_BUCKET, filename)
            content_type = stat.content_type or "application/octet-stream"
        except:
            content_type = "application/octet-stream"
        
        # Read the file content
        file_content = response.read()
        response.close()
        
        from fastapi.responses import Response
        return Response(
            content=file_content,
            media_type=content_type,
            headers={
                "Content-Length": str(len(file_content))
            }
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {str(e)}")

@app.delete("/delete-file/{filename}")
def delete_file(filename: str):
    try:
        # Check if file exists first and get metadata
        try:
            stat = minio_client.stat_object(MINIO_BUCKET, filename)
            content_type = stat.content_type or "application/octet-stream"
            file_size = stat.size
        except Exception:
            raise HTTPException(status_code=404, detail="File not found")
        
        # Delete the file from MinIO
        minio_client.remove_object(MINIO_BUCKET, filename)
        
        # Process deletion event directly and also emit to RabbitMQ
        deletion_event = {
            "event_type": "file_deletion",
            "filename": filename,
            "content_type": content_type,
            "file_size": file_size,
            "timestamp": time.time(),
        }
        
        # Process deletion analytics directly (backup method)
        try:
            # Track deleted files in Redis directly
            deleted_file_data = {
                "filename": filename,
                "content_type": content_type,
                "file_size": file_size,
                "deleted_at": deletion_event["timestamp"]
            }
            
            # Add to deleted files list (using a sorted set with timestamp as score)
            deleted_key = "analytics:files:deleted"
            deleted_data_json = json.dumps(deleted_file_data)
            result = redis_client.zadd(deleted_key, {deleted_data_json: deletion_event["timestamp"]})
            print(f"[DIRECT] Added to Redis deleted files: key={deleted_key}, result={result}", flush=True)
            
            # Update totals (decrease counts)
            current_total = int(redis_client.get("analytics:files:total") or 0)
            if current_total > 0:
                redis_client.decr("analytics:files:total")
            
            current_size = int(redis_client.get("analytics:files:total_size") or 0)
            if current_size >= file_size:
                redis_client.decrby("analytics:files:total_size", file_size)
            
            # Update by content type
            if content_type:
                type_key = content_type.split('/')[0] if '/' in content_type else content_type
                current_type_count = int(redis_client.hget("analytics:files:by_type", type_key) or 0)
                if current_type_count > 0:
                    redis_client.hincrby("analytics:files:by_type", type_key, -1)
            
            print(f"[DIRECT] Processed file deletion analytics: {filename}", flush=True)
            
        except Exception as e:
            print(f"[ERROR] Failed to process deletion analytics directly: {e}", flush=True)
        
        # Also emit to RabbitMQ for the analytics service (if it's running)
        print(f"[DEBUG] Attempting to emit deletion event for file: {filename}", flush=True)
        try:
            rabbitmq_channel.basic_publish(
                exchange='',
                routing_key='file-events',
                body=json.dumps(deletion_event),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            print(f"[RabbitMQ] Published file deletion event: {deletion_event}", flush=True)
        except Exception as e:
            print(f"[RabbitMQ Error] Failed to send file deletion event: {e}", flush=True)
            # Continue anyway since we processed it directly
        
        print(f"[INFO] File deleted successfully: {filename}", flush=True)
        return {"message": f"File '{filename}' deleted successfully", "filename": filename}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Failed to delete file {filename}: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")

@app.post("/ask-ai-about-file")
async def ask_ai_about_file(request: Request):
    try:
        data = await request.json()
        filename = data.get("filename")
        prompt = data.get("prompt")
        model = data.get("model", GEMINI_MODEL)
        
        print(f"[DEBUG] Processing file AI request: filename={filename}, prompt='{prompt[:50]}...', model={model}", flush=True)
        
        if not filename:
            raise HTTPException(status_code=400, detail="Filename is required")
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt is required")
        
        # Validate model
        if model not in AVAILABLE_MODELS:
            raise HTTPException(status_code=400, detail=f"Invalid model. Available models: {AVAILABLE_MODELS}")
    except Exception as e:
        print(f"[ERROR] Failed to parse request: {str(e)}", flush=True)
        raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")

    try:
        print(f"[DEBUG] Attempting to get file from MinIO: bucket={MINIO_BUCKET}, filename={filename}", flush=True)
        
        # Get the file from MinIO
        file_response = minio_client.get_object(MINIO_BUCKET, filename)
        file_content = file_response.read()
        file_response.close()
        print(f"[DEBUG] Successfully retrieved file, size: {len(file_content)} bytes", flush=True)
        
        # Get file metadata
        stat = minio_client.stat_object(MINIO_BUCKET, filename)
        content_type = stat.content_type or "application/octet-stream"
        print(f"[DEBUG] File metadata: content_type={content_type}, size={stat.size}", flush=True)
        
        # Create enhanced prompt with file context
        file_context = f"I have a file named '{filename}' with content type '{content_type}'."
        
        # Handle different file types
        if content_type.startswith('text/') or filename.lower().endswith(('.txt', '.md', '.csv', '.json', '.xml', '.html', '.css', '.js', '.py')):
            # For text files, include the content
            try:
                text_content = file_content.decode('utf-8')
                enhanced_prompt = f"{file_context}\n\nFile content:\n```\n{text_content}\n```\n\nUser question: {prompt}"
            except UnicodeDecodeError:
                enhanced_prompt = f"{file_context}\n\nNote: File contains binary data that cannot be displayed as text.\n\nUser question: {prompt}"
        elif content_type.startswith('image/'):
            # For images, we'll handle them with Gemini's vision capabilities
            print(f"[DEBUG] Processing image file: {filename}, content_type: {content_type}, size: {len(file_content)} bytes", flush=True)
            
            try:
                import base64
                # Keep the image data as bytes for Gemini API
                image_bytes = file_content
                print(f"[DEBUG] Image prepared successfully, size: {len(image_bytes)} bytes", flush=True)
            except Exception as e:
                print(f"[ERROR] Failed to prepare image: {str(e)}", flush=True)
                raise HTTPException(status_code=500, detail=f"Failed to prepare image: {str(e)}")
            
            # Use Gemini's multimodal capabilities
            try:
                # Create the inline data part correctly using bytes
                inline_data = types.Part(
                    inline_data=types.Blob(
                        mime_type=content_type,
                        data=image_bytes
                    )
                )
                text_part = types.Part(text=f"I have an image file named '{filename}'. {prompt}")
                
                contents = [
                    types.Content(
                        role="user",
                        parts=[text_part, inline_data]
                    )
                ]
                print(f"[DEBUG] Created multimodal content successfully", flush=True)
            except Exception as e:
                print(f"[ERROR] Failed to create multimodal content: {str(e)}", flush=True)
                # Try simpler approach - just text for now
                try:
                    print(f"[DEBUG] Falling back to text-only approach for image", flush=True)
                    enhanced_prompt = f"I have an image file named '{filename}' with content type '{content_type}'. The user is asking: {prompt}. Note: I cannot see the actual image content, but I can provide general information about this type of file."
                    contents = [
                        types.Content(
                            role="user",
                            parts=[types.Part(text=enhanced_prompt)]
                        )
                    ]
                    print(f"[DEBUG] Fallback content creation successful", flush=True)
                except Exception as e2:
                    print(f"[ERROR] Fallback approach also failed: {str(e2)}", flush=True)
                    raise HTTPException(status_code=500, detail=f"Failed to create content for Gemini: {str(e)} | Fallback: {str(e2)}")
            
            # Process with Gemini
            cache_key = f"{model}:file:{filename}:{prompt}"
            cached = redis_client.get(cache_key)
            
            if cached:
                return {"source": "cache", "response": cached, "model": model, "filename": filename}
            
            # Generate response
            source, reply = "gemini", ""
            start = time.time()
            config = types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=-1),
                response_mime_type="text/plain",
            )
            
            try:
                print(f"[DEBUG] Calling Gemini API with model: {model}", flush=True)
                for chunk in gemini_client.models.generate_content_stream(
                    model=model, contents=contents, config=config
                ):
                    if hasattr(chunk, 'text') and chunk.text:
                        reply += chunk.text
                print(f"[DEBUG] Gemini response received, length: {len(reply)}", flush=True)
            except Exception as e:
                print(f"[ERROR] Gemini API error: {str(e)}", flush=True)
                # Check if it's an API key issue
                if "API_KEY" in str(e) or "authentication" in str(e).lower():
                    raise HTTPException(status_code=500, detail="Gemini API authentication failed. Please check API key configuration.")
                # Check if it's a quota/billing issue
                elif "quota" in str(e).lower() or "billing" in str(e).lower():
                    raise HTTPException(status_code=500, detail="Gemini API quota exceeded or billing issue. Please check your Google Cloud account.")
                # Check if it's an unsupported model
                elif "model" in str(e).lower() and "not found" in str(e).lower():
                    raise HTTPException(status_code=500, detail=f"Model '{model}' not available. Please try a different model.")
                else:
                    raise HTTPException(status_code=500, detail=f"Gemini Error: {str(e)}")
            
            duration = time.time() - start
            redis_client.set(cache_key, reply)
            
            # Emit file AI event for images
            ts = time.time()
            file_ai_event = {
                "event_type": "file_ai_request",
                "filename": filename,
                "prompt": prompt,
                "response": reply,
                "source": source,
                "model": model,
                "content_type": content_type,
                "duration": duration,
                "timestamp": ts,
            }
            try:
                rabbitmq_channel.basic_publish(
                    exchange='',
                    routing_key='file-ai-events',
                    body=json.dumps(file_ai_event),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                print(f"[RabbitMQ] Published file AI event: {file_ai_event}", flush=True)
            except Exception as e:
                print(f"[RabbitMQ Error] Failed to send file AI event: {e}", flush=True)
            
            return {"source": source, "response": reply, "model": model, "filename": filename}
            
        else:
            # For other file types, provide file info only
            file_size = len(file_content)
            enhanced_prompt = f"{file_context}\n\nFile size: {file_size} bytes\n\nNote: This file type cannot be directly analyzed. I can only provide information about the file metadata.\n\nUser question: {prompt}"
        
        # Create cache key that includes filename and model
        cache_key = f"{model}:file:{filename}:{prompt}"
        
        # Check cache
        cached = redis_client.get(cache_key)
        if cached:
            return {"source": "cache", "response": cached, "model": model, "filename": filename}
        
        # Process with Gemini for non-image files
        source, reply = "gemini", ""
        start = time.time()
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=enhanced_prompt)]
            )
        ]
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
            response_mime_type="text/plain",
        )
        
        try:
            for chunk in gemini_client.models.generate_content_stream(
                model=model, contents=contents, config=config
            ):
                reply += chunk.text
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Gemini Error: {e}")
        
        duration = time.time() - start
        redis_client.set(cache_key, reply)
        
        # Log to database (optional)
        await database.execute(
            insert(llm_logs).values(
                prompt=f"File: {filename} - {prompt}",
                response=reply,
                source=source,
                created_at=datetime.datetime.utcnow(),
            )
        )
        
        # Emit file AI event to RabbitMQ for analytics
        ts = time.time()
        file_ai_event = {
            "event_type": "file_ai_request",
            "filename": filename,
            "prompt": prompt,
            "response": reply,
            "source": source,
            "model": model,
            "content_type": content_type,
            "duration": duration,
            "timestamp": ts,
        }
        try:
            rabbitmq_channel.basic_publish(
                exchange='',
                routing_key='file-ai-events',
                body=json.dumps(file_ai_event),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            print(f"[RabbitMQ] Published file AI event: {file_ai_event}", flush=True)
        except Exception as e:
            print(f"[RabbitMQ Error] Failed to send file AI event: {e}", flush=True)
        
        return {"source": source, "response": reply, "model": model, "filename": filename}
        
    except Exception as e:
        if "HTTPException" in str(type(e)):
            raise e
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

@app.get("/llm-history")
async def get_llm_history():
    rows = await database.fetch_all(
        llm_logs.select().order_by(llm_logs.c.created_at.desc()).limit(50)
    )
    return [dict(row) for row in rows]
