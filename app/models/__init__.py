"""Provider-independent research data structures."""

from app.models.resource import (DownloadableFile, LinkType, ResearchResource, ResourceLink,
                                 ResourceProvenance, ResourceType)

__all__ = ["DownloadableFile", "LinkType", "ResearchResource", "ResourceLink",
           "ResourceProvenance", "ResourceType"]
