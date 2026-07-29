"""
Clip Service for Kick Livestreams — bot-side owner of the rolling buffer.

Hand-kept mirror of the dashboard's utils/clip_service.py (same convention as
utils/slot_platforms.py). Keep the two in sync when changing buffer behaviour.

WHY THIS LIVES IN THE BOT, NOT THE DASHBOARD:
the buffer registry is a plain in-process dict holding live ffmpeg subprocesses.
The dashboard runs FOUR gunicorn workers, so a buffer started by one worker was
invisible to the other three: status polls reported "inactive" 3 times out of 4
and clip creation failed with "no buffer running" just as often. Worse,
gunicorn's max_requests recycling killed the recording mid-stream. The bot is a
single always-on process with one asyncio loop, so one authoritative registry
exists and it survives for as long as the stream does.

The dashboard now drives this over Redis and reads status back from a Redis key;
it never touches a buffer directly.

Architecture:
- FFmpeg writes 10-second .ts segments to a rolling buffer
- When a clip is requested, recent segments are concatenated into MP4
- Finished clips are uploaded to object storage and served by the dashboard
"""

import asyncio
import logging
import os
import random
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Configuration
PROJECT_ROOT = Path(__file__).parent.parent

# -------------------------
# Persistent Event Loop for Background Tasks
# -------------------------

_background_loop = None
_background_thread = None
_loop_lock = threading.Lock()


def get_background_loop():
    """Get or create the persistent background event loop."""
    # _background_loop is assigned by the inner run_loop()'s own `global`, not here.
    global _background_thread

    with _loop_lock:
        if _background_loop is None or not _background_loop.is_running():

            def run_loop():
                global _background_loop
                _background_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(_background_loop)
                logger.info("[Background Loop] Started persistent event loop")
                _background_loop.run_forever()
                logger.info("[Background Loop] Event loop stopped")

            _background_thread = threading.Thread(target=run_loop, daemon=True, name="ClipServiceLoop")
            _background_thread.start()

            # Wait for loop to be ready
            import time

            timeout = 5.0
            start = time.time()
            while _background_loop is None and (time.time() - start) < timeout:
                time.sleep(0.01)

            if _background_loop is None:
                raise RuntimeError("Failed to start background event loop")

        return _background_loop


def run_async_in_background(coro):
    """Run a coroutine in the persistent background event loop."""
    loop = get_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# Use environment variable for buffer/clips directories, or default to /data for Railway
# Railway provides persistent storage at /data
# The bot has no Railway volume — its Dockerfile creates /app/buffer and
# /app/clips. Both are scratch: segments are transient, and a finished clip is
# uploaded to object storage and then deleted locally.
BUFFER_DIR = Path(os.getenv("BUFFER_DIR", "/app/buffer"))
CLIPS_DIR = Path(os.getenv("CLIPS_DIR", "/app/clips"))

# Create directories
BUFFER_DIR.mkdir(exist_ok=True, parents=True)
CLIPS_DIR.mkdir(exist_ok=True, parents=True)

logger.info(f"[Clip Service] Buffer directory: {BUFFER_DIR}")
logger.info(f"[Clip Service] Clips directory: {CLIPS_DIR}")

# User agents for API requests
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


# Base URL for serving clips (set from environment)
def get_clips_base_url():
    """Get the base URL for serving clip files."""
    # Try environment variable first
    clips_url = os.getenv("CLIPS_BASE_URL", os.getenv("BASE_URL", ""))
    if clips_url:
        return clips_url

    # Fallback: Try to construct from app config (imported at runtime)
    try:
        from flask import current_app

        if current_app:
            base_domain = current_app.config.get("BASE_DOMAIN", "lelebot.xyz")
            return f"https://lele.{base_domain}"
    except:
        pass

    # Final fallback: hardcode lele subdomain (where clips are served)
    return "https://lele.lelebot.xyz"


