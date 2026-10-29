"""Desktop idle time: X11's screen saver extension (libXss) through ctypes, no extra dependency.

Read in a child process (`python -m miniteams.idle` prints the seconds, or nothing): Xlib exits the
process it runs in on an X I/O error, and a hung X server blocks the call forever.
"""

import asyncio
import ctypes
import ctypes.util
import math
import os
import sys

import structlog

log = structlog.get_logger()

_TIMEOUT = 2.0  # seconds a child may take before it is killed
_GRACE = 2  # failed samples in a row answered with the last good one: one slow reply must not lift an Away


class _Info(ctypes.Structure):
    """`XScreenSaverInfo`."""

    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),  # milliseconds since the last input
        ("event_mask", ctypes.c_ulong),
    ]


def _x11_session() -> bool:
    return bool(os.environ.get("DISPLAY")) and os.environ.get("XDG_SESSION_TYPE") != "wayland"


def _libs() -> tuple[ctypes.CDLL, ctypes.CDLL] | None:
    """libX11 and libXss; None without an X11 session or either library."""
    if not _x11_session():
        return None
    names = ctypes.util.find_library("X11"), ctypes.util.find_library("Xss")
    if None in names:
        return None
    try:
        x11, xss = ctypes.CDLL(names[0]), ctypes.CDLL(names[1])
    except OSError:
        return None
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [ctypes.c_void_p], ctypes.c_ulong
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    ints = ctypes.POINTER(ctypes.c_int)
    xss.XScreenSaverQueryExtension.argtypes = [ctypes.c_void_p, ints, ints]
    xss.XScreenSaverQueryExtension.restype = ctypes.c_int
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_Info)]
    xss.XScreenSaverQueryInfo.restype = ctypes.c_int
    return x11, xss


def x11_idle() -> float | None:
    """Seconds since the last keyboard or mouse input on the X11 desktop; None when it cannot be read.
    In-process: only the child runs it."""
    if (libs := _libs()) is None:
        return None
    x11, xss = libs
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        return None
    try:
        base, error = ctypes.c_int(), ctypes.c_int()
        if not xss.XScreenSaverQueryExtension(dpy, ctypes.byref(base), ctypes.byref(error)):
            return None  # querying a missing extension is an X error, which Xlib also exits on
        info = _Info()
        if not xss.XScreenSaverQueryInfo(dpy, x11.XDefaultRootWindow(dpy), ctypes.byref(info)):
            return None
        return float(info.idle) / 1000
    finally:
        x11.XCloseDisplay(dpy)


class DesktopIdle:
    """Async desktop idle seconds from a child process. A failed sample (exit, timeout, garbage, the previous
    child still running) answers the last good one up to `_GRACE` times in a row, then None; no X11 session
    or no child: None at once. Logged on each change, not each sample."""

    def __init__(self, argv: list[str] | None = None, timeout: float = _TIMEOUT) -> None:
        self.argv = argv or [sys.executable, "-I", "-m", "miniteams.idle"]
        self.timeout = timeout
        self.child: asyncio.subprocess.Process | None = None
        self._reason: str | None = ""  # "" = nothing said yet
        self._last: float | None = None  # the last good sample
        self._misses = 0  # failed samples since it

    async def __call__(self) -> float | None:
        if not _x11_session():
            return self._unknown("unavailable")
        if self.child is not None and self.child.returncode is None:
            return self._unknown("busy")  # at most one child: a killed one is reaped by the loop first
        try:
            child = self.child = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            return self._unknown("spawn", error_type=type(exc).__name__)
        try:
            out, _ = await asyncio.wait_for(child.communicate(), self.timeout)
        except TimeoutError:
            return self._unknown("timeout")
        finally:
            if child.returncode is None:
                child.kill()
        if child.returncode:
            return self._unknown("exit", code=child.returncode)
        if not out.strip():
            return self._unknown("unavailable")  # no X11 session, library or extension
        try:
            seconds = float(out.decode("ascii"))
        except ValueError:
            return self._unknown("garbage")
        if not math.isfinite(seconds) or seconds < 0:
            return self._unknown("garbage")
        self._note(None)
        self._last, self._misses = seconds, 0
        return seconds

    def _unknown(self, reason: str, **fields: object) -> float | None:
        self._note(reason, **fields)
        self._misses += 1
        if reason in ("unavailable", "spawn") or self._misses > _GRACE:
            self._last = None
        return self._last

    def _note(self, reason: str | None, **fields: object) -> None:
        if reason != self._reason:
            if reason is None:
                log.info("desktop_idle_known")
            else:
                log.info("desktop_idle_unknown", reason=reason, **fields)
            self._reason = reason


if __name__ == "__main__":
    if (found := x11_idle()) is not None:
        print(found)
