#!/bin/bash

# Create necessary directories in writable locations
mkdir -p /tmp/stwsaver/downloads
mkdir -p /tmp/stwsaver/temp

# Set environment variables for writable directories
export DOWNLOADS_DIR="/tmp/stwsaver/downloads"
export TEMP_DIR="/tmp/stwsaver/temp"

# Check if FFmpeg is available (it's pre-installed on Render)
if command -v ffmpeg &> /dev/null; then
    echo "✅ FFmpeg is available"
else
    echo "⚠️  FFmpeg not found, installing..."
    # Try to download static FFmpeg binary instead of using apt-get
    curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar xJ
    mv ffmpeg-*-static/ffmpeg /tmp/ffmpeg
    mv ffmpeg-*-static/ffprobe /tmp/ffprobe
    chmod +x /tmp/ffmpeg /tmp/ffprobe
    export PATH="/tmp:$PATH"
    echo "✅ FFmpeg installed in /tmp"
fi

# Start the application
echo "Starting STWSAVER Backend..."
uvicorn main:app --host 0.0.0.0 --port $PORT
