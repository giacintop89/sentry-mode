"""Just enough JSON Schema to hold the satellite to the same contract as the hub.

The hub validates with Pydantic, which the satellite will never have. Rather than trust
that both sides happen to agree, the satellite checks the published schema itself. This is
the subset that schema actually uses; anything outside it raises rather than passing
quietly, so the checker cannot drift into approving what it does not understand.
"""

import re

KNOWN = {
    "$defs",
    "$ref",
    "$id",
    "additionalProperties",
    "anyOf",
    "const",
    "default",
    "description",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
}


class Invalid(ValueError):
    """The document does not match the schema, with the path to the part that does not."""


def _type_matches(value: object, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise Invalid(f"the checker does not know the type {name!r}")


def validate(document: object, schema: dict, root: dict | None = None, path: str = "$") -> None:
    root = root if root is not None else schema
    unknown = set(schema) - KNOWN
    if unknown:
        raise Invalid(f"{path}: the checker does not understand {', '.join(sorted(unknown))}")

    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            raise Invalid(f"{path}: only local definitions are supported, not {reference}")
        validate(document, root["$defs"][reference.split("/")[-1]], root, path)
        return

    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(document, option, root, path)
                break
            except Invalid:
                continue
        else:
            raise Invalid(f"{path}: matches none of the allowed shapes")
        return

    if "type" in schema and not _type_matches(document, schema["type"]):
        raise Invalid(f"{path}: expected {schema['type']}, got {type(document).__name__}")
    if "const" in schema and document != schema["const"]:
        raise Invalid(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and document not in schema["enum"]:
        raise Invalid(f"{path}: {document!r} is not one of {schema['enum']}")

    if isinstance(document, str):
        if "pattern" in schema and not re.search(schema["pattern"], document):
            raise Invalid(f"{path}: {document!r} does not match {schema['pattern']}")
        if "maxLength" in schema and len(document) > schema["maxLength"]:
            raise Invalid(f"{path}: longer than {schema['maxLength']}")
        if "minLength" in schema and len(document) < schema["minLength"]:
            raise Invalid(f"{path}: shorter than {schema['minLength']}")

    if isinstance(document, int | float) and not isinstance(document, bool):
        if "minimum" in schema and document < schema["minimum"]:
            raise Invalid(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and document > schema["maximum"]:
            raise Invalid(f"{path}: above {schema['maximum']}")
        if "exclusiveMinimum" in schema and document <= schema["exclusiveMinimum"]:
            raise Invalid(f"{path}: not above {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and document >= schema["exclusiveMaximum"]:
            raise Invalid(f"{path}: not below {schema['exclusiveMaximum']}")

    if isinstance(document, list):
        if "maxItems" in schema and len(document) > schema["maxItems"]:
            raise Invalid(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(document) < schema["minItems"]:
            raise Invalid(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(document):
                validate(item, schema["items"], root, f"{path}[{index}]")

    if isinstance(document, dict):
        properties = schema.get("properties", {})
        extras = schema.get("additionalProperties")
        for name in schema.get("required", []):
            if name not in document:
                raise Invalid(f"{path}: {name} is required")
        if extras is False:
            extra = sorted(set(document) - set(properties))
            if extra:
                raise Invalid(f"{path}: does not take {', '.join(extra)}")
        for name, value in document.items():
            if name in properties:
                validate(value, properties[name], root, f"{path}.{name}")
            elif isinstance(extras, dict):
                # A map rather than a record: every other key is held to one shape, which is
                # how a node's sources or a source's options are written down.
                validate(value, extras, root, f"{path}.{name}")