class StreamBuffer:
    """
    Rolling buffer that continuously records a livestream.

    Usage:
        buffer = StreamBuffer("channelname", buffer_minutes=4)
        await buffer.start()  # Call when stream goes live

        # Later, when clip is requested:
        clip_info = await buffer.create_clip(duration=30)

        await buffer.stop()  # Call when stream goes offline
    """

    def __init__(self, channel_name: str, buffer_minutes: int = 4):
        self.channel_name = channel_name
        self.buffer_minutes = buffer_minutes
        self.segment_duration = 10  # seconds per segment
        self.max_segments = (buffer_minutes * 60) // self.segment_duration

        self.ffmpeg_process: Optional[asyncio.subprocess.Process] = None
        self.is_recording = False
        self.stream_url: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self._monitor_task: Optional[asyncio.Task] = None

        # Segment tracking
        self.segment_list_file = BUFFER_DIR / f"segments_{channel_name}.txt"

        logger.info(
            f"[Buffer] Initialized for {channel_name}: {buffer_minutes}min buffer, {self.max_segments} segments"
        )

    async def get_stream_url(self) -> Optional[str]:
        """Fetch the HLS stream URL from Kick API v1 (has playback_url, v2 doesn't)."""
        logger.info(f"[Buffer] Fetching stream URL for {self.channel_name}...")
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept": "application/json",
                }

                # Use v1 API - it has playback_url, v2 doesn't
                url = f"https://kick.com/api/v1/channels/{self.channel_name}"
                logger.info(f"[Buffer] Requesting Kick API: {url}")
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    logger.info(f"[Buffer] Kick API response: {response.status}")
                    if response.status != 200:
                        logger.info(f"[Buffer] v1 API returned {response.status}")
                        return None

                    data = await response.json()

                    # v1 API has playback_url at root level
                    playback_url = data.get("playback_url")
                    if playback_url:
                        logger.info(f"[Buffer] Found playback URL from v1 API")
                        return playback_url

                    # Fallback: check livestream object
                    livestream = data.get("livestream")
                    if livestream:
                        playback_url = livestream.get("playback_url")
                        if playback_url:
                            logger.info(f"[Buffer] Found playback URL from livestream object")
                            return playback_url

                    logger.info(f"[Buffer] No playback_url in v1 API response")
                    return None

        except asyncio.TimeoutError:
            logger.info(f"[Buffer] Timeout getting stream URL from Kick API")
            return None
        except Exception as e:
            logger.info(f"[Buffer] Error getting stream URL: {e}")
            return None

    async def check_stream_live(self) -> Dict[str, Any]:
        """Check if the stream is currently live."""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": random.choice(USER_AGENTS)}
                url = f"https://kick.com/api/v2/channels/{self.channel_name}"

                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return {"is_live": False, "error": f"HTTP {response.status}"}

                    data = await response.json()
                    livestream = data.get("livestream")

                    if not livestream:
                        return {"is_live": False}

                    return {
                        "is_live": True,
                        "title": livestream.get("session_title", ""),
                        "viewer_count": livestream.get("viewer_count", 0),
                        "started_at": livestream.get("created_at"),
                    }

        except Exception as e:
            return {"is_live": False, "error": str(e)}

    async def start(self, stream_url: Optional[str] = None) -> bool:
        """
        Start recording the stream to rolling buffer.

        Args:
            stream_url: Optional HLS URL. If not provided, will fetch from API.

        Returns:
            True if recording started successfully.
        """
        if self.is_recording:
            logger.info(f"[Buffer] Already recording {self.channel_name}")
            return True

        # Log what we received
        if stream_url:
            logger.info(f"[Buffer] ✅ Using provided playback URL: {stream_url[:80]}...")
        else:
            logger.info(f"[Buffer] ⚠️ No playback URL provided, attempting to fetch from API...")
            stream_url = await self.get_stream_url()

        if not stream_url:
            logger.info(f"[Buffer] ❌ No stream URL available - stream may not be live")
            return False

        self.stream_url = stream_url

        # Clean old buffer files
        await self._cleanup_buffer()

        # Use yt-dlp to extract the actual stream URL (handles authentication, format selection, etc.)
        try:
            ytdlp_process = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "-f",
                "best",
                "--get-url",
                "--quiet",
                "--no-warnings",
                stream_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(ytdlp_process.communicate(), timeout=15.0)

            if ytdlp_process.returncode != 0:
                logger.info(f"[Buffer] yt-dlp failed, using direct URL")
                actual_stream_url = stream_url
            else:
                actual_stream_url = stdout.decode("utf-8").strip()
        except asyncio.TimeoutError:
            logger.info(f"[Buffer] yt-dlp timed out, using direct URL")
            actual_stream_url = stream_url
        except FileNotFoundError:
            logger.info(f"[Buffer] yt-dlp not found, using direct URL")
            actual_stream_url = stream_url
        except Exception as e:
            logger.info(f"[Buffer] yt-dlp error: {e}, using direct URL")
            actual_stream_url = stream_url

        # Build FFmpeg command for rolling buffer
        # Based on working Kick.com recorder: ffmpeg -i "$URL" -map '0' -tune 'zerolatency' -y -c:v copy "$output"
        segment_pattern = str(BUFFER_DIR / f"seg_{self.channel_name}_%04d.ts")

        cmd = [
            "ffmpeg",
            "-y",  # Overwrite
            "-i",
            actual_stream_url,
            "-map",
            "0",  # Map all streams
            "-tune",
            "zerolatency",  # Low latency tuning for live streams
            "-c:v",
            "copy",  # Copy video (no re-encode)
            "-c:a",
            "aac",  # Re-encode audio to AAC
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_duration),
            "-segment_list",
            str(self.segment_list_file),
            "-segment_list_flags",
            "live",
            "-reset_timestamps",
            "1",
            segment_pattern,
        ]

        try:
            logger.info(f"[Buffer] Starting FFmpeg recording for {self.channel_name}...")
            logger.info(f"[Buffer] Buffer directory: {BUFFER_DIR}")

            self.ffmpeg_process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )

            # Give FFmpeg a moment to start
            await asyncio.sleep(2.0)

            # Check if FFmpeg is still running
            if self.ffmpeg_process.returncode is not None:
                stderr = await self.ffmpeg_process.stderr.read()
                stderr_text = stderr.decode("utf-8", errors="replace")
                logger.info(f"[Buffer] ❌ FFmpeg died immediately! Return code: {self.ffmpeg_process.returncode}")
                logger.info(f"[Buffer] FFmpeg stderr (full):")
                logger.info(stderr_text)
                return False

            self.is_recording = True
            self.started_at = datetime.now()

            logger.info(f"[Buffer] ✅ FFmpeg process started: PID={self.ffmpeg_process.pid}")

            # Start background task to monitor FFmpeg
            self._monitor_task = asyncio.create_task(self._monitor_ffmpeg())

            logger.info(f"[Buffer] ✅ Recording started for {self.channel_name}")

            # Wait a bit more and verify segment file is being created
            await asyncio.sleep(3.0)
            if self.segment_list_file.exists():
                logger.info(f"[Buffer] ✅ Segment list file created")
            else:
                logger.info(f"[Buffer] ⚠️ Segment list file not yet created - waiting for FFmpeg to write segments")

            return True

        except FileNotFoundError:
            logger.info(f"[Buffer] ❌ FFmpeg not found - please install FFmpeg")
            return False
        except Exception as e:
            logger.info(f"[Buffer] ❌ Failed to start recording: {e}")
            return False

    async def stop(self):
        """Stop recording."""
        if not self.is_recording:
            return

        self.is_recording = False

        # Cancel monitor task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.info(f"[Buffer] Error cancelling monitor task: {e}")
            self._monitor_task = None

        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.terminate()
                # Use shield to protect against cancellation during cleanup
                try:
                    await asyncio.wait_for(asyncio.shield(self.ffmpeg_process.wait()), timeout=5)
                except asyncio.TimeoutError:
                    self.ffmpeg_process.kill()
                    # Wait a bit for kill to take effect
                    try:
                        await asyncio.wait_for(self.ffmpeg_process.wait(), timeout=2)
                    except:
                        pass
            except Exception as e:
                logger.info(f"[Buffer] Error stopping FFmpeg: {e}")
                # Force kill if still running
                try:
                    if self.ffmpeg_process and self.ffmpeg_process.returncode is None:
                        self.ffmpeg_process.kill()
                except:
                    pass

            self.ffmpeg_process = None

        logger.info(f"[Buffer] Recording stopped for {self.channel_name}")

    async def _monitor_ffmpeg(self):
        """Monitor FFmpeg process and restart if needed."""
        try:
            while self.is_recording and self.ffmpeg_process:
                try:
                    # Check if process is still running
                    if self.ffmpeg_process.returncode is not None:
                        # Process has exited - read remaining stderr with timeout
                        try:
                            stderr = await asyncio.wait_for(self.ffmpeg_process.stderr.read(), timeout=2.0)
                            logger.info(f"[Buffer] FFmpeg exited with code {self.ffmpeg_process.returncode}")
                            if stderr:
                                logger.info(f"[Buffer] FFmpeg error: {stderr.decode()[:200]}")
                        except asyncio.TimeoutError:
                            logger.info(
                                f"[Buffer] FFmpeg exited with code {self.ffmpeg_process.returncode} (stderr read timeout)"
                            )
                        except Exception as read_err:
                            logger.info(
                                f"[Buffer] FFmpeg exited with code {self.ffmpeg_process.returncode} (stderr error: {read_err})"
                            )

                        # Ensure process is fully cleaned up
                        try:
                            await asyncio.wait_for(self.ffmpeg_process.wait(), timeout=1.0)
                        except:
                            pass

                        self.ffmpeg_process = None

                        # Try to restart if still supposed to be recording
                        if self.is_recording:
                            logger.info(f"[Buffer] Restarting recording...")
                            await asyncio.sleep(5)
                            await self.start()
                        break

                except asyncio.CancelledError:
                    logger.info(f"[Buffer] Monitor task cancelled")
                    break
                except Exception as e:
                    logger.info(f"[Buffer] Monitor error: {e}")
                    import traceback

                    traceback.print_exc()

                await asyncio.sleep(10)

        except asyncio.CancelledError:
            logger.info(f"[Buffer] Monitor task cancelled")
        except Exception as e:
            logger.info(f"[Buffer] Monitor task fatal error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            logger.info(f"[Buffer] Monitor task exiting for {self.channel_name}")

    async def _cleanup_buffer(self):
        """Remove old buffer files for this channel."""
        try:
            for f in BUFFER_DIR.glob(f"seg_{self.channel_name}_*.ts"):
                f.unlink()
            if self.segment_list_file.exists():
                self.segment_list_file.unlink()
            logger.info(f"[Buffer] Cleaned up old buffer files for {self.channel_name}")
        except Exception as e:
            logger.info(f"[Buffer] Cleanup error: {e}")

    def get_recent_segments(self, duration_seconds: int) -> List[Path]:
        """
        Get the most recent segment files for the requested duration.

        Args:
            duration_seconds: How many seconds of video to get

        Returns:
            List of segment file paths, oldest to newest
        """
        if not self.segment_list_file.exists():
            return []

        # Read segment list
        try:
            with open(self.segment_list_file, "r") as f:
                segments = [line.strip() for line in f if line.strip()]
        except Exception:
            return []

        # Calculate how many segments we need
        num_segments = (duration_seconds // self.segment_duration) + 1
        num_segments = min(num_segments, len(segments))

        # Get the last N segments
        recent = segments[-num_segments:]

        # Convert to full paths and verify they exist
        paths = []
        for seg in recent:
            path = BUFFER_DIR / seg
            if path.exists():
                paths.append(path)

        return paths

    async def create_clip(self, duration: int = 30, username: str = "", title: str = "") -> Optional[Dict[str, Any]]:
        """
        Create a clip from the rolling buffer.

        Args:
            duration: Clip duration in seconds (max = buffer_minutes * 60)
            username: Who requested the clip
            title: Optional clip title

        Returns:
            Dict with clip info or None if failed
        """
        if not self.is_recording:
            logger.info(f"[Buffer] Cannot create clip - not recording")
            return {"error": "not_recording", "message": "Stream buffer not active"}

        # Limit duration to buffer size
        max_duration = self.buffer_minutes * 60
        duration = min(duration, max_duration)

        # Get recent segments
        segments = self.get_recent_segments(duration)

        if not segments:
            logger.info(f"[Buffer] No segments available for clip")
            return {"error": "no_segments", "message": "No buffer data available yet - try again in 30 seconds"}

        logger.info(f"[Buffer] Creating clip from {len(segments)} segments ({duration}s requested)")

        # Generate clip filename with sanitized title
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Sanitize title for filename (remove special chars, replace spaces with hyphens)
        if title:
            # Remove special characters, keep alphanumeric and spaces
            sanitized_title = re.sub(r"[^a-zA-Z0-9\s-]", "", title)
            # Replace spaces with hyphens, collapse multiple hyphens
            sanitized_title = re.sub(r"\s+", "-", sanitized_title.strip())
            sanitized_title = re.sub(r"-+", "-", sanitized_title)
            # Limit length
            sanitized_title = sanitized_title[:50]
            filename = f"{sanitized_title}_{timestamp}.mp4"
        else:
            # No title - use default format
            filename = f"clip_{self.channel_name}_{timestamp}.mp4"

        output_path = CLIPS_DIR / filename

        # Create concat file for FFmpeg
        clip_id = str(uuid.uuid4())[:8]
        concat_file = BUFFER_DIR / f"concat_{clip_id}.txt"
        try:
            with open(concat_file, "w") as f:
                for seg in segments:
                    f.write(f"file '{seg.absolute()}'\n")

            # Concatenate segments into MP4
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

            # Clean up concat file
            concat_file.unlink()

            if process.returncode != 0:
                logger.info(f"[Buffer] FFmpeg concat error: {stderr.decode()[:300]}")
                return {"error": "ffmpeg_error", "message": "Failed to create clip"}

            if not output_path.exists():
                logger.info(f"[Buffer] Clip file not created")
                return {"error": "file_error", "message": "Clip file was not created"}

            file_size = output_path.stat().st_size
            actual_duration = len(segments) * self.segment_duration

            # Build clip URL
            base_url = get_clips_base_url()
            clip_url = f"{base_url}/clips/file/{filename}" if base_url else f"/clips/file/{filename}"

            logger.info(f"[Buffer] ✅ Clip created: {filename} ({file_size / 1024 / 1024:.2f} MB)")

            return {
                "success": True,
                "clip_id": clip_id,
                "filename": filename,
                "filepath": str(output_path),
                "clip_url": clip_url,
                "duration": actual_duration,
                "channel": self.channel_name,
                "created_by": username,
                "title": title or f"Clip by {username}",
                "created_at": datetime.now().isoformat(),
                "file_size": file_size,
                "file_size_mb": round(file_size / 1024 / 1024, 2),
            }

        except asyncio.TimeoutError:
            logger.info(f"[Buffer] Clip creation timed out")
            return {"error": "timeout", "message": "Clip creation timed out"}
        except Exception as e:
            logger.info(f"[Buffer] Error creating clip: {e}")
            return {"error": "exception", "message": str(e)}

    def get_segment_count(self) -> int:
        """Get the number of segments currently in the buffer."""
        if not self.segment_list_file.exists():
            return 0

        try:
            with open(self.segment_list_file, "r") as f:
                segments = [line.strip() for line in f if line.strip()]
            return len(segments)
        except Exception:
            return 0

    def get_status(self) -> Dict[str, Any]:
        """Get current buffer status."""
        segments = []
        if self.segment_list_file.exists():
            try:
                with open(self.segment_list_file, "r") as f:
                    segments = [line.strip() for line in f if line.strip()]
            except:
                pass

        return {
            "channel": self.channel_name,
            "is_recording": self.is_recording,
            "buffer_minutes": self.buffer_minutes,
            "segments_count": len(segments),
            "buffer_seconds": len(segments) * self.segment_duration,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stream_url": self.stream_url[:50] + "..." if self.stream_url else None,
        }


# -------------------------
# Global Buffer Manager
# -------------------------


class ClipServiceManager:
    """Manages stream buffers for multiple channels."""

    def __init__(self):
        self._buffers: Dict[str, StreamBuffer] = {}
        self._lock = threading.Lock()

    def get_buffer(self, channel_name: str) -> Optional[StreamBuffer]:
        """Get buffer for a channel."""
        with self._lock:
            return self._buffers.get(channel_name)

    def create_buffer(self, channel_name: str, buffer_minutes: int = 4) -> StreamBuffer:
        """Create or get buffer for a channel."""
        with self._lock:
            # Return existing buffer if already created
            if channel_name in self._buffers:
                return self._buffers[channel_name]
            # Create new buffer only if doesn't exist
            self._buffers[channel_name] = StreamBuffer(channel_name, buffer_minutes)
            return self._buffers[channel_name]

    def remove_buffer(self, channel_name: str):
        """Remove buffer for a channel."""
        with self._lock:
            if channel_name in self._buffers:
                del self._buffers[channel_name]

    def get_all_buffers(self) -> Dict[str, StreamBuffer]:
        """Get all active buffers."""
        with self._lock:
            return dict(self._buffers)

    def get_all_status(self) -> List[Dict[str, Any]]:
        """Get status of all buffers."""
        with self._lock:
            return [buf.get_status() for buf in self._buffers.values()]


# Global manager instance
clip_manager = ClipServiceManager()


def get_clip_manager() -> ClipServiceManager:
    """Get the global clip service manager."""
    return clip_manager


# -------------------------
# Utility Functions
# -------------------------


def list_clips(limit: int = 50) -> List[Dict[str, Any]]:
    """List available clips."""
    clips = []
    for filepath in sorted(CLIPS_DIR.glob("clip_*.mp4"), reverse=True)[:limit]:
        stat = filepath.stat()

        # Parse filename for metadata
        # Format: clip_channelname_YYYYMMDD_HHMMSS_clipid.mp4
        parts = filepath.stem.split("_")
        channel = parts[1] if len(parts) > 1 else "unknown"

        base_url = get_clips_base_url()
        clip_url = f"{base_url}/clips/file/{filepath.name}" if base_url else f"/clips/file/{filepath.name}"

        clips.append(
            {
                "filename": filepath.name,
                "url": clip_url,
                "channel": channel,
                "size_mb": round(stat.st_size / 1024 / 1024, 2),
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )

    return clips


def get_clip(filename: str) -> Optional[Path]:
    """Get clip file path if it exists."""
    # Security: prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        return None
    if not filename.endswith(".mp4"):
        return None

    filepath = CLIPS_DIR / filename
    if filepath.exists():
        return filepath
    return None


def delete_clip(filename: str) -> bool:
    """Delete a clip file."""
    filepath = get_clip(filename)
    if filepath:
        try:
            filepath.unlink()
            return True
        except:
            pass
    return False


def get_clips_dir() -> Path:
    """Get the clips directory path."""
    return CLIPS_DIR
