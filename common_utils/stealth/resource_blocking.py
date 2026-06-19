from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
DEFAULT_BLOCKED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}


@dataclass(frozen=True, slots=True)
class ResourceBlockPolicy:
    blocked_resource_types: set[str] = field(default_factory=lambda: set(DEFAULT_BLOCKED_RESOURCE_TYPES))
    blocked_extensions: set[str] = field(default_factory=lambda: set(DEFAULT_BLOCKED_EXTENSIONS))
    block_stylesheets: bool = False

    def should_block(self, resource_type: str | None, url: str | None) -> bool:
        normalized_type = (resource_type or "").lower()
        if normalized_type == "stylesheet" and self.block_stylesheets:
            return True
        if normalized_type in self.blocked_resource_types:
            return True
        normalized_url = (url or "").lower().split("?", 1)[0]
        return any(normalized_url.endswith(extension) for extension in self.blocked_extensions)

    async def handle_async_route(self, route) -> None:
        request = route.request
        if self.should_block(getattr(request, "resource_type", None), getattr(request, "url", None)):
            await route.abort()
        else:
            await route.continue_()

    def handle_sync_route(self, route) -> None:
        request = route.request
        if self.should_block(getattr(request, "resource_type", None), getattr(request, "url", None)):
            route.abort()
        else:
            route.continue_()


async def handle_async_route_with_policy(route: Any, policy: ResourceBlockPolicy) -> None:
    request = route.request
    if policy.should_block(getattr(request, "resource_type", None), getattr(request, "url", None)):
        await route.abort()
    else:
        await route.continue_()


def handle_sync_route_with_policy(route: Any, policy: ResourceBlockPolicy) -> None:
    request = route.request
    if policy.should_block(getattr(request, "resource_type", None), getattr(request, "url", None)):
        route.abort()
    else:
        route.continue_()
