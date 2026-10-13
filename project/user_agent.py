"""User Agent Rotation (UserAgentRotation.md).

Curated static pool of modern desktop browser identities. One User-Agent per
scraping session — never rotated mid-session. Weighted: Chrome 80%, Edge 15%,
Firefox 5%; Windows 70%, macOS 20%, Linux 10%. Always falls back to a static
Chrome identity; User-Agent is never empty.
"""

from __future__ import annotations

import random

from config import Config
from logger import logger

_CHROME_VER = "131.0.0.0"
_FIREFOX_VER = "133.0"

_WIN = "Windows NT 10.0; Win64; x64"
_MAC = "Macintosh; Intel Mac OS X 10_15_7"
_LINUX = "X11; Linux x86_64"

FALLBACK_USER_AGENT = (
    f"Mozilla/5.0 ({_WIN}) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{_CHROME_VER} Safari/537.36"
)

_POOL: dict[tuple[str, str], str] = {
    ("chrome", "windows"): FALLBACK_USER_AGENT,
    ("chrome", "macos"): (
        f"Mozilla/5.0 ({_MAC}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_VER} Safari/537.36"
    ),
    ("chrome", "linux"): (
        f"Mozilla/5.0 ({_LINUX}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_VER} Safari/537.36"
    ),
    ("edge", "windows"): (
        f"Mozilla/5.0 ({_WIN}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_VER} Safari/537.36 Edg/{_CHROME_VER}"
    ),
    ("edge", "macos"): (
        f"Mozilla/5.0 ({_MAC}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_VER} Safari/537.36 Edg/{_CHROME_VER}"
    ),
    ("firefox", "windows"): (
        f"Mozilla/5.0 ({_WIN}; rv:{_FIREFOX_VER}) Gecko/20100101 Firefox/{_FIREFOX_VER}"
    ),
    ("firefox", "macos"): (
        f"Mozilla/5.0 ({_MAC}; rv:{_FIREFOX_VER}) Gecko/20100101 Firefox/{_FIREFOX_VER}"
    ),
    ("firefox", "linux"): (
        f"Mozilla/5.0 ({_LINUX}; rv:{_FIREFOX_VER}) Gecko/20100101 Firefox/{_FIREFOX_VER}"
    ),
}

_BROWSER_WEIGHTS = (("chrome", 0.80), ("edge", 0.15), ("firefox", 0.05))
_OS_WEIGHTS = (("windows", 0.70), ("macos", 0.20), ("linux", 0.10))


class UserAgentProvider:
    """Provides one consistent browser identity per scraping session."""

    def __init__(self, config: Config, rng: random.Random | None = None) -> None:
        self._rotate = config.rotate_user_agent
        self._rng = rng if rng is not None else random.Random()

    def new_session(self) -> str:
        """Pick the User-Agent for a new scraping session.

        The caller keeps this value for every request/context in the session.
        """
        if not self._rotate:
            return FALLBACK_USER_AGENT
        try:
            browser = self._weighted_choice(_BROWSER_WEIGHTS)
            os_name = self._weighted_choice(_OS_WEIGHTS)
            user_agent = _POOL.get((browser, os_name))
            if user_agent is None:
                user_agent = _POOL.get(("chrome", os_name), FALLBACK_USER_AGENT)
            logger.debug("User-Agent session: {} / {}", browser, os_name)
            return user_agent
        except Exception as exc:
            logger.warning("User-Agent selection failed ({}), using fallback", exc)
            return FALLBACK_USER_AGENT

    def _weighted_choice(self, weighted: tuple[tuple[str, float], ...]) -> str:
        names = [name for name, _ in weighted]
        weights = [weight for _, weight in weighted]
        return self._rng.choices(names, weights=weights, k=1)[0]
