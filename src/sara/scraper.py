from __future__ import annotations

import math
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AreaConfig

DEFAULT_IMAGE = "gosom/google-maps-scraper:v1.18.1"


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


def build_docker_command(
    *,
    area: AreaConfig,
    queries_file: Path,
    output_file: Path,
    options: ScrapeOptions,
) -> list[str]:
    options.validate()
    queries_file = queries_file.resolve()
    output_file = output_file.resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not queries_file.is_file():
        raise FileNotFoundError(queries_file)

    command = [
        "docker", "run", "--rm",
        "-e", "DISABLE_TELEMETRY=1",
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


def shutil_which(binary: str) -> str | None:
    # Small local implementation keeps this module dependency-free.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
