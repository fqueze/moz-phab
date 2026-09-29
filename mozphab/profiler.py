# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Record a timeline of each run and save it in the Firefox Profiler format.

Markers are recorded for the phases of each command, VCS commands, network
requests, file I/O, sleeps, user input, log messages and telemetry.
`moz-phab profile` opens the saved profiles in https://profiler.firefox.com,
similar to `mach resource-usage`.
"""

import itertools
import json
import os
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from mozphab import environment

PROFILES_TO_KEEP = 20

# Index in the `categories` list of the profile metadata.
CATEGORY_OTHER = 0
CATEGORY_NETWORK = 1
CATEGORY_USER_INPUT = 2
# The marker chart sorts categories alphabetically, so this name puts phases
# at the top.
CATEGORY_PHASE = 3
CATEGORY_LOGGING = 4
CATEGORY_VCS = 5
# Only used by samples, for the time not spent waiting.
CATEGORY_PYTHON = 6

# When waiting for several things at once, e.g. network requests in worker
# threads during a git command, samples show the first one of these.
WAITING_PRIORITY = [CATEGORY_USER_INPUT, CATEGORY_VCS, CATEGORY_NETWORK]

# The timeline draws each sample from halfway to the previous sample to halfway
# to the next one, so a sample this close to the end of each segment keeps the
# color from changing before the segment ends.
SEGMENT_END_SAMPLE_OFFSET = 0.001

MARKER_INSTANT = 0
MARKER_INTERVAL = 1

# (name, category, start, end, data); instant markers have no end.
Marker = tuple[str, int, float, float | None, dict[str, Any]]
# Used to build samples: (start, end, category, frames) of what is being
# waited for, and (name, category) for the frames of their stacks.
Interval = tuple[float, float, int, list[str]]
Frame = tuple[str, int]

DEFAULT_TRACK_NAME = "moz-phab"

_start_wall: float = time.time()
_start: float = time.perf_counter()
_markers: list[Marker] = []
_track_name = DEFAULT_TRACK_NAME
_network_ids = itertools.count(1)
_discarded = False


def profiles_dir() -> Path:
    return Path(environment.MOZBUILD_PATH) / "profiles"


def start():
    """Start a new recording, dropping any previously recorded marker."""
    global _start_wall, _start, _network_ids, _track_name, _discarded
    _start_wall = time.time()
    _start = time.perf_counter()
    _network_ids = itertools.count(1)
    _track_name = DEFAULT_TRACK_NAME
    _discarded = False
    _markers.clear()


def discard():
    """Don't save the profile of this run."""
    global _discarded
    _discarded = True


def set_track_name(name: str):
    """Name the track after the command being run."""
    global _track_name
    _track_name = name


def _now() -> float:
    return (time.perf_counter() - _start) * 1000


def _append(
    name: str,
    category: int,
    start_time: float,
    end_time: float | None,
    data: dict[str, Any],
):
    """Add a marker to the recording.

    Markers of all threads are shown on a single track, so that they can be
    compared with the phases of the main thread; the marker chart puts
    overlapping markers on separate rows.
    """
    thread = threading.current_thread()
    if thread is not threading.main_thread():
        data["thread"] = thread.name
    # `list.append` is atomic, so markers can be added from worker threads.
    _markers.append((name, category, start_time, end_time, data))


@contextmanager
def marker(name: str, category: int, marker_type: str, **data):
    """Record a marker spanning the body of the `with` statement.

    Yields the marker data dict, which can be updated before the marker ends.
    """
    data["type"] = marker_type
    start_time = _now()
    try:
        yield data
    finally:
        _append(name, category, start_time, _now(), data)


def add_instant_marker(name: str, category: int, marker_type: str, **data):
    """Record a marker without duration."""
    data["type"] = marker_type
    _append(name, category, _now(), None, data)


def phase(name: str) -> AbstractContextManager[dict]:
    """Record a phase marker, the only kind shown in the timeline."""
    return marker("Phase", CATEGORY_PHASE, "Phase", phase=name)


def add_startup_phase():
    """Record a phase from the start of the recording until now.

    The recording starts when this module is imported, so this phase covers
    loading the other modules of moz-phab.
    """
    _append(
        "Phase",
        CATEGORY_PHASE,
        0,
        _now(),
        {"type": "Phase", "phase": "Loading Python modules"},
    )


