#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENDOR_DIR="$PROJECT_ROOT/vendor"
FFMPEG_DIR="$VENDOR_DIR/ffmpeg"
WHISPER_DIR="$VENDOR_DIR/whisper.cpp"
MODEL_DIR="$PROJECT_ROOT/models/whisper"

WHISPER_VERSION="${WHISPER_VERSION:-v1.8.6}"
LANGUAGE="${1:-auto}"

resolve_model_size() {
    local lang="${1,,}"
    case "$lang" in
        auto|en|english)    echo "base" ;;
        es|spanish|fr|french|de|german|pt|portuguese|it|italian|ru|russian|zh|chinese|ja|japanese|ko|korean|ar|arabic|hi|hindi) echo "small" ;;
        *)                  echo "base" ;;
    esac
}

resolve_model_path() {
    local size="$1"
    case "$size" in
        tiny)   echo "$MODEL_DIR/ggml-tiny.bin" ;;
        base)   echo "$MODEL_DIR/ggml-base.bin" ;;
        small)  echo "$MODEL_DIR/ggml-small.bin" ;;
        medium) echo "$MODEL_DIR/ggml-medium.bin" ;;
        large)  echo "$MODEL_DIR/ggml-large-v3.bin" ;;
        *)      echo "$MODEL_DIR/ggml-base.bin" ;;
    esac
}

detect_platform() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os" in
        Linux)  [[ "$arch" == "x86_64" ]] && echo "linux-x64" || echo "linux-arm64" ;;
        Darwin) [[ "$arch" == "arm64" ]] && echo "macos-arm64" || echo "macos-x64" ;;
        *)      echo "Unsupported OS: $os" >&2; exit 1 ;;
    esac
}

download_ffmpeg() {
    echo "==> Downloading ffmpeg..."
    mkdir -p "$FFMPEG_DIR"

    if [[ -x "$FFMPEG_DIR/ffmpeg" ]]; then
        echo "    Already exists at $FFMPEG_DIR/ffmpeg — skipping."
        FFMPEG_PATH="$FFMPEG_DIR/ffmpeg"
        return
    fi

    local platform
    platform="$(detect_platform)"

    local url
    case "$platform" in
        linux-x64)   url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" ;;
        linux-arm64) url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz" ;;
        macos-x64)   url="https://evermeet.cx/ffmpeg/ffmpeg-7.1.1.zip" ;;
        macos-arm64) url="https://evermeet.cx/ffmpeg/ffmpeg-7.1.1.zip" ;;
    esac

    local tmp_dir
    tmp_dir="$(mktemp -d)"
    trap "rm -rf '$tmp_dir'" RETURN

    if [[ "$platform" == macos-* ]]; then
        curl -fSL "$url" -o "$tmp_dir/ffmpeg.zip"
        unzip -qo "$tmp_dir/ffmpeg.zip" -d "$FFMPEG_DIR"
    else
        curl -fSL "$url" -o "$tmp_dir/ffmpeg.tar.xz"
        tar -xf "$tmp_dir/ffmpeg.tar.xz" -C "$tmp_dir"
        cp "$tmp_dir"/ffmpeg-*/bin/ffmpeg "$FFMPEG_DIR/ffmpeg"
        cp "$tmp_dir"/ffmpeg-*/bin/ffprobe "$FFMPEG_DIR/ffprobe"
    fi

    chmod +x "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe"
    FFMPEG_PATH="$FFMPEG_DIR/ffmpeg"
    echo "    ffmpeg -> $FFMPEG_DIR/ffmpeg"
    echo "    ffprobe -> $FFMPEG_DIR/ffprobe"
}

check_build_deps() {
    local missing=()
    command -v cmake >/dev/null 2>&1 || missing+=(cmake)
    command -v make  >/dev/null 2>&1 || missing+=(make)
    command -v gcc   >/dev/null 2>&1 || missing+=(gcc)
    if (( ${#missing[@]} > 0 )); then
        echo "ERROR: missing build dependencies: ${missing[*]}" >&2
        echo "Install them and re-run." >&2
        exit 1
    fi
}

build_whisper_cpp() {
    echo "==> Building whisper.cpp ${WHISPER_VERSION} (static)..."
    check_build_deps

    local tmp_dir
    tmp_dir="$(mktemp -d)"

    echo "    Cloning whisper.cpp ${WHISPER_VERSION}..."
    git clone --depth 1 --branch "$WHISPER_VERSION" \
        https://github.com/ggml-org/whisper.cpp.git "$tmp_dir" 2>/dev/null

    echo "    Configuring (BUILD_SHARED_LIBS=0)..."
    cmake -B "$tmp_dir/build" -S "$tmp_dir" \
        -DBUILD_SHARED_LIBS=0 \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CCACHE=OFF \
        >/dev/null 2>&1

    echo "    Compiling ($(nproc) threads)..."
    cmake --build "$tmp_dir/build" -j"$(nproc)" --config Release >/dev/null 2>&1

    mkdir -p "$WHISPER_DIR"
    cp "$tmp_dir/build/bin/whisper-cli" "$WHISPER_DIR/whisper-cli.new"
    mv -f "$WHISPER_DIR/whisper-cli.new" "$WHISPER_DIR/whisper-cli"
    cp "$tmp_dir/build/bin/whisper-server" "$WHISPER_DIR/whisper-server.new"
    mv -f "$WHISPER_DIR/whisper-server.new" "$WHISPER_DIR/whisper-server"
    ln -sf whisper-cli "$WHISPER_DIR/main"
    chmod +x "$WHISPER_DIR/whisper-cli" "$WHISPER_DIR/whisper-server"

    rm -rf "$tmp_dir"
    echo "    whisper.cpp -> $WHISPER_DIR/whisper-cli"
    echo "    whisper-server -> $WHISPER_DIR/whisper-server"
}

download_model() {
    local model_size
    model_size="$(resolve_model_size "$LANGUAGE")"
    local model_path
    model_path="$(resolve_model_path "$model_size")"

    if [[ -n "${KAI_WAHA_WHISPER_MODEL_PATH:-}" ]]; then
        model_path="$KAI_WAHA_WHISPER_MODEL_PATH"
    fi

    WHISPER_MODEL="$model_path"
    mkdir -p "$MODEL_DIR"

    echo "==> Downloading whisper model (ggml-${model_size}.bin for language: ${LANGUAGE})..."

    if [[ -f "$model_path" ]]; then
        echo "    Model already exists at $model_path — skipping."
        return
    fi

    local model_url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${model_size}.bin"
    curl -fSL "$model_url" -o "$model_path"
    echo "    model -> $model_path"
}

echo "Kai media setup"
echo "==============="
echo "  Language:  $LANGUAGE"
echo "  Whisper:   $WHISPER_VERSION"
echo ""

download_ffmpeg
build_whisper_cpp
download_model

echo ""
echo "Done. Paths:"
echo "  ffmpeg:        $FFMPEG_PATH"
echo "  whisper.cpp:   $WHISPER_DIR/whisper-cli"
echo "  whisper-server: $WHISPER_DIR/whisper-server"
echo "  whisper model: $WHISPER_MODEL"
