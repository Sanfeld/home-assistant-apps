# Changelog

## 1.1.2

- Short UniFi console hiccups no longer raise a warning: the console is only reported unreachable after `console_offline_after_minutes` (default 5) of continuous failures.
- Retries at the normal poll interval during a hiccup, so data comes back sooner.

## 1.1.1

- Fixed: the published image shipped paho-mqtt 1.6, so MQTT failed and the app fell back to basic REST sensors.

## 1.1.0

- Published as a GitHub app repository with pre-built images for amd64 and aarch64.
- Build settings moved from `build.yaml` into the Dockerfile.
- Phone alerts are now a blueprint where you pick your own phone.

## 1.0.3

- The ignore list can be managed from the dashboard with two dropdowns.
- Ignoring a flagged client clears its warning straight away.

## 1.0.2

- Fixed: the Supervisor token wasn't found under s6-overlay, so MQTT, events and notifications didn't work.

## 1.0.1

- MQTT lookup retries for 30 seconds and logs the reason when it fails.
- Optional manual MQTT broker settings.

## 1.0.0

- First release: per-client signal and experience monitoring for UniFi, debounced warnings, MQTT
  entities, HA events, persistent notification and a CSV log.