@contextmanager
def network_marker(uri: str, method: str = "GET"):
    """Record a network marker spanning the body of the `with` statement.

    Yields the marker data dict, where `responseStatus`, `count` (the size of
    the response), `contentType` and `httpVersion` can be set.
    """
    # The front-end merges a start and a stop marker with the same `id` into a
    # single request, displayed in the network track.
    # Gecko names these markers `Load <id>: <uri>`; the front-end only
    # expects a colon before the URI, so use the name to show the method.
    load_id = next(_network_ids)
    name = f"{method} {load_id}: {uri}"
    data: dict[str, Any] = {"type": "Network", "URI": uri, "id": load_id, "pri": 0}
    start_time = _now()
    _append(
        name,
        CATEGORY_NETWORK,
        start_time,
        start_time,
        {
            **data,
            "status": "STATUS_START",
            "startTime": start_time,
            "endTime": start_time,
        },
    )
    data["status"] = "STATUS_STOP"
    try:
        yield data
    except BaseException as e:
        data["status"] = "STATUS_CANCEL"
        data["requestStatus"] = type(e).__name__
        # `urllib.error.HTTPError` has the HTTP status code.
        if isinstance(code := getattr(e, "code", None), int):
            data["responseStatus"] = code
        raise
    finally:
        end_time = _now()
        data.update(
            startTime=start_time,
            endTime=end_time,
            requestStart=start_time,
            responseStart=end_time,
            responseEnd=end_time,
        )
        _append(name, CATEGORY_NETWORK, start_time, end_time, data)


def file_io(
    operation: str, filename: str | os.PathLike
) -> AbstractContextManager[dict]:
    """Record a file I/O marker, `operation` being e.g. "read" or "write"."""
    return marker(
        "FileIO",
        CATEGORY_OTHER,
        "FileIO",
        operation=operation,
        filename=str(filename),
    )


def sleep_marker(reason: str) -> AbstractContextManager[dict]:
    """Record a marker for the time spent sleeping or waiting."""
    return marker("Sleep", CATEGORY_OTHER, "Sleep", reason=reason)


def _build_meta(duration: float) -> dict[str, Any]:
    def schema(
        name: str,
        chart_label: str,
        fields: list[tuple[str, str, str]],
        label: str | None = None,
        timeline: bool = False,
    ):
        """Build a marker schema, `label` defaulting to `chart_label`."""
        fields = [*fields, ("thread", "Thread", "string")]
        return {
            "name": name,
            "tooltipLabel": label or chart_label,
            "tableLabel": label or chart_label,
            "chartLabel": chart_label,
            "display": [
                "marker-chart",
                "marker-table",
                *(["timeline-overview"] if timeline else []),
            ],
            "data": [
                {"key": key, "label": field_label, "format": field_format}
                for key, field_label, field_format in fields
            ],
        }

    return {
        "processType": 0,
        # The front-end would only keep the final digits and dots of a version
        # in `misc`, e.g. "20260929" for "0.1.dev842+g521dd9831.d20260929".
        "product": f"{environment.MOZPHAB_NAME} {environment.MOZPHAB_VERSION}",
        "stackwalk": 0,
        "version": 27,
        "preprocessedProfileVersion": 47,
        "symbolicationNotSupported": True,
        "interval": 1,
        "startTime": _start_wall * 1000,
        "profilingStartTime": 0,
        "profilingEndTime": duration,
        "categories": [
            {"name": "Other", "color": "grey", "subcategories": ["Other"]},
            {"name": "Network", "color": "blue", "subcategories": ["Other"]},
            # Transparent in the timeline, as moz-phab is idle.
            {"name": "User input", "color": "transparent", "subcategories": ["Other"]},
            {"name": "Activity", "color": "purple", "subcategories": ["Other"]},
            {"name": "Logging", "color": "grey", "subcategories": ["Other"]},
            {"name": "VCS", "color": "yellow", "subcategories": ["Other"]},
            {"name": "Python", "color": "green", "subcategories": ["Other"]},
        ],
        "markerSchema": [
            schema(
                "Phase",
                "{marker.data.phase}",
                [("phase", "Phase", "string")],
                timeline=True,
            ),
            {
                **schema(
                    "Log",
                    "{marker.data.message}",
                    [("message", "Message", "string")],
                ),
                "colorField": "color",
            },
            # The front-end only knows the FileIO schema from Gecko profiles.
            schema(
                "FileIO",
                "{marker.data.operation} — {marker.data.filename}",
                [
                    ("operation", "Operation", "string"),
                    ("filename", "Filename", "file-path"),
                ],
            ),
            schema(
                "Glean",
                "{marker.data.metric}",
                [
                    ("metric", "Metric", "string"),
                    ("operation", "Operation", "string"),
                    ("value", "Value", "string"),
                    ("telemetry", "Telemetry", "string"),
                ],
                label="{marker.data.metric}.{marker.data.operation}({marker.data.value})",
            ),
            schema("Sleep", "{marker.data.reason}", [("reason", "Reason", "string")]),
            schema(
                "UserInput",
                "{marker.data.question}",
                [("question", "Question", "string")],
            ),
            schema(
                "Command",
                "{marker.data.subcommand}",
                [
                    ("subcommand", "Subcommand", "string"),
                    ("command", "Command", "string"),
                    ("exitCode", "Exit code", "integer"),
                ],
                label="{marker.data.shortCommand}",
            ),
        ],
    }


