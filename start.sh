#!/bin/bash

# Make script exit on error
set -e

echo "🚀 Starting STWSAVER Backend Deployment..."
echo "=========================================="

# Create necessary directories in writable location
echo "📁 Creating directories..."
mkdir -p /tmp/stwsaver/downloads
mkdir -p /tmp/stwsaver/temp

# Set environment variables for writable directories
export DOWNLOADS_DIR="/tmp/stwsaver/downloads"
export TEMP_DIR="/tmp/stwsaver/temp"
export MAX_FILE_AGE="300"
export CLEANUP_INTERVAL="300"

echo "✅ Downloads directory: $DOWNLOADS_DIR"
echo "✅ Temp directory: $TEMP_DIR"

# Check if running on Render
if [ -n "$RENDER" ]; then
    echo "🖥️  Running on Render.com"
else
    echo "🖥️  Running locally"
fi

# Check Python version
echo "🐍 Python version: $(python --version)"

# Check installed packages
echo "📦 Installed packages:"
pip list | grep -E "fastapi|uvicorn|yt-dlp|instagrapi|aiohttp"

# Test FFmpeg availability (it's pre-installed on Render)
echo "🎬 Checking FFmpeg..."
if command -v ffmpeg &> /dev/null; then
    echo "✅ FFmpeg is available: $(ffmpeg -version | head -n1)"
else
    echo "⚠️  FFmpeg not found - MP3 conversion may not work"
fi

# Test directory write permissions
echo "📝 Testing write permissions..."
touch /tmp/stwsaver/test.txt && echo "✅ Can write to /tmp/stwsaver" || echo "❌ Cannot write to /tmp/stwsaver"
rm -f /tmp/stwsaver/test.txt

echo "=========================================="
echo "✅ Setup complete! Starting server..."
echo "=========================================="

# Start the application
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --timeout-keep-alive 120
