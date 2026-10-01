class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class ServiceUnavailable(ApiError):
    def __init__(self, label: str):
        super().__init__(503, f"{label} unavailable")