def _build_thread() -> dict[str, Any]:
    return {
        "processType": "default",
        "processName": environment.MOZPHAB_NAME,
        "processStartupTime": 0,
        "processShutdownTime": None,
        "registerTime": 0,
        "unregisterTime": None,
        "pausedRanges": [],
        "showMarkersInTimeline": True,
        "name": _track_name,
        "isMainThread": True,
        "pid": str(os.getpid()),
        "tid": threading.main_thread().native_id or 0,
        "samples": {
            "weightType": "tracing-ms",
            "weight": [],
            "stack": [],
            "time": [],
            "length": 0,
        },
        "markers": {
            "data": [],
            "name": [],
            "startTime": [],
            "endTime": [],
            "phase": [],
            "category": [],
            "length": 0,
        },
        "stackTable": {
            "frame": [],
            "prefix": [],
            "category": [],
            "subcategory": [],
            "length": 0,
        },
        "frameTable": {
            "address": [],
            "inlineDepth": [],
            "category": [],
            "subcategory": [],
            "func": [],
            "nativeSymbol": [],
            "innerWindowID": [],
            "implementation": [],
            "line": [],
            "column": [],
            "length": 0,
        },
        "funcTable": {
            "isJS": [],
            "relevantForJS": [],
            "name": [],
            "resource": [],
            "fileName": [],
            "lineNumber": [],
            "columnNumber": [],
            "length": 0,
        },
        "resourceTable": {"lib": [], "name": [], "host": [], "type": [], "length": 0},
        "nativeSymbols": {
            "libIndex": [],
            "address": [],
            "name": [],
            "functionSize": [],
            "length": 0,
        },
        "stringArray": [],
    }


def build_profile() -> dict[str, Any]:
    """Return the recorded markers as a Firefox Profiler processed profile."""
    duration = _now()
    thread = _build_thread()
    strings = thread["stringArray"]
    string_indexes: dict[str, int] = {}
    markers = thread["markers"]
    for name, category, start_time, end_time, data in sorted(
        _markers, key=lambda m: m[2]
    ):
        name_index = string_indexes.setdefault(name, len(strings))
        if name_index == len(strings):
            strings.append(name)

        markers["data"].append(data)
        markers["name"].append(name_index)
        markers["startTime"].append(start_time)
        markers["endTime"].append(end_time)
        markers["phase"].append(MARKER_INSTANT if end_time is None else MARKER_INTERVAL)
        markers["category"].append(category)
        markers["length"] += 1

    _add_samples(thread, duration)
    return {
        "meta": _build_meta(duration),
        "libs": [],
        "threads": [thread],
        "counters": [],
    }


def _waiting_frames(
    name: str, category: int, data: dict[str, Any]
) -> tuple[int, list[str]] | None:
    """Return the category and frames of what a marker is waiting for, if any."""
    marker_type = data["type"]
    if marker_type == "UserInput":
        return CATEGORY_USER_INPUT, ["Waiting for user input"]
    if marker_type == "Command":
        # Not all commands are VCS commands, e.g. when updating moz-phab.
        return category, [name, data["subcommand"]]
    # Start markers have no duration, so only the stop markers get here.
    if marker_type == "Network":
        # E.g. the Conduit API URL, then the method name.
        base, _, last = data["URI"].split("?")[0].rpartition("/")
        return CATEGORY_NETWORK, [base + "/", last]
    if marker_type == "Sleep":
        return CATEGORY_OTHER, ["Sleep", data["reason"]]
    return None


