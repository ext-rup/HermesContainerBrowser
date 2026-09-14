FROM docker.io/debian:trixie-slim

ARG UBOL_VERSION=2026.914.1325
ARG UBOL_SHA256=e16d2acc446e13e5d703ec6f7f5fe6c7b2680f0909d9272068e77b0a5ca23593

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates chromium curl fonts-liberation socat tini unzip util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 --shell /usr/sbin/nologin chrome \
 && mkdir -p /opt/ubol /data/profile \
 && curl -fL --retry 3 \
      -o /tmp/ubol.zip \
      "https://github.com/uBlockOrigin/uBOL-home/releases/download/${UBOL_VERSION}/uBOLite_${UBOL_VERSION}.chromium.zip" \
 && printf '%s  %s\n' "$UBOL_SHA256" /tmp/ubol.zip | sha256sum -c - \
 && unzip -q /tmp/ubol.zip -d /opt/ubol \
 && rm /tmp/ubol.zip \
 && chown -R chrome:chrome /data/profile

COPY start-browser.sh /usr/local/bin/start-browser
RUN chmod 0755 /usr/local/bin/start-browser

EXPOSE 9222
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-browser"]
