# Home Assistant App: Wi-Fi Signal Monitor

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Watches the signal strength of every Wi-Fi client on your UniFi network and warns you about bad
connections in Home Assistant and on your phone.

- Signal (dBm) and UniFi experience score for every client, read straight from your UniFi console
- Weak, critical and poor-experience warnings, with debouncing so walking around doesn't trigger them
- Warnings as HA entities, events, a persistent notification and optional phone pushes
- Per-access-point overview and one signal sensor per client
- Ignore list you manage from the dashboard

See the [documentation](DOCS.md) for setup.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
