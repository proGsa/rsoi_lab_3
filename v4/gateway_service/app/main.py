import logging
from uuid import UUID
from fastapi import FastAPI, Request, Response, Query, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import date

from .errors import ApiError, ServiceUnavailable
from .upstream import LIBRARY, RESERVATION, RATING, upstream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)

app = FastAPI(title="Gateway Service")

class ReservationRequest(BaseModel):
    bookUid: UUID
    libraryUid: UUID
    tillDate: date

class ReturnRequest(BaseModel):
    condition: str
    date: date


def to_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# ---------------- ERRORS ----------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content={"message": exc.message})


@app.exception_handler(ServiceUnavailable)
async def service_unavailable_handler(request: Request, exc: ServiceUnavailable):
    return JSONResponse(status_code=exc.status_code, content={"message": exc.message})


@app.get("/manage/health")
def health():
    return {"status": "UP"}


def passthrough(response) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


def unavailable(service: str) -> ServiceUnavailable:
    return ServiceUnavailable(upstream.label(service))


# ---------------- LIBRARY SERVICE ----------------

def get_library_info(library_uid) -> dict:
    response = upstream.try_call(LIBRARY, "GET", f"/api/v1/libraries/{library_uid}")
    if response is None or response.status_code != 200:
        return {"libraryUid": str(library_uid)}
    return response.json()


def get_book_info(library_uid, book_uid) -> dict:
    response = upstream.try_call(
        LIBRARY,
        "GET",
        f"/api/v1/libraries/{library_uid}/books",
        params={"page": 1, "size": 100, "showAll": True},
    )
    if response is None or response.status_code != 200:
        return {"bookUid": str(book_uid)}

    book = next(
        (item for item in response.json()["items"] if item["bookUid"] == str(book_uid)),
        None,
    )
    if book is None:
        return {"bookUid": str(book_uid)}
    return book


def enrich_reservation(reservation: dict, book: dict, library: dict) -> dict:
    return {
        "reservationUid": str(reservation["reservationUid"]),
        "status": reservation["status"],
        "startDate": to_date(reservation["startDate"]).isoformat(),
        "tillDate": to_date(reservation["tillDate"]).isoformat(),
        "book": {
            "bookUid": book["bookUid"],
            "name": book["name"],
            "author": book["author"],
            "genre": book["genre"],
        },
        "library": {
            "libraryUid": library["libraryUid"],
            "name": library["name"],
            "address": library["address"],
            "city": library["city"],
        },
    }


@app.get("/api/v1/libraries")
def get_libraries(city: str, page: int = Query(0, ge=0), size: int = Query(10, ge=1)):
    return proxy_get(
        f"{LIBRARY_SERVICE_URL}/api/v1/libraries",
        params={
            "city": city,
            "page": page,
            "size": size,
        },
    )

# GET /api/v1/libraries/{library_uid}/books?page=0&size=10&showAll=false
@app.get("/api/v1/libraries/{library_uid}/books")
def get_books(library_uid: UUID, page: int = Query(0, ge=0), size: int = Query(10, ge=1), showAll: bool = False):
    return proxy_get(
        f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{library_uid}/books",
        params={
            "page": page,
            "size": size,
            "showAll": showAll,
        },
    )

# ---------------- RESERVATION SERVICE ----------------

