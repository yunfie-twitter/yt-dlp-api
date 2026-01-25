import asyncio
import json
from typing import List

from fastapi import HTTPException

from app.infra.redis import get_redis
from app.models.response import SearchResponse, SearchResult
from app.services.ytdlp import SubprocessExecutor, YTDLPCommandBuilder
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

        # Search using yt-dlp
        cmd = YTDLPCommandBuilder.build_search_command(query, limit)

        try:
            result = await SubprocessExecutor.run(cmd, timeout=30.0)
            stdout = result.stdout.decode(errors="ignore").strip()

            results: List[SearchResult] = []
            if stdout:
                lines = stdout.strip().split('\n')
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        info = json.loads(line)
                        results.append(SearchResult(
                            title=info.get('title', 'Unknown'),
                            url=info.get('webpage_url', info.get('url', '')),
                            thumbnail=info.get('thumbnail'),
                            duration=info.get('duration'),
                            uploader=info.get('uploader')
                        ))
                    except json.JSONDecodeError:
                        continue

            response = SearchResponse(
                results=results,
                query=query
            )

            # Cache result
            if redis:
                try:
                    await redis.setex(
                        cache_key,
                        SEARCH_CACHE_TTL,
                        response.json()
                    )
                except Exception:
                    pass

            return response

        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output") from e
        except asyncio.TimeoutError as e:
            raise HTTPException(status_code=504, detail="yt-dlp timeout") from e
