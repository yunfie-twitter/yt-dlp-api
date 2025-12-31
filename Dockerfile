# --- Builder Stage ---
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --user ではなく直接インストール(パスを固定するため)
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Final Stage ---
FROM python:3.11-slim

WORKDIR /app

# 必要なランタイムパッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Deno 2.x インストール
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh

# yt-dlpインストール
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

# Denoランタイム設定
ENV YT_DLP_JS_RUNTIME=deno:/usr/local/bin/deno

# 非rootユーザー作成
RUN useradd -m -u 1000 appuser

# Builderステージからライブラリをコピーし、権限をappuserに譲渡する
# /usr/local にコピーすることで、パスを意識せずに利用可能にします
COPY --from=builder /install /usr/local
RUN chown -R appuser:appuser /app

# Denoキャッシュディレクトリ
ENV DENO_DIR=/home/appuser/.cache/deno
RUN mkdir -p $DENO_DIR && chown -R appuser:appuser /home/appuser/.cache

USER appuser

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# app.main:app として正しいモジュールパスを指定
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]