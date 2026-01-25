#!/bin/bash
set -e

# Update yt-dlp if possible
# Note: The Dockerfile puts the initial binary in /app/bin/yt-dlp owned by appuser
if [ -w "/app/bin/yt-dlp" ]; then
    echo "Checking for yt-dlp updates..."
    # Using curl to download latest release (safer than -U inside container sometimes)
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /app/bin/yt-dlp
    chmod a+rx /app/bin/yt-dlp
    echo "yt-dlp updated."
    /app/bin/yt-dlp --version
else
    echo "Warning: /app/bin/yt-dlp is not writable. Skipping update."
fi

# Execute CMD
exec "$@"
