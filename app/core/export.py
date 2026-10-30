"""Reusable UTF-8 CSV and JSON export for CLI, GUI, and other front ends."""

import csv
from dataclasses import asdict
import json
from pathlib import Path
import re
from collections.abc import Sequence

from app.models import LinkType, ResearchResource

CSV_FIELDS = ("resource_id", "resource_type", "title", "authors", "publication_year", "abstract",
              "doi", "source_provider", "provider_identifiers", "landing_page_url", "open_access_url",
              "links", "download_links", "license", "is_open_access", "provenance", "search_metadata", "search_status",
              "failed_providers")


def resource_json(resource: ResearchResource, search_metadata: dict | None = None) -> dict:
    data = {
        "resource_type": resource.resource_type.value,
        "title": resource.title,
        "description": resource.description,
        "authors": list(resource.authors) if resource.authors is not None else None,
        "publication_year": resource.publication_year,
        "source_provider": resource.source_provider,
        "external_id": resource.external_id,
        "doi": resource.doi,
        "keywords": list(resource.keywords) if resource.keywords is not None else None,
        "retrieved_at": resource.retrieved_at.isoformat(),
        "license": resource.license,
        "is_open_access": resource.is_open_access,
        "links": [asdict(link) | {"kind": link.kind.value} for link in resource.links],
        "downloadable_files": [asdict(file) for file in resource.downloadable_files],
        "provenance": {"source_provider": resource.source_provider,
                       "external_id": resource.external_id,
                       "retrieved_at": resource.retrieved_at.isoformat(),
                       "original_metadata": resource.original_metadata,
                       "provider_observations": [
                           {"source_provider": item.source_provider, "external_id": item.external_id,
                            "doi": item.doi, "retrieved_at": item.retrieved_at.isoformat(),
                            "original_metadata": item.original_metadata}
                           for item in resource.provenance
                       ]},
        "original_metadata": resource.original_metadata,
    }
    if search_metadata:
        data["search_metadata"] = search_metadata
    return data


def csv_row(resource: ResearchResource, search_metadata: dict | None = None) -> dict[str, str]:
    landing = next((link.url for link in resource.links if link.kind == LinkType.LANDING_PAGE), None)
    open_access = [link.url for link in resource.links if link.kind == LinkType.OPEN_ACCESS]
    return {
        "resource_id": resource.external_id or "",
        "resource_type": resource.resource_type.value,
        "title": resource.title or "",
        "authors": json.dumps(resource.authors, ensure_ascii=False) if resource.authors is not None else "",
        "publication_year": "" if resource.publication_year is None else str(resource.publication_year),
        "abstract": resource.description or "",
        "doi": resource.doi or "",
        "source_provider": resource.source_provider,
        "provider_identifiers": json.dumps([
            {"source_provider": item.source_provider, "external_id": item.external_id,
             "doi": item.doi}
            for item in resource.provenance
        ], ensure_ascii=False),
        "landing_page_url": landing or "",
        "open_access_url": json.dumps(open_access, ensure_ascii=False) if open_access else "",
        "links": json.dumps([
            {"kind": link.kind.value, "url": link.url, "source_provider": link.source_provider,
             "license": link.license, "is_open_access": link.is_open_access}
            for link in resource.links
        ], ensure_ascii=False),
        "download_links": json.dumps([asdict(file) for file in resource.downloadable_files], ensure_ascii=False),
        "license": resource.license or "",
        "is_open_access": "" if resource.is_open_access is None else str(resource.is_open_access).lower(),
        "provenance": json.dumps({"retrieved_at": resource.retrieved_at.isoformat(),
                                   "original_metadata": resource.original_metadata,
                                   "provider_observations": [
                                       {"source_provider": item.source_provider,
                                        "external_id": item.external_id, "doi": item.doi,
                                        "retrieved_at": item.retrieved_at.isoformat(),
                                        "original_metadata": item.original_metadata}
                                       for item in resource.provenance
                                   ]}, ensure_ascii=False),
        "search_status": (search_metadata or {}).get("status", ""),
        "search_metadata": json.dumps(search_metadata or {}, ensure_ascii=False),
        "failed_providers": json.dumps((search_metadata or {}).get("failed_providers", {}),
                                        ensure_ascii=False),
    }


def write_export(resources: Sequence[ResearchResource], output: str | Path, file_format: str,
                 search_metadata: dict | None = None) -> Path:
    """Write UTF-8 results to a new file; sanitize the leaf and refuse overwrite."""
    if file_format not in {"csv", "json"}:
        raise ValueError("format must be csv or json")
    requested = Path(output)
    if ".." in requested.parts:
        raise ValueError("export path cannot contain parent-directory traversal")
    safe_name = re.sub(r"[^\w.-]+", "_", requested.name, flags=re.UNICODE).strip("._")
    if not safe_name:
        raise ValueError("export filename must contain at least one letter or number")
    target = requested.parent / safe_name
    if target.exists():
        raise FileExistsError(f"export file already exists: {target}")
    with target.open("x", encoding="utf-8", newline="" if file_format == "csv" else None) as stream:
        if file_format == "csv":
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(csv_row(resource, search_metadata) for resource in resources)
        else:
            json.dump([resource_json(resource, search_metadata) for resource in resources], stream,
                      ensure_ascii=False, indent=2)
            stream.write("\n")
    return target
