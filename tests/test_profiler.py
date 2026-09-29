# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import subprocess
import threading
from unittest import mock

import pytest

from mozphab import environment, profiler
from mozphab.exceptions import CommandError
from mozphab.helpers import prompt
from mozphab.logger import init_logging, logger, stop_logging
from mozphab.subprocess_wrapper import check_output
from mozphab.telemetry import ProfiledGleanObject, TelemetryDisabled


def marker_names(thread):
    return [thread["stringArray"][name] for name in thread["markers"]["name"]]


def test_worker_thread_markers_are_on_the_main_track():
    profiler.start()
    with profiler.marker("Phase", profiler.CATEGORY_OTHER, "Phase", phase="Main"):
        pass

    def worker():
        with profiler.marker("Conduit", profiler.CATEGORY_NETWORK, "Conduit"):
            pass

    thread = threading.Thread(target=worker, name="Worker")
    thread.start()
    thread.join()

    (track,) = profiler.build_profile()["threads"]
    assert track["name"] == "moz-phab"
    assert marker_names(track) == ["Phase", "Conduit"]
    assert track["markers"]["data"] == [
        {"type": "Phase", "phase": "Main"},
        {"type": "Conduit", "thread": "Worker"},
    ]


def test_command_marker():
    profiler.start()
    check_output(["git", "-c", "core.pager=cat", "--no-pager", "config", "--list"])

    thread = profiler.build_profile()["threads"][0]
    assert marker_names(thread) == ["git"]
    assert thread["markers"]["category"] == [profiler.CATEGORY_VCS]
    assert thread["markers"]["data"] == [
        {
            "type": "Command",
            "subcommand": "config",
            "shortCommand": "git config --list",
            "command": "git -c core.pager=cat --no-pager config --list",
            "exitCode": 0,
        }
    ]


def test_prompt_marker_does_not_record_the_answer():
    profiler.start()
    with mock.patch("builtins.input", return_value="secret-token"):
        assert prompt("Paste API Token") == "secret-token"

    thread = profiler.build_profile()["threads"][0]
    assert marker_names(thread) == ["User input"]
    assert thread["markers"]["data"] == [
        {"type": "UserInput", "question": "Paste API Token"}
    ]


def test_network_marker():
    profiler.start()
    with profiler.network_marker("https://phab.test/api/user.whoami") as data:
        data["responseStatus"] = 200

    thread = profiler.build_profile()["threads"][0]
    assert marker_names(thread) == ["GET 1: https://phab.test/api/user.whoami"] * 2
    start, stop = thread["markers"]["data"]
    assert start["status"] == "STATUS_START"
    assert stop["status"] == "STATUS_STOP"
    assert stop["id"] == start["id"]
    assert stop["responseStatus"] == 200


def test_save_keeps_the_latest_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "MOZBUILD_PATH", str(tmp_path))
    monkeypatch.setattr(profiler, "PROFILES_TO_KEEP", 2)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    for name in ("profile-1.json", "profile-2.json"):
        (profiles_dir / name).write_text("{}")

    profiler.start()
    path = profiler.save()
    assert path is not None

    assert profiler.list_profiles() == [profiles_dir / "profile-2.json", path]
    product = json.loads(path.read_text())["meta"]["product"]
    assert product == f"MozPhab {environment.MOZPHAB_VERSION}"


def test_log_markers(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "MOZBUILD_PATH", str(tmp_path))
    init_logging()
    try:
        profiler.start()
        logger.debug("Not printed")
        logger.warning("Printed")
    finally:
        stop_logging()

    thread = profiler.build_profile()["threads"][0]
    markers = thread["markers"]
    assert marker_names(thread) == ["DEBUG", "WARNING"]
    assert markers["data"] == [
        {"type": "Log", "message": "Not printed"},
        {"type": "Log", "message": "Printed", "color": "orange"},
    ]
    assert markers["phase"] == [profiler.MARKER_INSTANT] * 2
    assert markers["category"] == [profiler.CATEGORY_LOGGING] * 2
    assert markers["endTime"] == [None, None]


def test_file_io_marker(tmp_path):
    profiler.start()
    with profiler.file_io("read", tmp_path / "cache"):
        pass

    thread = profiler.build_profile()["threads"][0]
    assert marker_names(thread) == ["FileIO"]
    assert thread["markers"]["data"] == [
        {
            "type": "FileIO",
            "operation": "read",
            "filename": str(tmp_path / "cache"),
        }
    ]


