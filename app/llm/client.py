import concurrent.futures
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Base exception for all LLM client errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.cause = cause


class LLMClient(Protocol):
    """Protocol for LLM interactions."""

    def generate_structured(
        self,
        system: str,
        payload: str,
        schema: type[T],
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> T: ...

    def generate_text(
        self,
        system: str,
        payload: str,
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> str: ...


class GeminiClient:
    """Production Gemini LLM client using google-genai SDK."""

    def __init__(self) -> None:
        from google import genai

        settings = get_settings()
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.warning("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key or "DUMMY_KEY")
        self._default_model = settings.GEMINI_MODEL
        self._timeout_seconds = settings.LLM_TIMEOUT_SECONDS
        self._max_retries = settings.LLM_MAX_RETRIES

    def _execute_with_timeout(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=self._timeout_seconds)
            except concurrent.futures.TimeoutError as e:
                msg = f"LLM request timed out after {self._timeout_seconds} seconds"
                raise LLMError(msg, cause=e) from e

    def _call_with_retries(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        delay = 1.0
        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                return self._execute_with_timeout(func, *args, **kwargs)
            except LLMError as e:
                last_exception = e
                if attempt < self._max_retries:
                    time.sleep(delay)
                    delay *= 2.0
                else:
                    raise
            except Exception as e:
                last_exception = e
                err_msg = str(e).lower()
                is_429 = "429" in err_msg or "resource_exhausted" in err_msg
                is_retryable = (
                    is_429
                    or "rate limit" in err_msg
                    or "503" in err_msg
                    or "500" in err_msg
                    or "timeout" in err_msg
                    or "unavailable" in err_msg
                )
                if is_retryable and attempt < self._max_retries:
                    wait_time = delay
                    if is_429:
                        m_sec = re.search(r"retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(e))
                        if not m_sec:
                            m_sec = re.search(r"retry in\s+([\d\.]+)s", str(e), re.IGNORECASE)
                        if m_sec:
                            try:
                                wait_time = min(30.0, max(1.0, float(m_sec.group(1))))
                            except ValueError:
                                wait_time = min(30.0, delay)
                        else:
                            wait_time = min(30.0, delay)
                    logger.warning(
                        "LLM call attempt %d hit retryable error (%s), waiting %.1fs...",
                        attempt + 1,
                        type(e).__name__,
                        wait_time,
                    )
                    time.sleep(wait_time)
                    delay *= 2.0
                else:
                    raise LLMError(f"LLM call failed: {e}", cause=e) from e

        raise LLMError(f"LLM call failed after {self._max_retries} retries: {last_exception}")

    def generate_structured(
        self,
        system: str,
        payload: str,
        schema: type[T],
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> T:
        from google.genai import types

        settings = get_settings()
        target_model = model or settings.GEMINI_NLU_MODEL or self._default_model
        t_level = thinking_level or settings.NLU_THINKING_LEVEL

        thinking_config = None
        if t_level:
            thinking_config = types.ThinkingConfig(thinking_level=t_level)

        config = types.GenerateContentConfig(
            system_instruction=system,
            thinking_config=thinking_config,
            response_mime_type="application/json",
            response_schema=schema,
        )

        def _call() -> Any:
            start_time = time.perf_counter()
            response = self._client.models.generate_content(
                model=target_model,
                contents=payload,
                config=config,
            )
            elapsed = time.perf_counter() - start_time
            usage = getattr(response, "usage_metadata", None)
            total_tokens = getattr(usage, "total_token_count", None) if usage else None
            logger.info(
                "Gemini structured call completed: model=%s latency=%.3fs tokens=%s",
                target_model,
                elapsed,
                total_tokens,
            )
            return response

        # Try generating and parsing, allowing 1 retry specifically on schema validation failure
        schema_retries = 1
        for schema_attempt in range(schema_retries + 1):
            try:
                response = self._call_with_retries(_call)
                # First check parsed object
                parsed = getattr(response, "parsed", None)
                if isinstance(parsed, schema):
                    return parsed
                # Fallback to model_validate_json on response text
                text = getattr(response, "text", "")
                if text:
                    return schema.model_validate_json(text)
                raise LLMError("Empty response text from LLM")
            except (ValidationError, Exception) as e:
                if schema_attempt < schema_retries:
                    logger.warning("Schema parsing failed (%s), retrying once...", e)
                    continue
                err_msg = f"Failed to produce valid structured output for {schema.__name__}: {e}"
                raise LLMError(err_msg, cause=e) from e

        raise LLMError(f"Structured output generation failed for {schema.__name__}")

    def generate_text(
        self,
        system: str,
        payload: str,
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> str:
        from google.genai import types

        settings = get_settings()
        target_model = model or settings.GEMINI_RESPONDER_MODEL or self._default_model
        t_level = thinking_level or settings.RESPONDER_THINKING_LEVEL

        thinking_config = None
        if t_level:
            thinking_config = types.ThinkingConfig(thinking_level=t_level)

        config = types.GenerateContentConfig(
            system_instruction=system,
            thinking_config=thinking_config,
        )

        def _call() -> Any:
            start_time = time.perf_counter()
            response = self._client.models.generate_content(
                model=target_model,
                contents=payload,
                config=config,
            )
            elapsed = time.perf_counter() - start_time
            usage = getattr(response, "usage_metadata", None)
            total_tokens = getattr(usage, "total_token_count", None) if usage else None
            logger.info(
                "Gemini text call completed: model=%s latency=%.3fs tokens=%s",
                target_model,
                elapsed,
                total_tokens,
            )
            return response

        response = self._call_with_retries(_call)
        text = getattr(response, "text", "")
        return text or ""


class SessionRateLimiter:
    """In-process sliding-window per-session rate limiter (max 12 req/min)."""

    def __init__(self, max_requests_per_minute: int = 12) -> None:
        self.max_requests = max_requests_per_minute
        self._history: dict[str, list[float]] = {}

    def is_allowed(self, session_id: str) -> bool:
        if not session_id:
            return True
        now = time.time()
        window_start = now - 60.0
        timestamps = self._history.get(session_id, [])
        valid = [t for t in timestamps if t >= window_start]
        if len(valid) >= self.max_requests:
            self._history[session_id] = valid
            return False
        valid.append(now)
        self._history[session_id] = valid
        return True

    def reset(self, session_id: str | None = None) -> None:
        if session_id:
            self._history.pop(session_id, None)
        else:
            self._history.clear()


session_limiter = SessionRateLimiter(max_requests_per_minute=12)


class FakeLLMClient:
    """Mock LLM client for deterministic testing without external API calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._structured_responses: list[Any] = []
        self._text_responses: list[str] = []
        self._structured_handler: Callable[[str, str, type], Any] | None = None
        self._text_handler: Callable[[str, str], str] | None = None
        self.force_timeout: bool = False
        self.force_error: bool = False

    def enqueue_structured(self, response: Any) -> None:
        self._structured_responses.append(response)

    def enqueue_text(self, response: str) -> None:
        self._text_responses.append(response)

    def set_structured_handler(self, handler: Callable[[str, str, type], Any]) -> None:
        self._structured_handler = handler

    def set_text_handler(self, handler: Callable[[str, str], str]) -> None:
        self._text_handler = handler

    def generate_structured(
        self,
        system: str,
        payload: str,
        schema: type[T],
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> T:
        self.calls.append(
            {
                "type": "structured",
                "system": system,
                "payload": payload,
                "schema": schema,
                "model": model,
                "thinking_level": thinking_level,
            }
        )
        if self.force_timeout:
            raise LLMError("LLM request timed out after 20 seconds")
        if self.force_error:
            raise LLMError("Simulated LLM error")

        if self._structured_handler:
            result = self._structured_handler(system, payload, schema)
            if isinstance(result, schema):
                return result
            if isinstance(result, str):
                return schema.model_validate_json(result)
            return schema.model_validate(result)

        if self._structured_responses:
            res = self._structured_responses.pop(0)
            if isinstance(res, Exception):
                raise res
            if isinstance(res, schema):
                return res
            if isinstance(res, str):
                return schema.model_validate_json(res)
            return schema.model_validate(res)

        raise LLMError(f"No mock structured response enqueued for schema {schema.__name__}")

    def generate_text(
        self,
        system: str,
        payload: str,
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "type": "text",
                "system": system,
                "payload": payload,
                "model": model,
                "thinking_level": thinking_level,
            }
        )
        if self.force_timeout:
            raise LLMError("LLM request timed out after 20 seconds")
        if self.force_error:
            raise LLMError("Simulated LLM error")

        if self._text_handler:
            return self._text_handler(system, payload)

        if self._text_responses:
            res = self._text_responses.pop(0)
            if isinstance(res, Exception):
                raise res
            return res

        try:
            from app.chat.grounding import render_fallback_template

            plan_obj = json.loads(payload)
            if isinstance(plan_obj, dict) and "type" in plan_obj:
                return render_fallback_template(plan_obj)
        except Exception:
            pass

        return "Default fake text response"
