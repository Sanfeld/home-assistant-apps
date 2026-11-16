# https://contributors.home-assistant.io/apps/guides/dockerfile/
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# Python from Alpine, paho-mqtt 2.x from PyPI (Alpine only ships 1.6).
# UniFi and Supervisor calls only use the standard library.
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages "paho-mqtt>=2.0,<3" \
    && python3 -c "import paho.mqtt.client as m; m.CallbackAPIVersion"

COPY app /app

CMD ["python3", "-u", "/app/monitor.py"]

LABEL \
    io.hass.type="app" \
    org.opencontainers.image.title="Home Assistant App: Wi-Fi Signal Monitor" \
    org.opencontainers.image.description="Watches the signal strength of every Wi-Fi client on a UniFi network and warns about bad connections." \
    org.opencontainers.image.source="https://github.com/Sanfeld/home-assistant-apps" \
    org.opencontainers.image.licenses="MIT"
