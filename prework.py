#!/usr/bin/env python3
"""Johto pre-work: one Docker lane for a pre-work run, then collect its outputs.

    python3 prework.py setup      build the lane for your Johto task
    python3 prework.py collect    write the diff and transcript for Studio

Setup clones the task's repo into a Docker container (the "lane") and leaves a
launcher in model_a/: `cd model_a && ./claude-dev` starts Claude Code in the
lane, where you /login with your own Claude account. Collect writes
prework_output/final.diff (Final Output) and the session transcript .jsonl
(Conversation Log) for you to attach to the task in Studio.

Standard library only, Python 3.9+, macOS, Linux and WSL. Everything this tool
creates is named prework-* (Docker) or lives under ~/.mercor-prework/ and in
the task folder, so it never touches other Docker lanes on the machine.
"""

import argparse
import errno
import hashlib
import http.client
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

TOOL_VERSION = "1.0.0"
# CI publishes this file as prework-v<RELEASE>; published versions never change,
# so any change here needs a higher RELEASE (a PR check enforces it).
RELEASE = 2

# --------------------------------------------------------------------------
# Pins. A new image or Claude Code build is a new prework-vN directory, so an
# expert mid-task is never moved onto a different build.
# --------------------------------------------------------------------------

CLAUDE_CODE_VERSION = "2.1.311"
# The images are published once, as prework-v1, and downloaded in the browser
# from the dashboard's Setup page, which a terminal cannot reach through Passport.
IMAGE_VERSION = "prework-v1"
SETUP_PAGE = "https://cc-johto.vercel.app/setup"
# image_ids holds both forms Docker reports: the config digest (classic image
# store) and the manifest digest (containerd image store).
LANE_IMAGES = {
    "amd64": {
        "file": "lane-image-amd64-28e11eaa6079.tar.gz",
        "sha256": "28e11eaa60799b7c97fddab87f477c3f15dae9fd6036e42f2365ebd4172aa7fa",
        "size": 765446073,
        "image_ids": (
            "sha256:94459c2fada557a842fb75f0a6e5c70aa63ed3837bba84d1c30e197b9302f019",
            "sha256:1dbce316a763e10c88a7b5d7adbe2e0f525022b189f59d744d9c4b56f8930b2b",
        ),
    },
    "arm64": {
        "file": "lane-image-arm64-d1a0aaf2094a.tar.gz",
        "sha256": "d1a0aaf2094a477509e9a1a8edd4a99c3c74763b7478b27594a5ff3f5585e0f7",
        "size": 738279972,
        "image_ids": (
            "sha256:c6592c5f33f41d1ad6e0ffbe4e18f5e0022abeb28a02a904ed3b0e2b35dda7fb",
            "sha256:6a4af88dfe8e9cd574a4131290c14826878edb78c50e8c7e92f9ff8afee38856",
        ),
    },
}
LANE_IMAGE_ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
# A copy of the pinned tarball somewhere else, still checked against the pin.
IMAGE_FILE_ENV = "PREWORK_IMAGE_FILE"

# The repo the published tarball tags its image with. The ONLY place that name
# appears: load_lane_image retags the image and puts that tag back as it found it.
TARBALL_IMAGE_REPO = "codeprefs-lane"
LANE_IMAGE_REPO = "mercor-prework-lane"

# --------------------------------------------------------------------------
# Names. Every Docker object starts with CONTAINER_PREFIX, and nothing here
# stops or removes an object by any other name.
# --------------------------------------------------------------------------

CONTAINER_PREFIX = "prework-"
LANE = "model_a"
CONTAINER_ROOT = "/exp"
LANE_HOME = "/home/lane"
LANE_IMAGE_UID = 1001  # useradd -u in the lane image
CONTAINER_HOME = f"{LANE_HOME}/.mercor-prework"
HARNESS_MOUNT = "/opt/prework-harness"
# (inside, host): a dev server on in-lane port 3000 reaches the host at 34000.
LANE_PORT_RANGES = (("3000-3019", "34000-34019"), ("8000-8009", "35000-35009"))
# The task folder that created a container; checked before any reuse or removal.
LANE_ROOT_LABEL = "prework.root"

TASK_ID_FILE = ".prework-task-id"
RUN_ID_FILE = ".prework-run-id"
DOCKER_BINDING = ".prework-docker.json"
MANIFEST = "prework-manifest.json"
OUTPUT_DIR = "prework_output"
STAGING_DIR = ".prework-clone"
# Where Claude Code on Linux keeps a /login, inside CLAUDE_CONFIG_DIR.
CLAUDE_LOGIN_FILE = ".credentials.json"
# Written onto the workspace volume for the duration of a build only.
LANE_BUILDING = ".prework-lane-building"

LANE_READY_TIMEOUT_SECONDS = 30.0
# 45% of the engine, or 90% under 8 GiB; 3 GiB is the least a build survives on.
LANE_MEM_SHARE_PCT = 45
LANE_MEM_SOLO_PCT = 90
SMALL_ENGINE_GIB = 8
LANE_MEM_FLOOR_GIB = 3
LANE_SWAP_FACTOR = 1.5
# About one CPU per 2 GiB: tools start a worker per CPU they see.
LANE_GIB_PER_CPU = 2
LANE_MIN_CPUS = 2

# A long agentic turn outlives Claude Code's default timeouts; the image pins the build.
LANE_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "API_TIMEOUT_MS": "1800000",
    "API_FORCE_IDLE_TIMEOUT": "0",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
}
LANE_DENIED_TOOLS = ("TodoWrite", "TaskCreate", "TaskGet", "TaskUpdate", "TaskList")

# Legal/ops: confirm this notice still applies to pre-work before release.
TRAINING_DATA_DISCLAIMER = (
    "This workspace is part of Anthropic's vendor program. Conversations and "
    "activity may be reviewed by Anthropic and used to train and improve our models."
)

CLONE_ATTEMPTS = 3
CLONE_RETRY_SECONDS = 3
GIT_TIMEOUT_SECONDS = 3600
# Streamed git calls are killed only after this long with no output at all.
CLONE_IDLE_TIMEOUT_SECONDS = int(
    os.environ.get("PREWORK_CLONE_IDLE_TIMEOUT", str(45 * 60))
)
CLONE_SIZE_MULTIPLIER = 1.5
MIN_FREE_BYTES = 5 * 1024**3
DIFF_TIMEOUT_SECONDS = 600


def run(args, **kwargs):
    """subprocess.run that never raises on a non-zero exit unless asked to."""
    check = kwargs.pop("check", False)
    return subprocess.run(args, check=check, **kwargs)


def prework_home():
    """~/.mercor-prework, created owner-only: it holds the lanes' Claude logins."""
    home = Path(os.path.expanduser("~")) / ".mercor-prework"
    if not home.is_dir():
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def error(message):
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)


def ok(message):
    print(f"[OK] {message}")


def info(message):
    print(f"[INFO] {message}")


def warn(message):
    print(f"[WARN] {message}")


def show_training_data_disclaimer():
    print("\n" + "=" * 50)
    print("NOTICE")
    print(TRAINING_DATA_DISCLAIMER)
    print("=" * 50 + "\n")


def rmtree_force(path):
    """rmtree that clears read-only bits and rides out macOS file sharing still holding a tree."""

    def _clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    for attempt in range(5):
        try:
            shutil.rmtree(path, onerror=_clear_readonly)
            return
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EBUSY) or attempt == 4:
                raise
            time.sleep(1)


# --------------------------------------------------------------------------
# Task id and Repo URL. Both end up in clone URLs and Docker names, so both
# patterns are anchored (fullmatch) and narrow.
# --------------------------------------------------------------------------

_REPO_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/tree/([0-9a-fA-F]{40}))?/?"
)
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_STUDIO_TASK_RE = re.compile(r"task_([0-9a-f]{32})", re.IGNORECASE)
_TASK_CODE_RE = re.compile(r"(?:task[ _]|prework_)?([a-z0-9]{8})", re.IGNORECASE)
# Docker's own rule for container, network and volume names.
_DOCKER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")


def parse_repo_url(url):
    """(https clone URL, sha or None) for a github.com repo URL, else None.

    Accepts https://github.com/<owner>/<repo>[.git][/], and the same with
    /tree/<40-hex sha>.
    """
    m = _REPO_RE.fullmatch((url or "").strip())
    if not m:
        return None
    owner, repo, sha = m.groups()
    repo = repo.removesuffix(".git")
    if not repo or {owner, repo} & {".", ".."}:
        return None
    return f"https://github.com/{owner}/{repo}.git", (sha.lower() if sha else None)


def parse_commit(value):
    """A full 40-hex commit sha, lower-cased, else None."""
    text = (value or "").strip()
    return text.lower() if _SHA_RE.fullmatch(text) else None


def normalize_task_id(raw):
    """'Task l50u5a0c' / 'l50u5a0c' -> 'PREWORK_l50u5a0c'; 'task_<32 hex>' -> itself; else None."""
    text = " ".join(str(raw or "").split())
    m = _STUDIO_TASK_RE.fullmatch(text)
    if m:
        return f"task_{m.group(1).lower()}"
    m = _TASK_CODE_RE.fullmatch(text)
    if m:
        return f"PREWORK_{m.group(1).lower()}"
    return None


def is_docker_name(name):
    return bool(_DOCKER_NAME_RE.fullmatch(name or ""))


# --------------------------------------------------------------------------
# Docker engine pin: one engine per machine, recorded once, verified on every
# run, and every docker call forced onto it.
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1
SCRUB_ENV = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_DEFAULT_PLATFORM",
    "DOCKER_CONFIG",
)
_RECORD_FIELDS = ("docker_executable", "context", "engine_id", "server_os", "host_mode")
_BINDING_FIELDS = ("context", "engine_id", "host_mode")


class DockerRuntimeError(RuntimeError):
    """The pinned Docker engine cannot be selected, reached or trusted."""


def machine_record_path():
    return prework_home() / "docker-runtime.json"


