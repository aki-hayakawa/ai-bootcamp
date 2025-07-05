import pika
import json
import time
import os

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
channel = connection.channel()

# Declare the queue
channel.queue_declare(queue='llm-events', durable=True)

print("[RabbitMQ Consumer] Listening to 'llm-events'...")

def callback(ch, method, properties, body):
    try:
        event = json.loads(body.decode('utf-8'))
        print(f"[RabbitMQ Event] {event['timestamp']} | {event['prompt'][:50]}... → {event['source']}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"[RabbitMQ Error] Failed to process message: {e}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

channel.basic_qos(prefetch_count=1)
channel.basic_consume(queue='llm-events', on_message_callback=callback)

try:
    channel.start_consuming()
except KeyboardInterrupt:
    print("\nStopping RabbitMQ consumer...")
    channel.stop_consuming()
finally:
    connection.close()
    print("RabbitMQ consumer closed.")