@app.get("/api/v1/reservations")
def get_reservations(x_user_name: str = Header(..., alias="X-User-Name")):
    try:
        response = httpx.get(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
            headers={"X-User-Name": x_user_name},
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Unable to get user reservations")

    libraries: dict[str, dict] = {}
    books: dict[tuple[str, str], dict] = {}
    reservations = []

    for reservation in response.json():
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

    # Rating Service – критичный источник: без рейтинга нельзя понять,
    # можно ли вообще выдавать книгу.
    rating_response = upstream.call(RATING, "GET", "/api/v1/rating", headers=headers)
    if rating_response.status_code != 200:
        raise ApiError(rating_response.status_code, "Unable to get user rating")

    stars = rating_response.json()["stars"]

    books_response = upstream.call(
        LIBRARY,
        "GET",
        f"/api/v1/libraries/{body.libraryUid}/books",
        params={"page": 1, "size": 100, "showAll": False},
    )
    if books_response.status_code != 200:
        raise ApiError(books_response.status_code, "Unable to get library books")

    book = next(
        (
            item
            for item in books_response.json()["items"]
            if item["bookUid"] == str(body.bookUid)
        ),
        None,
    )
    if book is None:
        raise ApiError(404, "Book not found")

    take_response = upstream.call(
        LIBRARY,
        "POST",
        f"/api/v1/libraries/{body.libraryUid}/books/{body.bookUid}/take",
    )
    if take_response.status_code != 200:
        raise ApiError(take_response.status_code, "Unable to take book")

    reservation_response = upstream.try_call(
        RESERVATION,
        "POST",
        "/api/v1/reservations",
        headers=headers,
        json=body.model_dump(mode="json"),
    )
    if reservation_response is None or reservation_response.status_code != 200:
        # Компенсация: книга уже списана с витрины – возвращаем на витрину.
        rollback = upstream.try_call(
            LIBRARY,
            "POST",
            f"/api/v1/libraries/{body.libraryUid}/books/{body.bookUid}/return",
        )
        if rollback is None:
            logging.getLogger("gateway").error(
                "rollback failed for book=%s library=%s", body.bookUid, body.libraryUid
            )
        if reservation_response is None:
            raise unavailable(RESERVATION)
        raise ApiError(reservation_response.status_code, "Unable to create reservation")

    reservation_data = reservation_response.json()
    library = get_library_info(str(reservation_data.get("libraryUid") or body.libraryUid))

    return JSONResponse(content={
        "reservationUid": str(reservation_data.get("reservationUid")),
        "status": reservation_data.get("status"),
        "startDate": to_date(reservation_data.get("startDate")).isoformat(),
        "tillDate": to_date(reservation_data.get("tillDate")).isoformat(),
        "book": {
            "bookUid": book["bookUid"],
            "name": book["name"],
            "author": book["author"],
            "genre": book["genre"],
        },
        "library": {
            key: library[key]
            for key in ("libraryUid", "name", "address", "city")
            if key in library
        },
        "rating": {"stars": stars},
    }, status_code=200)


@app.post("/api/v1/reservations/{reservation_uid}/return")
def return_reservation(reservation_uid: UUID, body: ReturnRequest, x_user_name: str = Header(..., alias="X-User-Name")):
    headers = {"X-User-Name": x_user_name}

    reservation_response = upstream.call(
        RESERVATION, "GET", f"/api/v1/reservations/{reservation_uid}", headers=headers
    )
    if reservation_response.status_code != 200:
        raise ApiError(reservation_response.status_code, "Unable to get reservation")

    reservation = reservation_response.json()

    if reservation["status"] != "RENTED":
        raise ApiError(400, "Reservation is not active")

    book_uid = reservation["bookUid"]
    library_uid = reservation["libraryUid"]

    is_late = to_date(body.date) > to_date(reservation["tillDate"])

    books_response = upstream.call(
        LIBRARY,
        "GET",
        f"/api/v1/libraries/{library_uid}/books",
        params={"page": 1, "size": 100, "showAll": True},
    )
    if books_response.status_code != 200:
        raise ApiError(books_response.status_code, "Unable to get library books")

    book = next(
        (item for item in books_response.json()["items"] if item["bookUid"] == str(book_uid)),
        None,
    )
    if book is None:
        raise ApiError(404, "Book not found")

    condition_changed = body.condition != book["condition"]

    # 1. Статус брони.
    status_response = upstream.call(
        RESERVATION,
        "POST",
        f"/api/v1/reservations/{reservation_uid}/return",
        headers=headers,
        json=body.model_dump(mode="json"),
    )
    if status_response.status_code not in (200, 204):
        raise ApiError(status_response.status_code, "Unable to update reservation status")

    # 2. Книга обратно на витрину.
    library_response = upstream.try_call(
        LIBRARY,
        "POST",
        f"/api/v1/libraries/{library_uid}/books/{book_uid}/return",
    )
    if library_response is None or library_response.status_code not in (200, 204):
        logging.getLogger("gateway").warning(
            "unable to put book=%s back to library=%s", book_uid, library_uid
        )

    # 3. Рейтинг.
    delta = 1
    if is_late:
        delta = -10
    if condition_changed:
        delta = -10

    rating_response = upstream.try_call(RATING, "GET", "/api/v1/rating", headers=headers)
    if rating_response is None or rating_response.status_code != 200:
        # Рейтинг – некритичный шаг: пользователь уже получил успешный ответ,
        # задача дорабатывается из очереди (см. queue.py).
        from .queue import schedule_rating_update

        schedule_rating_update(username=x_user_name, delta=delta)
        return Response(status_code=204)

    stars = rating_response.json()["stars"]

    update_response = upstream.try_call(
        RATING,
        "PATCH",
        "/api/v1/rating",
        headers=headers,
        json={"stars": max(1, min(100, stars + delta))},
    )
    if update_response is None or update_response.status_code not in (200, 204):
        from .queue import schedule_rating_update

        schedule_rating_update(username=x_user_name, delta=delta)

    return Response(status_code=204)


# ---------------- RATING SERVICE ----------------

@app.get("/api/v1/rating")
def get_rating(x_user_name: str = Header(..., alias="X-User-Name")):
    response = upstream.call(
        RATING,
        "GET",
        "/api/v1/rating",
        headers={"X-User-Name": x_user_name},
    )
    return passthrough(response)

@app.post("/api/v1/reservations")
def create_reservation(body: ReservationRequest, x_user_name: str = Header(..., alias="X-User-Name")):
    headers = {"X-User-Name": x_user_name}

    try:
        rating_response = httpx.get(
            f"{RATING_SERVICE_URL}/api/v1/rating",
            headers=headers,
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Rating Service is unavailable")

    if rating_response.status_code != 200:
        raise HTTPException(status_code=rating_response.status_code, detail=f"Unable to get user rating: {rating_response.text}")

    rating_data = rating_response.json()
    stars = rating_data["stars"]
    
    try:
        reservations_response = httpx.get(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
            headers=headers,
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

    if reservations_response.status_code != 200:
        raise HTTPException(status_code=reservations_response.status_code, detail="Unable to get user reservations")

    reservations = reservations_response.json()

    active_reservations = [
        reservation
        for reservation in reservations
        if reservation.get("status") == "RENTED"
    ]
    active_count = len(active_reservations)
    if active_count >= stars:
        raise HTTPException(status_code=400, detail="Maximum number of rented books reached")

    try:
        books_response = httpx.get(
            f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{body.libraryUid}/books",
            params={
                "page": 1,
                "size": 100,
                "showAll": False,
            },
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Library Service is unavailable")
    
    if books_response.status_code != 200:
        raise HTTPException(status_code=books_response.status_code, detail="Unable to get library books")

    books = books_response.json()
    book = next(
        (book for book in books["items"]
        if book["bookUid"] == str(body.bookUid)),
        None
    )

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    try:
        take_response = httpx.post(
            f"{LIBRARY_SERVICE_URL}/api/v1/libraries/"
            f"{body.libraryUid}/books/{body.bookUid}/take",
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Library Service is unavailable")

    if take_response.status_code != 200:
        raise HTTPException(status_code=take_response.status_code, detail="Unable to take book")

    try:
        reservation_response = httpx.post(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations",
            headers=headers,
            json=body.model_dump(mode="json"),
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

    if reservation_response.status_code != 200:
        raise HTTPException(status_code=reservation_response.status_code, detail=reservation_response.text)

    reservation_data = reservation_response.json()
    library = get_library_info(str(reservation_data.get("libraryUid") or body.libraryUid))
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

@app.post("/api/v1/reservations/{reservation_uid}/return")
def return_reservation(reservation_uid: UUID, body: ReturnRequest, x_user_name: str = Header(..., alias="X-User-Name")):
    headers = {"X-User-Name": x_user_name}
    try:
        reservation_response = httpx.get(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations/{reservation_uid}",
            headers=headers,
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

    if reservation_response.status_code != 200:
        raise HTTPException(status_code=reservation_response.status_code, detail=reservation_response.text)

    reservation = reservation_response.json()

    if reservation["status"] != "RENTED":
        raise HTTPException(status_code=400, detail="Reservation is not active")

    book_uid = reservation["bookUid"]
    library_uid = reservation["libraryUid"]

    till_date = to_date(reservation["tillDate"])
    return_date = to_date(body.date)
    is_late = return_date > till_date

    try:
        books_response = httpx.get(
            f"{LIBRARY_SERVICE_URL}/api/v1/libraries/{library_uid}/books",
            params={
                "page": 1,
                "size": 100,
                "showAll": True,
            },
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Library Service is unavailable")

    if books_response.status_code != 200:
        raise HTTPException(status_code=books_response.status_code, detail="Unable to get library books")

    books = books_response.json()

    book = next(
        (
            book for book in books["items"]
            if book["bookUid"] == str(book_uid)
        ),
        None
    )

    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    original_condition = book["condition"]
    condition_changed = body.condition != original_condition
    try:
        library_response = httpx.post(
            f"{LIBRARY_SERVICE_URL}/api/v1/libraries/"
            f"{library_uid}/books/{book_uid}/return",
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Library Service is unavailable")

    if library_response.status_code not in (200, 204):
        raise HTTPException(status_code=library_response.status_code, detail=library_response.text)

    try:
        status_response = httpx.post(
            f"{RESERVATION_SERVICE_URL}/api/v1/reservations/"
            f"{reservation_uid}/return",
            headers=headers,
            json=body.model_dump(mode="json"),
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Reservation Service is unavailable")

    if status_response.status_code not in (200, 204):
        raise HTTPException(status_code=status_response.status_code, detail=status_response.text)

    delta = 1
    if is_late:
        delta = -10
    if condition_changed:
        delta = -10
        
    try:
        rating_response = httpx.get(
            f"{RATING_SERVICE_URL}/api/v1/rating",
            headers=headers,
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Rating Service is unavailable")

    if rating_response.status_code != 200:
        raise HTTPException(status_code=rating_response.status_code, detail=rating_response.text)

    rating_data = rating_response.json()
    stars = rating_data["stars"]

    new_stars = max(1, min(100, stars + delta))

    try:
        update_rating_response = httpx.patch(
            f"{RATING_SERVICE_URL}/api/v1/rating",
            headers=headers,
            json={"stars": new_stars},
            timeout=5.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Rating Service is unavailable")

    if update_rating_response.status_code not in (200, 204):
        raise HTTPException(status_code=update_rating_response.status_code, detail=update_rating_response.text)

    return Response(status_code=204)

# ---------------- RATING SERVICE ----------------

@app.get("/api/v1/rating")
def get_rating(x_user_name: str = Header(..., alias="X-User-Name"),):
    return proxy_request(
        "GET",
        f"{RATING_SERVICE_URL}/api/v1/rating",
        headers={"X-User-Name": x_user_name},
    )