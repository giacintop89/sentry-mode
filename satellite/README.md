# sentry-satellite

The agent that runs on a small board — a Raspberry Pi Zero W, to begin with — and reports
what its sensors see to a Sentry Mode hub over MQTT.

It is a separate distribution on purpose. It shares no code with the hub, depends on
nothing but the standard library and Paho, and is installed on machines that will not have
OpenCV, a model, or a display. What the two sides do share are the files under
`../contracts/`, which both are tested against.

    python -m sentry_satellite.cli validate --config config/sensor-presence.example.toml
    python -m sentry_satellite.cli doctor
    python -m sentry_satellite.cli run --config /etc/sentry-satellite/node.toml

The agent is a peripheral of the protocol, not a second Sentry: it reads, it reports, and
it does what a signed command from the hub tells it to. It holds no rules and takes no
decisions about the house.
