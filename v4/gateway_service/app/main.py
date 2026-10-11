import os
from uuid import UUID
import httpx
from app.rabbitmq import publish_operation
from fastapi import FastAPI, Response, Query, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import date

from app.circuit_breaker import CircuitBreaker

app = FastAPI(title="Gateway Service")

LIBRARY_SERVICE_URL = os.getenv("LIBRARY_SERVICE_URL", "http://library-service:8060")
RESERVATION_SERVICE_URL = os.getenv("RESERVATION_SERVICE_URL", "http://reservation-service:8070")
RATING_SERVICE_URL = os.getenv("RATING_SERVICE_URL", "http://rating-service:8050")

library_breaker = CircuitBreaker()
reservation_breaker = CircuitBreaker()
rating_breaker = CircuitBreaker()

class ReservationRequest(BaseModel):
    bookUid: UUID
    libraryUid: UUID
    tillDate: date

class ReturnRequest(BaseModel):
    condition: str
    date: date
    
    
def get_with_circuit_breaker(url: str, breaker: CircuitBreaker, fallback, critical: bool = True, params: dict | None = None, headers: dict | None = None, ): 
    if not breaker.can_execute(): 
        if critical:
            return JSONResponse(
                status_code=503,
                content={"message": "Bonus Service unavailable"},
            )
        return fallback

    try: 
        response = httpx.get( url, params=params, headers=headers, timeout=5.0, ) 
        if response.status_code >= 500:
            breaker.record_failure()
            if critical:
                return JSONResponse(
                    status_code=503,
                    content={"message": "Bonus Service unavailable"},
                )
            return fallback
        breaker.record_success()

        if response.status_code >= 400:
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )

        return response.json()

    except httpx.RequestError:
        breaker.record_failure()
        if critical:
            return JSONResponse(
                status_code=503,
                content={"message": "Bonus Service unavailable"},
            )
        return fallback

def to_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])

@app.get("/manage/health")
def health():
    return {"status": "UP"}

def proxy_request(method: str, url: str, params: dict | None = None, headers: dict | None = None, json: dict | None = None):
    try:
        response = httpx.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
            json=json,
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Upstream service is unavailable")

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers={
            "content-type": response.headers.get("content-type", "application/json")
        },
    )

def proxy_get(url: str, params: dict | None = None, headers: dict | None = None):
    try:
        response = httpx.get(
            url,
            params=params,
            headers=headers,
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Upstream service is unavailable",
        )

    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


# ---------------- LIBRARY SERVICE ----------------

def get_library_info(library_uid) -> dict:
    return get_with_circuit_breaker(
        url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{library_uid}",
        breaker=library_breaker,
        fallback={"libraryUid": str(library_uid)},
        critical=False,
    )


def get_book_info(library_uid, book_uid) -> dict:
    response = get_with_circuit_breaker(
        url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{library_uid}/books",
        params={
            "page": 1,
            "size": 100,
            "showAll": True,
        },
        breaker=library_breaker,
        fallback=None,
        critical=False,
    )
    if response is None:
        return {
            "bookUid": str(book_uid),
        }

    book = next(
        (item for item in response["items"] if item["bookUid"] == str(book_uid)),
        None,
    )

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    return book


def enrich_reservation(reservation: dict, book: dict, library: dict) -> dict:
    result = {
        "reservationUid": str(reservation["reservationUid"]),
        "status": reservation["status"],
        "startDate": to_date(reservation["startDate"]).isoformat(),
        "tillDate": to_date(reservation["tillDate"]).isoformat(),
        "book": {
            "bookUid": book["bookUid"],
        },
        "library": {
            "libraryUid": library["libraryUid"],
        },
    }

    if "name" in book:
        result["book"] = {
            "bookUid": book["bookUid"],
            "name": book["name"],
            "author": book["author"],
            "genre": book["genre"],
        }

    if "name" in library:
        result["library"] = {
            "libraryUid": library["libraryUid"],
            "name": library["name"],
            "address": library["address"],
            "city": library["city"],
        }

    return result


