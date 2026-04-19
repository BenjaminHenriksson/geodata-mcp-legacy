"""Catalog of locally available datasets. Loaded once from catalog.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalog.json"


SourceType = Literal["geopackage", "parquet"]


@dataclass
class AttributeSpec:
    name: str
    type: str
    description_sv: str
    description_en: str
    sample_values: list[str] = field(default_factory=list)


@dataclass
class DatasetEntry:
    id: str
    name_sv: str
    name_en: str
    description_sv: str
    description_en: str
    source_type: SourceType
    file_path: str            # relative to repo root
    layer: str | None         # GPKG layer name; None for parquet
    crs_epsg: int | None      # None for tabular (parquet)
    geometry_type: str | None
    feature_count: int | None
    coverage: str             # e.g. "Stockholm kommun"
    temporal: str             # e.g. "2010-2024"
    keywords_sv: list[str]
    keywords_en: list[str]
    attributes: list[AttributeSpec]
    publisher: str
    license: str
    license_url: str
    source_url: str
    retrieved: str

    def absolute_path(self) -> Path:
        return ROOT / self.file_path

    def search_corpus(self) -> str:
        """Concatenated text used for fuzzy search."""
        parts = [
            self.id, self.name_sv, self.name_en,
            self.description_sv, self.description_en,
            *self.keywords_sv, *self.keywords_en,
            *(a.name for a in self.attributes),
            *(a.description_sv for a in self.attributes),
            *(a.description_en for a in self.attributes),
        ]
        return " ".join(p for p in parts if p)


def _entry_from_dict(d: dict) -> DatasetEntry:
    attrs = [AttributeSpec(**a) for a in d.get("attributes", [])]
    # Required fields — explicit so a typo at least fails loudly. Optional
    # metadata uses .get() so adding a dataset with missing extras doesn't
    # crash-loop the service (a hardening lesson from osm_addresses'
    # missing license_url).
    try:
        return DatasetEntry(
            id=d["id"], name_sv=d["name_sv"], name_en=d["name_en"],
            description_sv=d["description_sv"], description_en=d["description_en"],
            source_type=d["source_type"], file_path=d["file_path"],
            layer=d.get("layer"), crs_epsg=d.get("crs_epsg"),
            geometry_type=d.get("geometry_type"), feature_count=d.get("feature_count"),
            coverage=d.get("coverage", ""), temporal=d.get("temporal", ""),
            keywords_sv=d.get("keywords_sv", []), keywords_en=d.get("keywords_en", []),
            attributes=attrs,
            publisher=d.get("publisher", ""),
            license=d.get("license", ""),
            license_url=d.get("license_url", ""),
            source_url=d.get("source_url", ""),
            retrieved=d.get("retrieved", ""),
        )
    except KeyError as e:
        raise ValueError(
            f"catalog entry {d.get('id', '?')!r} is missing required field {e}. "
            f"Required: id, name_sv, name_en, description_sv, description_en, "
            f"source_type, file_path."
        ) from e


class Catalog:
    def __init__(self, entries: list[DatasetEntry]) -> None:
        self._entries = {e.id: e for e in entries}
        self._corpus = {e.id: e.search_corpus() for e in entries}

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> "Catalog":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([_entry_from_dict(d) for d in data["datasets"]])

    def get(self, dataset_id: str) -> DatasetEntry | None:
        return self._entries.get(dataset_id)

    def all(self) -> list[DatasetEntry]:
        return list(self._entries.values())

    def search(self, query: str, limit: int = 10) -> list[tuple[DatasetEntry, float]]:
        if not query.strip():
            return [(e, 0.0) for e in self._entries.values()][:limit]
        results = process.extract(
            query, self._corpus,
            scorer=fuzz.WRatio,
            processor=str.lower,
            limit=limit,
        )
        return [(self._entries[match_id], score) for match_text, score, match_id in results]
