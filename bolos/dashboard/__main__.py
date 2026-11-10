"""Open the dashboard.

    uv run python -m bolos.dashboard            open it in the browser (served by Bol OS, or here until Ctrl+C)
    uv run python -m bolos.dashboard --no-open  just serve it
"""

import argparse
import logging
import sys
import webbrowser

from bolos.config import load_settings
from bolos.controls import Controls
from bolos.dashboard.server import DEFAULT_PORT, Dashboard, find_dashboard
from bolos.history import history_path
from bolos.user_settings import UserSettings, env_defaults, settings_path


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python -m bolos.dashboard")
    cli.add_argument("--port", type=int, default=DEFAULT_PORT)
    cli.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = cli.parse_args(argv)
    if not 0 <= args.port <= 65535:
        cli.error("Choose a dashboard port from 0 to 65535.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    port = find_dashboard(args.port) if args.port else None
    if port is not None:  # Sahur (or another settings window) already serves it
        url = f"http://127.0.0.1:{port}/"
        print(f"Dashboard: {url}")
        if not args.no_open:
            webbrowser.open(url)
        return 0
    store = UserSettings.load(settings_path(), env_defaults(load_settings()))
    settings = load_settings(store.overrides())
    dashboard = Dashboard(Controls(store), history_path(settings.preferences_path), args.port)
    url = dashboard.start()
    print(f"Sahur settings: {url} (changes are saved for the next time Sahur opens). Ctrl+C to close.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        while not dashboard.stopped.wait(0.5):
            pass
        print("Sahur is open. Keep using this page for settings.")
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
