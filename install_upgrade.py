from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path


ROOT_FILES = [
    "db.py",
    "core.py",
    "initial_db.sql",
    "vinted_notifications.py",
    "url_normalizer.py",
]

PLUGIN_FILES = [
    Path("telegram_bot_plugin") / "telegram_bot.py",
    Path("web_ui_plugin") / "web_ui.py",
]


def locate_default_project() -> Path | None:
    candidates = [
        Path.cwd(),
        Path.home() / "Documents" / "Vinted-Notifications",
        Path("/opt/Vinted-Notifications"),
        Path("/srv/Vinted-Notifications"),
        Path.home() / "Vinted-Notifications",
    ]
    for candidate in candidates:
        if (candidate / "vinted_notifications.py").is_file():
            return candidate.resolve()
    return None


def copy_with_parents(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the Vinted Notifications quiet-hours and query-sorting upgrade."
    )
    parser.add_argument(
        "--project",
        help="Full path to the Vinted-Notifications project folder.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the final confirmation prompt.",
    )
    args = parser.parse_args()

    package = Path(__file__).resolve().parent
    default_project = locate_default_project()

    if args.project:
        project = Path(args.project).expanduser().resolve()
    else:
        prompt_default = str(default_project) if default_project else ""
        prompt = "Vinted-Notifications project folder"
        if prompt_default:
            prompt += f" [{prompt_default}]"
        prompt += ": "
        entered = input(prompt).strip()
        project = Path(entered or prompt_default).expanduser().resolve()

    if not (project / "vinted_notifications.py").is_file():
        print(f"ERROR: vinted_notifications.py was not found in {project}")
        return 1

    missing_package_files = [
        str(Path(name))
        for name in ROOT_FILES
        if not (package / name).is_file()
    ]
    missing_package_files.extend(
        str(relative)
        for relative in PLUGIN_FILES
        if not (package / relative).is_file()
    )
    if missing_package_files:
        print("ERROR: The extracted upgrade package is incomplete:")
        for name in missing_package_files:
            print(f"  - {name}")
        return 1

    print()
    print("Stop Vinted Notifications before continuing.")
    print(f"Project: {project}")
    print(f"Package: {package}")

    if not args.yes:
        answer = input("Type INSTALL to create a backup and continue: ").strip()
        if answer != "INSTALL":
            print("Nothing was changed.")
            return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = project / f"backup-quiet-hours-sorting-{timestamp}"
    backup.mkdir(parents=True, exist_ok=False)

    relative_files = [Path(name) for name in ROOT_FILES] + PLUGIN_FILES
    for relative in relative_files:
        existing = project / relative
        if existing.is_file():
            copy_with_parents(existing, backup / relative)

    database = project / "data" / "vinted_notifications.db"
    if database.is_file():
        copy_with_parents(database, backup / "data" / "vinted_notifications.db")

    for relative in relative_files:
        copy_with_parents(package / relative, project / relative)

    print()
    print("Installation completed.")
    print(f"Backup: {backup}")
    print()
    print("Restart Vinted Notifications, then open the Configuration page.")
    print("Keep timezone Europe/London for UK quiet hours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
