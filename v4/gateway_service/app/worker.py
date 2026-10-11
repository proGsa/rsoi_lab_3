import json
import os
import time

import pika
import httpx
from app.rabbitmq import publish_operation
from app.circuit_breaker import retry_request_until_success

RABBITMQ_URL = os.getenv(
    "RABBITMQ_URL",
    "amqp://guest:guest@rabbitmq:5672/",
)

QUEUE_NAME = "library_operations"

LIBRARY_SERVICE_URL = os.getenv(
    "LIBRARY_SERVICE_URL",
    "http://library-service:8060",
)

RESERVATION_SERVICE_URL = os.getenv(
    "RESERVATION_SERVICE_URL",
    "http://reservation-service:8070",
)

RATING_SERVICE_URL = os.getenv(
    "RATING_SERVICE_URL",
    "http://rating-service:8050",
)


def process_operation(operation: dict): 
    operation_type = operation["operation"] 
    if operation_type == "RETURN_LIBRARY": 
        process_return_library(operation) 

    elif operation_type == "RETURN_RESERVATION":
        process_return(operation)
    elif operation_type == "UPDATE_RATING": 
        process_update_rating(operation) 
    elif operation_type == "CREATE_RESERVATION": 
        process_create(operation) 
    else: 
        raise ValueError(f"Unknown operation: {operation_type}")

def process_return_library(operation: dict): 
    print(f"Processing RETURN_LIBRARY: {operation}") 
    library_uid = operation["libraryUid"] 
    book_uid = operation["bookUid"] 
    user = operation["user"] 
    new_stars = operation["newStars"] 
    # Повторяем возврат книги при ошибке. 
    response = httpx.post( f"{LIBRARY_SERVICE_URL}/api/v1/libraries/" f"{library_uid}/books/{book_uid}/return", timeout=5.0, ) 
    if response.status_code not in (200, 204): 
        raise RuntimeError( f"Library Service returned {response.status_code}" ) 
    print("Library return completed") 
    # Книга уже возвращена. Повторно её не возвращаем, даже если обновление рейтинга не удалось. 
    try: 
        response = httpx.patch( f"{RATING_SERVICE_URL}/api/v1/rating", headers={"X-User-Name": user}, json={"stars": new_stars}, timeout=5.0, )
        if response.status_code not in (200, 204): 
            raise RuntimeError( f"Rating Service returned {response.status_code}" ) 
        print("Rating update completed") 
    except (httpx.RequestError, RuntimeError) as error: 
        print(f"Rating update failed: {error}") 
        # Ставим в очередь только изменение рейтинга. 
        publish_operation({ "operation": "UPDATE_RATING", "user": user, "newStars": new_stars, })
       
def process_return(operation: dict):
    print(f"Processing RETURN_RESERVATION: {operation}")

    reservation_uid = operation["reservationUid"]
    user = operation["user"]
    condition = operation["condition"]
    return_date = operation["date"]
    headers = {"X-User-Name": user}

    response = httpx.get(
        f"{RESERVATION_SERVICE_URL}/api/v1/reservations/{reservation_uid}",
        headers=headers,
        timeout=5.0,
    )

    if response.status_code != 200:
        raise RuntimeError(f"Reservation Service returned {response.status_code}")

    reservation = response.json()
    book_uid = reservation["bookUid"]
    library_uid = reservation["libraryUid"]

    retry_request_until_success(
        method="POST",
        url=(
            f"{LIBRARY_SERVICE_URL}/api/v1/libraries/"
            f"{library_uid}/books/{book_uid}/return"
        ),
        retry_time=10.0,
        timeout=2.0,
    )
    print("Library return completed")

    response = httpx.post(
        f"{RESERVATION_SERVICE_URL}/api/v1/reservations/"
        f"{reservation_uid}/return",
        headers=headers,
        json={"condition": condition, "date": return_date},
        timeout=5.0,
    )

    if response.status_code not in (200, 204):
        raise RuntimeError(f"Reservation Service returned {response.status_code}")

    print("RETURN_RESERVATION completed")


def process_update_rating(operation: dict): 
    print(f"Processing UPDATE_RATING: {operation}") 
    response = httpx.patch( f"{RATING_SERVICE_URL}/api/v1/rating", headers={"X-User-Name": operation["user"]}, json={"stars": operation["newStars"]}, timeout=5.0, ) 
    if response.status_code not in (200, 204): 
        raise RuntimeError( f"Rating Service returned {response.status_code}" ) 
    print("UPDATE_RATING completed")

def process_create(operation: dict):
    print(f"Processing CREATE_RESERVATION: {operation}")
    user = operation["user"]
    headers = {"X-User-Name": user}

    response = httpx.post(
        f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
        headers=headers,
        json={
            "bookUid": operation["bookUid"],
            "libraryUid": operation["libraryUid"],
            "tillDate": operation["tillDate"],
        },
        timeout=5.0,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Reservation Service returned {response.status_code}"
        )

    print("CREATE_RESERVATION completed")


def connect():
    while True:
        try:
            parameters = pika.URLParameters(RABBITMQ_URL)
            connection = pika.BlockingConnection(parameters)

            print("Connected to RabbitMQ")
            return connection

        except Exception as e:
            print(f"RabbitMQ unavailable: {e}")
            time.sleep(5)


def main():
    connection = connect()
    channel = connection.channel()

    channel.queue_declare(queue=QUEUE_NAME, durable=True,)

    channel.basic_qos(prefetch_count=1)

    def callback(ch, method, properties, body):
        operation = json.loads(body)

        try:
            process_operation(operation)
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            print(f"Operation failed: {e}")

            # Сообщение остаётся в очереди
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True,)   
            time.sleep(5)

    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=callback,)

    print("Waiting for messages...")

    channel.start_consuming()


if __name__ == "__main__":
    main()