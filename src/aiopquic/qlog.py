"""qlog file loading — the parser every consumer otherwise rewrites.

picoquic emits either classic JSON (.qlog: one document, events nested
under traces[].events) or JSON-SEQ (.sqlog: RFC 7464 RS-delimited
records, first record the file header, each subsequent record one
event). load() handles both and returns a flat list of event dicts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_RS = b"\x1e"


def load(path) -> List[Dict[str, Any]]:
    """Parse a .qlog/.sqlog file into a flat list of event dicts.

    Classic JSON events arrive as qlog dicts; JSON-SEQ events keep
    their record shape ({time, name, data, ...}). The file header /
    trace metadata is not included — use load_header() for it.
    """
    raw = Path(path).read_bytes()
    if raw.lstrip()[:1] != _RS:
        doc = json.loads(raw)
        events = []
        for trace in doc.get("traces", []):
            # qlog 0.3-era traces carry array events with a per-trace
            # event_fields legend; normalize those to dicts.
            fields = trace.get("event_fields")
            for ev in trace.get("events", []):
                if isinstance(ev, list) and fields:
                    events.append(dict(zip(fields, ev)))
                else:
                    events.append(ev)
        return events
    events = []
    for record in raw.split(_RS):
        record = record.strip()
        if not record:
            continue
        obj = json.loads(record)
        # First record is the log header (qlog_version/title/trace);
        # events carry a time field.
        if "time" in obj or "name" in obj:
            events.append(obj)
    return events


def load_header(path) -> Dict[str, Any]:
    """The file header / trace metadata (qlog_version, vantage point,
    common_fields) for either format."""
    raw = Path(path).read_bytes()
    if raw.lstrip()[:1] != _RS:
        return {k: v for k, v in json.loads(raw).items() if k != "traces"}
    for record in raw.split(_RS):
        record = record.strip()
        if record:
            obj = json.loads(record)
            if "time" not in obj and "name" not in obj:
                return obj
            break
    return {}
