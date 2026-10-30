"""Canonical metadata for research resources and their evidenced links."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ResourceType(str, Enum):
    PAPER = "paper"
    DATASET = "dataset"


class LinkType(str, Enum):
    LANDING_PAGE = "landing_page"
    DOI = "doi"
    PUBLISHER = "publisher"
    REPOSITORY = "repository"
    OPEN_ACCESS = "open_access"
    DIRECT_FILE = "direct_file"


@dataclass(frozen=True)
class ResourceLink:
    kind: LinkType
    url: str
    source_provider: str
    license: str | None = None
    is_open_access: bool | None = None


@dataclass(frozen=True)
class DownloadableFile:
    """A provider-supplied direct file URL; availability is not rechecked here."""

    url: str
    file_format: str | None
    mime_type: str | None
    filename: str | None
    source_provider: str
    is_open_access: bool | None = None


@dataclass(frozen=True)
class ResourceProvenance:
    """One provider's original observation retained when records are merged."""

    source_provider: str
    external_id: str | None
    doi: str | None
    retrieved_at: datetime
    original_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchResource:
    resource_type: ResourceType
    title: str | None
    description: str | None
    authors: tuple[str, ...] | None
    publication_year: int | None
    source_provider: str
    external_id: str | None
    doi: str | None
    keywords: tuple[str, ...] | None
    retrieved_at: datetime
    license: str | None = None
    is_open_access: bool | None = None
    links: tuple[ResourceLink, ...] = ()
    downloadable_files: tuple[DownloadableFile, ...] = ()
    original_metadata: dict[str, Any] = field(default_factory=dict)
    provenance: tuple[ResourceProvenance, ...] = ()
