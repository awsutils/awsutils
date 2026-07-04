FROM python:3.12-slim-bookworm

ARG TTYD_VERSION=1.7.7

ENV AWSUTILS_INSTALL_DIR=/home/awsutils/.awsutils \
    PYTHONUNBUFFERED=1 \
    WEBSHELL_HOST=0.0.0.0 \
    WEBSHELL_PORT=8080 \
    WEBSHELL_SHELL=/bin/bash

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        awscli \
        bash \
        ca-certificates \
        curl \
        dnsutils \
        file \
        git \
        iproute2 \
        iputils-ping \
        jq \
        less \
        lsof \
        nano \
        netcat-openbsd \
        openssh-client \
        procps \
        psmisc \
        sudo \
        tar \
        tini \
        tree \
        unzip \
        vim \
        wget \
        zip \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) ttyd_arch="x86_64" ;; \
        arm64) ttyd_arch="aarch64" ;; \
        armhf) ttyd_arch="armhf" ;; \
        armel) ttyd_arch="arm" ;; \
        i386) ttyd_arch="i686" ;; \
        s390x) ttyd_arch="s390x" ;; \
        *) printf 'unsupported architecture for ttyd: %s\n' "$arch" >&2; exit 1 ;; \
    esac; \
    base_url="https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}"; \
    curl -fsSL "$base_url/ttyd.$ttyd_arch" -o /usr/local/bin/ttyd; \
    curl -fsSL "$base_url/SHA256SUMS" -o /tmp/ttyd.SHA256SUMS; \
    checksum="$(awk -v file="ttyd.$ttyd_arch" '$2 == file { print $1 }' /tmp/ttyd.SHA256SUMS)"; \
    printf '%s  /usr/local/bin/ttyd\n' "$checksum" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/ttyd; \
    rm -f /tmp/ttyd.SHA256SUMS; \
    ttyd --version

RUN useradd --create-home --shell /bin/bash awsutils \
    && printf 'awsutils ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/awsutils \
    && chmod 0440 /etc/sudoers.d/awsutils

WORKDIR /opt/awsutils
COPY . /opt/awsutils

RUN python3 -m pip install --no-cache-dir . \
    && mkdir -p /home/awsutils/.aws/cli /home/awsutils/.awsutils \
    && printf '[toplevel]\nutils = !awsutils\n' >/home/awsutils/.aws/cli/alias \
    && chown -R awsutils:awsutils /home/awsutils /opt/awsutils

COPY docker/webshell-entrypoint /usr/local/bin/awsutils-webshell
RUN chmod 0755 /usr/local/bin/awsutils-webshell

USER awsutils
WORKDIR /home/awsutils

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/awsutils-webshell"]
