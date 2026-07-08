FROM python:3.12-slim-bookworm

ARG TTYD_VERSION=1.7.7

ENV AWSUTILS_INSTALL_DIR=/home/ec2-user/.awsutils \
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
        openssh-server \
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

RUN useradd --create-home --shell /bin/bash ec2-user \
    && passwd -d ec2-user \
    && printf 'ec2-user ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/ec2-user \
    && chmod 0440 /etc/sudoers.d/ec2-user \
    && mkdir -p /run/sshd /home/ec2-user/.ssh \
    && chmod 0700 /home/ec2-user/.ssh \
    && printf '%s\n' \
        'PermitRootLogin no' \
        'PasswordAuthentication yes' \
        'PermitEmptyPasswords yes' \
        'KbdInteractiveAuthentication no' \
        'UsePAM no' \
        'AllowUsers ec2-user' \
        >/etc/ssh/sshd_config.d/awsutils.conf \
    && chown -R ec2-user:ec2-user /home/ec2-user/.ssh

WORKDIR /opt/awsutils
COPY . /opt/awsutils

RUN python3 -m pip install --no-cache-dir . \
    && mkdir -p /home/ec2-user/.aws/cli /home/ec2-user/.awsutils \
    && printf '[toplevel]\nutils = !awsutils\n' >/home/ec2-user/.aws/cli/alias \
    && chown -R ec2-user:ec2-user /home/ec2-user /opt/awsutils

COPY docker/webshell-entrypoint /usr/local/bin/awsutils-webshell
RUN chmod 0755 /usr/local/bin/awsutils-webshell

USER root
WORKDIR /home/ec2-user

EXPOSE 22 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/awsutils-webshell"]
