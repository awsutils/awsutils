FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ARG TTYD_VERSION=1.7.7

ENV AWSUTILS_INSTALL_DIR=/home/ec2-user/.awsutils \
    PYTHONUNBUFFERED=1 \
    WEBSHELL_HOST=0.0.0.0 \
    WEBSHELL_PORT=8080 \
    WEBSHELL_SHELL=/bin/bash

RUN dnf install -y \
        awscli \
        bash \
        bind-utils \
        ca-certificates \
        file \
        findutils \
        git \
        gzip \
        iproute \
        iputils \
        jq \
        less \
        lsof \
        nano \
        nmap-ncat \
        nodejs24 \
        openssh-server \
        openssh-clients \
        passwd \
        procps-ng \
        psmisc \
        python3 \
        python3-pip \
        shadow-utils \
        spal-release \
        sudo \
        tar \
        tree \
        unzip \
        vim \
        wget \
        zip \
    && dnf clean all \
    && rm -rf /var/cache/dnf

RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64) ttyd_arch="x86_64" ;; \
        aarch64) ttyd_arch="aarch64" ;; \
        armv7l) ttyd_arch="armhf" ;; \
        armv6l) ttyd_arch="arm" ;; \
        i386|i686) ttyd_arch="i686" ;; \
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

ENTRYPOINT ["/usr/local/bin/awsutils-webshell"]
