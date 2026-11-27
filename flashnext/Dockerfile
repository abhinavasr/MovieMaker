# Qwen3.8-Flash-Next for the podcast pipeline (2026-11-27, the user chose "podcasts on Flash-Next,
# chats on the 27B"). llama.cpp's server, started on demand by llama-swap and stopped when idle,
# so the GPU is free for WIS and Ollama whenever no podcast is thinking.
FROM ghcr.io/ggml-org/llama.cpp:server-cuda
ARG SWAP_VERSION=277
RUN curl -fsSL "https://github.com/mostlygeek/llama-swap/releases/download/v${SWAP_VERSION}/llama-swap_${SWAP_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin llama-swap && chmod +x /usr/local/bin/llama-swap
COPY config.yaml /etc/llama-swap.yaml
ENTRYPOINT ["/usr/local/bin/llama-swap", "--config", "/etc/llama-swap.yaml", "--listen", "0.0.0.0:8080"]
