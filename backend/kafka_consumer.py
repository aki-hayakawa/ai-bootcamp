from confluent_kafka import Consumer, KafkaException
import json
import time

consumer_config = {
    'bootstrap.servers': 'kafka:9092',
    'group.id': 'llm-event-consumer',
    'auto.offset.reset': 'earliest'
}

consumer = Consumer(consumer_config)
consumer.subscribe(["llm-events"])

print("[Kafka Consumer] Listening to 'llm-events'...")

try:
    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            raise KafkaException(msg.error())

        event = json.loads(msg.value().decode('utf-8'))
        print(f"[Kafka Event] {event['timestamp']} | {event['prompt'][:50]}... → {event['source']}")
except KeyboardInterrupt:
    pass
finally:
    print("Closing Kafka consumer...")
    consumer.close()
