# Changelog

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
