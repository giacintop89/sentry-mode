"""Epochs, grants and the difference between a quiet node and a broker that has gone."""

import pytest

from sentry_mode.satellites.config import HealthConfig, SessionsConfig
from sentry_mode.satellites.sessions import (
    BrokerState,
    Freshness,
    SessionError,
    SessionManager,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def manager(clock) -> SessionManager:
    epochs = iter(range(1, 1000))
    names = iter(f"grant-{number}" for number in range(1, 1000))
    manager = SessionManager(
        health=HealthConfig(heartbeat_seconds=15, stale_after_seconds=45, offline_after_seconds=60),
        sessions=SessionsConfig(grant_seconds=300, renew_every_seconds=120),
        next_epoch=lambda: next(epochs),
        clock=clock,
        new_id=lambda: next(names),
    )
    manager.broker_connected()
    return manager


def test_every_connection_gets_a_number_nobody_has_had(manager):
    first = manager.open("zero-entrance", connection_id="c1")
    second = manager.open("zero-garage", connection_id="c2")
    third = manager.open("zero-entrance", connection_id="c3")
    assert {first.hub_epoch, second.hub_epoch, third.hub_epoch} == {1, 2, 3}
    assert manager.current("zero-entrance").connection_id == "c3"


def test_a_node_is_live_until_it_goes_quiet(manager, clock):
    manager.open("zero-entrance", connection_id="c1")
    assert manager.freshness("zero-entrance") is Freshness.LIVE
    clock.advance(46)
    assert manager.freshness("zero-entrance") is Freshness.STALE
    clock.advance(20)
    assert manager.freshness("zero-entrance") is Freshness.OFFLINE
    manager.heartbeat("zero-entrance")
    assert manager.freshness("zero-entrance") is Freshness.LIVE


def test_a_broker_that_falls_over_is_one_fault_and_not_fifteen(manager, clock):
    for name in ("zero-entrance", "zero-garage", "zero-hall"):
        manager.open(name, connection_id=f"c-{name}")
    manager.broker_unavailable()
    assert manager.broker is BrokerState.UNAVAILABLE
    for name in ("zero-entrance", "zero-garage", "zero-hall"):
        assert manager.freshness(name) is Freshness.UNAVAILABLE
    manager.broker_connected()
    assert manager.freshness("zero-entrance") is Freshness.LIVE


def test_a_node_never_heard_from_is_not_an_offline_node(manager):
    assert manager.freshness("zero-nowhere") is Freshness.NEVER_SEEN


def test_a_goodbye_from_a_connection_that_is_over_is_ignored(manager):
    manager.open("zero-entrance", connection_id="c1")
    manager.open("zero-entrance", connection_id="c2")
    assert manager.close("zero-entrance", connection_id="c1") is False
    assert manager.current("zero-entrance") is not None
    assert manager.close("zero-entrance", connection_id="c2") is True
    assert manager.current("zero-entrance") is None


def test_a_grant_belongs_to_the_connection_it_was_issued_in(manager):
    manager.open("zero-entrance", connection_id="c1")
    grant = manager.grant("zero-entrance", "events")
    assert manager.grant_is_current("zero-entrance", grant.grant_id)
    manager.open("zero-entrance", connection_id="c2")
    assert not manager.grant_is_current("zero-entrance", grant.grant_id)


def test_a_grant_runs_out(manager, clock):
    manager.open("zero-entrance", connection_id="c1")
    grant = manager.grant("zero-entrance", "events", seconds=30)
    clock.advance(31)
    assert not manager.grant_is_current("zero-entrance", grant.grant_id)


def test_a_renewal_keeps_the_grant_and_counts(manager, clock):
    manager.open("zero-entrance", connection_id="c1")
    grant = manager.grant("zero-entrance", "events", seconds=100)
    clock.advance(50)
    renewed = manager.renew("zero-entrance", "events", seconds=100)
    assert renewed.grant_id == grant.grant_id
    assert renewed.sequence == 1
    assert renewed.expires_at > grant.expires_at
    assert manager.renew("zero-entrance", "events").sequence == 2


def test_there_is_nothing_to_renew_without_a_grant(manager):
    manager.open("zero-entrance", connection_id="c1")
    with pytest.raises(SessionError):
        manager.renew("zero-entrance", "events")


def test_what_is_nearly_out_of_time_is_what_gets_renewed(manager, clock):
    manager.open("zero-entrance", connection_id="c1")
    manager.grant("zero-entrance", "events", seconds=300)
    assert manager.due_for_renewal() == []
    clock.advance(181)
    assert [grant.node_id for grant in manager.due_for_renewal()] == ["zero-entrance"]


def test_taking_a_grant_back_is_immediate(manager):
    manager.open("zero-entrance", connection_id="c1")
    grant = manager.grant("zero-entrance", "events")
    assert manager.revoke("zero-entrance") == ["events"]
    assert not manager.grant_is_current("zero-entrance", grant.grant_id)


def test_a_node_without_a_session_cannot_be_granted_anything(manager):
    with pytest.raises(SessionError):
        manager.grant("zero-entrance", "events")


def test_a_capability_nobody_agreed_on_is_refused(manager):
    manager.open("zero-entrance", connection_id="c1")
    with pytest.raises(ValueError):
        manager.grant("zero-entrance", "everything")


def test_a_message_from_an_older_epoch_does_not_count(manager):
    session = manager.open("zero-entrance", connection_id="c1")
    assert manager.is_current("zero-entrance", connection_id="c1", hub_epoch=session.hub_epoch)
    older = session.hub_epoch - 1
    assert not manager.is_current("zero-entrance", connection_id="c1", hub_epoch=older)
    assert not manager.is_current("zero-entrance", connection_id="other")


def test_a_grant_command_says_everything_the_node_needs_and_nothing_else(manager):
    manager.open("zero-entrance", connection_id="c1")
    command = manager.grant("zero-entrance", "events", seconds=120).as_command()
    assert set(command) == {
        "command_id",
        "action",
        "node_id",
        "hub_epoch",
        "capability",
        "grant_id",
        "duration_seconds",
        "sequence",
    }
    assert command["action"] == "grant"
    assert command["duration_seconds"] == 120


def test_stopping_forgets_everyone(manager):
    manager.open("zero-entrance", connection_id="c1")
    manager.broker_stopped()
    assert manager.status()["nodes"] == {}
    assert manager.broker is BrokerState.STOPPED


def test_the_status_keeps_the_link_and_the_nodes_apart(manager):
    manager.open("zero-entrance", connection_id="c1")
    manager.grant("zero-entrance", "events")
    status = manager.status()
    assert status["broker"] == "connected"
    node = status["nodes"]["zero-entrance"]
    assert node["freshness"] == "live"
    assert node["grants"]["events"]["sequence"] == 0
