# Base stage with common dependencies
FROM python:3.11-slim AS base
SHELL ["/bin/bash", "-c"]

# Set environment variables to make Python print directly to the terminal and avoid .pyc files.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies required for package manager and build tools.
# sudo, wget, zip needed for some assistants, like junie
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    git \
    ssh \
    sudo \
    wget \
    zip \
    unzip \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install pipx.
RUN python3 -m pip install --no-cache-dir pipx \
    && pipx ensurepath

# Install nodejs
ENV NVM_VERSION=0.40.3
ENV NODE_VERSION=22.18.0
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh | bash
# standard location
ENV NVM_DIR=/root/.nvm
RUN . "$NVM_DIR/nvm.sh" && nvm install ${NODE_VERSION}
RUN . "$NVM_DIR/nvm.sh" && nvm use v${NODE_VERSION}
RUN . "$NVM_DIR/nvm.sh" && nvm alias default v${NODE_VERSION}
ENV PATH="${NVM_DIR}/versions/node/v${NODE_VERSION}/bin/:${PATH}"

# Add local bin to the path
ENV PATH="${PATH}:/root/.local/bin"

# Install the latest version of uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Java JDK (for Eclipse JDTLS, Kotlin LS)
ENV JAVA_VERSION=21
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-${JAVA_VERSION}-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-${JAVA_VERSION}-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Install .NET SDK (for C# language server)
ENV DOTNET_VERSION=8.0
RUN apt-get update && apt-get install -y --no-install-recommends libicu-dev && rm -rf /var/lib/apt/lists/*
RUN curl -sSL https://dot.net/v1/dotnet-install.sh | bash /dev/stdin \
    --channel ${DOTNET_VERSION} \
    --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/bin/dotnet
ENV DOTNET_ROOT=/usr/share/dotnet
ENV PATH="${PATH}:${DOTNET_ROOT}"
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1

# Install Go (for gopls)
ENV GO_VERSION=1.23.6
RUN curl -sSL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xzf -
ENV PATH="${PATH}:/usr/local/go/bin:/root/go/bin"
ENV GOPATH=/root/go

# Install Rust and rustup for rust-analyzer support (minimal profile)
ENV RUSTUP_HOME=/usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo
ENV PATH="${CARGO_HOME}/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    --default-toolchain stable \
    --profile minimal \
    && rustup component add rust-analyzer

# Set the working directory
WORKDIR /workspaces/serena

# Development target
FROM base AS development
# Copy all files for development
COPY . /workspaces/serena/

# Create virtual environment and install dependencies with dev extras
RUN uv venv
RUN . .venv/bin/activate
RUN uv pip install --all-extras -r pyproject.toml -e .
ENV PATH="/workspaces/serena/.venv/bin:${PATH}"

RUN chmod +x /workspaces/serena/docker-entrypoint.sh
ENTRYPOINT ["/workspaces/serena/docker-entrypoint.sh"]

# Production target
FROM base AS production
# Copy only necessary files for production
COPY pyproject.toml /workspaces/serena/
COPY README.md /workspaces/serena/
COPY src/ /workspaces/serena/src/
COPY docker-entrypoint.sh /workspaces/serena/
RUN chmod +x /workspaces/serena/docker-entrypoint.sh

# Create virtual environment and install dependencies (production only)
RUN uv venv
RUN . .venv/bin/activate
RUN uv pip install -r pyproject.toml -e .
ENV PATH="/workspaces/serena/.venv/bin:${PATH}"

ENTRYPOINT ["/workspaces/serena/docker-entrypoint.sh"]

