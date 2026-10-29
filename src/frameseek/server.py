"""Container entry point. Starts one local process; never deploys cloud resources."""

import os
import sys


def server_command(environ=None):
    env = os.environ if environ is None else environ
    component = env.get("FS_COMPONENT", "workspace")
    port = env.get("PORT", "8080")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("PORT must be an integer from 1 to 65535")
    if component not in {"workspace", "gateway"}:
        raise RuntimeError("FS_COMPONENT must be workspace or gateway")
    if component == "workspace" and env.get("K_SERVICE"):
        raise RuntimeError(
            "Cloud Run must use FS_COMPONENT=gateway; the SQLite workspace needs "
            "one backend with a durable local disk and verified media isolation"
        )
    if component == "workspace" and env.get("FS_REQUIRE_DATA_MOUNT") == "1":
        if not os.path.ismount(env.get("FS_DATA_DIR", "/data")):
            raise RuntimeError("Workspace requires an explicit durable data volume mount")
    factory = (
        "frameseek.gateway:create_gateway" if component == "gateway" else "frameseek.api:create_app"
    )
    return [
        sys.executable,
        "-m",
        "uvicorn",
        factory,
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--workers",
        "1",
        "--no-proxy-headers",
    ]


def main():
    try:
        command = server_command()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