def host_mode():
    """windows, wsl, macos or linux; WSL is its own mode, never shared with Windows."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        if (
            os.environ.get("WSL_DISTRO_NAME")
            or os.environ.get("WSL_INTEROP")
            or "microsoft" in platform.release().lower()
        ):
            return "wsl"
        return "linux"
    return system.lower() or "unknown"


def _scrubbed_env():
    env = dict(os.environ)
    for key in SCRUB_ENV:
        env.pop(key, None)
    return env


def _docker_executable():
    # Symlinks kept: Homebrew's link survives upgrades, its Cellar target does not.
    exe = shutil.which("docker")
    if not exe:
        raise DockerRuntimeError("Docker is not installed (no `docker` on PATH).")
    return str(Path(exe).absolute())


def _current_context(executable):
    try:
        res = run(
            [executable, "context", "show"],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DockerRuntimeError(f"Could not ask Docker for its current context: {e}")
    context = (res.stdout or "").strip()
    if res.returncode != 0 or not context:
        raise DockerRuntimeError(
            "`docker context show` did not answer with a context name."
        )
    return context


def _probe_engine(executable, context):
    """The engine identity behind `context`, or an error. No fallback is tried."""

    def not_answering(detail):
        return DockerRuntimeError(
            f"Docker is not answering on the context '{context}'.\n"
            + (f"   {detail}\n" if detail else "")
            + "   Start Docker, wait until it reports it is running, then run "
            "this again. Nothing is lost."
        )

    try:
        res = run(
            [executable, "--context", context, "info", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise not_answering(str(e))
    if res.returncode != 0:
        detail = next(
            (l.strip() for l in (res.stderr or "").splitlines() if l.strip()), ""
        )
        if "does not exist" in detail or "not found" in detail.lower():
            raise DockerRuntimeError(
                f"The Docker context '{context}' is not available on this machine."
            )
        raise not_answering(detail)
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        raise DockerRuntimeError(
            f"The Docker context '{context}' did not describe its engine."
        )
    engine_id = data.get("ID") if isinstance(data, dict) else None
    if not isinstance(engine_id, str) or not engine_id:
        # Some CLIs exit 0 with only the client half when the engine is down.
        errors = data.get("ServerErrors") if isinstance(data, dict) else None
        raise not_answering(
            str(errors[0]).strip() if isinstance(errors, list) and errors else ""
        )
    if data.get("OSType") != "linux":
        raise DockerRuntimeError(
            f"The Docker context '{context}' does not run Linux containers."
        )
    server_os = data.get("OperatingSystem")
    if not isinstance(server_os, str) or not server_os:
        raise DockerRuntimeError(
            f"The Docker context '{context}' did not report its operating system."
        )
    return {"engine_id": engine_id, "server_os": server_os}


def _write_json(path, data):
    """Write atomically, owner-only where the filesystem allows it."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def _read_state(path, fields, what, fix):
    """The recorded fields from a state file, or None when it does not exist."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise DockerRuntimeError(f"{what} could not be read: {e.strerror or e}")
    try:
        data = json.loads(text)
    except ValueError:
        raise DockerRuntimeError(f"{what} is not valid JSON. {fix}")
    if not isinstance(data, dict) or set(data) != {"schema_version", *fields}:
        raise DockerRuntimeError(f"{what} does not have the expected fields. {fix}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise DockerRuntimeError(
            f"{what} has a schema version this tool does not understand. {fix}"
        )
    for field in fields:
        if not isinstance(data[field], str) or not data[field]:
            raise DockerRuntimeError(f"{what} has no usable '{field}'. {fix}")
    return {field: data[field] for field in fields}


def _read_record():
    path = machine_record_path()
    return _read_state(
        path,
        _RECORD_FIELDS,
        f"The Docker engine record {path}",
        "Delete it and run this again, once no pre-work task is in progress.",
    )


def _read_binding(path):
    return _read_state(
        path,
        _BINDING_FIELDS,
        f"The Docker engine binding {path}",
        "Do not delete it: set the task up again in a new empty folder instead.",
    )


def _save_record(record):
    path = machine_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, {"schema_version": SCHEMA_VERSION, **record})
    return record


def select_runtime():
    """This machine's pinned engine; the current context on first use.

    The record is authoritative for the context. The engine behind it may be
    replaced (Docker reset or reinstalled), and the record follows it then.
    """
    mode = host_mode()
    stored = _read_record()
    if stored is None:
        executable = _docker_executable()
        context = _current_context(executable)
        engine = _probe_engine(executable, context)
        return _save_record(
            {
                "docker_executable": executable,
                "context": context,
                "host_mode": mode,
                **engine,
            }
        )
    if stored["host_mode"] != mode:
        raise DockerRuntimeError(
            f"The Docker engine recorded in {machine_record_path()} was selected on a "
            "different kind of host (WSL and Windows cannot share it)."
        )
    executable, fields = stored["docker_executable"], {}
    if not Path(executable).is_file():
        # A package upgrade moved the CLI; the engine check below still pins the engine.
        executable = _docker_executable()
        fields["docker_executable"] = executable
    engine = _probe_engine(executable, stored["context"])
    if engine["engine_id"] != stored["engine_id"]:
        fields.update(engine)
    return _save_record(dict(stored, **fields)) if fields else stored


def _verify_binding(binding, runtime):
    if binding["host_mode"] != runtime["host_mode"]:
        raise DockerRuntimeError(
            "This folder was set up on a different kind of host. Run it from the "
            "shell it was set up in: WSL and PowerShell cannot share one task folder."
        )
    if binding["context"] != runtime["context"]:
        raise DockerRuntimeError(
            "This folder is bound to a different Docker context than the one this "
            "machine now uses. Finish the task from a shell that uses that context."
        )
    if binding["engine_id"] != runtime["engine_id"]:
        raise DockerRuntimeError(
            "Docker was reset or reinstalled since this task was set up, and its lane "
            "did not survive: the engine that held it is gone.\n"
            "   Set the task up again in a new empty folder: python3 prework.py setup"
        )


def activate_runtime(root=None):
    """Verify the pinned engine, bind this folder to it, and force every docker call onto it."""
    runtime = select_runtime()
    folder = Path(root or Path.cwd())
    path = folder / DOCKER_BINDING
    binding = _read_binding(path)
    fresh = {k: runtime[k] for k in _BINDING_FIELDS}
    if binding is None:
        _write_json(path, {"schema_version": SCHEMA_VERSION, **fresh})
    elif (
        binding["engine_id"] != runtime["engine_id"]
        and binding["context"] == runtime["context"]
        and binding["host_mode"] == runtime["host_mode"]
        and not (folder / MANIFEST).is_file()
    ):
        # Nothing was built here, so nothing went with the old engine.
        _write_json(path, {"schema_version": SCHEMA_VERSION, **fresh})
    else:
        _verify_binding(binding, runtime)
    for key in SCRUB_ENV:
        os.environ.pop(key, None)
    os.environ["DOCKER_CONTEXT"] = runtime["context"]
    # Bare `docker` calls resolve through PATH, so the recorded CLI goes first.
    docker_dir = str(Path(runtime["docker_executable"]).parent)
    entries = [
        e for e in os.environ.get("PATH", "").split(os.pathsep) if e and e != docker_dir
    ]
    os.environ["PATH"] = os.pathsep.join([docker_dir, *entries])
    return dict(runtime)


def activate_or_exit():
    try:
        return activate_runtime()
    except DockerRuntimeError as e:
        error(str(e))


# --------------------------------------------------------------------------
# Folder markers and resource names
# --------------------------------------------------------------------------


class InvalidRunId(RuntimeError):
    """The run marker exists but does not hold a run id setup generated."""


def task_id_marker(root=None):
    try:
        value = (
            (Path(root or Path.cwd()) / TASK_ID_FILE)
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        return None
    return value if is_docker_name(value) else None


def read_manifest(root=None):
    try:
        data = json.loads(
            (Path(root or Path.cwd()) / MANIFEST).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_id_marker(root=None):
    """The run id recorded here, else the manifest's, else None.

    A marker that is not a generated id raises: falling back would point this
    folder at another run's container and volumes. The value is never echoed.
    """
    folder = Path(root or Path.cwd())
    try:
        value = (folder / RUN_ID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if not value:
        value = read_manifest(folder).get("run_id")
        if value is None:
            return None
    if isinstance(value, str) and _RUN_ID_RE.fullmatch(value):
        return value
    raise InvalidRunId(
        f"{RUN_ID_FILE} does not hold a run id setup generated. "
        "Set the task up again in a new empty folder."
    )


def resource_key(task, root=None):
    run_id = run_id_marker(root)
    return f"{task}-{run_id}" if run_id else task


def lane_container(task, root=None):
    return f"{CONTAINER_PREFIX}{resource_key(task, root)}-{LANE}"


def lane_volumes(task, root=None):
    """Workspace, inner-docker and inner-containerd volumes, in that order."""
    name = lane_container(task, root)
    return (name, f"{name}-dind", f"{name}-ctr")


def lane_network(task):
    return f"{CONTAINER_PREFIX}{resource_key(task)}"


def lane_workspace(task):
    return f"{CONTAINER_ROOT}/{task}/{LANE}"


def lane_config_key(task, root=None):
    return f"{resource_key(task, root)}_{LANE}"


def lane_config_dir(task, root=None):
    """Host side of the lane's CLAUDE_CONFIG_DIR; the expert's /login lands here."""
    return prework_home() / "cc-config" / lane_config_key(task, root)


def harness_dir(task, root=None):
    return prework_home() / "lane-harness" / lane_config_key(task, root)


def experiment_root():
    return Path.cwd().resolve().as_posix()


# --------------------------------------------------------------------------
# Docker plumbing for the one lane
# --------------------------------------------------------------------------


class LaneOwnerMismatch(RuntimeError):
    """A container of this name was not created by this task folder."""


class LaneDestinationNotEmpty(Exception):
    """The lane's volume already held files the clone would have merged into."""