def test_discarded_profile_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "MOZBUILD_PATH", str(tmp_path))
    profiler.start()
    profiler.discard()

    assert profiler.save() is None
    assert profiler.list_profiles() == []


def test_command_marker_exit_code():
    profiler.start()
    with pytest.raises(CommandError):
        check_output(["git", "--no-such-option"], stderr=subprocess.DEVNULL)

    (marker_data,) = profiler.build_profile()["threads"][0]["markers"]["data"]
    assert marker_data["exitCode"] == 129


def test_glean_markers():
    class StringMetric:
        value = None

        def set(self, value):
            self.value = value

    # Like `glean.load_metrics`, categories are classes.
    command = StringMetric()
    metrics = type("mozphab", (), {"usage": type("usage", (), {"command": command})})
    profiler.start()
    ProfiledGleanObject(metrics, "mozphab").usage.command.set("submit")
    disabled = TelemetryDisabled()
    disabled.submission.commits_count.add(2)
    disabled.usage.command_time.stop()

    assert command.value == "submit"
    assert profiler.build_profile()["threads"][0]["markers"]["data"] == [
        {
            "type": "Glean",
            "metric": "mozphab.usage.command",
            "operation": "set",
            "value": "submit",
        },
        {
            "type": "Glean",
            "metric": "mozphab.submission.commits_count",
            "operation": "add",
            "value": "2",
            "telemetry": "disabled",
        },
        {
            "type": "Glean",
            "metric": "mozphab.usage.command_time",
            "operation": "stop",
            "telemetry": "disabled",
        },
    ]


def sample_stacks(thread):
    """Return (time, weight, frame names, category) of each sample."""
    strings = thread["stringArray"]
    stack_table = thread["stackTable"]
    frame_table = thread["frameTable"]
    func_table = thread["funcTable"]
    result = []
    for time, stack, weight in zip(
        thread["samples"]["time"],
        thread["samples"]["stack"],
        thread["samples"]["weight"],
        strict=True,
    ):
        category = stack_table["category"][stack]
        names = []
        while stack is not None:
            func = frame_table["func"][stack_table["frame"][stack]]
            names.insert(0, strings[func_table["name"][func]])
            stack = stack_table["prefix"][stack]
        result.append((round(time, 3), round(weight, 3), names, category))
    return result


def test_samples(monkeypatch):
    profiler.start()
    profiler._markers.extend(
        [
            # Phases aren't in the stacks.
            ("Phase", profiler.CATEGORY_PHASE, 10, 50, {"type": "Phase", "phase": "A"}),
            (
                "git",
                profiler.CATEGORY_VCS,
                20,
                30,
                {"type": "Command", "subcommand": "log"},
            ),
            # Hidden by the git command, then shown until it ends.
            (
                "GET 1: https://phab.test/api/user.whoami?params={}",
                profiler.CATEGORY_NETWORK,
                25,
                40,
                {
                    "type": "Network",
                    "status": "STATUS_STOP",
                    "URI": "https://phab.test/api/user.whoami?params={}",
                },
            ),
            ("User input", profiler.CATEGORY_USER_INPUT, 60, 70, {"type": "UserInput"}),
            # Instant markers are ignored.
            ("INFO", profiler.CATEGORY_LOGGING, 65, None, {"type": "Log"}),
        ]
    )
    monkeypatch.setattr(profiler, "_now", lambda: 80)

    thread = profiler.build_profile()["threads"][0]
    assert thread["samples"]["weightType"] == "tracing-ms"
    root = ["moz-phab"]
    python = profiler.CATEGORY_PYTHON
    vcs = profiler.CATEGORY_VCS
    network = profiler.CATEGORY_NETWORK
    user_input = profiler.CATEGORY_USER_INPUT
    assert sample_stacks(thread) == [
        (0, 20, root, python),
        (19.999, 0, root, python),
        (20, 10, [*root, "git", "log"], vcs),
        (29.999, 0, [*root, "git", "log"], vcs),
        (30, 10, [*root, "https://phab.test/api/", "user.whoami"], network),
        (39.999, 0, [*root, "https://phab.test/api/", "user.whoami"], network),
        (40, 20, root, python),
        (59.999, 0, root, python),
        (60, 10, [*root, "Waiting for user input"], user_input),
        (69.999, 0, [*root, "Waiting for user input"], user_input),
        (70, 10, root, python),
        (79.999, 0, root, python),
    ]


def test_malformed_log_message_does_not_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "MOZBUILD_PATH", str(tmp_path))
    profiler.start()
    logger.debug("%s %s", 1)

    assert marker_names(profiler.build_profile()["threads"][0]) == []
