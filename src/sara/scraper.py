from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import AreaConfig
from .grid import estimate_grid, iter_grid_origins

DEFAULT_IMAGE = "gosom/google-maps-scraper:v1.18.1"
_RESUME_STATE_VERSION = 1
_GO_TRIM_SPACE_CHARS = (
    " \t\n\v\f\r"
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


@dataclass(frozen=True)
class ScrapeOptions:
    cell_km: float = 1.0
    depth: int = 5
    concurrency: int = 4
    browser_pool_size: int = 1
    pages_per_browser: int = 4
    lang: str = "en"
    zoom: int = 15
    resume: bool = True
    image: str = DEFAULT_IMAGE
    proxy_file: Path | None = None

    def validate(self) -> None:
        if not math.isfinite(self.cell_km) or self.cell_km <= 0:
            raise ValueError("cell_km must be a finite value greater than zero")
        if not (1 <= self.depth <= 10):
            raise ValueError("depth must be between 1 and 10")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be greater than zero")
        if self.browser_pool_size <= 0 or self.pages_per_browser <= 0:
            raise ValueError("browser pool and page counts must be greater than zero")
        if not (1 <= self.zoom <= 21):
            raise ValueError("grid zoom must be between 1 and 21")
        if not self.lang.strip():
            raise ValueError("language code cannot be empty")
        if not self.image.strip():
            raise ValueError("scraper image cannot be empty")
        if self.proxy_file is not None and not self.proxy_file.is_file():
            raise FileNotFoundError(self.proxy_file)


@dataclass(frozen=True)
class CompletionComparison:
    matched: int
    missing: int
    unexpected: int


def build_docker_command(
    *,
    area: AreaConfig,
    queries_file: Path,
    output_file: Path,
    options: ScrapeOptions,
    prepare_paths: bool = True,
    container_name: str | None = None,
    labels: dict[str, str] | None = None,
) -> list[str]:
    options.validate()
    queries_file = queries_file.resolve()
    output_file = output_file.resolve()

    if prepare_paths:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        if not queries_file.is_file():
            raise FileNotFoundError(queries_file)

        resume_state = Path(str(output_file) + ".resume.json")
        if options.resume and resume_state.exists() and not output_file.exists():
            # Match upstream's own resume invariant. Creating an empty results
            # file here would let a complete sidecar suppress all work and turn
            # missing raw evidence into a false successful run.
            raise RuntimeError("resume state exists but results file is missing")

        # The upstream container runs as root and resume mode creates new result
        # files with mode 0600. Pre-creating the bind-mounted result as the host
        # user preserves host ownership when the container appends or truncates it,
        # so Sara can ingest it immediately after Docker exits on rootful Linux.
        if not output_file.exists():
            output_file.touch(mode=0o600)

    command = [
        "docker", "run", "--rm",
        "-e", "DISABLE_TELEMETRY=1",
    ]
    if container_name is not None:
        command += ["--name", container_name]
    if labels:
        for key in sorted(labels):
            command += ["--label", f"{key}={labels[key]}"]
    command += [
        "-v", "gmaps-playwright-cache:/opt",
        "-v", f"{queries_file}:/queries.txt:ro",
        "-v", f"{output_file.parent}:/out",
    ]

    if options.proxy_file is not None:
        command += ["-v", f"{options.proxy_file.resolve()}:/run/secrets/gmaps-proxies:ro"]

    command += [
        options.image,
        "-input", "/queries.txt",
        "-results", f"/out/{output_file.name}",
        "-json",
        "-grid-bbox", area.bbox.as_scraper_arg(),
        "-grid-cell", str(options.cell_km),
        "-depth", str(options.depth),
        "-zoom", str(options.zoom),
        "-lang", options.lang,
        "-c", str(options.concurrency),
        "-browser-pool-size", str(options.browser_pool_size),
        "-pages-per-browser", str(options.pages_per_browser),
        "-exit-on-inactivity", "3m",
    ]
    if options.resume:
        command.append("-resume")
    if options.proxy_file is not None:
        command += ["-proxies-file", "/run/secrets/gmaps-proxies"]
    return command


def validate_recovery_queries(queries: list[str]) -> None:
    """Reject queries that cannot map one-to-one onto physical file records.

    Embedded newlines would expand one logical query into several physical
    query-file lines, invalidating any exact planned-search acknowledgment.
    Identity validation then mirrors the upstream resume parser so duplicate
    resume identities fail before any external effect.
    """
    for query in queries:
        if "\n" in query or "\r" in query:
            raise ValueError(f"query contains an embedded newline: {query!r}")
    validate_resume_query_identities(queries)


def recovery_query_snapshot_bytes(queries: list[str]) -> bytes:
    """Deterministic UTF-8 LF-separated query snapshot bytes."""
    return ("\n".join(queries) + "\n").encode("utf-8")


def command_for_display(command: list[str]) -> str:
    return shlex.join(command)


def run_scraper(command: list[str], *, env: dict[str, str] | None = None) -> int:
    if not shutil_which("docker"):
        raise RuntimeError("Docker is not installed or not available on PATH")
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(command, env=merged_env, check=False)
    return completed.returncode


def validate_resume_query_identities(queries: list[str]) -> None:
    """Reject query identities that upstream resume state cannot distinguish."""
    if not queries:
        raise ValueError("queries cannot be empty")

    seen: set[str] = set()
    for query_line in queries:
        query_text, query_id = _parse_upstream_query_identity(query_line)
        identity = query_id or query_text
        if identity in seen:
            raise ValueError("query IDs produce duplicate resume identities; use unique query IDs")
        seen.add(identity)


class ExpectedResumeInputs:
    """Lazy model of the deterministic v1.18.1 grid completion identities.

    Production comparison streams expected IDs through the one set already loaded
    from the upstream sidecar instead of allocating a second full expected-ID set.
    """

    def __init__(self, area: AreaConfig, queries: list[str], cell_km: float):
        validate_resume_query_identities(queries)
        estimate = estimate_grid(area.bbox, cell_km, len(queries))
        if estimate.cells == 0:
            raise ValueError("grid produced 0 cells; check bounding box and cell size")
        self._area = area
        self._queries = tuple(queries)
        self._cell_km = cell_km
        self._count = estimate.searches

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[str]:
        return _iter_expected_resume_input_ids(self._area, self._queries, self._cell_km)

    def __eq__(self, other) -> bool:
        if isinstance(other, ExpectedResumeInputs):
            return set(self) == set(other) and len(self) == len(other)
        if isinstance(other, set):
            return len(other) == self._count and set(self) == other
        return NotImplemented

    def compare_completed(self, completed: set[str]) -> CompletionComparison:
        """Compare exact completion evidence while consuming ``completed`` in place.

        The sidecar set is no longer needed after verification. Removing matches as
        expected IDs stream past keeps peak memory to one large ID set instead of
        materializing a second expected set for broad runs.
        """
        matched = 0
        missing = 0
        generated = 0

        for expected_id in self:
            generated += 1
            if expected_id in completed:
                completed.remove(expected_id)
                matched += 1
            else:
                missing += 1

        if generated != self._count:
            raise RuntimeError(
                f"internal completion model mismatch: generated {generated} IDs for {self._count} planned searches"
            )

        # If expected IDs collide at six-decimal coordinate precision, only one
        # sidecar ID can match them; the later duplicate is counted as missing and
        # the run therefore fails closed instead of being falsely accepted.
        return CompletionComparison(matched=matched, missing=missing, unexpected=len(completed))


def expected_resume_input_ids(
    area: AreaConfig,
    queries: list[str],
    cell_km: float,
) -> ExpectedResumeInputs:
    """Return the exact v1.18.1 grid completion model as a lazy iterable."""
    return ExpectedResumeInputs(area, queries, cell_km)


def _iter_expected_resume_input_ids(
    area: AreaConfig,
    queries: tuple[str, ...],
    cell_km: float,
) -> Iterator[str]:
    for query_line in queries:
        query_text, query_id = _parse_upstream_query_identity(query_line)
        identity = query_id or query_text
        for lat, lon in iter_grid_origins(area.bbox, cell_km):
            coordinates = f"{lat:.6f},{lon:.6f}"
            yield _deterministic_seed_id(identity, coordinates)


def load_resume_completed_input_ids(output_file: Path, image: str) -> set[str]:
    """Load upstream completion evidence, including root-owned sidecars on Linux."""
    state_path = Path(str(output_file) + ".resume.json")
    try:
        text = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except PermissionError:
        text = _read_file_via_container(state_path, image)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid resume state JSON: {state_path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("resume state must be a JSON object")

    version = payload.get("version")
    if type(version) is not int or version != _RESUME_STATE_VERSION:
        raise RuntimeError(f"unsupported resume state version: {version!r}")

    completed = payload.get("completed_inputs")
    if not isinstance(completed, list) or not all(isinstance(item, str) and item for item in completed):
        raise RuntimeError("resume state completed_inputs must be a list of non-empty strings")
    completed_set = set(completed)
    if len(completed_set) != len(completed):
        raise RuntimeError("resume state completed_inputs contains duplicate IDs")
    return completed_set


def _read_file_via_container(path: Path, image: str) -> str:
    if not shutil_which("docker"):
        raise RuntimeError("Docker is required to read the root-owned resume state")
    command = [
        "docker", "run", "--rm",
        "--network", "none",
        "--entrypoint", "/bin/cat",
        "-v", f"{path.parent.resolve()}:/out:ro",
        image,
        f"/out/{path.name}",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"could not read resume state through container: {detail}")
    return completed.stdout


def _go_trim_space(value: str) -> str:
    """Mirror Go strings.TrimSpace for upstream query parsing."""
    return value.strip(_GO_TRIM_SPACE_CHARS)


def _parse_upstream_query_identity(line: str) -> tuple[str, str]:
    value = _go_trim_space(line)
    if "#!#" in value:
        before, after = value.split("#!#", 1)
        query_text = _go_trim_space(before)
        query_id = _go_trim_space(after)
    else:
        query_text = value
        query_id = ""
    if not query_text:
        raise ValueError(f"invalid query line {line!r}: empty query text")
    return query_text, query_id


def _deterministic_seed_id(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"resume:{digest.hexdigest()}"


def shutil_which(binary: str) -> str | None:
    # Small local implementation keeps this module dependency-free.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
