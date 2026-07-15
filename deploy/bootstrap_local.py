#!/usr/bin/env python3
"""Create a private Symbient data directory from existing secret files.

The script never prints secret values. It exists so local provisioning can
reuse credentials without copying them through shell history or process args.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _required(values: dict[str, str], key: str, source: Path) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise SystemExit(f"{source} does not contain a non-empty {key}")
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{source} contains an invalid multiline {key}")
    return value


def _atomic_write(path: Path, content: str, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cg-env", type=Path, required=True)
    parser.add_argument("--openai-env", type=Path, required=True)
    parser.add_argument("--allowed-user", action="append", required=True)
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    args = parser.parse_args()

    cg = _read_env(args.cg_env)
    openai = _read_env(args.openai_env)
    allowed_users = [value.strip() for value in args.allowed_user if value.strip()]
    if not allowed_users:
        raise SystemExit("At least one non-empty --allowed-user is required")

    root = Path(__file__).resolve().parent
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.data_dir, 0o700)
    os.chown(args.data_dir, args.uid, args.gid)

    env_values = {
        "OPENAI_API_KEY": _required(openai, "OPENAI_API_KEY", args.openai_env),
        "COMMONGROUND_URL": _required(cg, "CG_URL", args.cg_env),
        "COMMONGROUND_BOT_TOKEN": _required(cg, "CG_BOT_TOKEN", args.cg_env),
        "COMMONGROUND_HOME_CHANNEL": _required(cg, "CG_CHANNEL_ID", args.cg_env),
        "COMMONGROUND_ALLOWED_USERS": ",".join(allowed_users),
        "COMMONGROUND_ALLOW_ALL_USERS": "false",
    }
    env_content = "".join(f"{key}={value}\n" for key, value in env_values.items())
    _atomic_write(args.data_dir / ".env", env_content, 0o600, args.uid, args.gid)
    _atomic_write(
        args.data_dir / "config.yaml",
        (root / "config.yaml").read_text(encoding="utf-8"),
        0o640,
        args.uid,
        args.gid,
    )
    _atomic_write(
        args.data_dir / "SOUL.md",
        (root / "SOUL.md").read_text(encoding="utf-8"),
        0o640,
        args.uid,
        args.gid,
    )
    print(f"Provisioned {args.data_dir} without displaying credentials")


if __name__ == "__main__":
    main()
