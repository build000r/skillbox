FROM node:22-trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GH_AXI_DISABLE_HOOKS=1 \
    PATH=/home/sandbox/.local/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    bat \
    build-essential \
    ca-certificates \
    curl \
    direnv \
    fd-find \
    fzf \
    git \
    golang-go \
    gh \
    jq \
    less \
    libssl-dev \
    make \
    openssh-client \
    pipx \
    pkg-config \
    procps \
    python3 \
    python3-pip \
    python3-yaml \
    python3-venv \
    ripgrep \
    rustc \
    cargo \
    tmux \
    zsh \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g \
    @anthropic-ai/claude-code@2.1.160 \
    @openai/codex@0.136.0 \
    gh-axi@0.1.11 \
    spaps@0.9.3

RUN useradd --create-home --shell /bin/zsh sandbox \
 && ln -s /home/sandbox /home/skillbox \
 && ln -s /home/sandbox/.claude/.claude.json /home/sandbox/.claude.json
RUN install -d -o sandbox -g sandbox /home/sandbox/.local/bin \
 && ln -sf /usr/bin/fdfind /home/sandbox/.local/bin/fd \
 && ln -sf /usr/bin/batcat /home/sandbox/.local/bin/bat

WORKDIR /workspace

COPY docker/sandbox-entrypoint.sh /usr/local/bin/sandbox-entrypoint.sh
RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

USER sandbox

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
CMD ["sleep", "infinity"]
