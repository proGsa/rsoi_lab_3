import json
import os

import pika


RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/",)
QUEUE_NAME = "library_operations"

def publish_operation(operation: dict):
    parameters = pika.URLParameters(RABBITMQ_URL)

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    channel.queue_declare(queue=QUEUE_NAME, durable=True,)

    channel.basic_publish(
        exchange="",
        routing_key=QUEUE_NAME,
        body=json.dumps(operation),
        properties=pika.BasicProperties(delivery_mode=2),
    )

    connection.close()