# Framesift CLI image for NAS use (linux/amd64 and linux/arm64). No GUI inside.
#   docker run --rm --user 1026:100 -v /volume1/photo:/data ghcr.io/henzel/framesift:latest scan --source /data/Archive
FROM python:3.12-slim-bookworm AS base

ARG TARGETARCH
ARG FFMPEG_SERIES=master
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FRAMESIFT_IN_DOCKER=1

# exiftool (Perl) from Debian; ffmpeg/ffprobe as BtbN LGPL static builds (no GPL components)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl curl xz-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && case "$TARGETARCH" in \
      amd64) FF=linux64 ;; \
      arm64) FF=linuxarm64 ;; \
      *) echo "unsupported arch $TARGETARCH" && exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-${FFMPEG_SERIES}-latest-${FF}-lgpl.tar.xz" -o /tmp/ffmpeg.tar.xz \
 && mkdir -p /tmp/ffmpeg && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
 && install -m 0755 /tmp/ffmpeg/bin/ffmpeg /tmp/ffmpeg/bin/ffprobe /usr/local/bin/ \
 && mkdir -p /usr/local/share/licenses/ffmpeg && cp /tmp/ffmpeg/LICENSE.txt /usr/local/share/licenses/ffmpeg/ \
 && rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz \
 && apt-get purge -y curl xz-utils && apt-get autoremove -y

WORKDIR /src
COPY pyproject.toml README.md LICENSE* ./
COPY framesift ./framesift
RUN pip install . && rm -rf /src

# Writable home/cache for arbitrary --user ids (Synology: --user 1026:100)
ENV HOME=/tmp/framesift-home \
    XDG_CACHE_HOME=/tmp/framesift-home/.cache \
    XDG_CONFIG_HOME=/tmp/framesift-home/.config
RUN mkdir -p /tmp/framesift-home && chmod 1777 /tmp/framesift-home
WORKDIR /data
ENTRYPOINT ["framesift"]
CMD ["--help"]
