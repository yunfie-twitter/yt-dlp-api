# --- Builder Stage ---
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Final Stage ---
FROM python:3.11-slim

WORKDIR /app

# 必要なランタイムパッケージ + aria2 + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Deno 2.x インストール
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh

# 非rootユーザー作成
RUN useradd -m -u 1000 appuser

# Builderステージからライブラリをコピー
COPY --from=builder /install /usr/local

# アプリ用ディレクトリ準備
# /app/bin を作成して yt-dlp を配置、appuserが書き込めるようにする
RUN mkdir -p /app/bin && chown appuser:appuser /app/bin

# yt-dlp初期インストール
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /app/bin/yt-dlp && \
    chmod a+rx /app/bin/yt-dlp && \
    chown appuser:appuser /app/bin/yt-dlp

# appディレクトリをコピー
COPY ./app /app/app
COPY ./entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 設定ファイル用ディレクトリを作成して権限を付与
RUN mkdir -p /config && chown -R appuser:appuser /config

RUN chown -R appuser:appuser /app

# Denoキャッシュディレクトリ
ENV DENO_DIR=/home/appuser/.cache/deno
RUN mkdir -p $DENO_DIR && chown -R appuser:appuser /home/appuser/.cache

# aria2 設定用ディレクトリ
RUN mkdir -p /home/appuser/.aria2 && chown -R appuser:appuser /home/appuser/.aria2

# PATH設定
ENV PATH="/app/bin:$PATH"
# Denoランタイム設定 (PATHが変わったので調整不要だが環境変数は維持)
ENV YT_DLP_JS_RUNTIME=deno:/usr/local/bin/deno

USER appuser

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
