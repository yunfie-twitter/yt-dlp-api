from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

# Base constants
UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

UA_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

LANG_US = "en-US,en;q=0.9"
LANG_GB = "en-GB,en;q=0.8"


class HttpRetryClient:
    """
    HTTP Client with smart 403 recovery logic.
    Tries to download content by adjusting headers step-by-step.
    """

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    def _get_base_headers(self, url: str) -> Dict[str, str]:
        parsed = urlparse(url)
        # Default Referer: scheme://host/
        referer = f"{parsed.scheme}://{parsed.netloc}/"

        return {
            "User-Agent": UA_CHROME,
            "Accept": "*/*",
            "Accept-Language": LANG_US,
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Referer": referer,
        }

    async def fetch_with_retry(
        self,
        url: str,
        original_page_url: Optional[str] = None,
        incoming_range: Optional[str] = None
    ) -> httpx.Response:
        """
        Fetch URL with intelligent 403 retry strategy.
        Returns the successful response or the last failed response.
        """

        # Initial headers
        headers = self._get_base_headers(url)
        if incoming_range:
            headers["Range"] = incoming_range

        # Attempt 0 (Standard)
        resp = await self._send(url, headers)
        if resp.status_code != 403:
            return resp

        # === Retry Plan ===

        # 1. Retry: Enforce specific Referer (if provided)
        if original_page_url:
            headers["Referer"] = original_page_url
            resp = await self._send(url, headers)
            if resp.status_code != 403:
                return resp

        # 2. Retry: Change Language
        headers["Accept-Language"] = LANG_GB
        resp = await self._send(url, headers)
        if resp.status_code != 403:
            return resp

        # 3. Retry: Add Sec-Fetch headers
        headers.update({
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "video",
        })
        resp = await self._send(url, headers)
        if resp.status_code != 403:
            return resp

        # 4. Retry: Force Range (if not present)
        if "Range" not in headers:
            headers["Range"] = "bytes=0-"
            resp = await self._send(url, headers)
            if resp.status_code != 403:
                return resp

            # If failed, remove it to not mess up next steps (unless it helped? no it failed)
            # Actually, keeping it might be okay for the last step.

        # 5. Retry: Change User-Agent (Final resort)
        headers["User-Agent"] = UA_SAFARI
        resp = await self._send(url, headers)

        return resp

    async def _send(self, url: str, headers: Dict[str, str]) -> httpx.Response:
        # We use stream=True to handle large files efficiently,
        # but for 403 checks we just need the headers.
        # However, to be generic, we return the stream context.
        # The caller must read the body.
        req = self.client.build_request("GET", url, headers=headers)
        return await self.client.send(req, stream=True)
