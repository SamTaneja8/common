from __future__ import annotations

import functools
import os
import random
import re
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from common_utils.stealth.resource_blocking import (
    ResourceBlockPolicy,
    handle_async_route_with_policy,
    handle_sync_route_with_policy,
)

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Written at image build time by each repo's own Dockerfile (which launches
# the just-installed browser immediately after `playwright install` and
# records `browser.version`) -- see dealnews1/Dockerfile and
# dealmoon1/Dockerfile. This lets User-Agent strings we hand-roll for
# rotation stay honest about the Chromium build actually running, instead of
# a manually maintained guess that silently drifts out of sync with whatever
# Playwright version is pinned. A mismatched claimed-vs-real version is
# trivially detectable via Client Hints / navigator.userAgentData regardless
# of what the UA header itself claims.
_CHROMIUM_VERSION_FILE = Path("/etc/chromium_version")


@functools.lru_cache(maxsize=1)
def bundled_chromium_major_version(default: int = 124) -> int:
    """Return the major version of the Chromium build bundled in this image.

    Falls back to `default` when the marker file isn't present (e.g. running
    outside a built image, in tests, or on an older image predating this
    file) so callers never hard-fail on a missing marker.
    """
    try:
        raw = _CHROMIUM_VERSION_FILE.read_text().strip()
        return int(raw.split(".")[0])
    except (OSError, ValueError, IndexError):
        return default


def _build_desktop_user_agents() -> tuple[str, str, str]:
    major = bundled_chromium_major_version()
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
    )


DESKTOP_USER_AGENTS = _build_desktop_user_agents()

_UA_CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")


def _extract_chrome_major(user_agent: str) -> int:
    """Parse the Chrome major version out of a resolved UA string, falling
    back to the real bundled version if the string doesn't contain one (e.g.
    a caller passed a fully custom user_agent). Deriving from the actual
    string in play -- rather than trusting bundled_chromium_major_version()
    a second time -- keeps this correct even if a profile overrides
    user_agent directly instead of drawing from DESKTOP_USER_AGENTS.
    """
    match = _UA_CHROME_VERSION_RE.search(user_agent)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return bundled_chromium_major_version()


def _platform_claim_for_user_agent(user_agent: str) -> tuple[str, str, str]:
    """Returns (navigator.platform value, Client-Hints platform name, GPU
    profile tag) consistent with the given UA string's claimed OS.

    Fixes a real bug: the previous static STEALTH_INIT_SCRIPT always claimed
    navigator.platform == 'Win32' regardless of which UA (Windows/Mac/Linux)
    the rotating DESKTOP_USER_AGENTS pool happened to pick -- a UA claiming
    "Macintosh" while navigator.platform reports "Win32" is a blatant,
    trivially-checked mismatch.
    """
    if "Windows" in user_agent:
        return "Win32", "Windows", "windows"
    if "Macintosh" in user_agent:
        return "MacIntel", "macOS", "mac"
    return "Linux x86_64", "Linux", "linux"


