import asyncio
import json
from typing import List

from fastapi import HTTPException

from app.models.response import SearchResponse, SearchResult
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor
from app.infra.redis import get_redis
from app.utils.hash import hash_stable

SEARCH_CACHE_TTL = 3600  # 1 hour

class VideoSearchService:
    @staticmethod
    async def search(query: str, limit: int, locale: str) -> SearchResponse:
        # Check cache
        cache_key = f"search:{hash_stable(f'{query}:{limit}')}"
        redis = get_redis()

        if redis:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    return SearchResponse(**json.loads(cached))
            except Exception:
                pass

        # locale is currently unused but kept for future i18n/error mapping consistency
        cmd = YTDLPCommandBuilder.build_search_command(query=query, limit=limit)

        try:
            result = await SubprocessExecutor.run(cmd, timeout=30.0)

            if result.returncode != 0:
                error_msg = result.stderr.decode(errors="ignore").strip()
                raise HTTPException(status_code=400, detail=error_msg[:500] or "yt-dlp search failed")

            stdout = result.stdout.decode(errors="ignore").strip()
            
            results: List[SearchResult] = []
            if stdout:
                for line in stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        info = json.loads(line)
                        results.append(
                            SearchResult(
                                id=info.get("id"),
                                title=info.get("title", "Unknown"),
                                description=info.get("description"),
                                duration=info.get("duration"),
                                view_count=info.get("view_count"),
                                uploader=info.get("uploader") or info.get("channel"),
                                thumbnail=info.get("thumbnail"),
                                webpage_url=info.get("webpage_url") or info.get("original_url") or "",
                            )
                        )
                    except json.JSONDecodeError:
                        continue

            response = SearchResponse(query=query, results=results)

            # Cache result
            if redis:
                try:
                    await redis.setex(cache_key, SEARCH_CACHE_TTL, response.json())
                except Exception:
                    pass

            return response

        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="yt-dlp timeout")
