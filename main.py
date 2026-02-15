# File: main.py
import os
import uuid
import json
import shutil
import asyncio
import aiohttp
import aiofiles
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
import tempfile
import re
import requests
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
import uvicorn

# For video downloading
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False
    print("Warning: yt_dlp not installed")

# For Instagram - improved import handling
try:
    from instagrapi import Client
    from instagrapi.exceptions import LoginRequired, ClientError, MediaNotFound
    INSTAGRAPI_AVAILABLE = True
except ImportError:
    INSTAGRAPI_AVAILABLE = False
    print("Warning: instagrapi not installed")

# Alternative Instagram download method using yt-dlp
try:
    import yt_dlp
    YT_DLP_INSTAGRAM_AVAILABLE = True
except ImportError:
    YT_DLP_INSTAGRAM_AVAILABLE = False

# ======================
# MODELS
# ======================

class DownloadRequest(BaseModel):
    url: str
    format: str = "mp4"
    quality: Optional[str] = None

class VideoInfo(BaseModel):
    title: str
    duration: int
    thumbnail: Optional[str] = None
    author: Optional[str] = None
    available_formats: List[Dict[str, Any]]
    platform: str

class ConversionStatus(BaseModel):
    file_id: str
    progress: int
    status: str
    download_url: Optional[str] = None
    filename: Optional[str] = None
    estimated_time: Optional[int] = None
    message: Optional[str] = None

# ======================
# APP SETUP
# ======================

