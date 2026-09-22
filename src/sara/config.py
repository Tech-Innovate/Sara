from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BoundingBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def validate(self) -> None:
        if not (-90 <= self.min_lat < self.max_lat <= 90):
            raise ValueError("latitude bounds must satisfy -90 <= min_lat < max_lat <= 90")
        if not (-180 <= self.min_lon < self.max_lon <= 180):
            raise ValueError("longitude bounds must satisfy -180 <= min_lon < max_lon <= 180")

    def contains(self, latitude: float, longitude: float) -> bool:
        return self.min_lat <= latitude <= self.max_lat and self.min_lon <= longitude <= self.max_lon

    def as_scraper_arg(self) -> str:
        return f"{self.min_lat},{self.min_lon},{self.max_lat},{self.max_lon}"


@dataclass(frozen=True)
class AreaConfig:
    name: str
    bbox: BoundingBox


def load_area(path: str | Path) -> AreaConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    bbox_raw = raw["bbox"]
    bbox = BoundingBox(
        min_lat=float(bbox_raw["min_lat"]),
        min_lon=float(bbox_raw["min_lon"]),
        max_lat=float(bbox_raw["max_lat"]),
        max_lon=float(bbox_raw["max_lon"]),
    )
    bbox.validate()
    name = str(raw.get("name") or Path(path).stem).strip()
    if not name:
        raise ValueError("area name cannot be empty")
    return AreaConfig(name=name, bbox=bbox)


def load_queries(path: str | Path) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value in seen:
            continue
        seen.add(value)
        queries.append(value)
    if not queries:
        raise ValueError("query file contains no queries")
    return queries


def write_query_snapshot(path: str | Path, queries: list[str]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(queries) + "\n", encoding="utf-8")
    return destination
