"""Moving rules between the first and second versions of the document, losing nothing.

Going up always works: every first-version rule is a vision rule on this node's camera.
Going down works only while the whole set still means the same thing in the old shape.
When it does not, the answer is `schema_upgrade_required`, never a partial copy. A client
that saved a partial copy would delete the rules it could not see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sentry_mode.sentry.config import (
    SCHEMA_VERSION,
    AudioAction,
    FaultPolicy,
    PhotoAction,
    Rule,
    RuleV2,
    SentryConfig,
    SentryConfigV2,
    VideoAction,
    VisionTrigger,
)
from sentry_mode.sources.models import PRIMARY_CAMERA, PRIMARY_MICROPHONE

LEGACY_SCHEMA = 1


class SchemaUpgradeRequired(ValueError):
    """The rules use something the first version of the document cannot say."""

    code = "schema_upgrade_required"

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(
            "These rules need the second version of the rules editor: " + "; ".join(reasons)
        )


@dataclass(frozen=True)
class Document:
    config: SentryConfigV2
    revision: int
    schema_version: int


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].strip("-")
    return text or "rule"


def assign_ids(names: list[str], known: dict[str, str] | None = None) -> list[str]:
    """One permanent identifier per rule, reusing the one a rule of that name already has.

    The identifiers are derived from the names the first time, so running the conversion
    twice on the same document gives the same answer.
    """
    known = known or {}
    taken: set[str] = set()
    ids = []
    for name in names:
        wanted = known.get(name) or slug(name)
        candidate, counter = wanted, 2
        while candidate in taken:
            suffix = f"-{counter}"
            candidate = wanted[: 64 - len(suffix)].rstrip("-") + suffix
            counter += 1
        taken.add(candidate)
        ids.append(candidate)
    return ids


def rule_to_v2(rule: Rule, rule_id: str) -> RuleV2:
    return RuleV2(
        id=rule_id,
        name=rule.name,
        enabled=rule.enabled,
        trigger=VisionTrigger(
            source_id=PRIMARY_CAMERA,
            object=rule.object,
            min_confidence=rule.min_confidence,
            min_count=rule.min_count,
            consecutive_detections=rule.consecutive_detections,
            rearm_after_absence_seconds=rule.rearm_after_absence_seconds,
            region=rule.region,
        ),
        cooldown_seconds=rule.cooldown_seconds,
        actions=[action.model_copy(deep=True) for action in rule.actions],
    )


def to_v2(
    config: SentryConfig,
    *,
    previous: SentryConfigV2 | None = None,
    fault_policy: FaultPolicy = "global",
) -> SentryConfigV2:
    """Convert a whole first-version document.

    With `previous`, the rules keep the identifiers they already had, matched by name,
    and the fault policy stays as it was. Without it, the policy is `global`, which is
    what the first version always did.
    """
    known = {rule.name: rule.id for rule in previous.rules} if previous else {}
    ids = assign_ids([rule.name for rule in config.rules], known)
    return SentryConfigV2(
        detection_fps=config.detection_fps,
        test_mode=config.test_mode,
        action_ttl_seconds=config.action_ttl_seconds,
        fault_policy=previous.fault_policy if previous else fault_policy,
        rules=[rule_to_v2(rule, rule_id) for rule, rule_id in zip(config.rules, ids, strict=True)],
        ssh_commands={k: v.model_copy(deep=True) for k, v in config.ssh_commands.items()},
        telegram=config.telegram.model_copy(deep=True),
    )


def obstacles(config: SentryConfigV2) -> list[str]:
    """Everything in these rules that the first version of the document cannot express."""
    found = []
    for rule in config.rules:
        trigger = rule.trigger
        if not isinstance(trigger, VisionTrigger):
            found.append(f"{rule.name} is set off by {trigger.type.replace('_', ' ')}")
        elif trigger.source_id != PRIMARY_CAMERA:
            found.append(f"{rule.name} watches camera {trigger.source_id}")
        for action in rule.actions:
            if (
                isinstance(action, (PhotoAction, VideoAction))
                and action.source_id != PRIMARY_CAMERA
            ):
                found.append(f"{rule.name} records from camera {action.source_id}")
            if isinstance(action, (PhotoAction, VideoAction)) and action.if_unavailable != "fail":
                found.append(f"{rule.name} {action.if_unavailable}s when its camera is missing")
            if isinstance(action, AudioAction):
                microphone: str | None = action.audio_source_id
            elif isinstance(action, VideoAction):
                microphone = (
                    action.microphone if action.audio else action.audio_source_id
                ) or PRIMARY_MICROPHONE
            else:
                continue
            if microphone != PRIMARY_MICROPHONE:
                found.append(f"{rule.name} records sound from {microphone}")
    return found


def to_v1(config: SentryConfigV2) -> SentryConfig:
    reasons = obstacles(config)
    if reasons:
        raise SchemaUpgradeRequired(reasons)
    rules = []
    for rule in config.rules:
        trigger = rule.trigger
        assert isinstance(trigger, VisionTrigger)
        rules.append(
            Rule(
                name=rule.name,
                enabled=rule.enabled,
                object=trigger.object,
                min_confidence=trigger.min_confidence,
                min_count=trigger.min_count,
                consecutive_detections=trigger.consecutive_detections,
                rearm_after_absence_seconds=trigger.rearm_after_absence_seconds,
                cooldown_seconds=rule.cooldown_seconds,
                region=trigger.region,
                actions=[action.model_copy(deep=True) for action in rule.actions],
            )
        )
    return SentryConfig(
        detection_fps=config.detection_fps,
        test_mode=config.test_mode,
        action_ttl_seconds=config.action_ttl_seconds,
        rules=rules,
        ssh_commands={k: v.model_copy(deep=True) for k, v in config.ssh_commands.items()},
        telegram=config.telegram.model_copy(deep=True),
    )


# The fields the first version of each action never had. Its API leaves them out so the
# shape old clients see is exactly the shape they always saw; `obstacles` has already made
# sure the values left out are the ones they would have assumed.
V1_HIDDEN = {
    "photo": {"source_id", "if_unavailable"},
    "video": {"source_id", "audio_source_id", "if_unavailable"},
    "audio": {"audio_source_id"},
}


def dump_v1(config: SentryConfig) -> dict:
    data = config.model_dump(mode="json")
    for rule in data["rules"]:
        for action in rule["actions"]:
            for name in V1_HIDDEN.get(action["type"], ()):
                action.pop(name, None)
    return data


def read_document(data: dict) -> Document:
    """Read a saved rules file of either version. The version is the file's, not a guess."""
    version = data.get("schema_version", LEGACY_SCHEMA)
    revision = int(data["revision"])
    if version == SCHEMA_VERSION:
        return Document(SentryConfigV2.model_validate(data["config"]), revision, SCHEMA_VERSION)
    if version == LEGACY_SCHEMA:
        legacy = SentryConfig.model_validate(data["config"])
        return Document(to_v2(legacy), revision, LEGACY_SCHEMA)
    raise ValueError(f"Rules file version {version} is newer than this program understands.")


def write_document(config: SentryConfigV2, revision: int, schema_version: int) -> dict:
    """What goes to disk, in the version the file already has, with the secret included.

    A file that has not been migrated stays in the first version, so the previous release
    can still read it. Rules the first version cannot hold are refused here, with the
    advice to migrate, rather than saved in a shape the previous release would reject.
    """
    if schema_version == LEGACY_SCHEMA:
        body = dump_v1(to_v1(config))
        token = config.telegram.bot_token.get_secret_value()
        body["telegram"]["bot_token"] = token
        return {"config": body, "revision": revision}
    body = config.model_dump(mode="json")
    body["telegram"]["bot_token"] = config.telegram.bot_token.get_secret_value()
    return {"schema_version": SCHEMA_VERSION, "config": body, "revision": revision}


__all__ = [
    "LEGACY_SCHEMA",
    "Document",
    "SchemaUpgradeRequired",
    "assign_ids",
    "dump_v1",
    "obstacles",
    "read_document",
    "rule_to_v2",
    "slug",
    "to_v1",
    "to_v2",
    "write_document",
]
