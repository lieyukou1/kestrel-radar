from __future__ import annotations

import time

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    attempts: int = 3,
    base_delay: float = 3.0,
) -> httpx.Response:
    """带退避的 GET；429/5xx 和传输错误会重试。"""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if response.status_code not in RETRY_STATUS:
                response.raise_for_status()
                return response

            last_error = httpx.HTTPStatusError(
                f"retryable status {response.status_code}",
                request=response.request,
                response=response,
            )
            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else base_delay * (2**attempt)
            )
            if attempt < attempts - 1:
                time.sleep(delay)
                continue
            response.raise_for_status()

    assert last_error is not None
    raise last_error
