#!/usr/bin/env python3
import time, json
from confluent_kafka import Consumer, KafkaException, TopicPartition

print("🚀 analytics.py starting…", flush=True)

conf = {
    "bootstrap.servers": "kafka:9092",
    "group.id":          "analytics-group",
    "auto.offset.reset": "earliest",     # only applied on *no* committed offset
    "enable.auto.commit": False,
}
consumer = Consumer(conf)

def on_assign(consumer, partitions):
    # force all partitions to start at beginning
    tp = [TopicPartition(p.topic, p.partition, 0) for p in partitions]
    consumer.assign(tp)

print("🔧 Consumer configured, now subscribing…", flush=True)
topic = "llm-events"
consumer.subscribe([topic], on_assign=on_assign)
print(f"🔍 Subscribed to '{topic}', seeking to earliest…", flush=True)

try:
    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            if int(time.time()) % 15 == 0:
                print("⏳ still waiting…", flush=True)
            continue
        if msg.error():
            raise KafkaException(msg.error())
        event   = json.loads(msg.value().decode())
        prompt  = event.get("prompt","<no-prompt>")
        source  = event.get("source","unknown")
        ts0     = float(event.get("timestamp", time.time()))
        latency = time.time() - ts0
        print(f"✅ Processed prompt='{prompt}' source={source} latency={latency:.2f}s", flush=True)

except KeyboardInterrupt:
    pass
finally:
    consumer.close()
    print("👋 Consumer closed", flush=True)
