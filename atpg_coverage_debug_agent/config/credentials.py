"""Persistent storage for the GitHub token used by the Copilot CLI backend.

The token is a credential, so it is deliberately kept out of
``settings.json`` (which is world-readable and gets copied around). It is
stored either in the OS keyring, when one is available, or in a dedicated
file that is created with owner-only permissions.

The store is **per user, not per working directory**: several people can run
the GUI from different directories on the same host and each one gets only
their own token. The location is derived from the account that owns the
process (the passwd database, not the ``HOME`` environment variable, which
can be inherited from whoever launched the session), and a token file that
turns out to belong to a different account is refused rather than read.

The file fallback is *plaintext* — it protects the token with filesystem
permissions only. :func:`storage_description` returns a human-readable
description of where the token ends up so the GUI can tell the user the
truth about it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Keyring service/user identifiers, used only when ``keyring`` is installed.
_KEYRING_SERVICE = "atpg_debug_agent"
_KEYRING_USER = "github_token"

_KEY = "github_token"


def _home_dir() -> Path:
    """Home directory of the account that owns this process.

    ``Path.home()`` trusts ``$HOME``, which survives ``su``/``sudo`` and
    shared job-runner sessions. The passwd entry for the effective uid is
    the authoritative answer to "who is running the GUI".
    """
    try:
        import pwd  # noqa: PLC0415  (POSIX only, probed lazily)

        home = pwd.getpwuid(os.getuid()).pw_dir
        if home:
            return Path(home)
    except Exception:  # noqa: BLE001
        pass
    return Path.home()


_DEFAULT_CONFIG_DIR = _home_dir() / ".atpg_debug_agent"
_DEFAULT_CREDENTIALS_FILE = _DEFAULT_CONFIG_DIR / "credentials.json"


def user_config_dir() -> Path:
    """Per-user directory for this app's private state (not per directory)."""
    return _DEFAULT_CONFIG_DIR


def _owned_by_current_user(path: Path) -> bool:
    """True if *path* exists and belongs to the uid running this process."""
    try:
        return path.stat().st_uid == os.getuid()
    except OSError:
        return False


def _keyring():
    """Return the ``keyring`` module if it is installed and usable, else None."""
    try:
        import keyring  # noqa: PLC0415  (optional dependency, probed lazily)
        from keyring.backends.fail import Keyring as _FailKeyring  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        if isinstance(keyring.get_keyring(), _FailKeyring):
            return None
    except Exception:  # noqa: BLE001
        return None
    return keyring


def storage_description(path: Optional[Path] = None) -> str:
    """Describe where a saved token is kept, for display in the UI."""
    if _keyring() is not None:
        return "the system keyring"
    return f"{path or _DEFAULT_CREDENTIALS_FILE} (plaintext, owner-only)"


def save_github_token(token: str, path: Optional[Path] = None) -> bool:
    """Persist *token* for later sessions. Returns True on success.

    An empty *token* clears any stored value. The file fallback is written
    with ``0600`` permissions inside a ``0700`` directory; if those
    permissions cannot be applied the token is *not* written.
    """
    token = (token or "").strip()
    if not token:
        clear_github_token(path)
        return True

    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Keyring store failed (%s) — falling back to file", exc)

    cred_path = path or _DEFAULT_CREDENTIALS_FILE
    try:
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        # A shared home would let one account's directory (or planted
        # symlink) capture another account's token. Only write into a
        # directory and file this uid owns.
        if not _owned_by_current_user(cred_path.parent):
            logger.warning("Refusing to use %s: it belongs to another user",
                           cred_path.parent)
            return False
        if cred_path.exists() and not _owned_by_current_user(cred_path):
            logger.warning("Refusing to overwrite %s: it belongs to another user",
                           cred_path)
            return False
        os.chmod(cred_path.parent, stat.S_IRWXU)          # 0700
        # Create with 0600 before any content is written, so the secret is
        # never briefly readable by other users. O_NOFOLLOW stops a symlink
        # left in place by someone else from redirecting the write.
        fd = os.open(cred_path,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     stat.S_IRUSR | stat.S_IWUSR)         # 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({_KEY: token}, fh)
        finally:
            os.chmod(cred_path, stat.S_IRUSR | stat.S_IWUSR)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not save the GitHub token to %s: %s",
                       cred_path, exc)
        return False


def load_github_token(path: Optional[Path] = None) -> str:
    """Return the stored token, or an empty string if there is none.

    A file-backed token is ignored if its permissions allow group or other
    access — that means it is exposed, and silently reusing it would hide
    the problem.
    """
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(_KEYRING_SERVICE, _KEYRING_USER)
            if value:
                return value.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Keyring lookup failed: %s", exc)

    cred_path = path or _DEFAULT_CREDENTIALS_FILE
    if not cred_path.is_file():
        return ""
    try:
        st = cred_path.stat()
        if st.st_uid != os.getuid():
            logger.warning("Ignoring %s: it belongs to uid %d, not this user",
                           cred_path, st.st_uid)
            return ""
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.warning("Ignoring %s: permissions %o are too permissive",
                           cred_path, stat.S_IMODE(st.st_mode))
            return ""
        with open(cred_path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get(_KEY, "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the GitHub token from %s: %s",
                       cred_path, exc)
        return ""


def clear_github_token(path: Optional[Path] = None) -> None:
    """Remove any stored token from the keyring and the file fallback."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:  # noqa: BLE001
            pass  # Nothing stored, or the backend refused — nothing to undo.

    cred_path = path or _DEFAULT_CREDENTIALS_FILE
    try:
        if cred_path.exists() and not _owned_by_current_user(cred_path):
            logger.warning("Refusing to remove %s: it belongs to another user",
                           cred_path)
            return
        cred_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not remove %s: %s", cred_path, exc)
