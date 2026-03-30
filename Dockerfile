FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    less \
    make \
    openssh-client \
    procps \
    python3 \
    python3-pip \
    python3-yaml \
    python3-venv \
    ripgrep \
    tmux \
    zsh \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/zsh sandbox

WORKDIR /workspace

COPY docker/sandbox-entrypoint.sh /usr/local/bin/sandbox-entrypoint.sh
RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

USER sandbox

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
CMD ["sleep", "infinity"]