# UNMASKED_VENDOR_WEBGL/UNMASKED_RENDERER_WEBGL strings to report per claimed
# platform. Headless/containerized Chromium without real GPU passthrough
# reports a software renderer (SwiftShader/llvmpipe) by default -- a
# well-known, heavily-weighted automation signal since almost no real
# desktop reports it. These are common, unremarkable real-hardware strings
# chosen to blend in rather than stand out (an exotic/high-end GPU claim is
# its own tell).
_WEBGL_PROFILES: dict[str, tuple[str, str]] = {
    "windows": (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    "mac": (
        "Google Inc. (Apple)",
        "ANGLE (Apple, Apple M1, OpenGL 4.1)",
    ),
    "linux": (
        "Google Inc. (Mesa)",
        "ANGLE (Mesa, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6)",
    ),
}

# Template uses __TOKEN__ placeholders substituted via .replace() rather than
# str.format()/f-string interpolation -- the script body is full of literal
# JS braces, which would need constant {{ }} escaping otherwise.
_STEALTH_INIT_SCRIPT_TEMPLATE = """
(function () {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  Object.defineProperty(navigator, 'platform', { get: () => '__NAV_PLATFORM__' });
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => __HARDWARE_CONCURRENCY__ });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => __DEVICE_MEMORY__ });
  Object.defineProperty(window, 'chrome', { get: () => ({ runtime: {} }) });
  Object.defineProperty(navigator, 'pdfViewerEnabled', { get: () => true });

  const originalPermissionsQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalPermissionsQuery) {
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalPermissionsQuery(parameters)
    );
  }

  // Client Hints consistency: navigator.userAgentData exposes the real
  // engine version via getHighEntropyValues()/brands regardless of what the
  // UA string header claims. A mismatch between the two is a strong,
  // trivially-checked automation signal, so keep both aligned to the same
  // Chrome major version this profile's UA string uses.
  if (navigator.userAgentData) {
    try {
      const brands = [
        { brand: 'Not.A/Brand', version: '99' },
        { brand: 'Chromium', version: '__CHROME_MAJOR__' },
        { brand: 'Google Chrome', version: '__CHROME_MAJOR__' },
      ];
      Object.defineProperty(navigator.userAgentData, 'brands', { get: () => brands });
      Object.defineProperty(navigator.userAgentData, 'mobile', { get: () => false });
      Object.defineProperty(navigator.userAgentData, 'platform', { get: () => '__CH_PLATFORM__' });
      const originalGetHighEntropyValues = navigator.userAgentData.getHighEntropyValues.bind(navigator.userAgentData);
      navigator.userAgentData.getHighEntropyValues = (hints) => originalGetHighEntropyValues(hints).then((result) => ({
        ...result,
        platform: '__CH_PLATFORM__',
        uaFullVersion: '__CHROME_MAJOR__.0.0.0',
        fullVersionList: brands.map((b) => (
          (b.brand === 'Chromium' || b.brand === 'Google Chrome')
            ? { ...b, version: '__CHROME_MAJOR__.0.0.0' }
            : b
        )),
      }));
    } catch (e) {}
  }

  // WebGL vendor/renderer spoofing -- see _WEBGL_PROFILES above for why.
  const patchWebGL = (proto) => {
    if (!proto) return;
    const original = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return '__WEBGL_VENDOR__';
      if (parameter === 37446) return '__WEBGL_RENDERER__';
      return original.call(this, parameter);
    };
  };
  patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
  patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);

  // Canvas fingerprint noise: a tiny, session-stable per-pixel perturbation
  // defeats canvas-hash fingerprinting (an identical hash across visits is
  // the tell) without making rendered canvases visibly different. Returning
  // a constant/blank canvas is itself a well-known, easily detected evasion,
  // so nudge a small fraction of pixels by +/-1 instead, seeded once per
  // context so repeated calls within the same page stay consistent (a real
  // browser's canvas output doesn't change between calls in one session).
  (function () {
    let seed = __CANVAS_SEED__;
    const rand = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };
    const noisify = (imageData) => {
      const data = imageData.data;
      for (let i = 0; i < data.length; i += 4) {
        if (rand() < 0.02) {
          data[i] = Math.min(255, Math.max(0, data[i] + (rand() < 0.5 ? -1 : 1)));
        }
      }
      return imageData;
    };
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (...args) {
      const imageData = origGetImageData.apply(this, args);
      return noisify(imageData);
    };
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {
      try {
        const ctx = this.getContext('2d');
        if (ctx) {
          const imageData = origGetImageData.call(ctx, 0, 0, this.width, this.height);
          noisify(imageData);
          ctx.putImageData(imageData, 0, 0);
        }
      } catch (e) {}
      return origToDataURL.apply(this, args);
    };
  })();

  // AudioContext fingerprint noise: same rationale as canvas, applied to
  // AudioBuffer sample data (used by AudioContext-based fingerprinting).
  (function () {
    let seed = __AUDIO_SEED__;
    const rand = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };
    if (window.AudioBuffer) {
      const origGetChannelData = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function (...args) {
        const data = origGetChannelData.apply(this, args);
        for (let i = 0; i < data.length; i += 100) {
          data[i] += (rand() - 0.5) * 1e-7;
        }
        return data;
      };
    }
  })();
})();
"""


def build_stealth_init_script(user_agent: str) -> str:
    """Render the init script for a specific resolved user_agent so
    navigator.platform, Client Hints, and the WebGL vendor/renderer claim
    all stay internally consistent with whichever UA this context picked,
    instead of one static script used for every rotation.
    """
    chrome_major = _extract_chrome_major(user_agent)
    nav_platform, ch_platform, gpu_tag = _platform_claim_for_user_agent(user_agent)
    webgl_vendor, webgl_renderer = _WEBGL_PROFILES.get(gpu_tag, _WEBGL_PROFILES["windows"])

    replacements = {
        "__NAV_PLATFORM__": nav_platform,
        "__CH_PLATFORM__": ch_platform,
        "__CHROME_MAJOR__": str(chrome_major),
        "__HARDWARE_CONCURRENCY__": str(random.choice((4, 8, 12, 16))),
        "__DEVICE_MEMORY__": str(random.choice((4, 8, 16))),
        "__WEBGL_VENDOR__": webgl_vendor,
        "__WEBGL_RENDERER__": webgl_renderer,
        "__CANVAS_SEED__": str(random.randint(1, 2**31 - 1)),
        "__AUDIO_SEED__": str(random.randint(1, 2**31 - 1)),
    }
    script = _STEALTH_INIT_SCRIPT_TEMPLATE
    for token, value in replacements.items():
        script = script.replace(token, value)
    return script


def random_desktop_viewport(base_width: int = 1440, base_height: int = 900) -> dict[str, int]:
    # Landscape desktop shape. The previous default (base_height=2200) produced
    # a near-portrait viewport paired with a "desktop Chrome" UA/platform claim
    # — no proven-working scraper in this codebase (dealnews's or dealmoon's
    # pre-shared-image versions) ever used a shape like that; both either used
    # Playwright's own default or an explicit realistic landscape range.
    return {
        "width": max(1200, base_width + random.randint(-120, 160)),
        "height": max(760, base_height + random.randint(-120, 120)),
    }


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    user_agent: str | None = None
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    color_scheme: str = "light"
    viewport: dict[str, int] = field(default_factory=random_desktop_viewport)
    extra_http_headers: dict[str, str] = field(default_factory=dict)

    def resolved_user_agent(self) -> str:
        return self.user_agent or random.choice(DESKTOP_USER_AGENTS)


class ContextBuilder:
    def __init__(
        self,
        *,
        profile: BrowserProfile | None = None,
        resource_policy: ResourceBlockPolicy | None = None,
        apply_playwright_stealth: bool | None = None,
    ) -> None:
        self.profile = profile or BrowserProfile()
        self.resource_policy = resource_policy or ResourceBlockPolicy()
        self.apply_playwright_stealth = (
            _env_bool("ENABLE_PLAYWRIGHT_STEALTH", False)
            if apply_playwright_stealth is None
            else apply_playwright_stealth
        )

    def launch_kwargs(self, *, proxy: dict[str, str] | None = None, headless: bool = True) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process,Translate",
                "--disable-infobars",
                "--disable-popup-blocking",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        }
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    async def new_async_context(self, browser, *, storage_state: str | dict | None = None):
        viewport = self.profile.viewport
        user_agent = self.profile.resolved_user_agent()
        context_kwargs: dict[str, Any] = dict(
            user_agent=user_agent,
            locale=self.profile.locale,
            timezone_id=self.profile.timezone_id,
            viewport=viewport,
            screen=viewport,
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            color_scheme=self.profile.color_scheme,
            extra_http_headers=self.profile.extra_http_headers,
        )
        if storage_state is not None:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.add_init_script(build_stealth_init_script(user_agent))
        return context

    async def new_async_page(self, context):
        page = await context.new_page()
        async def _route_handler(route):
            await handle_async_route_with_policy(route, self.resource_policy)

        await page.route("**/*", _route_handler)
        if self.apply_playwright_stealth:
            try:
                from playwright_stealth import Stealth

                stealth = Stealth()
                apply_page = getattr(stealth, "apply_stealth_async", None)
                if callable(apply_page):
                    await apply_page(page)
            except ImportError:
                pass
            except Exception as exc:
                logger.warning("playwright_stealth async application failed; continuing with init-script stealth only: %s", exc)
                pass
        return page

    def new_sync_context(self, browser, *, storage_state: str | dict | None = None):
        viewport = self.profile.viewport
        user_agent = self.profile.resolved_user_agent()
        context_kwargs: dict[str, Any] = dict(
            user_agent=user_agent,
            locale=self.profile.locale,
            timezone_id=self.profile.timezone_id,
            viewport=viewport,
            screen=viewport,
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            color_scheme=self.profile.color_scheme,
            extra_http_headers=self.profile.extra_http_headers,
        )
        if storage_state is not None:
            context_kwargs["storage_state"] = storage_state
        context = browser.new_context(**context_kwargs)
        context.add_init_script(build_stealth_init_script(user_agent))
        return context

    def new_sync_page(self, context):
        page = context.new_page()
        def _route_handler(route):
            handle_sync_route_with_policy(route, self.resource_policy)

        page.route("**/*", _route_handler)
        if self.apply_playwright_stealth:
            try:
                from playwright_stealth import stealth_sync

                stealth_sync(page)
            except ImportError:
                pass
            except Exception as exc:
                logger.warning("playwright_stealth sync application failed; continuing with init-script stealth only: %s", exc)
                pass
        return page