app = FastAPI(
    title="STWSAVER API",
    description="Backend for downloading YouTube and Instagram videos",
    version="1.0.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================
# CONFIGURATION
# ======================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', os.path.join(BASE_DIR, "downloads"))
TEMP_DIR = os.getenv('TEMP_DIR', os.path.join(BASE_DIR, "temp"))

# Create directories
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Cleanup settings
CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', '300'))
MAX_FILE_AGE = int(os.getenv('MAX_FILE_AGE', '300'))

# Storage
conversion_statuses: Dict[str, ConversionStatus] = {}
download_tasks: Dict[str, asyncio.Task] = {}

# Instagram client (initialize lazily)
_instagram_client = None

def get_instagram_client():
    """Lazy initialization of Instagram client"""
    global _instagram_client
    if _instagram_client is None and INSTAGRAPI_AVAILABLE:
        try:
            _instagram_client = Client()
            # You might want to login here if you have credentials
            # _instagram_client.login(username="your_username", password="your_password")
        except Exception as e:
            print(f"Failed to initialize Instagram client: {e}")
    return _instagram_client

# ======================
# UTILITY FUNCTIONS
# ======================

def generate_file_id():
    return f"stwsaver_{uuid.uuid4().hex[:12]}"

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be safe for all operating systems"""
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Replace spaces with underscores
    filename = filename.replace(' ', '_')
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:195] + ext
    return filename

def get_file_path(file_id: str, filename: str) -> str:
    return os.path.join(DOWNLOADS_DIR, file_id, filename)

def cleanup_old_files():
    """Remove files older than MAX_FILE_AGE seconds"""
    try:
        current_time = datetime.now()
        
        # Clean downloads directory
        if os.path.exists(DOWNLOADS_DIR):
            for file_id_dir in os.listdir(DOWNLOADS_DIR):
                dir_path = os.path.join(DOWNLOADS_DIR, file_id_dir)
                
                if os.path.isdir(dir_path):
                    dir_time = datetime.fromtimestamp(os.path.getctime(dir_path))
                    age = (current_time - dir_time).total_seconds()
                    
                    if age > MAX_FILE_AGE:
                        print(f"Cleaning up: {file_id_dir}")
                        shutil.rmtree(dir_path, ignore_errors=True)
                        
                        if file_id_dir in conversion_statuses:
                            del conversion_statuses[file_id_dir]
        
        # Clean temp directory
        if os.path.exists(TEMP_DIR):
            for temp_file in os.listdir(TEMP_DIR):
                temp_path = os.path.join(TEMP_DIR, temp_file)
                
                if os.path.isfile(temp_path):
                    file_time = datetime.fromtimestamp(os.path.getctime(temp_path))
                    age = (current_time - file_time).total_seconds()
                    
                    if age > MAX_FILE_AGE:
                        os.remove(temp_path)
                        
    except Exception as e:
        print(f"Cleanup error: {e}")

async def periodic_cleanup():
    """Background task to periodically clean up old files"""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        cleanup_old_files()

def detect_platform(url: str) -> str:
    """Detect which platform the URL is from"""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    elif "instagram.com" in url_lower:
        return "instagram"
    elif "tiktok.com" in url_lower:
        return "tiktok"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower:
        return "facebook"
    else:
        raise ValueError("Unsupported platform. Currently supported: YouTube, Instagram")

def extract_instagram_shortcode(url: str) -> Optional[str]:
    """Extract Instagram shortcode from URL"""
    patterns = [
        r'instagram\.com/p/([A-Za-z0-9_-]+)',
        r'instagram\.com/reel/([A-Za-z0-9_-]+)',
        r'instagram\.com/tv/([A-Za-z0-9_-]+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

# ======================
# YOUTUBE DOWNLOADER
# ======================

class YouTubeDownloader:
    
    @staticmethod
    def get_video_info(url: str) -> VideoInfo:
        if not YT_DLP_AVAILABLE:
            raise HTTPException(status_code=500, detail="YouTube downloader not available")
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                available_formats = []
                
                if 'formats' in info:
                    for fmt in info['formats']:
                        if fmt.get('vcodec') != 'none' or fmt.get('acodec') != 'none':
                            quality = fmt.get('format_note', fmt.get('height', 'unknown'))
                            ext = fmt.get('ext', 'mp4')
                            
                            available_formats.append({
                                'format_id': fmt.get('format_id'),
                                'quality': str(quality),
                                'extension': ext,
                                'filesize': fmt.get('filesize'),
                                'video_codec': fmt.get('vcodec'),
                                'audio_codec': fmt.get('acodec'),
                                'has_video': fmt.get('vcodec') != 'none',
                                'has_audio': fmt.get('acodec') != 'none',
                            })
                
                # Sort by quality (height) if available
                available_formats.sort(key=lambda x: (
                    int(x['quality'].replace('p', '')) if str(x['quality']).replace('p', '').isdigit() else 0
                ), reverse=True)
                
                return VideoInfo(
                    title=info.get('title', 'Unknown Title'),
                    duration=info.get('duration', 0),
                    thumbnail=info.get('thumbnail'),
                    author=info.get('uploader', 'Unknown Author'),
                    available_formats=available_formats,
                    platform="youtube"
                )
                
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to get video info: {str(e)}")
    
    @staticmethod
    async def download_video(
        url: str, 
        file_id: str, 
        format_type: str = "mp4", 
        quality: Optional[str] = None
    ) -> Dict:
        if not YT_DLP_AVAILABLE:
            raise HTTPException(status_code=500, detail="YouTube downloader not available")
        
        output_dir = os.path.join(DOWNLOADS_DIR, file_id)
        os.makedirs(output_dir, exist_ok=True)
        
        # Update status
        conversion_statuses[file_id] = ConversionStatus(
            file_id=file_id,
            progress=5,
            status="initializing",
            download_url=None,
            filename=None,
            estimated_time=120,
            message="Starting YouTube download..."
        )
        
        # Configure yt-dlp options
        ydl_opts = {
            'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [lambda d: YouTubeDownloader.progress_hook(d, file_id)],
        }
        
        # Format selection based on user preference
        if format_type == "mp3":
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'keepvideo': False,
            })
        else:
            if quality:
                # Extract numeric quality if it has 'p' suffix
                quality_num = quality.replace('p', '') if quality else ''
                if quality_num.isdigit():
                    ydl_opts['format'] = f'bestvideo[height<={quality_num}]+bestaudio/best'
                else:
                    ydl_opts['format'] = 'bestvideo+bestaudio/best'
            else:
                ydl_opts['format'] = 'bestvideo+bestaudio/best'
            
            ydl_opts['merge_output_format'] = 'mp4'
        
        try:
            # Extract info first
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                conversion_statuses[file_id].progress = 10
                conversion_statuses[file_id].status = "fetching_info"
                conversion_statuses[file_id].message = "Fetching video information..."
                
                info = ydl.extract_info(url, download=False)
                title = sanitize_filename(info.get('title', 'video'))
                
                conversion_statuses[file_id].progress = 20
                conversion_statuses[file_id].status = "downloading"
                conversion_statuses[file_id].message = "Downloading video..."
            
            # Download the video
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await asyncio.to_thread(ydl.download, [url])
            
            # Find downloaded file
            downloaded_files = os.listdir(output_dir)
            if not downloaded_files:
                raise Exception("No file was downloaded")
            
            # Get the most recent file
            filename = max(
                downloaded_files,
                key=lambda f: os.path.getctime(os.path.join(output_dir, f))
            )
            
            # Handle different extensions based on format
            if format_type == "mp3" and not filename.endswith('.mp3'):
                # If yt-dlp didn't convert to mp3 properly, we need to convert
                file_path = os.path.join(output_dir, filename)
                audio_filename = filename.rsplit('.', 1)[0] + '.mp3'
                audio_path = os.path.join(output_dir, audio_filename)
                
                conversion_statuses[file_id].progress = 80
                conversion_statuses[file_id].status = "converting"
                conversion_statuses[file_id].message = "Converting to MP3..."
                
                try:
                    # Convert to MP3 using FFmpeg
                    cmd = ['ffmpeg', '-i', file_path, '-q:a', '0', '-map', 'a', audio_path, '-y']
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await process.communicate()
                    
                    if os.path.exists(audio_path):
                        os.remove(file_path)
                        filename = audio_filename
                except Exception as e:
                    print(f"FFmpeg conversion error: {e}")
                    # Keep the original file if conversion fails
            
            download_url = f"/download/{file_id}/{filename}"
            
            conversion_statuses[file_id] = ConversionStatus(
                file_id=file_id,
                progress=100,
                status="completed",
                download_url=download_url,
                filename=filename,
                estimated_time=0,
                message="Download completed successfully!"
            )
            
            return {
                "file_id": file_id,
                "filename": filename,
                "download_url": download_url,
                "title": title,
                "format": format_type,
                "quality": quality,
                "platform": "youtube"
            }
                
        except Exception as e:
            conversion_statuses[file_id] = ConversionStatus(
                file_id=file_id,
                progress=0,
                status="failed",
                download_url=None,
                filename=None,
                estimated_time=0,
                message=f"Download failed: {str(e)}"
            )
            raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")
    
    @staticmethod
    def progress_hook(d, file_id):
        """Progress hook for yt-dlp"""
        if file_id not in conversion_statuses:
            return
            
        if d['status'] == 'downloading':
            if 'total_bytes' in d and d['total_bytes'] > 0:
                progress = int((d['downloaded_bytes'] / d['total_bytes']) * 100)
                conversion_statuses[file_id].progress = min(80, 20 + int(progress * 0.6))
            elif 'total_bytes_estimate' in d and d['total_bytes_estimate'] > 0:
                progress = int((d['downloaded_bytes'] / d['total_bytes_estimate']) * 100)
                conversion_statuses[file_id].progress = min(80, 20 + int(progress * 0.6))
        
        elif d['status'] == 'finished':
            conversion_statuses[file_id].progress = 80
            conversion_statuses[file_id].status = "finalizing"
            conversion_statuses[file_id].message = "Processing downloaded file..."

# ======================
# IMPROVED INSTAGRAM DOWNLOADER
# ======================

class InstagramDownloader:
    
    def __init__(self):
        self.client = None
        if INSTAGRAPI_AVAILABLE:
            try:
                self.client = Client()
                # You can add login credentials here if needed
                # self.client.login(username="your_username", password="your_password")
            except Exception as e:
                print(f"Instagram client initialization error: {e}")
    
    def get_video_info(self, url: str) -> VideoInfo:
        """Get Instagram video information using multiple methods"""
        
        # Try with instagrapi first
        if INSTAGRAPI_AVAILABLE and self.client:
            try:
                return self._get_info_instagrapi(url)
            except Exception as e:
                print(f"instagrapi failed: {e}")
        
        # Fallback to yt-dlp
        if YT_DLP_AVAILABLE:
            try:
                return self._get_info_ytdlp(url)
            except Exception as e:
                print(f"yt-dlp fallback failed: {e}")
        
        raise HTTPException(status_code=500, detail="Instagram downloader not available")
    
    def _get_info_instagrapi(self, url: str) -> VideoInfo:
        """Get video info using instagrapi"""
        try:
            # Extract media ID from URL
            media_id = self.client.media_pk_from_url(url)
            media_info = self.client.media_info(media_id)
            
            available_formats = []
            
            # Check if it's a video
            if media_info.media_type == 2:  # 2 = video
                if hasattr(media_info, 'video_versions') and media_info.video_versions:
                    for i, video in enumerate(media_info.video_versions):
                        available_formats.append({
                            'format_id': f"video_{i}",
                            'quality': f"{video.get('height', 0)}p",
                            'extension': 'mp4',
                            'filesize': None,
                            'url': video.get('url'),
                        })
            
            # Get caption/text
            caption = ""
            if hasattr(media_info, 'caption_text'):
                caption = media_info.caption_text
            elif hasattr(media_info, 'caption') and media_info.caption:
                caption = media_info.caption_text
            
            title = caption[:100] if caption else "Instagram Video"
            if len(title) < 10:  # If caption is too short, add username
                username = media_info.user.username if hasattr(media_info, 'user') else "instagram"
                title = f"{username} - Instagram Video"
            
            return VideoInfo(
                title=title,
                duration=int(getattr(media_info, 'video_duration', 0)),
                thumbnail=media_info.thumbnail_url if hasattr(media_info, 'thumbnail_url') else None,
                author=media_info.user.username if hasattr(media_info, 'user') else "Instagram User",
                available_formats=available_formats,
                platform="instagram"
            )
            
        except Exception as e:
            raise Exception(f"instagrapi info extraction failed: {str(e)}")
    
    def _get_info_ytdlp(self, url: str) -> VideoInfo:
        """Get video info using yt-dlp as fallback"""
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                available_formats = []
                
                if 'formats' in info:
                    for fmt in info['formats']:
                        if fmt.get('vcodec') != 'none':
                            quality = fmt.get('format_note', fmt.get('height', 'unknown'))
                            ext = fmt.get('ext', 'mp4')
                            
                            available_formats.append({
                                'format_id': fmt.get('format_id'),
                                'quality': str(quality),
                                'extension': ext,
                                'filesize': fmt.get('filesize'),
                                'video_codec': fmt.get('vcodec'),
                            })
                
                return VideoInfo(
                    title=info.get('title', 'Instagram Video'),
                    duration=info.get('duration', 0),
                    thumbnail=info.get('thumbnail'),
                    author=info.get('uploader', 'Instagram User'),
                    available_formats=available_formats,
                    platform="instagram"
                )
                
        except Exception as e:
            raise Exception(f"yt-dlp info extraction failed: {str(e)}")
    
    async def download_video(
        self, 
        url: str, 
        file_id: str, 
        format_type: str = "mp4", 
        quality: Optional[str] = None
    ) -> Dict:
        """Download Instagram video with multiple fallback methods"""
        
        output_dir = os.path.join(DOWNLOADS_DIR, file_id)
        os.makedirs(output_dir, exist_ok=True)
        
        # Initialize status
        conversion_statuses[file_id] = ConversionStatus(
            file_id=file_id,
            progress=5,
            status="initializing",
            download_url=None,
            filename=None,
            estimated_time=60,
            message="Starting Instagram download..."
        )
        
        # Try multiple download methods
        methods = [
            self._download_with_instagrapi,
            self._download_with_ytdlp,
            self._download_with_requests_fallback
        ]
        
        last_error = None
        for method in methods:
            try:
                conversion_statuses[file_id].message = f"Trying {method.__name__.replace('_download_with_', '')} method..."
                result = await method(url, file_id, output_dir, format_type, quality)
                if result:
                    return result
            except Exception as e:
                last_error = e
                print(f"Download method {method.__name__} failed: {e}")
                continue
        
        # If all methods failed
        error_msg = f"All download methods failed. Last error: {last_error}"
        conversion_statuses[file_id].status = "failed"
        conversion_statuses[file_id].message = error_msg
        raise HTTPException(status_code=500, detail=error_msg)
    
    async def _download_with_instagrapi(self, url, file_id, output_dir, format_type, quality):
        """Download using instagrapi"""
        if not INSTAGRAPI_AVAILABLE or not self.client:
            raise Exception("instagrapi not available")
        
        conversion_statuses[file_id].progress = 10
        conversion_statuses[file_id].message = "Fetching Instagram video info..."
        
        try:
            # Get media info
            media_id = self.client.media_pk_from_url(url)
            media_info = self.client.media_info(media_id)
            
            if media_info.media_type != 2:  # Not a video
                raise Exception("Not a video post")
            
            conversion_statuses[file_id].progress = 30
            conversion_statuses[file_id].message = "Downloading video..."
            
            # Get video URL
            if not hasattr(media_info, 'video_versions') or not media_info.video_versions:
                raise Exception("No video versions found")
            
            # Select quality
            video_url = media_info.video_versions[0].url
            if quality and media_info.video_versions:
                # Try to find matching quality
                quality_num = int(quality.replace('p', '')) if quality else 0
                for video in media_info.video_versions:
                    if video.get('height', 0) <= quality_num + 100:  # Allow some flexibility
                        video_url = video.get('url')
                        break
            
            # Generate filename
            username = media_info.user.username if hasattr(media_info, 'user') else "instagram"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{username}_instagram_{timestamp}.mp4"
            filename = sanitize_filename(filename)
            file_path = os.path.join(output_dir, filename)
            
            # Download video with progress tracking
            async with aiohttp.ClientSession() as session:
                async with session.get(video_url) as resp:
                    if resp.status != 200:
                        raise Exception(f"Download failed with status {resp.status}")
                    
                    total_size = int(resp.headers.get('content-length', 0))
                    downloaded = 0
                    chunk_size = 8192
                    
                    async with aiofiles.open(file_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            await f.write(chunk)
                            downloaded += len(chunk)
                            
                            if total_size > 0:
                                progress = 30 + int((downloaded / total_size) * 40)
                                conversion_statuses[file_id].progress = min(70, progress)
            
            conversion_statuses[file_id].progress = 70
            
            # Convert to MP3 if requested
            if format_type == "mp3":
                conversion_statuses[file_id].status = "converting"
                conversion_statuses[file_id].message = "Converting to MP3..."
                
                audio_filename = filename.replace('.mp4', '.mp3')
                audio_path = os.path.join(output_dir, audio_filename)
                
                try:
                    # Convert using FFmpeg
                    cmd = ['ffmpeg', '-i', file_path, '-q:a', '0', '-map', 'a', audio_path, '-y']
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await process.communicate()
                    
                    if os.path.exists(audio_path):
                        os.remove(file_path)
                        filename = audio_filename
                        
                except Exception as e:
                    print(f"FFmpeg conversion error: {e}")
                    format_type = "mp4"  # Fallback to MP4
            
            download_url = f"/download/{file_id}/{filename}"
            
            conversion_statuses[file_id] = ConversionStatus(
                file_id=file_id,
                progress=100,
                status="completed",
                download_url=download_url,
                filename=filename,
                estimated_time=0,
                message="Download completed successfully!"
            )
            
            return {
                "file_id": file_id,
                "filename": filename,
                "download_url": download_url,
                "title": f"{username}_instagram",
                "format": format_type,
                "quality": quality,
                "platform": "instagram"
            }
            
        except Exception as e:
            raise Exception(f"instagrapi download failed: {str(e)}")
    
    async def _download_with_ytdlp(self, url, file_id, output_dir, format_type, quality):
        """Download using yt-dlp"""
        if not YT_DLP_AVAILABLE:
            raise Exception("yt-dlp not available")
        
        conversion_statuses[file_id].progress = 10
        conversion_statuses[file_id].message = "Using yt-dlp for Instagram download..."
        
        ydl_opts = {
            'outtmpl': os.path.join(output_dir, '%(title)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [lambda d: self._ytdlp_progress_hook(d, file_id)],
        }
        
        if format_type == "mp3":
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            ydl_opts['format'] = 'best[height<=?1080]'
        
        try:
            # Extract info first
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = sanitize_filename(info.get('title', 'instagram_video'))
            
            # Download
            conversion_statuses[file_id].progress = 20
            conversion_statuses[file_id].status = "downloading"
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await asyncio.to_thread(ydl.download, [url])
            
            # Find downloaded file
            files = os.listdir(output_dir)
            if not files:
                raise Exception("No file downloaded")
            
            filename = max(files, key=lambda f: os.path.getctime(os.path.join(output_dir, f)))
            download_url = f"/download/{file_id}/{filename}"
            
            conversion_statuses[file_id] = ConversionStatus(
                file_id=file_id,
                progress=100,
                status="completed",
                download_url=download_url,
                filename=filename,
                estimated_time=0,
                message="Download completed with yt-dlp!"
            )
            
            return {
                "file_id": file_id,
                "filename": filename,
                "download_url": download_url,
                "title": title,
                "format": format_type,
                "quality": quality,
                "platform": "instagram"
            }
            
        except Exception as e:
            raise Exception(f"yt-dlp download failed: {str(e)}")
    
    def _ytdlp_progress_hook(self, d, file_id):
        """Progress hook for yt-dlp downloads"""
        if file_id not in conversion_statuses:
            return
            
        if d['status'] == 'downloading':
            if 'total_bytes' in d and d['total_bytes'] > 0:
                progress = int((d['downloaded_bytes'] / d['total_bytes']) * 100)
                conversion_statuses[file_id].progress = min(80, 20 + int(progress * 0.6))
    
    async def _download_with_requests_fallback(self, url, file_id, output_dir, format_type, quality):
        """Ultimate fallback: try to extract video URL and download with requests"""
        conversion_statuses[file_id].progress = 10
        conversion_statuses[file_id].message = "Using fallback download method..."
        
        try:
            # Extract shortcode
            shortcode = extract_instagram_shortcode(url)
            if not shortcode:
                raise Exception("Could not extract Instagram shortcode")
            
            # Try to get video URL from oEmbed or public endpoints
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            # Try Instagram's oEmbed endpoint
            oembed_url = f"https://api.instagram.com/oembed/?url=http://instagram.com/p/{shortcode}"
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed_url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get('title', 'instagram_video')
            
            # For actual video download, we'll need to use a public API or service
            # This is a simplified version - in production, you might want to use a service like saveinsta.app API
            
            # As a last resort, use a public Instagram video downloader API
            # Note: You should replace this with a reliable service
            fallback_api = f"https://insta.saveinsta.app/api/ajaxSearch?insta_url={url}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(fallback_api) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Parse response to get video URL (this depends on the API)
                        # This is just an example structure
                        if 'video' in data:
                            video_url = data['video']
                            
                            # Download video
                            filename = f"instagram_{shortcode}.mp4"
                            file_path = os.path.join(output_dir, filename)
                            
                            async with session.get(video_url) as video_resp:
                                if video_resp.status == 200:
                                    async with aiofiles.open(file_path, 'wb') as f:
                                        async for chunk in video_resp.content.iter_chunked(8192):
                                            await f.write(chunk)
                            
                            download_url = f"/download/{file_id}/{filename}"
                            
                            conversion_statuses[file_id] = ConversionStatus(
                                file_id=file_id,
                                progress=100,
                                status="completed",
                                download_url=download_url,
                                filename=filename,
                                estimated_time=0,
                                message="Download completed with fallback method!"
                            )
                            
                            return {
                                "file_id": file_id,
                                "filename": filename,
                                "download_url": download_url,
                                "title": title,
                                "format": format_type,
                                "quality": quality,
                                "platform": "instagram"
                            }
            
            raise Exception("Fallback download method failed")
            
        except Exception as e:
            raise Exception(f"Fallback download failed: {str(e)}")

# ======================
# API ENDPOINTS
# ======================

@app.get("/")
async def root():
    return {
        "message": "STWSAVER API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "GET /": "API information",
            "POST /api/video-info": "Get video information",
            "POST /api/download": "Start video download",
            "GET /api/progress/{file_id}": "Check download progress",
            "GET /download/{file_id}/{filename}": "Download converted file",
            "DELETE /api/files/{file_id}": "Delete files manually",
            "GET /api/health": "Health check"
        }
    }

@app.post("/api/video-info", response_model=VideoInfo)
async def get_video_info(request: DownloadRequest):
    try:
        platform = detect_platform(request.url)
        
        if platform == "youtube":
            downloader = YouTubeDownloader()
            return downloader.get_video_info(request.url)
        elif platform == "instagram":
            downloader = InstagramDownloader()
            return downloader.get_video_info(request.url)
        else:
            # For other platforms, try using yt-dlp
            if YT_DLP_AVAILABLE:
                try:
                    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                        info = ydl.extract_info(request.url, download=False)
                        return VideoInfo(
                            title=info.get('title', 'Video'),
                            duration=info.get('duration', 0),
                            thumbnail=info.get('thumbnail'),
                            author=info.get('uploader', 'Unknown'),
                            available_formats=[],
                            platform=platform
                        )
                except:
                    pass
            raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting video info: {str(e)}")

@app.post("/api/download")
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    try:
        platform = detect_platform(request.url)
        
        file_id = generate_file_id()
        
        conversion_statuses[file_id] = ConversionStatus(
            file_id=file_id,
            progress=0,
            status="pending",
            download_url=None,
            filename=None,
            estimated_time=120,
            message="Preparing download..."
        )
        
        if platform == "youtube":
            downloader = YouTubeDownloader()
            task = asyncio.create_task(
                downloader.download_video(
                    request.url, 
                    file_id, 
                    request.format, 
                    request.quality
                )
            )
        elif platform == "instagram":
            downloader = InstagramDownloader()
            task = asyncio.create_task(
                downloader.download_video(
                    request.url, 
                    file_id, 
                    request.format, 
                    request.quality
                )
            )
        else:
            # Try with yt-dlp for other platforms
            if YT_DLP_AVAILABLE:
                downloader = YouTubeDownloader()  # Reuse YouTube downloader for yt-dlp
                task = asyncio.create_task(
                    downloader.download_video(
                        request.url, 
                        file_id, 
                        request.format, 
                        request.quality
                    )
                )
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
        
        download_tasks[file_id] = task
        
        def cleanup_task(task_file_id: str):
            if task_file_id in download_tasks:
                del download_tasks[task_file_id]
        
        task.add_done_callback(lambda t: cleanup_task(file_id))
        
        return {
            "message": "Download started",
            "file_id": file_id,
            "status_url": f"/api/progress/{file_id}",
            "estimated_time": 120,
            "platform": platform
        }
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error starting download: {str(e)}")

@app.get("/api/progress/{file_id}", response_model=ConversionStatus)
async def get_progress(file_id: str):
    if file_id not in conversion_statuses:
        raise HTTPException(status_code=404, detail="File ID not found")
    
    # Check if file still exists for completed downloads
    if (conversion_statuses[file_id].status in ["completed", "failed"] and 
        conversion_statuses[file_id].download_url):
        
        file_dir = os.path.join(DOWNLOADS_DIR, file_id)
        if not os.path.exists(file_dir):
            conversion_statuses[file_id].status = "expired"
            conversion_statuses[file_id].progress = 0
            conversion_statuses[file_id].message = "File has expired and been deleted"
    
    return conversion_statuses[file_id]

@app.get("/download/{file_id}/{filename}")
async def download_file(file_id: str, filename: str):
    file_path = get_file_path(file_id, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Update file access time to delay cleanup
    os.utime(file_path, None)
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/octet-stream'
    )

@app.delete("/api/files/{file_id}")
async def delete_files(file_id: str):
    file_dir = os.path.join(DOWNLOADS_DIR, file_id)
    
    if os.path.exists(file_dir):
        shutil.rmtree(file_dir, ignore_errors=True)
        
        if file_id in conversion_statuses:
            del conversion_statuses[file_id]
        
        return {"message": f"Files for {file_id} deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail="File ID not found")

@app.get("/api/health")
async def health_check():
    dirs_ok = all(os.path.exists(d) for d in [DOWNLOADS_DIR, TEMP_DIR])
    
    deps_ok = {
        "yt_dlp": YT_DLP_AVAILABLE,
        "instagrapi": INSTAGRAPI_AVAILABLE,
        "yt_dlp_instagram": YT_DLP_AVAILABLE,
    }
    
    active_downloads = len([s for s in conversion_statuses.values() 
                          if s.status in ["pending", "downloading", "converting", "initializing", "fetching_info"]])
    
    # Check FFmpeg availability
    ffmpeg_available = False
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        ffmpeg_available = result.returncode == 0
    except:
        pass
    
    return {
        "status": "healthy" if dirs_ok else "degraded",
        "timestamp": datetime.now().isoformat(),
        "directories": {
            "downloads": DOWNLOADS_DIR,
            "temp": TEMP_DIR,
            "exists": dirs_ok
        },
        "dependencies": {
            **deps_ok,
            "ffmpeg": ffmpeg_available
        },
        "stats": {
            "active_downloads": active_downloads,
            "total_conversions": len(conversion_statuses),
            "files_on_disk": len(os.listdir(DOWNLOADS_DIR)) if os.path.exists(DOWNLOADS_DIR) else 0
        }
    }

# ======================
# STARTUP AND SHUTDOWN
# ======================

@app.on_event("startup")
async def startup_event():
    print("=" * 50)
    print("STWSAVER Backend starting up...")
    print("=" * 50)
    print(f"Downloads directory: {DOWNLOADS_DIR}")
    print(f"Temp directory: {TEMP_DIR}")
    print(f"Cleanup interval: {CLEANUP_INTERVAL}s")
    print(f"Max file age: {MAX_FILE_AGE}s")
    print("-" * 50)
    
    # Check dependencies
    print("Dependency status:")
    print(f"  yt-dlp: {'✅ Available' if YT_DLP_AVAILABLE else '❌ Not available'}")
    print(f"  instagrapi: {'✅ Available' if INSTAGRAPI_AVAILABLE else '❌ Not available'}")
    
    # Check FFmpeg
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            print("  FFmpeg: ✅ Available")
        else:
            print("  FFmpeg: ❌ Not available (MP3 conversion may fail)")
    except:
        print("  FFmpeg: ❌ Not available (MP3 conversion may fail)")
    
    print("-" * 50)
    
    # Start cleanup task
    asyncio.create_task(periodic_cleanup())
    
    # Initial cleanup
    cleanup_old_files()
    
    print("Backend started successfully!")
    print("=" * 50)

@app.on_event("shutdown")
async def shutdown_event():
    print("STWSAVER Backend shutting down...")
    
    # Cancel all running tasks
    for task in download_tasks.values():
        task.cancel()
    
    if download_tasks:
        await asyncio.gather(*download_tasks.values(), return_exceptions=True)
    
    print("Backend shutdown complete.")

# ======================
# MAIN ENTRY POINT
# ======================

if __name__ == "__main__":
    port = int(os.getenv('PORT', 13959))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
