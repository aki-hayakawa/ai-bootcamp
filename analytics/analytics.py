#!/usr/bin/env python3
import time, json, os
import pika
import redis

print("🚀 analytics.py starting…", flush=True)

# RabbitMQ connection
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

print("🔧 Connecting to RabbitMQ and Redis…", flush=True)

# Initialize connections
connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
channel = connection.channel()

# Declare the queues
channel.queue_declare(queue='llm-events', durable=True)
channel.queue_declare(queue='file-events', durable=True)
channel.queue_declare(queue='file-ai-events', durable=True)

# Redis connection for analytics
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

print("🔍 Ready to consume from 'llm-events', 'file-events', and 'file-ai-events' queues…", flush=True)

def process_llm_message(ch, method, properties, body):
    try:
        event = json.loads(body.decode())
        prompt = event.get("prompt", "<no-prompt>")
        source = event.get("source", "unknown")
        ts0 = float(event.get("timestamp", time.time()))
        duration = float(event.get("duration", 0))
        latency = time.time() - ts0
        
        # Update analytics in Redis
        redis_client.incr("analytics:llm:total")
        redis_client.hincrby("analytics:llm:by_source", source, 1)
        redis_client.hincrbyfloat("analytics:llm:latency_sum", "sum", duration)
        redis_client.hincrby("analytics:llm:latency_sum", "count", 1)
        
        # Track top prompts
        redis_client.zincrby("analytics:llm:top_prompts", 1, prompt[:100])  # truncate for storage
        
        print(f"✅ Processed LLM prompt='{prompt[:50]}...' source={source} latency={latency:.2f}s", flush=True)
        
        # Acknowledge the message
        ch.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        print(f"❌ Error processing LLM message: {e}", flush=True)
        # Reject and requeue the message
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

def process_file_message(ch, method, properties, body):
    try:
        event = json.loads(body.decode())
        event_type = event.get("event_type")
        
        if event_type == "file_upload":
            filename = event.get("filename", "unknown")
            file_size = int(event.get("file_size", 0))
            content_type = event.get("content_type", "unknown")
            
            # Update file analytics in Redis
            redis_client.incr("analytics:files:total")
            redis_client.incrby("analytics:files:total_size", file_size)
            
            # Track by content type
            if content_type:
                type_key = content_type.split('/')[0] if '/' in content_type else content_type
                redis_client.hincrby("analytics:files:by_type", type_key, 1)
            
            print(f"✅ Processed file upload: {filename} ({file_size} bytes, {content_type})", flush=True)
            
        elif event_type == "file_deletion":
            filename = event.get("filename", "unknown")
            file_size = int(event.get("file_size", 0))
            content_type = event.get("content_type", "unknown")
            timestamp = event.get("timestamp", time.time())
            
            print(f"🗑️  Processing file deletion event: {filename}", flush=True)
            
            # Track deleted files in Redis
            deleted_file_data = {
                "filename": filename,
                "content_type": content_type,
                "file_size": file_size,
                "deleted_at": timestamp
            }
            
            # Add to deleted files list (using a sorted set with timestamp as score)
            deleted_key = "analytics:files:deleted"
            deleted_data_json = json.dumps(deleted_file_data)
            result = redis_client.zadd(deleted_key, {deleted_data_json: timestamp})
            print(f"🗑️  Added to Redis deleted files: key={deleted_key}, result={result}", flush=True)
            
            # Verify it was added
            count = redis_client.zcard(deleted_key)
            print(f"🗑️  Total deleted files in Redis: {count}", flush=True)
            
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
            
            print(f"✅ Processed file deletion: {filename} ({file_size} bytes, {content_type})", flush=True)
        
        # Acknowledge the message
        ch.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        print(f"❌ Error processing file message: {e}", flush=True)
        # Reject and requeue the message
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

def process_file_ai_message(ch, method, properties, body):
    try:
        event = json.loads(body.decode())
        event_type = event.get("event_type")
        
        if event_type == "file_ai_request":
            filename = event.get("filename", "unknown")
            model = event.get("model", "unknown")
            content_type = event.get("content_type", "unknown")
            source = event.get("source", "unknown")
            
            # Update file AI analytics in Redis
            redis_client.incr("analytics:file_ai:total")
            
            # Track by content type
            if content_type:
                type_key = content_type.split('/')[0] if '/' in content_type else content_type
                redis_client.hincrby("analytics:file_ai:by_type", type_key, 1)
            
            # Track by AI model
            redis_client.hincrby("analytics:file_ai:by_model", model, 1)
            
            # Track top files analyzed
            redis_client.zincrby("analytics:file_ai:top_files", 1, filename)
            
            print(f"✅ Processed file AI request: {filename} with {model} ({content_type})", flush=True)
        
        # Acknowledge the message
        ch.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        print(f"❌ Error processing file AI message: {e}", flush=True)
        # Reject and requeue the message
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

# Set up consumers
channel.basic_qos(prefetch_count=1)
channel.basic_consume(queue='llm-events', on_message_callback=process_llm_message)
channel.basic_consume(queue='file-events', on_message_callback=process_file_message)
channel.basic_consume(queue='file-ai-events', on_message_callback=process_file_ai_message)

print("🔍 Starting to consume messages from all queues…", flush=True)

try:
    channel.start_consuming()
except KeyboardInterrupt:
    print("\n⏹️  Stopping consumer…", flush=True)
    channel.stop_consuming()
finally:
    connection.close()
    print("👋 Consumer closed", flush=True)
