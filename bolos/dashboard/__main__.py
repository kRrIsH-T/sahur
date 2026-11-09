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
from bolos.dashboard.server import DEFAULT_PORT, Dashboard, ping
from bolos.history import history_path
from bolos.user_settings import UserSettings, env_defaults, settings_path


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python -m bolos.dashboard")
    cli.add_argument("--port", type=int, default=DEFAULT_PORT)
    cli.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = cli.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    url = f"http://127.0.0.1:{args.port}/"
    if ping(args.port) is not None:  # Bol OS (or another window of this) already serves it
        print(f"Dashboard: {url}")
        if not args.no_open:
            webbrowser.open(url)
        return 0
    store = UserSettings.load(settings_path(), env_defaults(load_settings()))
    settings = load_settings(store.overrides())
    dashboard = Dashboard(Controls(store), history_path(settings.preferences_path), args.port)
    url = dashboard.start()
    print(f"Dashboard: {url} (Bol OS is not running: changes are saved for its next start). Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        while not dashboard.stopped.wait(0.5):
            pass
        print("Bol OS started and serves the dashboard now.")  # it asked this one to step aside
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