@app.get("/api/v1/libraries")
def get_libraries(city: str, page: int = Query(1, ge=1), size: int = Query(10, ge=1)):
    params = {
            "city": city,
            "page": page,
            "size": size,
        }

    return get_with_circuit_breaker( url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries", params=params, breaker=library_breaker, fallback=[], critical=True,)


# GET /api/v1/libraries/{library_uid}/books?page=0&size=10&showAll=false
@app.get("/api/v1/libraries/{library_uid}/books")
def get_books(library_uid: UUID, page: int = Query(1, ge=1), size: int = Query(10, ge=1), showAll: bool = False):
    return get_with_circuit_breaker(
        url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{library_uid}/books",
        params={
            "page": page,
            "size": size,
            "showAll": showAll,
        },
        breaker=library_breaker,
        fallback={"items": []},
        critical=True,
    )

# ---------------- RESERVATION SERVICE ----------------

@app.get("/api/v1/reservations")
def get_reservations(x_user_name: str = Header(..., alias="X-User-Name")):
    response = get_with_circuit_breaker(
        url=f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
        headers={"X-User-Name": x_user_name},
        breaker=reservation_breaker,
        fallback=[],
        critical=True,
    )
    if isinstance(response, Response):
        return response

    libraries: dict[str, dict] = {}
    books: dict[tuple[str, str], dict] = {}
    reservations = []

    for reservation in response:
        library_uid = str(reservation["libraryUid"])
        book_uid = str(reservation["bookUid"])

        if library_uid not in libraries:
            libraries[library_uid] = get_library_info(library_uid)
        if (library_uid, book_uid) not in books:
            books[(library_uid, book_uid)] = get_book_info(library_uid, book_uid)

        reservations.append(
            enrich_reservation(
                reservation,
                books[(library_uid, book_uid)],
                libraries[library_uid],
            )
        )

    return JSONResponse(content=reservations, status_code=200)

@app.post("/api/v1/reservations")
def create_reservation(body: ReservationRequest, x_user_name: str = Header(..., alias="X-User-Name")):
    headers = {"X-User-Name": x_user_name}

    rating_response = get_with_circuit_breaker(
        url=f"{RATING_SERVICE_URL}/api/v1/rating",
        headers=headers,
        breaker=rating_breaker,
        fallback={},
        critical=True,
    )
    stars = rating_response["stars"]
    
    reservations = get_with_circuit_breaker(
        url=f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
        headers=headers,
        breaker=reservation_breaker,
        fallback=[],
        critical=True,
    )

    active_reservations = [
        reservation
        for reservation in reservations
        if reservation.get("status") == "RENTED"
    ]
    active_count = len(active_reservations)
    if active_count >= stars:
        raise HTTPException(status_code=400, detail="Maximum number of rented books reached")

    books = get_with_circuit_breaker(
        url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{body.libraryUid}/books",
        params={
            "page": 1,
            "size": 100,
            "showAll": False,
        },
        breaker=library_breaker,
        fallback={},
        critical=True,
    )
    book = next(
        (book for book in books["items"]
        if book["bookUid"] == str(body.bookUid)),
        None
    )

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    
    library = get_with_circuit_breaker(
        url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{body.libraryUid}",
        breaker=library_breaker,
        fallback={},
        critical=True,
    )

    try:
        reservation_response = httpx.post(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
            headers=headers,
            json=body.model_dump(mode="json"),
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Reservation Service is unavailable",
        )

    if reservation_response.status_code != 200:
        raise HTTPException(
            status_code=reservation_response.status_code,
            detail=reservation_response.text,
        )

    reservation_data = reservation_response.json()
    reservation_uid = reservation_data["reservationUid"]

    try:
        take_response = httpx.post(
            f"{LIBRARY_SERVICE_URL}/api/v1/libraries/"
            f"{body.libraryUid}/books/{body.bookUid}/take",
            timeout=5.0,
        )
    except httpx.RequestError:
        take_response = None

    if take_response is None or take_response.status_code != 200:
        try:
            rollback_response = httpx.delete(
                f"{RESERVATION_SERVICE_URL}/api/v1/reservations/"
                f"{reservation_uid}",
                headers=headers,
                timeout=5.0,
            )
        except httpx.RequestError:
            raise HTTPException(
                status_code=503,
                detail="Library Service is unavailable and rollback failed",
            )

        if rollback_response.status_code not in (200, 204):
            raise HTTPException(
                status_code=503,
                detail="Failed to rollback reservation",
            )

        raise HTTPException(
            status_code=503,
            detail="Library Service is unavailable",
        )
        
    
    book = {
            "bookUid": book["bookUid"],
            "name": book["name"],
            "author": book["author"],
            "genre": book["genre"],
        }

    return JSONResponse(content={
        "reservationUid": str(reservation_data.get("reservationUid")),
        "status": reservation_data.get("status"),
        "startDate": to_date(reservation_data.get("startDate")).isoformat(),
        "tillDate": to_date(reservation_data.get("tillDate")).isoformat(),
        "book": book,
        "library": {
            "libraryUid": library["libraryUid"],
            "name": library["name"],
            "address": library["address"],
            "city": library["city"],
        },
        "rating": {"stars": stars},
    }, status_code=200)

# @app.post("/api/v1/reservations")
# def create_reservation(body: ReservationRequest, x_user_name: str = Header(..., alias="X-User-Name")):
#     headers = {"X-User-Name": x_user_name}

#     try:
#         rating_response = httpx.get(
#             f"{RATING_SERVICE_URL}/api/v1/rating",
#             headers=headers,
#             timeout=5.0,
#         )
#     except httpx.RequestError:
#         raise HTTPException(status_code=503, detail="Rating Service is unavailable")

#     if rating_response.status_code != 200:
#         raise HTTPException(status_code=rating_response.status_code, detail=f"Unable to get user rating: {rating_response.text}")

#     rating_data = rating_response.json()
#     stars = rating_data["stars"]
    
#     try:
#         reservations_response = httpx.get(
#             f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
#             headers=headers,
#             timeout=5.0,
#         )
#     except httpx.RequestError:
#         raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

#     if reservations_response.status_code != 200:
#         raise HTTPException(status_code=reservations_response.status_code, detail="Unable to get user reservations")

#     reservations = reservations_response.json()

#     active_reservations = [
#         reservation
#         for reservation in reservations
#         if reservation.get("status") == "RENTED"
#     ]
#     active_count = len(active_reservations)
#     if active_count >= stars:
#         raise HTTPException(status_code=400, detail="Maximum number of rented books reached")

#     try:
#         books_response = httpx.get(
#             f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{body.libraryUid}/books",
#             params={
#                 "page": 1,
#                 "size": 100,
#                 "showAll": False,
#             },
#             timeout=5.0,
#         )
#     except httpx.RequestError:
#         raise HTTPException(status_code=503, detail="Library Service is unavailable")
    
#     if books_response.status_code != 200:
#         raise HTTPException(status_code=books_response.status_code, detail="Unable to get library books")

#     books = books_response.json()
#     book = next(
#         (book for book in books["items"]
#         if book["bookUid"] == str(body.bookUid)),
#         None
#     )

#     if book is None:
#         raise HTTPException(status_code=404, detail="Book not found")
#     try:
#         take_response = httpx.post(
#             f"{LIBRARY_SERVICE_URL}/api/v1/libraries/"
#             f"{body.libraryUid}/books/{body.bookUid}/take",
#             timeout=5.0,
#         )
#     except httpx.RequestError:
#         raise HTTPException(status_code=503, detail="Library Service is unavailable")

#     if take_response.status_code != 200:
#         raise HTTPException(status_code=take_response.status_code, detail="Unable to take book")

#     try:
#         reservation_response = httpx.post(
#             f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
#             headers=headers,
#             json=body.model_dump(mode="json"),
#             timeout=5.0,
#         )
#     except httpx.RequestError:
#         raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

#     if reservation_response.status_code != 200:
#         raise HTTPException(status_code=reservation_response.status_code, detail=reservation_response.text)

#     reservation_data = reservation_response.json()
#     library = get_library_info(str(reservation_data.get("libraryUid") or body.libraryUid))
#     book = {
#             "bookUid": book["bookUid"],
#             "name": book["name"],
#             "author": book["author"],
#             "genre": book["genre"],
#         }

#     return JSONResponse(content={
#         "reservationUid": str(reservation_data.get("reservationUid")),
#         "status": reservation_data.get("status"),
#         "startDate": to_date(reservation_data.get("startDate")).isoformat(),
#         "tillDate": to_date(reservation_data.get("tillDate")).isoformat(),
#         "book": book,
#         "library": {
#             "libraryUid": library["libraryUid"],
#             "name": library["name"],
#             "address": library["address"],
#             "city": library["city"],
#         },
#         "rating": {"stars": stars},
#     }, status_code=200)

@app.post("/api/v1/reservations/{reservation_uid}/return")
def return_reservation(reservation_uid: UUID, body: ReturnRequest, x_user_name: str = Header(..., alias="X-User-Name")):
    headers = {"X-User-Name": x_user_name}

    reservation = get_with_circuit_breaker(
        url=f"{RESERVATION_SERVICE_URL}/api/v1/reservations/{reservation_uid}",
        headers=headers,
        breaker=reservation_breaker,
        fallback={},
        critical=True,
    )
    if isinstance(reservation, Response):
        return reservation

    if reservation["status"] != "RENTED":
        raise HTTPException(status_code=400, detail="Reservation is not active")

    book_uid = reservation["bookUid"]
    library_uid = reservation["libraryUid"]

    till_date = to_date(reservation["tillDate"])
    return_date = to_date(body.date)
    try:
        books = get_with_circuit_breaker(
            url=f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{library_uid}/books",
            params={
                "page": 1,
                "size": 100,
                "showAll": True,
            },
            breaker=library_breaker,
            fallback={},
            critical=True,
        )
    except HTTPException as error:
        if error.status_code != 500:
            raise

        publish_operation({
            "operation": "RETURN_RESERVATION",
            "reservationUid": str(reservation_uid),
            "user": x_user_name,
            "condition": body.condition,
            "date": body.date.isoformat(),
        })
        return Response(status_code=204)
        
    if isinstance(books, Response):
        return books

    book = next(
        (
            item for item in books["items"]
            if item["bookUid"] == str(book_uid)
        ),
        None
    )

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    is_late = return_date > till_date
    condition_changed = body.condition != book["condition"]

    delta = 1
    if is_late:
        delta = -10
    if condition_changed:
        delta = -10
        
    rating_data = get_with_circuit_breaker(
        url=f"{RATING_SERVICE_URL}/api/v1/rating",
        headers=headers,
        breaker=rating_breaker,
        fallback={},
        critical=True,
    )

    if isinstance(rating_data, Response):
        raise HTTPException(status_code=500, detail="Rating Service is unavailable",)

    new_stars = max(1, min(100, rating_data["stars"] + delta))

    try:
        status_response = httpx.post(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations/"
            f"{reservation_uid}/return",
            headers=headers,
            json=body.model_dump(mode="json"),
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Reservation Service is unavailable",
        )

    if status_response.status_code not in (200, 204):
        raise HTTPException(
            status_code=status_response.status_code,
            detail=status_response.text,)

    try:
        library_response = httpx.post(
            (
                f"{LIBRARY_SERVICE_URL}/api/v1/libraries/"
                f"{library_uid}/books/{book_uid}/return"
            ),
            timeout=5.0,
        )
    except httpx.RequestError:
        library_response = None

    # Если Library недоступен — ставим возврат в очередь.
    if library_response is None or library_response.status_code >= 500:
        publish_operation({
            "operation": "RETURN_LIBRARY",
            "reservationUid": str(reservation_uid),
            "user": x_user_name,
            "condition": body.condition,
            "date": body.date.isoformat(),
        })
        return Response(status_code=204)

    if library_response.status_code not in (200, 204):
        raise HTTPException(
            status_code=library_response.status_code,
            detail=library_response.text,
        )

    # 6. Книга возвращена. Теперь обновляем рейтинг.
    try:
        rating_response = httpx.patch(
            f"{RATING_SERVICE_URL}/api/v1/rating",
            headers=headers,
            json={"stars": new_stars},
            timeout=5.0,
        )
    except httpx.RequestError:
        rating_response = None

    # Если Rating недоступен — ставим в очередь ТОЛЬКО рейтинг.
    if rating_response is None or rating_response.status_code >= 500:
        publish_operation({
            "operation": "UPDATE_RATING",
            "user": x_user_name,
            "newStars": new_stars,
        })
        return Response(status_code=204)

    if rating_response.status_code not in (200, 204):
        raise HTTPException(
            status_code=rating_response.status_code,
            detail=rating_response.text,
        )

    return Response(status_code=204)

# ---------------- RATING SERVICE ----------------

@app.get("/api/v1/rating")
def get_rating(x_user_name: str = Header(..., alias="X-User-Name"),):
    return get_with_circuit_breaker(
        url=f"{RATING_SERVICE_URL}/api/v1/rating",
        headers={"X-User-Name": x_user_name},
        breaker=rating_breaker,
        fallback={},
        critical=True,
    )