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
else is installed for video, and nothing is re-encoded in software). A board with a
microphone needs `alsa-utils` (`arecord`); sound is sent as raw PCM, with no codec.
A board that watches for a device over Bluetooth needs BlueZ running — it is already on
Raspberry Pi OS — and its adapter unblocked (`rfkill list bluetooth`); nothing is
installed for it, because the agent talks to `bluetoothd` over D-Bus itself.

Sensors need their buses switched on in `/boot/firmware/config.txt` (`dtoverlay=w1-gpio`
for a DS18B20, `dtparam=i2c_arm=on` for a BME280 or an ADS1115) and the service user in
the `gpio`, `i2c`, `audio`, `video` and `bluetooth` groups, which the unit in `systemd/`
sets. The sources the hub has
sent are kept in `/var/lib/sentry-satellite` (`--state-dir`); the file under `/etc` is
never rewritten. See [satellites](../docs/satellites.md#sensors) for the options of each
kind.

    python -m sentry_satellite.cli validate --config config/sensor-presence.example.toml
    python -m sentry_satellite.cli doctor
    python -m sentry_satellite.cli run --config /etc/sentry-satellite/node.toml

A release is built from a checkout and installed on the board; nothing is compiled there,
and the agent runs from `/opt/sentry-satellite/current` under the system Python:

    python -m sentry_satellite.cli package --into dist      # reproducible: same tree, same bytes
    sudo scripts/install.sh --release dist/sentry-satellite-0.1.0.tar.gz
    sudo scripts/backup.sh --into /var/backups
    sudo scripts/rollback.sh --list
    python -m sentry_satellite.cli verify                   # is what is installed still intact

`install.sh` can be run again with the same release and changes nothing, never writes over
a configuration or an identity, and touches no service but its own. See
[satellites](../docs/satellites.md#installing-a-node-updating-it-and-going-back).

The agent is a peripheral of the protocol, not a second Sentry: it reads, it reports, and
it does what a signed command from the hub tells it to. It holds no rules and takes no
decisions about the house.
