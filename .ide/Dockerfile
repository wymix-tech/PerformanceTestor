FROM cnbcool/default-dev-env:latest

WORKDIR /plugins/

RUN apt-get update && apt-get install -y gettext-base && rm -rf /var/lib/apt/lists/*

RUN [ ! -d "/workspace/.codebuddy" ] || mkdir -p /workspace/.codebuddy && cp -r /root/.codebuddy/* /workspace/.codebuddy && mv /root/.codebuddy /root/.codebuddy_bak && ln -s /workspace/.codebuddy /root/.codebuddy