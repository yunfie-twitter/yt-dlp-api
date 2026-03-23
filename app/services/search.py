import asyncio
import json
from typing import List

from fastapi import HTTPException

from app.core.state import state
from app.infra.redis import get_redis
from app.models.response import SearchResponse, SearchResult
from app.services.ytdlp import YTDLPCommandBuilder
from app.utils.hash import hash_stable

SEARCH_CACHE_TTL = 3600  # 1 hour


class VideoSearchService:
    """Video search service using yt-dlp"""

    @staticmethod
    async def search(query: str, limit: int, locale: str) -> SearchResponse:
        """
        Search videos with Redis caching
        """
        # Check cache
        cache_key = f"search:{hash_stable(query)}:{limit}"
        redis = get_redis()

        if redis:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    return SearchResponse(**data)
            except Exception:
                pass

        # Search using yt-dlp via worker pool
        opts = YTDLPCommandBuilder.build_search_opts(query, limit)

        try:
            entries = await state.worker_pool.run_search(query, opts, limit)

            results: List[SearchResult] = []
            for info in entries:
                results.append(
                    SearchResult(
                        title=info.get("title", "Unknown"),
                        url=info.get("webpage_url", info.get("url", "")),
                        thumbnail=info.get("thumbnail"),
                        duration=info.get("duration"),
                        uploader=info.get("uploader"),
                    )
                )

            response = SearchResponse(results=results, query=query)

            # Cache result
            if redis:
                try:
                    await redis.setex(cache_key, SEARCH_CACHE_TTL, response.json())
                except Exception:
                    pass

            return response

        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output") from e
        except asyncio.TimeoutError as e:
            raise HTTPException(status_code=504, detail="yt-dlp timeout") from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Search failed: {str(e)[:200]}") from e