class LaneSourceUnreadable(Exception):
    """The copy into the lane could not read part of the clone."""


# populate's guard and the Python must agree on these exit codes.
DESTINATION_NOT_EMPTY = 73
SOURCE_UNREADABLE = 74
SKIPPED_MARK = "prework-skipped: "
LANE_ESSENTIAL = (".git",)


def docker_unreachable_reason():
    """None when the daemon answers, else one line saying why it does not."""
    try:
        res = run(["docker", "info"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "Docker is not installed (no `docker` on PATH)."
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Could not query the Docker daemon: {e}"
    if res.returncode == 0:
        return None
    detail = ((res.stderr or "").strip().splitlines() or [""])[0].strip()
    return detail or "`docker info` failed."


def image_platform(image):
    """'linux/arm64' for the loaded image, so DOCKER_DEFAULT_PLATFORM cannot pick another."""
    res = run(
        ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
        capture_output=True,
        text=True,
    )
    value = (res.stdout or "").strip() if res.returncode == 0 else ""
    return value if re.fullmatch(r"[a-z0-9]+/[a-z0-9]+", value) else None


def _platform_args(image):
    plat = image_platform(image)
    return ["--platform", plat] if plat else []


def lane_uid():
    """The host uid on Linux (remapped at start so bind-mount writes stay the expert's), else the image's."""
    return os.getuid() if platform.system() == "Linux" else LANE_IMAGE_UID


def engine_resources():
    """(memory GiB, CPU count) of the engine; each None when it cannot be read."""
    try:
        res = run(
            ["docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        total, ncpu = res.stdout.split()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    gib = int(total) / (1024**3) if total.isdigit() else None
    return gib, (int(ncpu) if ncpu.isdigit() else None)


def small_engine(engine_gib):
    # Round half up: an 8 GB setting reports about 7.7 GiB.
    return int(engine_gib + 0.5) < SMALL_ENGINE_GIB


def lane_memory_gib(engine_gib):
    pct = LANE_MEM_SOLO_PCT if small_engine(engine_gib) else LANE_MEM_SHARE_PCT
    return max(LANE_MEM_FLOOR_GIB, engine_gib * pct / 100)


def lane_memory_args(engine_gib):
    if not engine_gib:
        return []
    cap = lane_memory_gib(engine_gib)
    return [
        "--memory",
        f"{int(cap * 1024)}m",
        "--memory-swap",
        f"{int(cap * LANE_SWAP_FACTOR * 1024)}m",
    ]


def lane_cpu_args(engine_gib, ncpu):
    if not engine_gib or not ncpu:
        return []
    limit = ncpu if small_engine(engine_gib) else ncpu // 2
    if limit < LANE_MIN_CPUS:
        return ["--cpuset-cpus", f"0-{ncpu - 1}"]
    n = min(
        limit, max(LANE_MIN_CPUS, int(lane_memory_gib(engine_gib) // LANE_GIB_PER_CPU))
    )
    return ["--cpuset-cpus", f"0-{n - 1}"]


def lane_resource_args():
    gib, ncpu = engine_resources()
    return [*lane_memory_args(gib), *lane_cpu_args(gib, ncpu)]


def _labelled_owner(name):
    """(exists, running, owner label or None)."""
    fmt = (
        '{{.State.Running}}\t{{if .Config.Labels}}{{index .Config.Labels "'
        + LANE_ROOT_LABEL
        + '"}}{{end}}'
    )
    res = run(
        ["docker", "inspect", "--type", "container", "-f", fmt, name],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False, False, None
    running, _, owner = res.stdout.strip().partition("\t")
    return True, running.strip() == "true", owner.strip() or None


def assert_lane_owner(task, root=None):
    """(exists, running); raises LaneOwnerMismatch unless this folder created the container.

    Read-only. A container with no owner label was not made by this tool, so
    it is refused like one another folder owns: never reused, never removed.
    """
    name = lane_container(task, root)
    exists, running, owner = _labelled_owner(name)
    if not exists:
        return False, False
    here = Path(root).resolve().as_posix() if root else experiment_root()
    if owner != here:
        raise LaneOwnerMismatch(
            f"The container {name} belongs to\n  {owner or '(no task folder recorded)'}\n"
            f"and not to this folder\n  {here}\n"
            "It was left alone. Set this task up in a new empty folder, or finish "
            "it from the folder that created the container."
        )
    return True, running


def lane_volume_exists(task):
    return (
        run(
            ["docker", "volume", "inspect", lane_volumes(task)[0]], capture_output=True
        ).returncode
        == 0
    )


def existing_lane_volumes(task, root=None):
    return [
        v
        for v in lane_volumes(task, root)
        if run(["docker", "volume", "inspect", v], capture_output=True).returncode == 0
    ]


def _lane_volume_sh(task, image, script):
    """Run a shell snippet against the workspace volume, container up or not."""
    return run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            *_platform_args(image),
            "-v",
            f"{lane_volumes(task)[0]}:/dst",
            image,
            "sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
    )


def mark_lane_building(task, image):
    _lane_volume_sh(task, image, f"touch /dst/{LANE_BUILDING}")


def clear_lane_building(task, image):
    return _lane_volume_sh(task, image, f"rm -f /dst/{LANE_BUILDING}").returncode == 0


def lane_is_incomplete(task, image):
    """True only when a build is known to have stopped partway (its sentinel is still there)."""
    return _lane_volume_sh(task, image, f"test -f /dst/{LANE_BUILDING}").returncode == 0


def ensure_network(task):
    name = lane_network(task)
    if run(["docker", "network", "inspect", name], capture_output=True).returncode != 0:
        run(
            ["docker", "network", "create", name], check=True, stdout=subprocess.DEVNULL
        )
    return name


def remove_network(task):
    run(["docker", "network", "rm", lane_network(task)], capture_output=True)


def teardown_lane(task):
    """Remove this run's container and volumes. Only for a lane this run just built."""
    run(["docker", "rm", "-f", lane_container(task)], capture_output=True)
    for volume in lane_volumes(task):
        run(["docker", "volume", "rm", "-f", volume], capture_output=True)


def stage_harness(task):
    """Write the in-lane launcher where the container binds it read-only.

    Outside the repo, so the model cannot stash or delete it and the repo's own
    .claude/ is never shadowed.
    """
    dest = harness_dir(task)
    dest.mkdir(parents=True, exist_ok=True)
    files = {"claude-dev": CLAUDE_DEV, "lane_cmd.sh": LANE_CMD}
    for name, body in files.items():
        (dest / name).write_bytes(body.encode("utf-8"))
        os.chmod(dest / name, 0o755)
    for child in dest.iterdir():
        if child.name not in files:
            if child.is_dir() and not child.is_symlink():
                rmtree_force(child)
            else:
                child.unlink()
    os.chmod(dest, 0o755)
    return dest


def ensure_container(task, image):
    """Create or start the lane container. Returns its name."""
    name = lane_container(task)
    workspace = lane_workspace(task)
    config_key = lane_config_key(task)
    cc_dir = lane_config_dir(task)
    cc_dir.mkdir(parents=True, exist_ok=True)

    # Ownership first, and it raises: another folder's container never reaches teardown.
    exists, running = assert_lane_owner(task)
    if lane_volume_exists(task) and lane_is_incomplete(task, image):
        info(f"{name} was left mid-build; rebuilding it")
        teardown_lane(task)
        exists = running = False
    elif exists:
        if not running:
            _start(task)
        return name

    uid = lane_uid()
    volume, dind, ctr = lane_volumes(task)
    args = [
        "docker",
        "run",
        "-d",
        "--privileged",
        *_platform_args(image),
        *lane_resource_args(),
        "--name",
        name,
        "--label",
        f"{LANE_ROOT_LABEL}={experiment_root()}",
        "--network",
        ensure_network(task),
        "-v",
        f"{volume}:{workspace}",
        # Only this lane's own config dir is visible under the tmpfs.
        "--tmpfs",
        f"{CONTAINER_HOME}:mode=755,uid={uid},gid={uid}",
        "--tmpfs",
        f"{CONTAINER_HOME}/cc-config:mode=755,uid={uid},gid={uid}",
        "-v",
        f"{cc_dir}:{CONTAINER_HOME}/cc-config/{config_key}",
        "-v",
        f"{harness_dir(task)}:{HARNESS_MOUNT}:ro",
        "-v",
        f"{dind}:/var/lib/docker",
        "-v",
        f"{ctr}:/var/lib/containerd",
        "-e",
        f"CLAUDE_CONFIG_DIR={CONTAINER_HOME}/cc-config/{config_key}",
    ]
    # Loopback only: dev servers stay off the expert's network.
    for inside, outside in LANE_PORT_RANGES:
        args += ["-p", f"127.0.0.1:{outside}:{inside}"]
    args += ["--user", "0"]
    if platform.system() == "Linux":
        args += ["-e", f"LANE_UID={uid}"]
    args.append(image)
    run(args, check=True, stdout=subprocess.DEVNULL)
    return name


def populate_lane_script(uid):
    """Guard + copy + chown, run inside the lane image.

    The volume may hold only the build sentinel; anything else is an earlier
    run's leftover, refused (exit 73) rather than merged or deleted.
    """
    return (
        "set -eu; "
        f'unexpected="$(find /dst -mindepth 1 ! -path /dst/{LANE_BUILDING} -print -quit)"; '
        'if [ -n "$unexpected" ]; then '
        'echo "lane destination is not empty: $unexpected" >&2; '
        f"exit {DESTINATION_NOT_EMPTY}; fi; "
        # cp -a exits non-zero on one unreadable file, so name what is missing instead.
        "copied=0; cp -a /src/. /dst || copied=$?; "
        f"chowned=0; chown -R {uid}:{uid} /dst || chowned=$?; "
        'if [ "$copied" -ne 0 ]; then '
        'missing="$(cd /src && find . -mindepth 1 -print | while IFS= read -r p; do '
        'if [ -d "$p" ] && [ ! -L "$p" ] && ! ls -A "$p" >/dev/null 2>&1; '
        "then printf '%s\\n' \"${p#./}\"; "
        'elif [ -e "/dst/$p" ] || [ -L "/dst/$p" ]; then :; '
        "else printf '%s\\n' \"${p#./}\"; fi; "
        'done)" || true; '
        'if [ -n "$missing" ]; then '
        f"echo \"$missing\" | sed 's|^|{SKIPPED_MARK}|'; "
        f"exit {SOURCE_UNREADABLE}; fi; fi; "
        'if [ "$chowned" -ne 0 ]; then '
        'echo "could not give the lane user its files under /dst" >&2; '
        'exit "$chowned"; fi'
    )


def populate_lane(task, staging_dir, image):
    """Copy the prepared clone onto the workspace volume. Returns paths it could not read."""
    staging = str(Path(staging_dir).resolve())
    done = run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            *_platform_args(image),
            "-v",
            f"{lane_volumes(task)[0]}:/dst",
            "-v",
            f"{staging}:/src:ro",
            image,
            "sh",
            "-c",
            populate_lane_script(lane_uid()),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    for line in (done.stdout or "").splitlines():
        if not line.startswith(SKIPPED_MARK):
            print(line)
    if done.returncode == DESTINATION_NOT_EMPTY:
        raise LaneDestinationNotEmpty(
            "The lane's volume still holds an earlier run's files, so the clone was not "
            "copied over them. Nothing has been deleted.\n"
            "   Set the task up again in a new empty folder."
        )
    if done.returncode == SOURCE_UNREADABLE:
        skipped = sorted(
            l[len(SKIPPED_MARK) :]
            for l in done.stdout.splitlines()
            if l.startswith(SKIPPED_MARK)
        )
        essential = [p for p in skipped if p.split("/", 1)[0] in LANE_ESSENTIAL]
        if essential:
            raise LaneSourceUnreadable(
                f"The clone is missing {len(essential)} path(s) the lane cannot run without:\n"
                + "".join(f"     {p}\n" for p in essential)
                + f"   Check permissions or antivirus locks on {staging}, then run setup again."
            )
        print(
            f"  The lane was populated without {len(skipped)} path(s) the copy could not read:"
        )
        for p in skipped:
            print(f"    {p}")
        return skipped
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, done.args)
    return []


def wait_for_lane_user(task, timeout=LANE_READY_TIMEOUT_SECONDS):
    """True once `docker exec -u lane` works; the entrypoint remaps the uid after start."""
    name = lane_container(task)
    deadline = time.time() + timeout
    while True:
        if (
            run(
                ["docker", "exec", "-u", "lane", name, "true"], capture_output=True
            ).returncode
            == 0
        ):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


# Above the default pid_max, so dockerd reads the stale pid file as nobody's.
_IMPOSSIBLE_PID = "4194304"


def _clear_stale_dind_pid(name):
    """Defuse the docker.pid a killed lane leaves; dockerd refuses to start past a live pid."""
    fd, stale = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as f:
            f.write(_IMPOSSIBLE_PID)
        run(["docker", "cp", stale, f"{name}:/var/run/docker.pid"], capture_output=True)
    finally:
        os.unlink(stale)


def _start(task):
    name = lane_container(task)
    _clear_stale_dind_pid(name)
    # Before the start: a cpuset naming CPUs the engine no longer has blocks it.
    resources = lane_resource_args()
    if resources:
        run(["docker", "update", *resources, name], capture_output=True)
    run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL)
    if not wait_for_lane_user(task):
        warn(
            f"{name} started, but its user was not ready within "
            f"{LANE_READY_TIMEOUT_SECONDS:.0f}s; commands in the lane may fail."
        )


def ensure_running(task):
    """Start the lane container if it is stopped. False when it does not exist."""
    res = run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "-f",
            "{{.State.Running}}",
            lane_container(task),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False
    if res.stdout.strip() != "true":
        _start(task)
    return True


def lane_exec(task, args, timeout=None):
    ensure_running(task)
    return run(
        [
            "docker",
            "exec",
            "-u",
            "lane",
            "-e",
            f"HOME={LANE_HOME}",
            lane_container(task),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def wait_for_dind(task, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lane_exec(task, ["docker", "info"], timeout=30).returncode == 0:
            return True
        time.sleep(1)
    return False


def git_prefix(task):
    """Command prefix that runs git in the lane's workspace as `lane`."""
    return [
        "docker",
        "exec",
        "-u",
        "lane",
        "-e",
        f"HOME={LANE_HOME}",
        "-w",
        lane_workspace(task),
        lane_container(task),
    ]


# --------------------------------------------------------------------------
# Launchers. lane_cmd.sh and claude-dev run inside the lane; the wrapper in
# model_a/ runs on the host and execs into the container.
# --------------------------------------------------------------------------


def _fill(template, **values):
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    return template


# tmux keeps the session alive when the terminal drops; re-running rejoins it.
# SIGCONT first: a Ctrl+Z'd pane looks dead until it gets one.
LANE_CMD = _fill(
    """\
if command -v tmux >/dev/null 2>&1; then
  p="$(tmux -f "$HOME/.tmux.lane.conf" list-panes -t lane -F "#{pane_pid}" 2>/dev/null | head -1)"
  [ -n "$p" ] && { kill -CONT "-$p" 2>/dev/null || kill -CONT "$p" 2>/dev/null; }
  exec tmux -f "$HOME/.tmux.lane.conf" new-session -A -s lane @HARNESS@/claude-dev "$@"
fi
exec @HARNESS@/claude-dev "$@"
""",
    HARNESS=HARNESS_MOUNT,
)

CLAUDE_DEV = _fill(
    r"""#!/usr/bin/env bash
# claude-dev: start Claude Code in this pre-work lane, on the expert's own login.
set -euo pipefail

PINNED_VERSION="@VERSION@"
_launcher="${PREWORK_LAUNCHER:-./claude-dev}"

# The container points CLAUDE_CONFIG_DIR at this task's own config dir, so the
# expert's personal Claude config and login never enter the lane.
if [ -z "${CLAUDE_CONFIG_DIR:-}" ] || [ ! -d "$CLAUDE_CONFIG_DIR" ]; then
  echo "ERROR: this lane has no Claude config directory. Run $_launcher from model_a/ in the task folder." >&2
  exit 1
fi

@ENV_EXPORTS@

# One live session per lane: after exec this shell's PID is the running claude.
_pid="$(cat "$CLAUDE_CONFIG_DIR/.launch.pid" 2>/dev/null || true)"
if [[ "$_pid" =~ ^[0-9]+$ ]] && kill -0 "$_pid" 2>/dev/null; then
  kill -CONT "$_pid" 2>/dev/null || true
  echo "This lane is already running in another terminal. Type your follow-up there instead." >&2
  echo "  Pressed Ctrl+Z there and it looks dead? Go back to that window and press Enter." >&2
  exit 1
fi
umask 077
printf '%s' "$$" > "$CLAUDE_CONFIG_DIR/.launch.pid"

# A lane killed for memory looks like a model failure, so say so on the next launch.
python3 - "$CLAUDE_CONFIG_DIR" "$_launcher" <<'PYOOM' 2>/dev/null || true
import re, sys
from pathlib import Path
events = Path("/sys/fs/cgroup/memory.events")
if not events.is_file():
    sys.exit(0)
m = re.search(r"^oom_kill (\d+)", events.read_text(), re.M)
kills = int(m.group(1)) if m else 0
seen_path = Path(sys.argv[1]) / ".oom_kills_seen"
try:
    seen = int(seen_path.read_text().strip() or 0)
except Exception:
    seen = 0
if kills > seen:
    limit = Path("/sys/fs/cgroup/memory.max")
    cap = limit.read_text().strip() if limit.is_file() else ""
    size = f" ({int(cap) / 1024 ** 3:.1f} GiB)" if cap.isdigit() else ""
    print(f"Note: this lane used all of its memory{size}, so a process in it was killed.")
    print(f"  To give it more, raise Docker's memory in its settings, restart Docker, then run {sys.argv[2]} again.")
seen_path.write_text(str(kills))
PYOOM

LAUNCH_VERSION="$(claude --version 2>/dev/null | awk 'NR==1{print $1}' || true)"
if [ "$LAUNCH_VERSION" != "$PINNED_VERSION" ]; then
  echo "ERROR: this lane runs Claude Code $PINNED_VERSION, but found ${LAUNCH_VERSION:-none}." >&2
  echo "  Set the task up again in a new empty folder: python3 prework.py setup" >&2
  exit 1
fi

echo "Pre-work: this lane runs on your own Claude login. On first launch, type /login."
echo "Finish with /exit. Ctrl+Z or lost the window? Run $_launcher again: it picks this session back up."

# Resume this lane's session unless the expert picked one or asked for --new.
_resume=1
_args=()
for _a in "$@"; do
  case "$_a" in
    --new) _resume=0 ;;
    --continue|-c|--resume|-r) _resume=0; _args+=("$_a") ;;
    *) _args+=("$_a") ;;
  esac
done
set -- ${_args[@]+"${_args[@]}"}
if [ "$_resume" = 1 ]; then
  for _t in "$CLAUDE_CONFIG_DIR"/projects/*/*.jsonl; do
    if [ -f "$_t" ]; then
      set -- --continue "$@"
      echo "Resuming your session. Run $_launcher --new only to start over." >&2
      break
    fi
  done
fi

# --strict-mcp-config: no personal MCP servers. --settings comes last: it takes
# exactly one value, so the greedy flags before it cannot swallow the prompt.
exec claude --enable-auto-mode --permission-mode auto --strict-mcp-config \
  --debug \
  --disallowedTools "@DENIED_CSV@" \
  --settings '@SETTINGS_JSON@' \
  "$@"
""",
    VERSION=CLAUDE_CODE_VERSION,
    ENV_EXPORTS="\n".join(f"export {k}={shlex.quote(v)}" for k, v in LANE_ENV.items()),
    DENIED_CSV=",".join(LANE_DENIED_TOOLS),
    SETTINGS_JSON=json.dumps(
        {
            "alwaysThinkingEnabled": True,
            "permissions": {"deny": list(LANE_DENIED_TOOLS)},
        },
        separators=(",", ":"),
    ),
)

# The host launcher talks to the engine this folder was bound to, through the
# recorded CLI and context, and checks the engine id before anything else.
WRAPPER_SH = r"""#!/usr/bin/env bash
set -euo pipefail
CONTAINER=@CONTAINER@
WORKSPACE=@WORKSPACE@
EXPECTED_ENGINE=@ENGINE_ID@
DOCKER=( @DOCKER_EXE@ --context @CONTEXT@ )
# A package upgrade can move the CLI while the engine stays; the engine check pins it.
if [ ! -x "${DOCKER[0]}" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: the Docker command line this task was set up with is gone, and no docker is on PATH." >&2
    exit 1
  fi
  DOCKER[0]="$(command -v docker)"
fi
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_DEFAULT_PLATFORM DOCKER_CONFIG
ENGINE="$("${DOCKER[@]}" info --format '{{.ID}}' 2>/dev/null || true)"
if [ -z "$ENGINE" ]; then
  echo "ERROR: the Docker engine this task was set up on is not answering." >&2
  echo "  Your lane and its work are untouched. Start Docker, wait for it, then run this again." >&2
  exit 1
fi
if [ "$ENGINE" != "$EXPECTED_ENGINE" ]; then
  echo "ERROR: Docker was reset or reinstalled since this task was set up, and its lane is gone." >&2
  echo "  Set the task up again in a new empty folder: python3 prework.py setup" >&2
  exit 1
fi
if ! "${DOCKER[@]}" container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "ERROR: this lane has no container: $CONTAINER" >&2
  echo "  Run 'python3 prework.py setup' again in the task folder to bring it back." >&2
  exit 1
fi
# Same memory and CPU caps as setup, sized from the engine as it is now.
read -r TOTAL NCPU <<< "$("${DOCKER[@]}" info --format '{{.MemTotal}} {{.NCPU}}' 2>/dev/null || true)" || true
[[ "${NCPU:-}" =~ ^[0-9]+$ ]] || NCPU=0
if [[ "${TOTAL:-}" =~ ^[0-9]+$ ]]; then
  GIB=$(( (TOTAL + 512 * 1024 * 1024) / (1024 * 1024 * 1024) ))
  if [ "$GIB" -lt @SMALL_GIB@ ]; then
    CAP=$(( TOTAL * @SOLO_PCT@ / 100 )); LIMIT="$NCPU"
  else
    CAP=$(( TOTAL * @SHARE_PCT@ / 100 )); LIMIT=$(( NCPU / 2 ))
  fi
  [ "$CAP" -lt $(( @FLOOR_GIB@ * 1024 * 1024 * 1024 )) ] && CAP=$(( @FLOOR_GIB@ * 1024 * 1024 * 1024 ))
  RESOURCES=( --memory "$CAP" --memory-swap "$(( CAP * 3 / 2 ))" )
  if [ "$NCPU" -gt 0 ]; then
    if [ "$LIMIT" -lt @MIN_CPUS@ ]; then
      CPUSET="0-$(( NCPU - 1 ))"
    else
      N=$(( CAP / (@GIB_PER_CPU@ * 1024 * 1024 * 1024) ))
      [ "$N" -lt @MIN_CPUS@ ] && N=@MIN_CPUS@
      [ "$N" -gt "$LIMIT" ] && N="$LIMIT"
      CPUSET="0-$(( N - 1 ))"
    fi
    RESOURCES+=( --cpuset-cpus "$CPUSET" )
  fi
  "${DOCKER[@]}" update "${RESOURCES[@]}" "$CONTAINER" >/dev/null 2>&1 || true
fi
if ! "${DOCKER[@]}" ps -q -f "name=^${CONTAINER}$" | grep -q .; then
  # As setup does: defuse a killed lane's docker.pid, then wait for the
  # entrypoint to remap the lane user before exec'ing as it.
  PIDFILE="$(mktemp)"
  printf '%s' @IMPOSSIBLE_PID@ > "$PIDFILE"
  "${DOCKER[@]}" cp "$PIDFILE" "$CONTAINER:/var/run/docker.pid" >/dev/null 2>&1 || true
  rm -f "$PIDFILE"
  "${DOCKER[@]}" start "$CONTAINER" >/dev/null
  READY=""
  for _ in $(seq 1 @READY_TRIES@); do
    if "${DOCKER[@]}" exec -u lane "$CONTAINER" true >/dev/null 2>&1; then READY=1; break; fi
    sleep 0.5
  done
  [ -n "$READY" ] || echo "WARNING: the lane started but its user is not ready yet; if this fails, run ./claude-dev again." >&2
fi
exec "${DOCKER[@]}" exec -it -u lane -e HOME=@HOME@ -e PREWORK_LAUNCHER=./claude-dev \
  -w "$WORKSPACE" "$CONTAINER" sh -l @HARNESS@/lane_cmd.sh "$@"
"""


def _sh_literal(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def write_lane_wrapper(task, dest_dir, runtime):
    dest = Path(dest_dir)
    dest.mkdir(exist_ok=True)
    wrapper = dest / "claude-dev"
    wrapper.write_text(
        _fill(
            WRAPPER_SH,
            CONTAINER=_sh_literal(lane_container(task)),
            WORKSPACE=_sh_literal(lane_workspace(task)),
            ENGINE_ID=_sh_literal(runtime["engine_id"]),
            DOCKER_EXE=_sh_literal(runtime["docker_executable"]),
            CONTEXT=_sh_literal(runtime["context"]),
            SMALL_GIB=SMALL_ENGINE_GIB,
            SOLO_PCT=LANE_MEM_SOLO_PCT,
            SHARE_PCT=LANE_MEM_SHARE_PCT,
            FLOOR_GIB=LANE_MEM_FLOOR_GIB,
            MIN_CPUS=LANE_MIN_CPUS,
            GIB_PER_CPU=LANE_GIB_PER_CPU,
            HOME=LANE_HOME,
            HARNESS=HARNESS_MOUNT,
            IMPOSSIBLE_PID=_IMPOSSIBLE_PID,
            READY_TRIES=int(LANE_READY_TIMEOUT_SECONDS / 0.5),
        )
    )
    os.chmod(wrapper, 0o700)
    return wrapper


# --------------------------------------------------------------------------
# Lane image: download, verify against the pin, load, retag
# --------------------------------------------------------------------------


def docker_arch():
    """The daemon's arch: a Rosetta python reports x86_64 on an arm64 daemon."""
    res = run(
        ["docker", "version", "--format", "{{.Server.Arch}}"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and (res.stdout or "").strip():
        return res.stdout.strip().lower()
    return (platform.machine() or "").lower()


def lane_image_arch():
    return LANE_IMAGE_ARCHES.get(docker_arch())


def lane_image_ref():
    return f"{LANE_IMAGE_REPO}:{CLAUDE_CODE_VERSION}"


def local_image_id(ref):
    res = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True,
        text=True,
    )
    image_id = (res.stdout or "").strip() if res.returncode == 0 else ""
    return image_id or None


def file_sha256(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# The Windows system drive's user profiles inside WSL; a constant so tests can move it.
WSL_WINDOWS_USERS = Path("/mnt/c/Users")
WINDOWS_BUILTIN_PROFILES = {
    "All Users",
    "Default",
    "Default User",
    "Public",
    "desktop.ini",
}


def _exists(path, kind):
    """is_file()/is_dir() that treats an unreadable path as absent, as other users' are."""
    try:
        return path.is_file() if kind == "file" else path.is_dir()
    except OSError:
        return False


def tarball_candidates(pin):
    """Where the expert may have put the tarball, nearest first, yielded lazily.

    PREWORK_IMAGE_FILE, then this folder, then ~/Downloads. Under WSL the browser
    is a Windows one, so last comes C:'s Users/*/Downloads -- only C:, the local
    system drive: other mounts may be slow or network drives that stall WSL.
    """
    local = os.environ.get(IMAGE_FILE_ENV)
    if local:
        yield Path(local).expanduser()
        return
    yield Path.cwd() / pin["file"]
    yield Path.home() / "Downloads" / pin["file"]
    if host_mode() == "wsl":
        try:
            profiles = sorted(WSL_WINDOWS_USERS.iterdir())
        except OSError:
            return
        for profile in profiles:
            if profile.name not in WINDOWS_BUILTIN_PROFILES and _exists(profile, "dir"):
                yield profile / "Downloads" / pin["file"]


def verified_tarball(pin):
    """The pinned tarball, checked against its size and sha256 before anything loads it."""
    found = next((p for p in tarball_candidates(pin) if _exists(p, "file")), None)
    if found is None:
        error(
            f"Download {pin['file']} ({pin['size'] / 1e9:.1f} GB) from the Setup page,\n"
            f"   {SETUP_PAGE}\n"
            "   into this folder or your Downloads folder, then run setup again."
        )
    info(f"Checking {found} against the pinned lane image...")
    if found.stat().st_size != pin["size"] or file_sha256(found) != pin["sha256"]:
        error(
            f"{found} is not the pinned lane image {pin['file']} (wrong size or checksum).\n"
            f"   Delete it and download it again from {SETUP_PAGE}"
        )
    return found


def load_lane_image(tarball, pin):
    """docker load the verified tarball and tag it as ours.

    The tarball carries TARBALL_IMAGE_REPO's tag. That tag is put back exactly
    as it was: removed if this load created it, re-pointed if the load moved it.
    """
    source = f"{TARBALL_IMAGE_REPO}:{CLAUDE_CODE_VERSION}"
    before = local_image_id(source)
    info("Loading the lane image into Docker...")
    try:
        # On stdin: a sandboxed docker CLI may not see the file's path.
        with open(tarball, "rb") as tar:
            load = run(
                ["docker", "load"],
                stdin=tar,
                capture_output=True,
                text=True,
                timeout=1800,
            )
    except (OSError, subprocess.SubprocessError) as e:
        error(f"Could not load the lane image: {e}")
    loaded = local_image_id(source)
    try:
        if load.returncode != 0 or loaded is None:
            detail = [
                l for l in (load.stderr or load.stdout or "").splitlines() if l.strip()
            ][-1:]
            error(
                "Could not load the lane image."
                + (f"\n   {detail[0]}" if detail else "")
            )
        if loaded not in pin["image_ids"]:
            error("The loaded lane image is not the pinned build. Report this.")
        if (
            run(
                ["docker", "tag", loaded, lane_image_ref()], capture_output=True
            ).returncode
            != 0
        ):
            error(f"Could not tag the lane image as {lane_image_ref()}.")
    finally:
        if before is None:
            # Untags only: the image stays under our own tag.
            run(["docker", "rmi", source], capture_output=True)
        elif before != loaded:
            run(["docker", "tag", before, source], capture_output=True)
    ok(f"Lane image verified and loaded ({lane_image_ref()})")


def ensure_lane_image():
    """The pinned lane image's tag, loading it from the expert's download once per machine."""
    arch = lane_image_arch()
    pin = LANE_IMAGES.get(arch) if arch else None
    if not pin:
        error(
            f"No lane image is published for this machine's Docker ({docker_arch()}); "
            "it is built for amd64 and arm64."
        )
    ref = lane_image_ref()
    if local_image_id(ref) in pin["image_ids"]:
        ok(f"Lane image {ref} already loaded")
        return ref
    # The expert's own download is left in place; they may set up another task.
    load_lane_image(verified_tarball(pin), pin)
    return ref


# --------------------------------------------------------------------------
# Repo: reachability, disk, clone, checkout
# --------------------------------------------------------------------------


class _IdleTimeout(Exception):
    """A streamed git command produced no output for CLONE_IDLE_TIMEOUT_SECONDS."""


def _git_env():
    # No password prompt: a missing or private repo fails instead of hanging setup.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def run_streaming(argv, cwd=None, idle_timeout=None):
    """Run argv with its stderr shown live; kill it only after idle_timeout seconds of silence."""
    idle_timeout = idle_timeout or CLONE_IDLE_TIMEOUT_SECONDS
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=_git_env(),
    )
    last = [time.monotonic()]

    def pump():
        # Raw reads: git redraws its progress line with \r, not \n.
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                return
            last[0] = time.monotonic()
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    idle = False
    try:
        while True:
            try:
                returncode = proc.wait(timeout=max(0.1, min(1.0, idle_timeout / 10)))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - last[0] > idle_timeout:
                    idle = True
                    proc.kill()
                    returncode = proc.wait()
                    break
    except BaseException:
        # Ctrl+C must not leave git running and holding the clone dir.
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        reader.join(timeout=5)
    if idle:
        raise _IdleTimeout(f"no output for {idle_timeout}s")
    return returncode


def repo_is_reachable(clone_url):
    """(ok, detail) without cloning: `git ls-remote` fails the same way a clone would."""
    try:
        res = run(
            ["git", "ls-remote", clone_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=120,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        return False, "`git ls-remote` did not answer within 120s."
    except OSError as e:
        return False, f"Could not run git: {e}"
    if res.returncode == 0:
        return True, ""
    lines = [l.strip() for l in (res.stderr or "").splitlines() if l.strip()]
    return False, (lines[-1] if lines else f"`git ls-remote` exited {res.returncode}.")


def estimate_repo_bytes(clone_url):
    """Packed size from the GitHub API, or None. Advisory: never raises."""
    m = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git", clone_url or ""
    )
    if not m:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}",
        headers={"User-Agent": f"mercor-prework/{TOOL_VERSION}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            size_kb = (json.loads(r.read().decode("utf-8")) or {}).get("size")
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return None
    # 0 means "not computed yet", not "needs no space".
    if (
        not isinstance(size_kb, (int, float))
        or isinstance(size_kb, bool)
        or size_kb <= 0
    ):
        return None
    return int(size_kb) * 1024


def _gib(num_bytes):
    gib = num_bytes / 1024**3
    return "<0.1 GiB" if 0 < gib < 0.05 else f"{gib:.1f} GiB"


def preflight_disk_space(clone_url):
    """Refuse before cloning when the disk cannot hold the clone."""
    free = shutil.disk_usage(os.getcwd()).free
    packed = estimate_repo_bytes(clone_url)
    if packed is None:
        if free < MIN_FREE_BYTES:
            error(
                f"Only {_gib(free)} free on this disk. Free up space and run setup again."
            )
        warn(f"Could not estimate the repository size; {_gib(free)} free.")
        return
    needed = int(packed * CLONE_SIZE_MULTIPLIER)
    if free < needed:
        error(
            f"Not enough disk space to clone this repository: about {_gib(needed)} "
            f"needed, {_gib(free)} free. Free up space and run setup again."
        )
    info(f"Disk check: ~{_gib(needed)} needed, {_gib(free)} free")


def clone_repository(clone_url, target_dir):
    """Clone with live progress, retrying a dropped connection."""
    returncode = None
    try:
        for attempt in range(1, CLONE_ATTEMPTS + 1):
            info(
                f"Cloning {clone_url} (progress below)..."
                if attempt == 1
                else f"Retrying the clone ({attempt} of {CLONE_ATTEMPTS})..."
            )
            returncode = run_streaming(
                ["git", "clone", "--progress", clone_url, str(target_dir)]
            )
            if returncode == 0:
                ok("Repository cloned")
                return
            if attempt < CLONE_ATTEMPTS:
                # git refuses a non-empty target, so the partial tree goes first.
                if os.path.isdir(target_dir):
                    rmtree_force(target_dir)
                time.sleep(CLONE_RETRY_SECONDS * attempt)
    except _IdleTimeout:
        error(
            f"git clone produced no output for {CLONE_IDLE_TIMEOUT_SECONDS}s and was stopped. "
            "Run setup again; raise the limit with PREWORK_CLONE_IDLE_TIMEOUT=<seconds>."
        )
    error(
        f"git clone failed (exit {returncode}) after {CLONE_ATTEMPTS} attempts; "
        "git's own reason is above."
    )


def checkout_commit(commit, target_dir):
    """Put the clone at `commit`, fetching it by id when no branch has it. False on failure."""
    try:
        if (
            run(
                ["git", "checkout", "-q", commit],
                cwd=target_dir,
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
            ).returncode
            == 0
        ):
            return True
        info(f"Fetching {commit[:8]} (progress below)...")
        if (
            run_streaming(
                ["git", "fetch", "--progress", "origin", commit], cwd=target_dir
            )
            != 0
        ):
            return False
        return (
            run(
                ["git", "checkout", "-q", commit],
                cwd=target_dir,
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
            ).returncode
            == 0
        )
    except _IdleTimeout:
        warn(f"git fetch produced no output for {CLONE_IDLE_TIMEOUT_SECONDS}s")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        warn(f"git failed: {e}")
        return False


def lane_head(target_dir):
    try:
        res = run(
            ["git", "rev-parse", "HEAD"], cwd=target_dir, capture_output=True, text=True
        )
    except OSError:
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def prompt_manual_checkout(commit, target_dir):
    """Let the expert do the checkout by hand, then verify it. Never prompts without a TTY."""
    if not sys.stdin.isatty():
        return False
    for _ in range(2):
        warn(
            f"Could not check out {commit[:8]} automatically. Run this in another terminal:\n"
        )
        print(
            f"    cd {shlex.quote(os.path.abspath(target_dir))} && "
            f"git fetch origin {commit} && git checkout {commit}\n"
        )
        try:
            input("Press Enter once that is done...")
        except (EOFError, KeyboardInterrupt):
            return False
        if lane_head(target_dir) == commit:
            return True
    return False


# --------------------------------------------------------------------------
# The task folder: markers, task switches, archiving
# --------------------------------------------------------------------------

# What a previous task leaves in the folder. The engine binding stays: the
# folder remains bound to the engine its lanes were built on.
ARCHIVE_ASIDE = (MANIFEST, TASK_ID_FILE, RUN_ID_FILE, OUTPUT_DIR, LANE)


def archive_previous_run(leave_config_dir=False):
    """Move the previous run's files into archive_<time>/ and return (archive, moved).

    The config dir moves too, less the expert's Claude login: the archive sits
    in the task folder, which may be zipped and shared, so the login is deleted
    first. If it cannot be deleted, the dir stays under ~/.mercor-prework.
    leave_config_dir keeps it there regardless (its lane may still be running).
    """
    task = task_id_marker()
    # Resolved before the markers it is keyed by move.
    config_dir = lane_config_dir(task) if task and not leave_config_dir else None
    harness = harness_dir(task) if config_dir else None
    archive = Path(f"archive_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    moved = []
    for name in ARCHIVE_ASIDE:
        src = Path(name)
        if not os.path.lexists(src):
            continue
        archive.mkdir(exist_ok=True)
        shutil.move(str(src), str(archive / name))
        moved.append(name)
    if config_dir is not None and config_dir.exists():
        login = config_dir / CLAUDE_LOGIN_FILE
        try:
            if os.path.lexists(login):
                login.unlink()
        except OSError as e:
            warn(
                f"Could not delete the Claude login in {config_dir} ({e.strerror or 'error'}); "
                "left that folder where it is instead of archiving it."
            )
        else:
            dest = archive / "cc-config" / config_dir.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(config_dir), str(dest))
            moved.append(config_dir.name)
            if harness.exists():
                rmtree_force(harness)
    return (archive, moved) if moved else (None, [])


def stop_previous_lane():
    """Remove this folder's lane container, keeping its volumes. True only when it is confirmed gone.

    A live lane bind-mounts its config dir and can rewrite the expert's login
    there at any moment, so that dir must not move while the lane runs.
    """
    task = task_id_marker()
    if not task:
        return True
    if docker_unreachable_reason() is not None:
        return False
    try:
        name = lane_container(task)
        assert_lane_owner(task)
    except (LaneOwnerMismatch, InvalidRunId):
        return False
    run(["docker", "rm", "-f", name], capture_output=True, text=True)
    # Gone only when Docker names it gone; a dead daemon fails this inspect too.
    probe = run(
        ["docker", "container", "inspect", name], capture_output=True, text=True
    )
    return probe.returncode != 0 and "no such container" in (probe.stderr or "").lower()


def archive_if_previous_task_differs(task_id):
    """Set the previous task aside when this folder last ran a different one."""
    previous = read_manifest().get("task_id") or task_id_marker()
    if not previous or previous == task_id:
        return None
    try:
        volumes = existing_lane_volumes(task_id_marker()) if task_id_marker() else []
    except InvalidRunId:
        volumes = []
    leave = not stop_previous_lane()
    if leave:
        warn(
            f"Could not confirm {previous}'s lane has stopped, so its Claude config stays "
            "under ~/.mercor-prework instead of moving into this folder: a live session "
            "there could write your Claude login into it."
        )
    archive, moved = archive_previous_run(leave_config_dir=leave)
    if moved:
        info(
            f"This folder last ran {previous}. Its {', '.join(moved)} moved into {archive}/."
        )
    if volumes:
        # Kept: they hold that task's work, and only the expert knows it is collected.
        info(
            f"{previous}'s lane volumes are kept. Remove them once you no longer need "
            f"that work:\n   docker volume rm {' '.join(volumes)}"
        )
    return archive


def previous_lane_gone(task_id):
    """True when Docker answers and none of this run's volumes exist."""
    if docker_unreachable_reason() is not None:
        return False
    return not existing_lane_volumes(task_id)


def write_run_id_marker(run_id):
    path = Path(RUN_ID_FILE)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(run_id, encoding="utf-8")
    os.replace(tmp, path)


def ensure_run_id(task_id):
    """This run's id: kept while its lane exists, fresh once Docker lost it."""
    try:
        existing = run_id_marker()
    except InvalidRunId as e:
        error(str(e))
    if existing and task_id_marker() == task_id and previous_lane_gone(task_id):
        archive, moved = archive_previous_run()
        info(
            "The previous attempt's lane is gone from Docker, so this is a new attempt"
            + (f"; its {', '.join(moved)} moved into {archive}/." if moved else ".")
        )
        existing = None
    if existing:
        if not Path(RUN_ID_FILE).is_file():
            write_run_id_marker(existing)
        return existing
    run_id = uuid.uuid4().hex
    write_run_id_marker(run_id)
    return run_id


def record_task_id(task_id):
    if not is_docker_name(task_id):
        error(f"{task_id!r} cannot name a Docker lane.")
    Path(TASK_ID_FILE).write_text(task_id, encoding="utf-8")


# Names setup itself puts in a folder; anything else means the folder holds other work.
# The Setup page has experts download README.md and Dockerfile.lane alongside.
_OWN_NAMES = {
    "prework.py",
    "README.md",
    "Dockerfile.lane",
    ".DS_Store",
    *(p["file"] for p in LANE_IMAGES.values()),
    DOCKER_BINDING,
    STAGING_DIR,
    *ARCHIVE_ASIDE,
}


def check_folder_is_ours():
    """Refuse to set up in a folder that holds anything but this tool's own files."""
    foreign = sorted(
        p.name
        for p in Path.cwd().iterdir()
        if p.name not in _OWN_NAMES and not p.name.startswith("archive_")
    )
    if foreign:
        shown = ", ".join(foreign[:5]) + (" ..." if len(foreign) > 5 else "")
        error(
            "Run setup in a new, empty folder for this task (prework.py and README.md may be in it). "
            f"This folder already holds: {shown}"
        )


def wsl_windows_drive_folder():
    """'/mnt/c' when cwd is a Windows drive mounted into WSL, else None."""
    if host_mode() != "wsl":
        return None
    m = re.match(r"^(/mnt/[a-z])(?:/|$)", Path.cwd().resolve().as_posix())
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


def seed_config_dir(task):
    """Answer Claude Code's first-run prompts (theme, onboarding, folder trust) for the lane.

    No account state is written or copied: the expert's /login provides it.
    """
    config_dir = lane_config_dir(task)
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, 0o700)
    except OSError:
        pass
    config = config_dir / ".claude.json"
    if config.exists():
        return  # a re-run must not clobber a lane in progress
    _write_json(
        config,
        {
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "installMethod": "native",
            "projects": {
                lane_workspace(task): {
                    "hasTrustDialogAccepted": True,
                    "hasCompletedProjectOnboarding": True,
                    "hasClaudeMdExternalIncludesApproved": True,
                    "allowedTools": [],
                }
            },
        },
    )


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def blocked_lane_ports():
    blocked = []
    for _inside, outside in LANE_PORT_RANGES:
        first, _, last = outside.partition("-")
        blocked += [
            p for p in range(int(first), int(last or first) + 1) if not _port_free(p)
        ]
    return blocked


def running_prework_containers():
    """Names of running prework-* containers, to say what holds the lane's ports."""
    res = run(
        [
            "docker",
            "ps",
            "--filter",
            f"name=^{CONTAINER_PREFIX}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    return sorted(n for n in res.stdout.split() if n.startswith(CONTAINER_PREFIX))


def preflight(task, clone_url):
    """Refuse everything knowable before the clone and the image download, all at once.

    Ports held by another pre-work lane are reported, never reclaimed: that
    lane belongs to another task folder and may have a session running.
    """
    problems = []
    blocked = blocked_lane_ports()
    if blocked:
        holders = [n for n in running_prework_containers() if n != lane_container(task)]
        shown = ", ".join(str(p) for p in blocked[:3]) + (
            f" (and {len(blocked) - 3} more)" if len(blocked) > 3 else ""
        )
        if holders:
            how = (
                "Another pre-work lane holds them. Stop it (its work is kept, and its "
                "./claude-dev starts it again):\n"
                + "".join(f"     docker stop {n}\n" for n in holders)
            )
        else:
            how = (
                f"Stop the process holding them; find it with `lsof -i :{blocked[0]}`."
            )
        problems.append(f"The lane's ports are taken: {shown}.\n   {how}")
    if existing_lane_volumes(task):
        problems.append(
            "A lane volume from an earlier attempt in this folder still holds files, and "
            "setting up over it would mix that run into this one.\n"
            "   Set the task up in a new empty folder."
        )
    reachable, detail = repo_is_reachable(clone_url)
    if not reachable:
        problems.append(
            f"The repository cannot be reached, so the clone would fail:\n"
            f"   {clone_url}\n   {detail}"
        )
    if problems:
        error(
            "Setup cannot continue. Fix these first:\n\n"
            + "\n\n".join(f" - {p}" for p in problems)
        )
    ok("Pre-flight passed")


def build_lane(task, image, staging, runtime):
    """Create the container, copy the clone onto its volume, and leave the host launcher."""
    try:
        assert_lane_owner(task)
    except LaneOwnerMismatch as e:
        error(str(e))
    stage_harness(task)
    try:
        ensure_container(task, image)
        # Marked for the whole build, so a lane cut off partway is rebuilt, not reused.
        mark_lane_building(task, image)
        populate_lane(task, staging, image)
        if not clear_lane_building(task, image):
            warn(
                "The lane is built but could not be unmarked as mid-build; a later setup "
                "in this folder would rebuild it. Collect before running setup here again."
            )
        write_lane_wrapper(task, LANE, runtime)
    except LaneDestinationNotEmpty as e:
        # A refusal, not a crash: the volume's files stay. Clear the sentinel so the
        # next setup does not read it as an interrupted build and rebuild it away.
        clear_lane_building(task, image)
        error(str(e))
    except LaneSourceUnreadable as e:
        teardown_lane(task)
        remove_network(task)
        error(str(e) + "\n   The half-copied lane was removed.")
    except LaneOwnerMismatch:
        raise
    except BaseException:
        # Only what this run just created: the ports are fixed, so a leftover blocks the retry.
        teardown_lane(task)
        remove_network(task)
        info("Removed this run's lane container and volumes.")
        raise
    rmtree_force(staging)
    ok(f"Lane ready in container {lane_container(task)}")


def write_manifest(task_id, run_id, clone_url, base_commit, image, started):
    _write_json(
        MANIFEST,
        {
            "mode": "prework",
            "task_id": task_id,
            "run_id": run_id,
            "repo_url": clone_url,
            "base_commit": base_commit,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool_version": TOOL_VERSION,
            "claude_code_version": CLAUDE_CODE_VERSION,
            "lane": {
                "container": lane_container(task_id),
                "image": image,
                "workspace": lane_workspace(task_id),
            },
            # OS and version only; no host or user name.
            "platform": platform.platform(),
            "setup_duration_seconds": round(time.time() - started, 2),
        },
    )


def verify_lane(task):
    problems = []
    if not ensure_running(task):
        problems.append("the lane container is missing")
    else:
        if (
            lane_exec(task, ["test", "-d", f"{lane_workspace(task)}/.git"]).returncode
            != 0
        ):
            problems.append("the repository is missing from the lane")
        if not wait_for_dind(task):
            problems.append("the lane's inner Docker daemon did not start")
        res = lane_exec(task, ["claude", "--version"], timeout=60)
        version = (res.stdout or "").split()[:1]
        if version != [CLAUDE_CODE_VERSION]:
            problems.append(
                f"Claude Code in the lane is {version[0] if version else 'missing'}, "
                f"not {CLAUDE_CODE_VERSION}"
            )
    if problems:
        error("Setup verification failed: " + "; ".join(problems) + ".")
    ok(f"Lane verified (Claude Code {CLAUDE_CODE_VERSION})")


def print_next_steps():
    print("\n" + "=" * 50)
    ok("Pre-work setup complete.")
    print("\nNext steps:")
    print(f"1. cd {LANE} && ./claude-dev")
    print("2. First launch only: type /login and sign in with your own Claude account.")
    print("   The login stays in this task's lane; your own Claude Code is untouched.")
    print("3. Paste your opening prompt and do the run.")
    print("4. Finish with /exit.")
    print("5. Back in this folder: python3 prework.py collect")
    print(
        f"   Attach {OUTPUT_DIR}/final.diff to Final Output and the session .jsonl to"
    )
    print("   Conversation Log on your Studio task.")


def prompt(label, parse, hint):
    if not sys.stdin.isatty():
        error(f"{label} is required. Pass it as a flag (see --help).")
    for _ in range(5):
        try:
            raw = input(f"{label}: ")
        except (EOFError, KeyboardInterrupt):
            error("Cancelled.")
        if parse(raw) is not None:
            return raw
        print(f"  {hint}")
    error(f"No valid {label.lower()} given.")


def cmd_setup(args):
    started = time.time()
    show_training_data_disclaimer()

    raw_task = (
        args.task
        if args.task is not None
        else prompt(
            "Task (for example 'Task l50u5a0c')",
            normalize_task_id,
            "Use the task's name from Studio, like 'Task l50u5a0c', or its id 'task_<32 characters>'.",
        )
    )
    task_id = normalize_task_id(raw_task)
    if task_id is None:
        error(
            "The task must look like 'Task l50u5a0c', 'l50u5a0c' or 'task_<32 hex characters>'."
        )
    raw_repo = (
        args.repo
        if args.repo is not None
        else prompt(
            "Repo URL from the task",
            parse_repo_url,
            "Use https://github.com/<owner>/<repo>, optionally ending /tree/<40-character commit>.",
        )
    )
    parsed = parse_repo_url(raw_repo)
    if parsed is None:
        error(
            "The Repo URL must be https://github.com/<owner>/<repo>, optionally with "
            "/tree/<40-character commit>."
        )
    clone_url, commit = parsed
    if args.commit is not None:
        flag_commit = parse_commit(args.commit)
        if flag_commit is None:
            error("--commit must be a full 40-character commit sha.")
        if commit and commit != flag_commit:
            error("--commit names a different commit than the Repo URL.")
        commit = flag_commit
    ok(
        f"Task {task_id}, repository {clone_url}"
        + (f" at {commit[:8]}" if commit else "")
    )

    if not (Path(MANIFEST).exists() or Path(TASK_ID_FILE).exists()):
        check_folder_is_ours()
    runtime = activate_or_exit()
    archive_if_previous_task_differs(task_id)
    run_id = ensure_run_id(task_id)
    record_task_id(task_id)

    manifest = read_manifest()
    if manifest.get("mode") == "prework" and manifest.get("task_id") == task_id:
        try:
            exists, _ = assert_lane_owner(task_id)
        except LaneOwnerMismatch as e:
            error(str(e))
        image = ensure_lane_image()
        if not exists:
            # A Docker prune took the container; its volumes, and the work, remain.
            info(
                "Bringing back this task's lane container; your work is on its volume."
            )
            stage_harness(task_id)
            ensure_container(task_id, image)
            write_lane_wrapper(task_id, LANE, runtime)
        verify_lane(task_id)
        ok("This folder is already set up for this task.")
        print_next_steps()
        return

    model_a = Path(LANE)
    if model_a.exists() and {p.name for p in model_a.iterdir()} - {"claude-dev"}:
        error(f"{LANE}/ already holds files. Set the task up in a new empty folder.")
    preflight(task_id, clone_url)
    preflight_disk_space(clone_url)
    image = ensure_lane_image()

    staging = Path(STAGING_DIR)
    if staging.exists():
        rmtree_force(staging)
    clone_repository(clone_url, staging)
    if commit:
        if not checkout_commit(commit, staging) and not prompt_manual_checkout(
            commit, staging
        ):
            error(f"Could not check out {commit[:8]}, the commit the task starts from.")
        if lane_head(staging) != commit:
            error(f"The clone is not at {commit[:8]}, the commit the task starts from.")
        base_commit = commit
    else:
        base_commit = lane_head(staging)
        if not base_commit:
            error("Could not read the clone's commit.")
        info(
            "The Repo URL names no commit, so the task starts from the default branch's "
            f"latest commit:\n   {base_commit}"
        )
    ok(f"Base commit {base_commit}")

    seed_config_dir(task_id)
    build_lane(task_id, image, staging, runtime)
    write_manifest(task_id, run_id, clone_url, base_commit, image, started)
    verify_lane(task_id)
    print_next_steps()


# --------------------------------------------------------------------------
# Collect
# --------------------------------------------------------------------------

_DIFF_EXCLUDES = (
    "__pycache__/",
    "node_modules/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".DS_Store",
    ".vscode/",
    ".idea/",
)


def create_git_diff_patch(patch_file_path, base_commit, exec_prefix=()):
    """Write `git diff <base_commit>`, untracked files included, run through exec_prefix."""
    prefix = list(exec_prefix)
    patch = os.path.abspath(patch_file_path)
    if not base_commit:
        return False
    try:
        untracked = run(
            prefix + ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if untracked.returncode == 0:
            files = [
                f.strip()
                for f in untracked.stdout.splitlines()
                if f.strip() and not any(x in f for x in _DIFF_EXCLUDES)
            ]
            # Intent-to-add so new files show in the diff; one exec per 200 paths.
            for i in range(0, len(files), 200):
                batch = files[i : i + 200]
                if (
                    run(
                        prefix + ["git", "add", "-N", "--", *batch],
                        capture_output=True,
                        timeout=120,
                    ).returncode
                    != 0
                ):
                    for f in batch:
                        run(
                            prefix + ["git", "add", "-N", "--", f],
                            capture_output=True,
                            timeout=30,
                        )
        result = run(
            prefix
            + [
                "git",
                "diff",
                base_commit,
                "--",
                ".",
                ":!**/.mypy_cache",
                ":!**/__pycache__",
                ":!**/.pytest_cache",
                ":!**/.DS_Store",
                ":!**/node_modules",
                ":!**/.vscode",
                ":!**/.idea",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"git diff failed: {e}")
        return False
    if result.returncode != 0:
        print(f"git diff failed (exit {result.returncode})")
        for line in (result.stderr or "").strip().splitlines()[-3:]:
            print(f"    {line}")
        # A stale patch from an earlier collect must not pass for this one.
        if os.path.exists(patch):
            os.remove(patch)
        return False
    with open(patch, "w", encoding="utf-8") as f:
        f.write(result.stdout or "")
    return True


def session_transcripts(config_dir):
    """projects/*/*.jsonl under the config dir: regular files only, nothing reached through a link."""
    projects = Path(config_dir) / "projects"
    if projects.is_symlink() or not projects.is_dir():
        return []
    root = projects.resolve()
    found = []
    for p in sorted(projects.glob("*/*.jsonl")):
        if p.is_symlink() or p.parent.is_symlink() or not p.is_file():
            continue
        if p.resolve().parent.parent != root:
            continue
        found.append(p)
    return found


def cmd_collect(_args):
    manifest = read_manifest()
    if manifest.get("mode") != "prework":
        error(
            f"collect runs only in a task folder set up by `python3 prework.py setup` "
            f"(no pre-work {MANIFEST} here)."
        )
    task = task_id_marker()
    if not task or task != manifest.get("task_id"):
        error(f"This folder's {TASK_ID_FILE} does not match its {MANIFEST}.")
    activate_or_exit()
    out = Path(OUTPUT_DIR)
    out.mkdir(exist_ok=True)

    diff = out / "final.diff"
    if not ensure_running(task):
        warn(
            f"The lane container {lane_container(task)} is gone, so {diff} was not written. "
            "Run `python3 prework.py setup` here to bring it back, then collect again."
        )
    elif create_git_diff_patch(diff, manifest.get("base_commit"), git_prefix(task)):
        ok(f"Wrote {diff} ({diff.stat().st_size:,} bytes)")
    else:
        warn(f"Could not write {diff}: the lane's git diff failed.")

    # Only transcripts: the same dir holds the expert's login, which never leaves it.
    transcripts = session_transcripts(lane_config_dir(task))
    for src in transcripts:
        shutil.copyfile(src, out / src.name)
    if transcripts:
        ok(f"Copied {len(transcripts)} session transcript(s) into {out}/")
        print(f"\nAttach to your Studio task:\n  Final Output:     {diff}")
        for src in transcripts:
            print(f"  Conversation Log: {out / src.name}")
    else:
        warn(
            "No session transcript yet: run ./claude-dev in model_a/ and finish with /exit first."
        )


# --------------------------------------------------------------------------


def main(argv=None):
    if platform.system() == "Windows":
        error(
            "Native Windows is not supported yet. Install WSL, open a WSL terminal, and run "
            "this from a folder in your WSL home (not under /mnt/c)."
        )
    if sys.version_info < (3, 9):  # noqa: UP036 - experts run whatever python3 they have
        error(f"Python 3.9 or newer is required (this is {platform.python_version()}).")
    parser = argparse.ArgumentParser(
        prog="prework.py",
        description="Set up a Docker lane for a Johto pre-work task, and collect its outputs.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {TOOL_VERSION}"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup", help="build the lane for a task (run in a new, empty folder)"
    )
    setup.add_argument(
        "--task", help="the task's name, e.g. 'Task l50u5a0c', or its task_<id>"
    )
    setup.add_argument(
        "--repo",
        help="the task's Repo URL, https://github.com/<owner>/<repo>[/tree/<commit>]",
    )
    setup.add_argument(
        "--commit",
        help="the 40-character commit to start from, when the Repo URL has none",
    )
    sub.add_parser(
        "collect", help="write prework_output/ (diff and transcript) for Studio"
    )
    args = parser.parse_args(argv)

    drive = wsl_windows_drive_folder()
    if drive:
        error(
            f"This folder is on a Windows drive mounted into WSL ({drive}). Docker loses such "
            "folders partway through setup. Use a folder in your WSL home instead."
        )
    if args.command == "setup":
        cmd_setup(args)
    else:
        cmd_collect(args)


if __name__ == "__main__":
    main()
