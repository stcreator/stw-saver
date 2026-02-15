#!/bin/bash

# Create necessary directories
mkdir -p downloads temp

# Install FFmpeg (required for audio conversion)
apt-get update && apt-get install -y ffmpeg

# Start the application
uvicorn main:app --host 0.0.0.0 --port $PORT
