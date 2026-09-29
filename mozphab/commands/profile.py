# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import os
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

from mozphab import profiler
from mozphab.exceptions import Error
from mozphab.logger import logger

# Can be set to e.g. http://localhost:4242 to use a local Firefox Profiler.
PROFILER_ORIGIN = os.environ.get(
    "PROFILER_ORIGIN", "https://profiler.firefox.com"
).rstrip("/")


class ProfileServer(HTTPServer):
    """Serve a single profile to the Firefox Profiler, then stop."""

    def __init__(self, path: Path):
        super().__init__(("127.0.0.1", 0), ProfileRequestHandler)
        self.profile_path = path
        self.served = False


class ProfileRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        assert isinstance(server, ProfileServer)
        if self.path != "/" + quote(server.profile_path.name):
            self.send_error(404)
            return

        data = server.profile_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", PROFILER_ORIGIN)
        self.end_headers()
        self.wfile.write(data)
        server.served = True

    def log_message(self, format, *args):
        logger.debug(format, *args)


def list_profiles():
    profiles = profiler.list_profiles()
    if not profiles:
        logger.info("No profile found in %s", profiler.profiles_dir())
        return

    for path in reversed(profiles):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        logger.info("%s  %s", modified.strftime("%Y-%m-%d %H:%M:%S"), path)


def open_profile(args: argparse.Namespace):
    # Don't let this run replace one of the profiles it lists or opens.
    profiler.discard()

    if args.list:
        list_profiles()
        return

    if args.path:
        path = Path(args.path).resolve()
        if not path.is_file():
            raise Error(f"Profile not found: {path}")
    else:
        profiles = profiler.list_profiles()
        if not profiles:
            raise Error(f"No profile found in {profiler.profiles_dir()}")
        path = profiles[-1]

    server = ProfileServer(path)
    port = server.server_address[1]
    profile_url = f"http://127.0.0.1:{port}/{quote(path.name)}"
    profiler_url = (
        f"{PROFILER_ORIGIN}/from-url/{quote(profile_url, safe='')}/marker-chart/"
    )

    logger.info("Serving %s at %s", path, profile_url)
    # The browser may not be able to reach this server, e.g. over SSH.
    logger.info("Opening %s", profiler_url)
    webbrowser.open_new_tab(profiler_url)

    try:
        while not server.served:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def add_parser(parser):
    profile_parser = parser.add_parser(
        "profile",
        help="Open the profile of a previous run in the Firefox Profiler.",
        description=(
            "Open the profile of a previous run in the Firefox Profiler. Set "
            "PROFILER_ORIGIN to use another instance of the profiler, "
            "e.g. http://localhost:4242."
        ),
    )
    profile_parser.add_argument(
        "path",
        nargs="?",
        help="Profile to open. Defaults to the most recent one.",
    )
    profile_parser.add_argument(
        "--list",
        action="store_true",
        help="List the saved profiles, most recent first.",
    )
    profile_parser.set_defaults(func=open_profile, needs_repo=False)
