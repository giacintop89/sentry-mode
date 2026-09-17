# sentry-satellite

The agent that runs on a small board — a Raspberry Pi Zero W, to begin with — and reports
what its sensors see to a Sentry Mode hub over MQTT.

It is a separate distribution on purpose. It shares no code with the hub, depends on
nothing but the standard library and Paho, and is installed on machines that will not have
OpenCV, a model, or a display. What the two sides do share are the files under
`../contracts/`, which both are tested against.

On Raspberry Pi OS the two libraries come from the distribution, not from pip:

    sudo apt install python3-libgpiod python3-paho-mqtt

A board with a camera also needs `rpicam-apps-core` (the encoder is `rpicam-vid`; nothing
else is installed for video, and nothing is re-encoded in software).

Sensors need their buses switched on in `/boot/firmware/config.txt` (`dtoverlay=w1-gpio`
for a DS18B20, `dtparam=i2c_arm=on` for a BME280 or an ADS1115) and the service user in
the `gpio`, `i2c` and `video` groups, which the unit in `systemd/` sets. The sources the hub has
sent are kept in `/var/lib/sentry-satellite` (`--state-dir`); the file under `/etc` is
never rewritten. See [satellites](../docs/satellites.md#sensors) for the options of each
kind.

    python -m sentry_satellite.cli validate --config config/sensor-presence.example.toml
    python -m sentry_satellite.cli doctor
    python -m sentry_satellite.cli run --config /etc/sentry-satellite/node.toml

The agent is a peripheral of the protocol, not a second Sentry: it reads, it reports, and
it does what a signed command from the hub tells it to. It holds no rules and takes no
decisions about the house.
