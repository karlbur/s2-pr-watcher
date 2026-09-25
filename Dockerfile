FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl git python3 python3-pip jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt thư viện Python dùng chung cho script review
RUN pip3 install --no-cache-dir "PyGithub>=2.3" "openai>=1.30"

RUN useradd -m runner
WORKDIR /home/runner

# Download GitHub Runner CLI
RUN RUNNER_VERSION=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/v//') \
    && curl -o runner.tar.gz -L https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    && tar xzf runner.tar.gz && rm runner.tar.gz \
    && ./bin/installdependencies.sh

COPY entrypoint.sh /entrypoint.sh
COPY scripts/ /home/runner/scripts/

USER root
RUN chmod +x /entrypoint.sh && chown -R runner:runner /home/runner

USER runner
ENTRYPOINT ["/entrypoint.sh"]
