from fastapi import Request
import logging
from typing import Any

logger = logging.getLogger(__name__)

def log_with_context(
    request: Request,
    level: int,
    message: str,
    **kwargs: Any
) -> None:
    """
    Log with request context.
    Automatically includes request_id for tracing.
    """
    extra = {
        "request_id": getattr(request.state, "request_id", "unknown"),
        **kwargs
    }
    logger.log(level, message, extra=extra)

def log_info(request: Request, message: str, **kwargs: Any) -> None:
    log_with_context(request, logging.INFO, message, **kwargs)

def log_error(request: Request, message: str, **kwargs: Any) -> None:
    log_with_context(request, logging.ERROR, message, **kwargs)

def log_warning(request: Request, message: str, **kwargs: Any) -> None:
    log_with_context(request, logging.WARNING, message, **kwargs)

def log_debug(request: Request, message: str, **kwargs: Any) -> None:
    log_with_context(request, logging.DEBUG, message, **kwargs)
