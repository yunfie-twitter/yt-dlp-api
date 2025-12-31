import asyncio
import json
import heapq
from typing import Optional
from fastapi import HTTPException
from app.config.settings import config
from app.models.request import InfoRequest
from app.models.response import VideoInfo
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor
from app.infra.redis import get_redis
from app.utils.hash import hash_stable
from app.i18n import i18n
import functools

INFO_CACHE_TTL = 300

class VideoInfoService:
    """Video info fetching service"""
    
    @staticmethod
    async def fetch(video_request: InfoRequest, locale: str) -> VideoInfo:
        """
        Fetch video information with Redis caching.
        Reduces load from repeated requests for same URL.
        """
        _ = functools.partial(i18n.get, locale=locale)
        
        # Check cache
        cache_key = f"info:{hash_stable(str(video_request.url))}"
        redis = get_redis()
        
        if redis:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    return VideoInfo(**json.loads(cached))
            except Exception:
                pass
        
        # Fetch from yt-dlp
        cmd = YTDLPCommandBuilder.build_info_command(str(video_request.url))
        
        try:
            result = await SubprocessExecutor.run(cmd, timeout=30.0)
            
            if result.returncode != 0:
                error_msg = result.stderr.decode().strip()
                raise HTTPException(
                    status_code=400,
                    detail=_("error.fetch_info_failed", reason=error_msg[:200])
                )
            
            info = json.loads(result.stdout.decode())
            
            is_live = info.get('is_live', False)
            if is_live and not config.ytdlp.enable_live_streams:
                raise HTTPException(status_code=400, detail=_("error.live_not_supported"))
            
            # Intelligent format selection
            all_formats = info.get("formats", [])
            
            # Helper to check if format is audio-only
            def is_audio_only(f):
                return f.get("vcodec") == "none" and f.get("acodec") != "none"

            # Helper to check if format is video
            def is_video(f):
                return f.get("vcodec") != "none"

            video_formats = [f for f in all_formats if is_video(f)]
            audio_formats = [f for f in all_formats if is_audio_only(f)]

            # Select top video formats (prioritize resolution, then filesize)
            top_videos = heapq.nlargest(
                15,
                video_formats,
                key=lambda f: (f.get("height") or 0, f.get("filesize") or 0)
            )
            
            # Select top audio formats (prioritize filesize/bitrate)
            top_audios = heapq.nlargest(
                5,
                audio_formats,
                key=lambda f: (f.get("filesize") or 0, f.get("tbr") or 0)
            )
            
            # Combine and sort by generic quality indicator for display
            selected_formats = sorted(
                top_videos + top_audios,
                key=lambda f: (f.get("height") or 0, f.get("filesize") or 0),
                reverse=True
            )
            
            video_info = VideoInfo(
                id=info.get("id"),
                title=info.get("title", "Unknown"),
                description=info.get("description"),
                duration=info.get("duration"),
                view_count=info.get("view_count"),
                like_count=info.get("like_count"),
                comment_count=info.get("comment_count"),
                uploader=info.get("uploader"),
                channel=info.get("channel"),
                channel_id=info.get("channel_id"),
                age_limit=info.get("age_limit"),
                subtitles=info.get("subtitles"),
                ext=info.get("ext", "mp4"),
                filesize=info.get("filesize"),
                formats=[
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution"),
                        "filesize": f.get("filesize"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                    }
                    for f in selected_formats
                ],
                thumbnail=info.get("thumbnail"),
                webpage_url=info.get("webpage_url", str(video_request.url)),
                is_live=is_live
            )
            
            # Cache result
            if redis:
                try:
                    await redis.setex(cache_key, INFO_CACHE_TTL, video_info.json())
                except Exception:
                    pass
            
            return video_info
            
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail=_("error.parse_failed"))
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=_("error.timeout"))