def _add_samples(thread: dict[str, Any], duration: float):
    """Add samples coloring the timeline by what moz-phab was waiting for.

    The time is split in segments during which moz-phab is waiting for the
    same thing, or not waiting and running Python code. Each segment gets a
    sample at its start, weighted by its duration for the call tree, and a
    sample at its end for the timeline.
    """
    intervals: list[Interval] = []
    for name, category, start_time, end_time, data in _markers:
        if end_time is None or end_time <= start_time:
            continue
        if waiting := _waiting_frames(name, category, data):
            intervals.append((start_time, end_time, *waiting))

    def waiting_rank(interval: Interval) -> tuple[int, float]:
        category = interval[2]
        priority = len(WAITING_PRIORITY)
        if category in WAITING_PRIORITY:
            priority = WAITING_PRIORITY.index(category)
        # Prefer the most recent one.
        return priority, -interval[0]

    # Segments as (start, end, stack), merging the consecutive ones that have
    # the same stack. Every interval start and end is a boundary, so intervals
    # running at the start of a segment run until its end.
    segments: list[tuple[float, float, tuple[Frame, ...]]] = []
    boundaries = sorted({0, duration, *(t for i in intervals for t in i[:2])})
    by_start = sorted(intervals, key=lambda i: i[0], reverse=True)
    by_end = sorted(intervals, key=lambda i: i[1], reverse=True)
    active: list[Interval] = []
    for start, end in itertools.pairwise(boundaries):
        while by_start and by_start[-1][0] <= start:
            active.append(by_start.pop())
        while by_end and by_end[-1][1] <= start:
            active.remove(by_end.pop())

        stack: list[Frame] = [(_track_name, CATEGORY_PYTHON)]
        if active:
            _, _, category, frames = min(active, key=waiting_rank)
            stack += [(frame, category) for frame in frames]
        if segments and segments[-1][2] == tuple(stack):
            segments[-1] = (segments[-1][0], end, segments[-1][2])
        else:
            segments.append((start, end, tuple(stack)))

    strings = thread["stringArray"]
    func_indexes: dict[str, int] = {}
    frame_indexes: dict[tuple[str, int], int] = {}
    stack_indexes: dict[tuple[int | None, int], int] = {}

    def add_row(table: dict[str, Any], **row):
        for key, value in row.items():
            table[key].append(value)
        table["length"] += 1
        return table["length"] - 1

    def get_stack(frames: tuple[Frame, ...]) -> int:
        stack_index = None
        for name, category in frames:
            if name not in func_indexes:
                strings.append(name)
                func_indexes[name] = add_row(
                    thread["funcTable"],
                    isJS=False,
                    relevantForJS=False,
                    name=len(strings) - 1,
                    resource=-1,
                    fileName=None,
                    lineNumber=None,
                    columnNumber=None,
                )
            if (name, category) not in frame_indexes:
                frame_indexes[name, category] = add_row(
                    thread["frameTable"],
                    address=-1,
                    inlineDepth=0,
                    category=category,
                    subcategory=0,
                    func=func_indexes[name],
                    nativeSymbol=None,
                    innerWindowID=0,
                    implementation=None,
                    line=None,
                    column=None,
                )
            key = (stack_index, frame_indexes[name, category])
            if key not in stack_indexes:
                stack_indexes[key] = add_row(
                    thread["stackTable"],
                    frame=key[1],
                    prefix=stack_index,
                    category=category,
                    subcategory=0,
                )
            stack_index = stack_indexes[key]
        assert stack_index is not None
        return stack_index

    samples = thread["samples"]
    for start, end, frames in segments:
        stack_index = get_stack(frames)
        add_row(samples, time=start, stack=stack_index, weight=end - start)
        if end - start > 2 * SEGMENT_END_SAMPLE_OFFSET:
            add_row(
                samples,
                time=end - SEGMENT_END_SAMPLE_OFFSET,
                stack=stack_index,
                weight=0,
            )


def list_profiles() -> list[Path]:
    """Return the saved profiles, oldest first."""
    return sorted(profiles_dir().glob("profile-*.json"))


def save() -> Path | None:
    """Save the recorded profile and delete the oldest ones.

    Returns the path of the profile, or `None` if it was discarded.
    """
    if _discarded:
        return None

    directory = profiles_dir()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(_start_wall))
    path = directory / f"profile-{timestamp}-{os.getpid()}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(build_profile(), f, separators=(",", ":"))

    for old_profile in list_profiles()[:-PROFILES_TO_KEEP]:
        old_profile.unlink(missing_ok=True)

    return path
