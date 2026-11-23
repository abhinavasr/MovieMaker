# Burmese speech engine for Story Studio: VoxCPM2 (Apache-2.0) + a CPU Burmese ASR.
# Pinned from the environment measured in the 2026-11-20 bake-off (~/mm-tts-bakeoff).
FROM python:3.11-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
COPY requirements.lock /tmp/requirements.lock
RUN pip install --extra-index-url https://download.pytorch.org/whl/cu128 -r /tmp/requirements.lock
WORKDIR /studio-tts-my
