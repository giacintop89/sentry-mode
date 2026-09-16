"""A configuration file is checked in full before the agent does anything with it."""

import tomllib
from pathlib import Path

import pytest
from conftest import SATELLITE

from sentry_satellite import config as configuration
from sentry_satellite.names import InvalidName, local_name

EXAMPLES = sorted((SATELLITE / "config").glob("*.example.toml"))

MINIMAL = """
[node]
id = "zero-entrance"
profile = "sensor-presence"

[hub]
mqtt_host = "192.168.11.10"

[tls]
ca_file = "/etc/sentry-satellite/ca.crt"
cert_file = "/etc/sentry-satellite/node.crt"
key_file = "/etc/sentry-satellite/node.key"

[[sources]]
id = "pir-1"
kind = "gpio"
"""


def parse(text: str) -> configuration.Config:
    return configuration.parse(tomllib.loads(text), identity_file=Path("/nowhere/identity.json"))


def test_a_minimal_file_is_enough():
    config = parse(MINIMAL)
    assert config.node_id == "zero-entrance"
    assert config.hub.mqtt_port == 8883
    assert config.limits.event_max_count == 256
    assert config.source("pir-1").kind == "gpio"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_every_example_shipped_with_the_agent_is_one_it_would_accept(path):
    assert configuration.load(path).node_id


def test_a_source_may_be_written_out_in_full_as_long_as_it_is_ours():
    config = parse(MINIMAL.replace('id = "pir-1"', 'id = "zero-entrance.pir-1"'))
    assert config.sources[0].id == "pir-1"


def test_a_node_cannot_configure_a_source_that_belongs_to_another_node():
    with pytest.raises(configuration.ConfigError, match="belongs to node"):
        parse(MINIMAL.replace('id = "pir-1"', 'id = "zero-garage.pir-1"'))


def test_a_capability_outside_the_profile_is_refused():
    with pytest.raises(configuration.ConfigError, match="has no csi sources"):
        parse(MINIMAL.replace('kind = "gpio"', 'kind = "csi"'))


def without(section: str) -> str:
    """The file with one whole table taken out of it."""
    kept, dropping = [], False
    for line in MINIMAL.splitlines():
        if line.startswith("["):
            dropping = line.startswith(f"[{section}]")
        if not dropping:
            kept.append(line)
    return "\n".join(kept)


@pytest.mark.parametrize("section", ["node", "hub", "tls"])
def test_a_section_that_is_missing_is_named(section):
    with pytest.raises(configuration.ConfigError, match=f"\\[{section}\\] section is missing"):
        parse(without(section))


@pytest.mark.parametrize("key", ["id", "profile"])
def test_a_setting_that_is_missing_is_named(key):
    text = "\n".join(line for line in MINIMAL.splitlines() if not line.startswith(f"{key} ="))
    with pytest.raises(configuration.ConfigError, match=f"missing {key}"):
        parse(text)


def test_a_typo_is_refused_rather_than_ignored():
    with pytest.raises(configuration.ConfigError, match="does not take"):
        parse(MINIMAL + "\n[limits]\nevent_max_cnt = 10\n")


def test_a_node_name_that_would_not_survive_a_topic_is_refused():
    with pytest.raises(configuration.ConfigError):
        parse(MINIMAL.replace('id = "zero-entrance"', 'id = "Zero Entrance"'))


def test_a_source_cannot_be_declared_twice():
    with pytest.raises(configuration.ConfigError, match="declared twice"):
        parse(MINIMAL + '\n[[sources]]\nid = "pir-1"\nkind = "gpio"\n')


def test_the_agent_speaks_one_version_of_mqtt():
    with pytest.raises(configuration.ConfigError, match="protocol must be 5"):
        parse(MINIMAL.replace("[tls]", "protocol = 3\n\n[tls]", 1))


def test_a_limit_has_to_be_a_positive_number():
    with pytest.raises(configuration.ConfigError, match="positive"):
        parse(MINIMAL + "\n[limits]\nevent_max_count = 0\n")


def test_a_summary_is_safe_to_print():
    summary = configuration.summary(parse(MINIMAL))
    assert "node.key" not in str(summary)
    assert summary["hub"] == "192.168.11.10:8883"


def test_a_bare_name_is_returned_unchanged():
    assert local_name("pir-1", "zero-entrance") == "pir-1"
    with pytest.raises(InvalidName):
        local_name("PIR", "zero-entrance")
