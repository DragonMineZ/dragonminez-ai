import asyncio
import requests

from typing import Any


async def request(method: str, url: str, *, headers: dict[str, str] | None = None,
                  params: dict[str, Any] | None = None, json: Any | None = None,
                  timeout: int = 30) -> requests.Response:
    def _do():
        return requests.request(
            method, url,
            headers=headers,
            params=params,
            json=json,
            timeout=timeout,
        )
    return await asyncio.to_thread(_do)
