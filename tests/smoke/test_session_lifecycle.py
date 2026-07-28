"""Synthetic session transition and operation-ownership checks."""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web.session import SessionLifecycle  # noqa: E402


class _Cookies:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


class _Client:
    def __init__(self):
        self.cookies = _Cookies()


class _Session:
    def __init__(self):
        self.session_account = "alice"
        self.session_source = "browser"
        self._client = _Client()


def main() -> int:
    failures = 0
    os.environ["UOF_SESSION_DIR"] = tempfile.mkdtemp(prefix="uof-lifecycle-")

    lifecycle = SessionLifecycle()
    session = _Session()
    lifecycle._session = session
    token = lifecycle.begin_browser(session, force=True)
    lifecycle.reset()
    accepted = lifecycle.complete_browser(session, token, "alice")
    failures += _common.check(
        "logout invalidates pending browser generation so late success cannot revive identity",
        not accepted and lifecycle.current() is None and session.session_source is None,
    )

    lifecycle = SessionLifecycle()
    session = _Session()
    lifecycle._session = session
    entered = threading.Event()
    release = threading.Event()
    reset_done = threading.Event()

    def operation():
        with lifecycle.operation():
            entered.set()
            release.wait(2)

    worker = threading.Thread(target=operation)
    worker.start()
    entered.wait(2)
    resetter = threading.Thread(target=lambda: (lifecycle.reset(), reset_done.set()))
    resetter.start()
    failures += _common.check(
        "logout waits for the complete multi-step operation lease",
        not reset_done.wait(0.05),
    )
    release.set()
    worker.join(2)
    resetter.join(2)
    failures += _common.check(
        "logout proceeds after operation lease is released",
        reset_done.is_set() and lifecycle.current() is None,
    )

    print("=" * 50)
    print("session lifecycle 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
