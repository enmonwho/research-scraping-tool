"""Versioned SQLite storage for searches and normalized resources."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from app.core.deduplication import (canonical_url, deduplicate_resources, find_duplicate,
                                    merge_resources, normalize_doi)
from app.models import (DownloadableFile, LinkType, ResearchResource, ResourceLink,
                        ResourceProvenance, ResourceType)

DEFAULT_DB_PATH = Path("data/research.db")
SCHEMA_VERSION = 3


class StorageError(Exception):
    """Database setup or operation failed without completing the write."""


@dataclass(frozen=True)
class SearchSummary:
    id: int
    topic: str
    searched_at: datetime
    result_count: int
    metadata: dict | None = None


def _resource_data(resource: ResearchResource) -> dict:
    """A lossless JSON-compatible snapshot for one search observation."""
    return {
        "resource_type": resource.resource_type.value,
        "title": resource.title,
        "description": resource.description,
        "authors": resource.authors,
        "publication_year": resource.publication_year,
        "source_provider": resource.source_provider,
        "external_id": resource.external_id,
        "doi": resource.doi,
        "keywords": resource.keywords,
        "retrieved_at": resource.retrieved_at.isoformat(),
        "license": resource.license,
        "is_open_access": resource.is_open_access,
        "links": [
            {"kind": link.kind.value, "url": link.url, "source_provider": link.source_provider,
             "license": link.license, "is_open_access": link.is_open_access}
            for link in resource.links
        ],
        "downloadable_files": [
            {"url": file.url, "file_format": file.file_format, "mime_type": file.mime_type,
             "filename": file.filename, "source_provider": file.source_provider,
             "is_open_access": file.is_open_access}
            for file in resource.downloadable_files
        ],
        "original_metadata": resource.original_metadata,
        "provenance": [
            {"source_provider": item.source_provider, "external_id": item.external_id,
             "doi": item.doi, "retrieved_at": item.retrieved_at.isoformat(),
             "original_metadata": item.original_metadata}
            for item in resource.provenance
        ],
    }


def _resource_from_data(data: dict) -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType(data["resource_type"]),
        title=data["title"],
        description=data["description"],
        authors=None if data["authors"] is None else tuple(data["authors"]),
        publication_year=data["publication_year"],
        source_provider=data["source_provider"],
        external_id=data["external_id"],
        doi=data["doi"],
        keywords=None if data["keywords"] is None else tuple(data["keywords"]),
        retrieved_at=datetime.fromisoformat(data["retrieved_at"]),
        license=data["license"],
        is_open_access=data["is_open_access"],
        links=tuple(ResourceLink(LinkType(link["kind"]), link["url"], link["source_provider"],
                                 link["license"], link["is_open_access"]) for link in data["links"]),
        downloadable_files=tuple(DownloadableFile(
            file["url"], file["file_format"], file["mime_type"], file["filename"],
            file["source_provider"], file["is_open_access"]
        ) for file in data["downloadable_files"]),
        original_metadata=data["original_metadata"],
        provenance=tuple(ResourceProvenance(
            item["source_provider"], item["external_id"], item.get("doi"),
            datetime.fromisoformat(item["retrieved_at"]), item.get("original_metadata", {}),
        ) for item in data.get("provenance", [])),
    )


class ResearchStore:
    """One SQLite connection; each saved search is a single transaction."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        try:
            if str(self.db_path) != ":memory:":
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(self.db_path))
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._initialize()
        except (OSError, sqlite3.Error, StorageError) as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise StorageError(f"Cannot open research database at {self.db_path}: {exc}") from exc

    def __enter__(self) -> "ResearchStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @property
    def schema_version(self) -> int:
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def _initialize(self) -> None:
        version = self.schema_version
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise StorageError(f"database schema version {version} is newer than supported {SCHEMA_VERSION}")
        # Migrations are ordered by version. Version 0 is an uninitialized database.
        migrations = {0: self._migrate_0_to_1, 1: self._migrate_1_to_2,
                      2: self._migrate_2_to_3}
        while version < SCHEMA_VERSION:
            migration = migrations.get(version)
            if migration is None:
                raise StorageError(f"no migration for database schema version {version}")
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                migration()
                self._connection.execute(f"PRAGMA user_version = {version + 1}")
                self._connection.commit()
            except (sqlite3.Error, StorageError):
                self._connection.rollback()
                raise
            version += 1

    def _migrate_0_to_1(self) -> None:
        names = ("searches", "resources", "search_resources", "resource_links", "downloadable_files")
        existing = {row[0] for row in self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if existing.intersection(names):
            raise StorageError("unversioned database already contains research tables")
        schema = """
            CREATE TABLE searches (
                id INTEGER PRIMARY KEY,
                topic TEXT NOT NULL,
                searched_at TEXT NOT NULL
            );
            CREATE TABLE resources (
                id INTEGER PRIMARY KEY,
                resource_type TEXT NOT NULL,
                source_provider TEXT NOT NULL,
                resource_key TEXT,
                external_id TEXT,
                doi TEXT,
                title TEXT,
                description TEXT,
                authors_json TEXT,
                publication_year INTEGER,
                keywords_json TEXT,
                retrieved_at TEXT NOT NULL,
                license TEXT,
                is_open_access INTEGER,
                original_metadata_json TEXT NOT NULL,
                UNIQUE (source_provider, resource_key)
            );
            CREATE TABLE search_resources (
                search_id INTEGER NOT NULL REFERENCES searches(id),
                resource_id INTEGER NOT NULL REFERENCES resources(id),
                position INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                PRIMARY KEY (search_id, resource_id),
                UNIQUE (search_id, position)
            );
            CREATE TABLE resource_links (
                id INTEGER PRIMARY KEY,
                resource_id INTEGER NOT NULL REFERENCES resources(id),
                link_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                url TEXT NOT NULL,
                source_provider TEXT NOT NULL,
                license TEXT,
                is_open_access INTEGER,
                UNIQUE (resource_id, link_key)
            );
            CREATE TABLE downloadable_files (
                id INTEGER PRIMARY KEY,
                resource_id INTEGER NOT NULL REFERENCES resources(id),
                file_key TEXT NOT NULL,
                url TEXT NOT NULL,
                file_format TEXT,
                mime_type TEXT,
                filename TEXT,
                source_provider TEXT NOT NULL,
                is_open_access INTEGER,
                UNIQUE (resource_id, file_key)
            );
            CREATE INDEX idx_search_resources_resource ON search_resources(resource_id);
        """
        for statement in schema.split(";"):
            if statement.strip():
                self._connection.execute(statement)

    def _migrate_1_to_2(self) -> None:
        """Add identity aliases and provider provenance without rewriting snapshots."""
        self._connection.execute("""
            CREATE TABLE resource_identifiers (
                id INTEGER PRIMARY KEY,
                resource_id INTEGER NOT NULL REFERENCES resources(id),
                namespace TEXT NOT NULL,
                identifier TEXT NOT NULL,
                source_provider TEXT NOT NULL DEFAULT '',
                UNIQUE(resource_id, namespace, identifier, source_provider)
            )
        """)
        self._connection.execute(
            "CREATE INDEX idx_resource_identifiers_lookup "
            "ON resource_identifiers(namespace, identifier, source_provider, resource_id)"
        )
        self._connection.execute("""
            CREATE TABLE resource_provenance (
                id INTEGER PRIMARY KEY,
                resource_id INTEGER NOT NULL REFERENCES resources(id),
                source_provider TEXT NOT NULL,
                external_id TEXT,
                doi TEXT,
                retrieved_at TEXT NOT NULL,
                original_metadata_json TEXT NOT NULL
            )
        """)
        self._connection.execute(
            "CREATE INDEX idx_resource_provenance_resource ON resource_provenance(resource_id)"
        )
        for row in self._connection.execute(
            "SELECT id, source_provider, external_id, doi, retrieved_at, original_metadata_json FROM resources"
        ).fetchall():
            resource_id, provider, external_id, doi, retrieved_at, metadata = row
            self._add_identifier(resource_id, "provider_id",
                                 canonical_url(external_id) or (external_id.strip() if external_id else None),
                                 provider.strip().casefold())
            self._add_identifier(resource_id, "url", canonical_url(external_id))
            self._add_identifier(resource_id, "doi", normalize_doi(doi), "")
            for (url,) in self._connection.execute(
                "SELECT url FROM resource_links WHERE resource_id = ?", (resource_id,)
            ).fetchall():
                self._add_identifier(resource_id, "url", canonical_url(url), "")
            self._connection.execute("""
                INSERT INTO resource_provenance
                (resource_id, source_provider, external_id, doi, retrieved_at, original_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (resource_id, provider, external_id, normalize_doi(doi) or doi,
                   retrieved_at, metadata))

    def _migrate_2_to_3(self) -> None:
        """Add optional search-level metadata without rewriting historical snapshots."""
        self._connection.execute(
            "ALTER TABLE searches ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )

    def _add_identifier(self, resource_id: int, namespace: str, value: str | None,
                        source_provider: str = "") -> None:
        if not value:
            return
        self._connection.execute("""
            INSERT OR IGNORE INTO resource_identifiers
            (resource_id, namespace, identifier, source_provider) VALUES (?, ?, ?, ?)
        """, (resource_id, namespace, value, source_provider))

    def _resource_by_id(self, resource_id: int) -> ResearchResource:
        row = self._connection.execute("""
            SELECT resource_type, title, description, authors_json, publication_year,
                   source_provider, external_id, doi, keywords_json, retrieved_at, license,
                   is_open_access, original_metadata_json
            FROM resources WHERE id = ?
        """, (resource_id,)).fetchone()
        links = self._connection.execute("""
            SELECT kind, url, source_provider, license, is_open_access
            FROM resource_links WHERE resource_id = ? ORDER BY id
        """, (resource_id,)).fetchall()
        files = self._connection.execute("""
            SELECT url, file_format, mime_type, filename, source_provider, is_open_access
            FROM downloadable_files WHERE resource_id = ? ORDER BY id
        """, (resource_id,)).fetchall()
        provenance = self._connection.execute("""
            SELECT source_provider, external_id, doi, retrieved_at, original_metadata_json
            FROM resource_provenance WHERE resource_id = ? ORDER BY id
        """, (resource_id,)).fetchall()
        return ResearchResource(
            ResourceType(row[0]), row[1], row[2],
            None if row[3] is None else tuple(json.loads(row[3])), row[4], row[5], row[6], row[7],
            None if row[8] is None else tuple(json.loads(row[8])), datetime.fromisoformat(row[9]),
            row[10], None if row[11] is None else bool(row[11]),
            tuple(ResourceLink(LinkType(item[0]), item[1], item[2], item[3],
                               None if item[4] is None else bool(item[4])) for item in links),
            tuple(DownloadableFile(item[0], item[1], item[2], item[3], item[4],
                                   None if item[5] is None else bool(item[5])) for item in files),
            json.loads(row[12]),
            tuple(ResourceProvenance(item[0], item[1], item[2], datetime.fromisoformat(item[3]),
                                     json.loads(item[4])) for item in provenance),
        )

    def _find_resource(self, resource: ResearchResource) -> int | None:
        """Use indexed strong identifiers, then strict title matching only without IDs."""
        doi = normalize_doi(resource.doi)
        provider_id = canonical_url(resource.external_id) or (
            resource.external_id.strip() if resource.external_id else None
        )
        provider = resource.source_provider.strip().casefold()
        urls = sorted({canonical_url(link.url) for link in resource.links
                       if link.kind in (LinkType.LANDING_PAGE, LinkType.PUBLISHER,
                                        LinkType.REPOSITORY, LinkType.OPEN_ACCESS)} - {None}
                      | ({canonical_url(resource.external_id)} if canonical_url(resource.external_id) else set()))
        lookups = []
        if doi:
            lookups.append(("doi", doi, ""))
        if provider_id:
            lookups.append(("provider_id", provider_id, provider))
        lookups.extend(("url", url, "") for url in urls)
        for namespace, value, namespace_provider in lookups:
            ids = self._connection.execute("""
                SELECT DISTINCT resource_id FROM resource_identifiers
                WHERE namespace = ? AND identifier = ? AND source_provider = ? ORDER BY resource_id
            """, (namespace, value, namespace_provider)).fetchall()
            for (resource_id,) in ids:
                existing = self._resource_by_id(resource_id)
                if existing.resource_type != resource.resource_type:
                    continue
                existing_doi = normalize_doi(existing.doi)
                known_dois = {value for value in (
                    existing_doi,
                    *(normalize_doi(item.doi) for item in existing.provenance),
                ) if value}
                if doi and known_dois and any(value != doi for value in known_dois):
                    continue
                return resource_id
        # Title fallback requires no stronger identity on either side and a strict full author match.
        if not doi and not provider_id and not urls:
            rows = self._connection.execute("""
                SELECT id FROM resources
                WHERE resource_type = ? AND doi IS NULL AND external_id IS NULL
                ORDER BY id
            """, (resource.resource_type.value,)).fetchall()
            for (resource_id,) in rows:
                existing = self._resource_by_id(resource_id)
                has_url = self._connection.execute("""
                    SELECT 1 FROM resource_identifiers WHERE resource_id = ? AND namespace = 'url' LIMIT 1
                """, (resource_id,)).fetchone()
                if not has_url and find_duplicate(resource, [existing]) == 0:
                    return resource_id
        return None

    def _record_provenance(self, resource_id: int, resource: ResearchResource) -> None:
        provenance = resource.provenance or (ResourceProvenance(
            resource.source_provider, resource.external_id, normalize_doi(resource.doi),
            resource.retrieved_at, resource.original_metadata,
        ),)
        for item in provenance:
            self._connection.execute("""
                INSERT INTO resource_provenance
                (resource_id, source_provider, external_id, doi, retrieved_at, original_metadata_json)
                SELECT ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM resource_provenance WHERE resource_id = ?
                    AND source_provider = ? AND external_id IS ? AND retrieved_at = ?
                    AND original_metadata_json = ?
                )
            """, (resource_id, item.source_provider, item.external_id, normalize_doi(item.doi) or item.doi,
                   item.retrieved_at.isoformat(), json.dumps(item.original_metadata, ensure_ascii=False),
                   resource_id, item.source_provider, item.external_id, item.retrieved_at.isoformat(),
                   json.dumps(item.original_metadata, ensure_ascii=False)))

    def _record_identifiers(self, resource_id: int, resource: ResearchResource) -> None:
        observations = resource.provenance or (ResourceProvenance(
            resource.source_provider, resource.external_id, resource.doi,
            resource.retrieved_at, resource.original_metadata,
        ),)
        for observation in observations:
            self._add_identifier(resource_id, "doi", normalize_doi(observation.doi))
            provider_id = canonical_url(observation.external_id) or (
                observation.external_id.strip() if observation.external_id else None
            )
            self._add_identifier(resource_id, "provider_id", provider_id,
                                 observation.source_provider.strip().casefold())
            self._add_identifier(resource_id, "url", canonical_url(observation.external_id))
        for link in resource.links:
            if link.kind in (LinkType.LANDING_PAGE, LinkType.PUBLISHER,
                             LinkType.REPOSITORY, LinkType.OPEN_ACCESS):
                self._add_identifier(resource_id, "url", canonical_url(link.url))

    def save_search(
        self, topic: str, resources: Iterable[ResearchResource], *, searched_at: datetime | None = None,
        metadata: dict | None = None,
    ) -> int:
        searched_at = searched_at or datetime.now(timezone.utc)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            search_id = self._connection.execute(
                "INSERT INTO searches(topic, searched_at, metadata_json) VALUES (?, ?, ?)",
                (topic, searched_at.isoformat(), json.dumps(metadata or {}, ensure_ascii=False)),
            ).lastrowid
            for position, resource in enumerate(deduplicate_resources(resources), start=1):
                resource_id = self._find_resource(resource)
                if resource_id is None:
                    doi = normalize_doi(resource.doi)
                    key = (f"{resource.resource_type.value}:id:{resource.external_id}|doi:{doi}"
                           if resource.external_id and doi else
                           f"{resource.resource_type.value}:id:{resource.external_id}"
                           if resource.external_id else
                           f"{resource.resource_type.value}:doi:{doi}" if doi else None)
                    self._connection.execute("""
                        INSERT INTO resources (
                            resource_type, source_provider, resource_key, external_id, doi,
                            title, description, authors_json, publication_year, keywords_json,
                            retrieved_at, license, is_open_access, original_metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        resource.resource_type.value, resource.source_provider, key, resource.external_id,
                        doi or resource.doi, resource.title, resource.description,
                        None if resource.authors is None else json.dumps(resource.authors, ensure_ascii=False),
                        resource.publication_year,
                        None if resource.keywords is None else json.dumps(resource.keywords, ensure_ascii=False),
                        resource.retrieved_at.isoformat(), resource.license,
                        None if resource.is_open_access is None else int(resource.is_open_access),
                        json.dumps(resource.original_metadata, ensure_ascii=False),
                    ))
                    resource_id = self._connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                    merged = resource
                else:
                    merged = merge_resources(self._resource_by_id(resource_id), resource)
                    self._connection.execute("""
                        UPDATE resources SET
                            doi = COALESCE(doi, ?), title = COALESCE(title, ?),
                            description = COALESCE(description, ?), authors_json = COALESCE(?, authors_json),
                            publication_year = COALESCE(publication_year, ?),
                            keywords_json = COALESCE(?, keywords_json), license = COALESCE(license, ?),
                            is_open_access = COALESCE(is_open_access, ?)
                        WHERE id = ?
                    """, (
                        normalize_doi(merged.doi) or merged.doi, merged.title, merged.description,
                        None if merged.authors is None else json.dumps(merged.authors, ensure_ascii=False),
                        merged.publication_year,
                        None if merged.keywords is None else json.dumps(merged.keywords, ensure_ascii=False),
                        merged.license, None if merged.is_open_access is None else int(merged.is_open_access),
                        resource_id,
                    ))
                # Current search snapshots describe this observation, enriched only with
                # complementary accumulated links/files/provenance. Older snapshots stay intact.
                snapshot = ResearchResource(
                    resource_type=merged.resource_type,
                    title=resource.title if resource.title is not None else merged.title,
                    description=resource.description if resource.description is not None else merged.description,
                    authors=resource.authors if resource.authors is not None else merged.authors,
                    publication_year=(resource.publication_year if resource.publication_year is not None
                                     else merged.publication_year),
                    source_provider=resource.source_provider,
                    external_id=resource.external_id,
                    doi=normalize_doi(resource.doi) or resource.doi or merged.doi,
                    keywords=merged.keywords,
                    retrieved_at=resource.retrieved_at,
                    license=resource.license if resource.license is not None else merged.license,
                    is_open_access=(resource.is_open_access if resource.is_open_access is not None
                                    else merged.is_open_access),
                    links=merged.links,
                    downloadable_files=merged.downloadable_files,
                    original_metadata=resource.original_metadata,
                    provenance=merged.provenance,
                )
                snapshot_json = json.dumps(_resource_data(snapshot), ensure_ascii=False)
                self._connection.execute("""
                    INSERT OR IGNORE INTO search_resources(search_id, resource_id, position, snapshot_json)
                    VALUES (?, ?, ?, ?)
                """, (search_id, resource_id, position, snapshot_json))
                for link in merged.links:
                    link_key = json.dumps([link.kind.value, link.url, link.source_provider,
                                           link.license, link.is_open_access], ensure_ascii=False)
                    self._connection.execute("""
                        INSERT OR IGNORE INTO resource_links
                        (resource_id, link_key, kind, url, source_provider, license, is_open_access)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (resource_id, link_key, link.kind.value, link.url, link.source_provider,
                           link.license, link.is_open_access))
                for file in merged.downloadable_files:
                    file_key = json.dumps([file.url, file.file_format, file.mime_type, file.filename,
                                           file.source_provider, file.is_open_access], ensure_ascii=False)
                    self._connection.execute("""
                        INSERT OR IGNORE INTO downloadable_files
                        (resource_id, file_key, url, file_format, mime_type, filename,
                         source_provider, is_open_access) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (resource_id, file_key, file.url, file.file_format, file.mime_type,
                           file.filename, file.source_provider, file.is_open_access))
                self._record_identifiers(resource_id, resource)
                self._record_identifiers(resource_id, merged)
                self._record_provenance(resource_id, resource)
                self._record_provenance(resource_id, merged)
            self._connection.commit()
            return search_id
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise StorageError(f"Could not save search: {exc}") from exc
        except BaseException:
            self._connection.rollback()
            raise

    def list_searches(self) -> list[SearchSummary]:
        try:
            rows = self._connection.execute("""
                SELECT s.id, s.topic, s.searched_at, COUNT(sr.resource_id), s.metadata_json
                FROM searches AS s LEFT JOIN search_resources AS sr ON sr.search_id = s.id
                GROUP BY s.id ORDER BY s.id DESC
            """).fetchall()
            return [SearchSummary(row[0], row[1], datetime.fromisoformat(row[2]), row[3],
                                  json.loads(row[4]))
                    for row in rows]
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not read search history: {exc}") from exc

    def merge_search_metadata(self, search_id: int, additions: dict) -> None:
        """Transactionally add derived metadata without touching resource snapshots."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT metadata_json FROM searches WHERE id = ?", (search_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"saved search ID {search_id} was not found")
            metadata = json.loads(row[0])
            metadata.update(additions)
            self._connection.execute(
                "UPDATE searches SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), search_id),
            )
            self._connection.commit()
        except StorageError:
            self._connection.rollback()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._connection.rollback()
            raise StorageError(f"Could not update search metadata: {exc}") from exc

    def get_search_resources(self, search_id: int) -> list[ResearchResource]:
        try:
            rows = self._connection.execute("""
                SELECT snapshot_json FROM search_resources
                WHERE search_id = ? ORDER BY position
            """, (search_id,)).fetchall()
            return [_resource_from_data(json.loads(row[0])) for row in rows]
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise StorageError(f"Could not read search resources: {exc}") from exc

    def get_search_resources_with_ids(self, search_id: int) -> list[tuple[int, ResearchResource]]:
        """Return saved resources with their local stable IDs, preserving snapshot order."""
        try:
            rows = self._connection.execute("""
                SELECT sr.resource_id, sr.snapshot_json
                FROM search_resources AS sr
                WHERE sr.search_id = ? ORDER BY sr.position
            """, (search_id,)).fetchall()
            return [(int(row[0]), _resource_from_data(json.loads(row[1]))) for row in rows]
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise StorageError(f"Could not read identified search resources: {exc}") from exc
