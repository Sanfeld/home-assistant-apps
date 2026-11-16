# Wi-Fi Signal Monitor

Reads every connected Wi-Fi client from your UniFi console once a minute, flags clients with a
weak signal or a poor UniFi "Wi-Fi experience" score, and warns you in Home Assistant and on
your phone.

Home Assistant's UniFi integration doesn't expose per-client signal strength, so this app reads
it straight from the console.

## Requirements

- A UniFi OS console (UCG Max/Ultra/Fiber, UDM, UDR, Cloud Key Gen2+, UniFi OS Server) running
  UniFi Network.
- The **Mosquitto broker** app (or another MQTT broker set up in Home Assistant) for the full set
  of entities. Without MQTT the app falls back to a few basic sensors.

## Connecting to the UniFi console

**API key (recommended).** In UniFi Network, open *Settings → Control Plane → Integrations*
(on some versions *Settings → Integrations*, or go straight to
`https://<console-ip>/network/default/integrations/`) and create an API key. Paste it into
`api_key`.

**Or a local account.** Create a local *View only* admin on the console (not a UI.com cloud
account, since 2FA logins won't work). Fill in `username` and `password`, and leave `api_key` empty.

Set `unifi_host` to the console's LAN IP, which is usually your gateway address.

## How a client is judged

| Level | Rule (defaults) |
|---|---|
| 🔴 Critical | signal below `critical_dbm` (-80 dBm) |
| 🟠 Weak | signal below `warn_dbm` (-70 dBm) |
| 🟡 Poor | signal OK but UniFi experience below `min_satisfaction` (50 %) |

A client is only reported after `bad_polls_before_alert` bad readings in a row (3 by default, i.e.
three minutes). It's cleared after `good_polls_before_clear` good readings, so someone walking
around with a phone doesn't set off alerts.

The thresholds are also number entities, so you can tune them from a dashboard without
restarting the app.

## Ignoring clients

Phones, tablets and guests move around and drop off all the time, so you'll usually want to
ignore them. There are two ways:

- **From the dashboard:** use the *Ignore a client* and *Stop ignoring a client* dropdowns
  (`select.wifi_monitor_ignore_client` / `select.wifi_monitor_unignore_client`). Changes apply
  immediately and are remembered.
- **By pattern:** list MAC addresses or names in the `ignore_clients` option, with wildcards
  allowed (`Phone*`, `Guest*`, `aa:bb:cc:*`). Pattern matches can't be un-ignored from the
  dashboard.

Most phones use a randomized Wi-Fi address, so ignore them by name rather than by MAC.

## Entities

| Entity | What it shows |
|---|---|
| `sensor.wifi_monitor_bad_connections` | Number of flagged clients; attribute `alerts` lists them |
| `sensor.wifi_monitor_wifi_clients` | Connected Wi-Fi clients; attributes `clients`, `access_points`, `ignored_clients` |
| `sensor.wifi_monitor_worst_signal` / `_average_signal` | dBm, with the weakest client as attribute |
| `sensor.wifi_monitor_critical` / `_weak` / `_poor` | Count per level |
| `binary_sensor.wifi_monitor_problem` | On while any client is flagged |
| `binary_sensor.wifi_monitor_unifi_console` | Off when the console can't be read (attribute `error`) |
| `number.wifi_monitor_warn_dbm` / `_critical_dbm` / `_min_satisfaction` | Thresholds |
| `select.wifi_monitor_ignore_client` / `_unignore_client` | Manage the ignore list |
| `sensor.wifi_signal_<client>` | One signal sensor per client (diagnostic), with AP, band, channel and rates |

Per-client sensors become *unavailable* while the client is offline, and are removed after 7 days away.

## Warnings

- **Event** `wifi_monitor_alert` with `type` = `new`, `escalated`, `cleared` or `disconnected`,
  plus `name`, `mac`, `signal`, `level`, `ap`, `band`, `channel`, `ssid` and `message`.
- **Persistent notification** in Home Assistant listing all current bad connections
  (turn off with `persistent_notification: false`).
- **CSV log** of every event in `/share/wifi_monitor/alerts.csv`.

## Dashboard and phone alerts

The repository has two extras next to the app:

- [`dashboards/wifi_monitor.yaml`](https://github.com/Sanfeld/home-assistant-apps/blob/main/dashboards/wifi_monitor.yaml)
  is a ready-made dashboard using only built-in cards. It has warning banners, an access point
  table, all clients sorted weakest first, 24-hour history, thresholds and the ignore list.
- The [phone alerts blueprint](https://github.com/Sanfeld/home-assistant-apps/blob/main/blueprints/automation/wifi_monitor_phone_alerts.yaml)
  pushes a notification to the phone you pick and clears it again when the client recovers.

## MQTT settings

Normally the app finds the Mosquitto broker on its own. If it can't, set `mqtt_host`
(e.g. `core-mosquitto`), `mqtt_username` and `mqtt_password`.
