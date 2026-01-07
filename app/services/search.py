import asyncio
import json
from typing import List

from fastapi import HTTPException

from app.models.response import SearchResponse, SearchResult
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor

class VideoSearchService:
    @staticmethod
    async def search(query: str, limit: int, locale: str) -> SearchResponse:
        # locale is currently unused but kept for future i18n/error mapping consistency
        cmd = YTDLPCommandBuilder.build_search_command(query=query, limit=limit)

        try:
            result = await SubprocessExecutor.run(cmd, timeout=30.0)

            if result.returncode != 0:
                error_msg = result.stderr.decode(errors="ignore").strip()
                raise HTTPException(status_code=400, detail=error_msg[:500] or "yt-dlp search failed")

            stdout = result.stdout.decode(errors="ignore").strip()
            if not stdout:
                return SearchResponse(query=query, results=[])

            results: List[SearchResult] = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
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

            return SearchResponse(query=query, results=results)

        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="yt-dlp timeout")
