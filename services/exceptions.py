from pydantic import BaseModel
from typing import Optional, Dict, Any

class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None

class ErrorResponse(BaseModel):
    error: ErrorDetail

class APIException(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(self.message)

class NotFoundException(APIException):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__("NOT_FOUND", message, 404, details)

class InitializationException(APIException):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__("INIT_ERROR", message, 500, details)

class CalculationException(APIException):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__("CALCULATION_ERROR", message, 422, details)

class MarketDataUnavailableException(APIException):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__("MARKET_DATA_UNAVAILABLE", message, 422, details)
