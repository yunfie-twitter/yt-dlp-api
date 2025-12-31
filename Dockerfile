# --- Builder Stage ---
FROM python:3.11-slim AS builder

WORKDIR /build

# ビルド用パッケージインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# --- Final Stage ---
FROM python:3.11-slim

WORKDIR /app

# 必要なランタイムパッケージをインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Deno 2.x インストール
# DENO_INSTALLを/usr/localに設定すると、バイナリは /usr/local/bin/deno に直接配置されます
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh && \
    deno --version

# yt-dlp最新版をインストール
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp && \
    yt-dlp --version

# Denoランタイムを明示的に設定
ENV YT_DLP_JS_RUNTIME=deno:/usr/local/bin/deno

# Builderステージからpython依存関係をコピー
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# アプリケーションコードをコピー
COPY ./app /app

# 非rootユーザー作成とディレクトリ権限の設定
RUN useradd -m -u 1000 appuser && \
    mkdir -p /home/appuser/.cache/deno && \
    chown -R appuser:appuser /app /home/appuser/.cache

USER appuser

# Denoのキャッシュディレクトリ環境変数
ENV DENO_DIR=/home/appuser/.cache/deno

# ヘルスチェック (起動確認)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ポート公開
EXPOSE 8000

# Uvicorn起動
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
