"""R6 contract validator (server side) plus the v1->v2 projection mirror.

The wire shape is owned by ``contracts/r6/contracts.schema.json``. This module
evaluates that bundle plus the semantic invariants C1-C12 from
``docs/r6/contracts.md``; it never restates a shape. ``src/shared/contracts.js``
is the browser-side twin and must return the same verdicts for the same
fixture cases (``tests/fixtures/r6/contracts/cases.json``).

Run as a script for a quick self-check::

    python3.9 tools/r6_contract_validate.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "contracts" / "r6" / "contracts.schema.json"
STATE_PATH = ROOT / "contracts" / "r6" / "state-machine.json"
CASES_PATH = ROOT / "tests" / "fixtures" / "r6" / "contracts" / "cases.json"
PROJECTION_PATH = ROOT / "tests" / "fixtures" / "r6" / "contracts" / "projection.json"

REF_PREFIX = "#/$defs/"
KINDS = ("text", "kv", "stack", "mask", "unparsed")

TS_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})?$"
)
DAYS = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def valid_timestamp(text) -> bool:
    """C13: a timestamp must name a real instant, not merely the right digit count."""
    if not isinstance(text, str):
        return False
    match = TS_RE.match(text)
    if not match:
        return False
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour, minute, second = int(match.group(4)), int(match.group(5)), int(match.group(6))
    if not 1 <= month <= 12:
        return False
    limit = DAYS[month] + (1 if month == 2 and _leap(year) else 0)
    if not 1 <= day <= limit:
        return False
    if hour > 23 or minute > 59 or second > 60:
        return False
    offset = match.group(8)
    if offset and offset != "Z":
        if int(offset[1:3]) > 23 or int(offset[4:6]) > 59:
            return False
    return True



def load_json(path: Path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    return load_json(path)


def load_state_machine(path: Path = STATE_PATH) -> dict:
    return load_json(path)


def type_of(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unknown"


def matches_type(value, expected: str) -> bool:
    actual = type_of(value)
    if expected == "number":
        return actual in ("number", "integer")
    return actual == expected


def compare_decimal(a, b) -> int:
    """Compare decimal strings without float conversion (offsets exceed 2**53)."""
    x, y = str(a), str(b)
    nx, ny = x.startswith("-"), y.startswith("-")
    if nx != ny:
        return -1 if nx else 1
    dx, dy = (x[1:], y[1:]) if nx else (x, y)
    sign = -1 if nx else 1
    if len(dx) != len(dy):
        return -sign if len(dx) < len(dy) else sign
    if dx == dy:
        return 0
    return -sign if dx < dy else sign


def _resolve(schema: dict, node: dict) -> dict:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if not ref.startswith(REF_PREFIX):
        raise ValueError("unsupported $ref: %s" % ref)
    name = ref[len(REF_PREFIX):]
    if name not in schema["$defs"]:
        raise ValueError("missing $defs entry: %s" % name)
    return schema["$defs"][name]


def _validate_node(doc, node, schema, path, errors) -> None:
    node = _resolve(schema, node)
    if not isinstance(node, dict):
        return

    if "const" in node and doc != node["const"]:
        errors.append("%s: expected const %r" % (path, node["const"]))
    if "enum" in node and doc not in node["enum"]:
        errors.append("%s: value %r not in enum" % (path, doc))
    if "type" in node:
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(matches_type(doc, t) for t in types):
            errors.append("%s: expected %s, got %s" % (path, "|".join(types), type_of(doc)))
            return

    if isinstance(doc, str):
        if "pattern" in node and re.search(node["pattern"], doc) is None:
            errors.append("%s: does not match %s" % (path, node["pattern"]))
        if isinstance(node.get("minLength"), int) and len(doc) < node["minLength"]:
            errors.append("%s: shorter than %d" % (path, node["minLength"]))
        if isinstance(node.get("maxLength"), int) and len(doc) > node["maxLength"]:
            errors.append("%s: longer than %d" % (path, node["maxLength"]))
    if isinstance(doc, (int, float)) and not isinstance(doc, bool):
        if isinstance(node.get("minimum"), (int, float)) and doc < node["minimum"]:
            errors.append("%s: below minimum %s" % (path, node["minimum"]))
        if isinstance(node.get("maximum"), (int, float)) and doc > node["maximum"]:
            errors.append("%s: above maximum %s" % (path, node["maximum"]))
    if isinstance(doc, list):
        if isinstance(node.get("minItems"), int) and len(doc) < node["minItems"]:
            errors.append("%s: fewer than %d items" % (path, node["minItems"]))
        if isinstance(node.get("maxItems"), int) and len(doc) > node["maxItems"]:
            errors.append("%s: more than %d items" % (path, node["maxItems"]))
        if "items" in node:
            for index, item in enumerate(doc):
                _validate_node(item, node["items"], schema, "%s[%d]" % (path, index), errors)
    if isinstance(doc, dict):
        for key in node.get("required", []):
            if key not in doc:
                errors.append("%s: missing required %s" % (path, key))
        props = node.get("properties", {})
        for key, value in doc.items():
            if key in props:
                _validate_node(value, props[key], schema, "%s.%s" % (path, key), errors)
            elif node.get("additionalProperties") is False:
                errors.append("%s: unexpected property %s" % (path, key))
            elif isinstance(node.get("additionalProperties"), dict):
                _validate_node(value, node["additionalProperties"], schema, "%s.%s" % (path, key), errors)
    if isinstance(node.get("oneOf"), list):
        passed = 0
        hint = ""
        for branch in node["oneOf"]:
            bucket: list = []
            _validate_node(doc, branch, schema, path, bucket)
            if not bucket:
                passed += 1
            elif not hint:
                hint = bucket[0]
        if passed != 1:
            errors.append("%s: matched %d of %d oneOf branches (%s)"
                          % (path, passed, len(node["oneOf"]), hint))
    if isinstance(node.get("anyOf"), list):
        for branch in node["anyOf"]:
            bucket: list = []
            _validate_node(doc, branch, schema, path, bucket)
            if not bucket:
                break
        else:
            errors.append("%s: matched no anyOf branch" % path)
    if isinstance(node.get("allOf"), list):
        for branch in node["allOf"]:
            _validate_node(doc, branch, schema, path, errors)


def _semantics(def_name: str, doc, errors, path="$.$") -> None:
    if not isinstance(doc, dict):
        return
    at = path
    if def_name == "RecordRef":
        if isinstance(doc.get("bytes"), dict):
            if compare_decimal(doc["bytes"]["start"], doc["bytes"]["end"]) > 0:
                errors.append("%s.bytes: start must be <= end (C1)" % at)
        lines = doc.get("lines")
        if isinstance(lines, dict) and lines["last"] < lines["first"]:
            errors.append("%s.lines: last must be >= first (C2)" % at)
    elif def_name == "RecordHeader":
        if isinstance(doc.get("ref"), dict):
            _semantics("RecordRef", doc["ref"], errors, at + ".ref")
        ref = doc.get("ref")
        if isinstance(ref, dict) and ref.get("bytes") is None and doc.get("provisional") is False:
            errors.append("%s: bytes=null requires provisional=true (C10)" % at)
        if doc.get("ts") is None and "ts_ns" in doc:
            errors.append("%s: ts_ns requires ts (C11)" % at)
        if doc.get("ts") is not None and not valid_timestamp(doc.get("ts")):
            errors.append("%s: ts is not a real instant (C13)" % at)
        for index, field in enumerate(doc.get("fields") or []):
            _semantics("FieldValue", field, errors, "%s.fields[%d]" % (at, index))
    elif def_name == "FieldValue":
        vt, value = doc.get("value_type"), doc.get("value")
        if vt == "integer":
            good = isinstance(value, int) and not isinstance(value, bool)
        elif vt == "number":
            good = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif vt == "boolean":
            good = isinstance(value, bool)
        elif vt == "string":
            good = isinstance(value, str)
        elif vt == "null":
            good = value is None
        else:
            good = False
        if not good:
            errors.append("%s: value_type=%s does not match carried value (C7)" % (at, vt))
    elif def_name == "Finding":
        if isinstance(doc.get("ref"), dict):
            _semantics("RecordRef", doc["ref"], errors, at + ".ref")
    elif def_name == "QuerySpec":
        window = doc.get("time_range")
        if isinstance(window, dict):
            if compare_decimal(window["from_ns"], window["to_ns"]) > 0:
                errors.append("%s.time_range: from_ns must be <= to_ns (C12)" % at)
    elif def_name == "QueryState":
        if isinstance(doc.get("count"), dict):
            _semantics("CountRelation", doc["count"], errors, at + ".count")
        coverage = doc.get("coverage")
        if isinstance(coverage, dict):
            _semantics("Coverage", coverage, errors, at + ".coverage")
        if doc.get("state") == "paused":
            token = doc.get("resume_token")
            if not isinstance(token, str) or token == "":
                errors.append("%s: state=paused requires a resume_token (C5)" % at)
        if doc.get("state") == "partial" and isinstance(coverage, dict) and coverage.get("complete") is True:
            errors.append("%s: state=partial requires coverage.complete=false (C5)" % at)
        count = doc.get("count")
        if doc.get("state") == "done" and isinstance(count, dict) and count.get("relation") == "unknown":
            errors.append("%s: state=done cannot carry an unknown count (C5)" % at)
    elif def_name == "CountRelation":
        relation, matched, returned = doc.get("relation"), doc.get("matched"), doc.get("returned")
        if relation == "unknown" and matched is not None:
            errors.append("%s: relation=unknown requires matched=null (C3)" % at)
        if relation in ("exact", "at_least") and matched is None:
            errors.append("%s: relation=%s requires an integer matched (C3)" % (at, relation))
        if isinstance(matched, int) and isinstance(returned, int) and returned > matched:
            errors.append("%s: returned exceeds matched (C3)" % at)
    elif def_name == "Coverage":
        if compare_decimal(doc["scanned_bytes"], doc["total_bytes"]) > 0:
            errors.append("%s: scanned_bytes exceeds total_bytes (C4)" % at)
        complete = compare_decimal(doc["scanned_bytes"], doc["total_bytes"]) >= 0
        if doc.get("complete") is not complete:
            errors.append("%s: complete must equal (scanned_bytes >= total_bytes) (C4)" % at)
    elif def_name == "TaskProgress":
        if compare_decimal(doc["bytes_done"], doc["bytes_total"]) > 0:
            errors.append("%s: bytes_done exceeds bytes_total (C6)" % at)
        if doc.get("phase") == "uploaded" and doc.get("indexed") is not False:
            errors.append("%s: phase=uploaded cannot imply indexed (C6)" % at)
        if doc.get("phase") == "indexed" and doc.get("indexed") is not True:
            errors.append("%s: phase=indexed requires indexed=true (C6)" % at)


def validate(doc, def_name: str, schema: dict | None = None) -> list:
    """Return a list of human-readable violations; empty means the document is valid."""
    if schema is None:
        schema = load_schema()
    if def_name not in schema["$defs"]:
        raise ValueError("unknown definition: %s" % def_name)
    errors: list = []
    _validate_node(doc, {"$ref": REF_PREFIX + def_name}, schema, "$", errors)
    _semantics(def_name, doc, errors, "$")
    return errors


def project_v1(record):
    """Mirror of ``projectV1`` in src/shared/contracts.js. Returns (ok, value_or_error)."""
    if not isinstance(record, dict):
        return False, "record is not an object"
    if not isinstance(record.get("id"), str) or record["id"] == "":
        return False, "record.id is required"
    if not isinstance(record.get("fileId"), str) or record["fileId"] == "":
        return False, "record.fileId is required"
    if record.get("kind") not in KINDS:
        return False, "unknown kind: %s" % record.get("kind")
    line = record.get("line")
    line_end = record.get("lineEnd", line)
    if not isinstance(line, int) or not isinstance(line_end, int) or line < 1 or line_end < 1:
        return False, "line numbers are 1-based"
    if line_end < line:
        return False, "lineEnd precedes line"
    if not isinstance(record.get("level"), str) or record["level"] == "":
        return False, "record.level is required"
    fields = record.get("fields")
    if not isinstance(fields, list):
        return False, "record.fields must be an array of [key, value] pairs"
    projected = []
    for index, pair in enumerate(fields):
        if not isinstance(pair, list) or len(pair) != 2:
            return False, "fields[%d] is not a [key, value] pair" % index
        projected.append({"key": str(pair[0]), "value": str(pair[1]), "value_type": "string"})
    ordinal = record.get("ordinal") if isinstance(record.get("ordinal"), int) else 0
    ts = record.get("ts")
    return True, {
        "ref": {
            "dataset_id": record["fileId"],
            "record_id": record["id"],
            "version": 1,
            "ordinal": ordinal,
            "bytes": None,
            "lines": {"first": line, "last": line_end},
        },
        "ts": ts if ts else None,
        "level": record["level"],
        "logger": record.get("logger") if isinstance(record.get("logger"), str) else "",
        "kind": record["kind"],
        "has_trace": record.get("hasTrace") is True,
        "message": record.get("message") if isinstance(record.get("message"), str) else "",
        "fields": projected,
        "provisional": True,
    }


def can_transition(machine: dict, kind: str, source: str, target: str) -> bool:
    group = machine.get(kind)
    if not group:
        return False
    return target in group["transitions"].get(source, [])


def main() -> int:
    schema = load_schema()
    cases = load_json(CASES_PATH)["cases"]
    failures = 0
    for case in cases:
        errors = validate(case["doc"], case["def"], schema)
        got = not errors
        if got != case["valid"]:
            failures += 1
            print("FAIL %-40s expected valid=%s got=%s %s"
                  % (case["name"], case["valid"], got, errors[:2]))
    projection = load_json(PROJECTION_PATH)["cases"]
    for case in projection:
        ok, value = project_v1(case["input"])
        if ok != case["valid"]:
            failures += 1
            print("FAIL projection %-28s expected valid=%s got=%s %s"
                  % (case["name"], case["valid"], ok, value))
        elif ok and value != case["expect"]:
            failures += 1
            print("FAIL projection %-28s output mismatch" % case["name"])
    print("contract cases: %d, projection cases: %d, failures: %d"
          % (len(cases), len(projection), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
