"""Free Cth.ai models (Gemini and open-source) for the Copilot CLI backend.

Cth.ai 2026.09+ ships a ``copilot`` wrapper whose ``--cth.model <id>`` routes
the GitHub Copilot CLI to Intel-hosted models that are not counted toward the
Copilot premium-request budget. The wrapper only exists inside a Cth shell, so
when the GUI was started elsewhere each call first enters the Cth setup.
Every model authenticates with a cached Autobots token, which needs a one-time
``autobots login``.

Selected models are stored as ``cth.ai:<id>`` so the route is explicit. The
site's setup command, catalogue and tool versions are data, read from
``profiles/cthai/free_models.json`` (or ``$ATPG_CTHAI_CONFIG``).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Prefix of a free-model selection in ``AgentConfig.cli_model``.
PREFIX = "cth.ai:"

SITE_CONFIG = (Path(__file__).resolve().parents[2] / "profiles" / "cthai"
               / "free_models.json")
SITE_CONFIG_ENV = "ATPG_CTHAI_CONFIG"
CATALOG_ENV = "ATPG_CTHAI_CATALOG"
#: Overrides the site's setup command when set (tests use it).
SETUP_COMMAND: Optional[str] = None
#: A user's own ``-cfg_ov`` file, for when the pinned versions go stale.
CFG_OV_ENV = "ATPG_CTHAI_CFG_OV"

TOKEN_DIR = Path.home() / ".autobots_tokens"
DEFAULT_SCOPE = "llm_access"

#: Seconds added to the call timeout when the Cth setup has to be entered.
PSETUP_OVERHEAD_S = 90

BEGIN_MARKER = "@@ATPG_CTHAI_BEGIN@@"
END_MARKER = "@@ATPG_CTHAI_END rc="

SLOW_NOTE = (
    "Slower here: this app was not opened from a Cth.ai shell (or from a "
    "terminal of a Cth.ai VS Code window), so every request first enters the "
    "Cth.ai setup in the background, adding roughly 5-15 seconds per answer. "
    "Open the app from a Cth.ai shell to remove the delay.")

LIMITS_NOTE = "Rate limits: 50 calls per minute and 2000 per day."

LOGIN_NEEDED = (
    "Free Cth.ai models need a one-time login. Click 'Log in…' on the "
    "readiness line (or see Help > AI Debug Agent > Free Cth.ai models), or "
    "in a Cth.ai shell run:  autobots login --device-code")

#: Hints from the Cth.ai wiki, shown in the model tooltip.
_HINTS = {
    "google/gemini-3.8-flash": "Preferred for general-purpose and agentic use.",
    "qwen3.8-27b-bf16": "Preferred for long-running, high-token tasks "
                        "(hosted inside Intel).",
}


class CthLoginRequired(RuntimeError):
    """The Autobots token for a free model is missing or no longer valid."""


@dataclass(frozen=True)
class FreeModel:
    """One free Cth.ai model, as advertised by the shared catalogue."""

    model_id: str
    name: str
    scope_group: str = DEFAULT_SCOPE
    context_length: int = 0

    @property
    def selection(self) -> str:
        return PREFIX + self.model_id

    def tooltip(self) -> str:
        lines = [f"{self.name}  ({self.model_id})",
                 "Free: not counted toward your Copilot premium budget."]
        if self.context_length:
            lines.append(f"Context window: {self.context_length:,} tokens.")
        hint = _HINTS.get(self.model_id)
        if hint:
            lines.append(hint)
        lines.append(LIMITS_NOTE)
        return "\n".join(lines)


def free_model_id(selection: str) -> Optional[str]:
    """The Cth.ai model id of *selection*, or None for a Copilot model."""
    text = (selection or "").strip()
    if text.startswith(PREFIX) and len(text) > len(PREFIX):
        return text[len(PREFIX):]
    return None


def site_config() -> dict:
    path = os.environ.get(SITE_CONFIG_ENV, "").strip() or str(SITE_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.debug("No Cth.ai site settings at %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def setup_command() -> str:
    return SETUP_COMMAND or str(site_config().get("setup_command") or "")


def load_free_models(path: Optional[str] = None) -> List[FreeModel]:
    """Read model ids and names from the catalogue; never its provider keys."""
    cat = (path or os.environ.get(CATALOG_ENV, "").strip()
           or str(site_config().get("catalog") or ""))
    if not cat:
        return []
    try:
        with open(cat, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.debug("No Cth.ai model catalogue at %s: %s", cat, exc)
        return []
    out = []
    for entry in data.get("models") or []:
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        name = str(entry.get("displayName") or mid).strip()
        if name.lower().startswith("cth.ai "):
            name = name[len("cth.ai "):]
        auth = entry.get("auth") or {}
        try:
            ctx = int(entry.get("context_length") or 0)
        except (TypeError, ValueError):
            ctx = 0
        out.append(FreeModel(model_id=mid, name=name,
                             scope_group=str(auth.get("scope_group")
                                             or DEFAULT_SCOPE),
                             context_length=ctx))
    return out


def scope_for(model_id: str, models: Optional[List[FreeModel]] = None) -> str:
    for m in models if models is not None else load_free_models():
        if m.model_id == model_id:
            return m.scope_group
    return DEFAULT_SCOPE


def token_path(scope_group: str = DEFAULT_SCOPE) -> Path:
    return TOKEN_DIR / f"{scope_group}.json"


def login_done(scope_group: str = DEFAULT_SCOPE) -> bool:
    """True once ``autobots login`` has cached a token for *scope_group*."""
    return token_path(scope_group).is_file()


def find_wrapper(environ: Optional[dict] = None) -> Optional[str]:
    """The Cth.ai ``copilot`` wrapper on PATH, when this is a Cth.ai shell."""
    env = os.environ if environ is None else environ
    found = shutil.which("copilot", path=env.get("PATH", ""))
    if not found:
        return None
    try:
        with open(os.path.realpath(found), "r", encoding="utf-8",
                  errors="replace") as fh:
            head = fh.read(200_000)
    except OSError:
        return None
    return found if "--cth.model" in head else None


def ward_dir() -> Path:
    """Private Cth work area used when the app has to enter the setup itself."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = str(os.getuid())
    ward = Path(tempfile.gettempdir()) / f"atpg_cthai_{user}"
    ward.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ward


def cfg_ov_file(ward: Path) -> str:
    own = os.environ.get(CFG_OV_ENV, "").strip()
    if own:
        return own
    cfg = ward / "cth-cfg.txt"
    versions = site_config().get("tool_versions") or {}
    body = "[toolversion]\n" + "".join(
        f"{tool} = {ver}\n" for tool, ver in versions.items())
    if not cfg.is_file() or cfg.read_text(encoding="utf-8") != body:
        cfg.write_text(body, encoding="utf-8")
    return str(cfg)


def headless_env(env: dict) -> dict:
    """*env* without a display: the Cth setup shows a CTH.ai splash screen
    (and can open VS Code) whenever it finds one, and a model call needs none."""
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(name, None)
    return env


def psetup_argv(script: str, ward: Optional[Path] = None) -> List[str]:
    """Run *script* inside a Cth.ai setup in the app's private work area."""
    ward = ward or ward_dir()
    # "-x" keeps a Cth.ai shell open afterwards, and that shell opens VS Code.
    flag = str(site_config().get("command_flag") or "-cmd")
    return [setup_command(), "-cfg_ov", cfg_ov_file(ward), "-ward", str(ward),
            flag, script]


def build_command(model_id: str, args: List[str], scratch: str,
                  environ: Optional[dict] = None,
                  stdin_path: Optional[str] = None) -> Tuple[List[str], bool]:
    """argv that runs ``copilot --cth.model <model_id> <args>``.

    Returns ``(argv, via_psetup)``. Inside a Cth.ai shell the wrapper is run
    directly. Otherwise a script in *scratch* runs it through the Cth setup
    between two marker lines, because the setup prints its own banner and
    does not pass the inner exit status on; that script feeds *stdin_path*
    (the prompt, when it is too long for the command line) to the CLI.
    """
    wrapper = find_wrapper(environ)
    if wrapper:
        return [wrapper, "--cth.model", model_id] + list(args), False
    inner = " ".join(shlex.quote(a) for a in
                     ["copilot", "--cth.model", model_id] + list(args))
    source = shlex.quote(stdin_path) if stdin_path else "/dev/null"
    script = os.path.join(scratch, "cthai_run.sh")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 f"echo '{BEGIN_MARKER}'\n"
                 f"{inner} < {source}\n"
                 "rc=$?\n"
                 f"echo \"{END_MARKER}$rc@@\"\n"
                 "exit $rc\n")
    os.chmod(script, 0o700)
    return psetup_argv(script), True


def login_script(ward: Optional[Path] = None) -> str:
    """A script that logs in, then waits so the user can read the result."""
    ward = ward or ward_dir()
    script = ward / "cthai_login.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'One-time login for the free Cth.ai models.'\n"
        "echo 'Open the link below in a browser, enter the code, and sign in"
        " with your Intel account.'\n"
        "echo\n"
        "autobots login --device-code\n"
        "echo\n"
        "printf 'Done. Press Enter to close this window. '\n"
        "read _\n", encoding="utf-8")
    os.chmod(script, 0o700)
    return str(script)


def login_argv() -> List[str]:
    """argv that opens the one-time login inside a Cth.ai setup."""
    return psetup_argv(login_script())


def login_command_text() -> str:
    """The same login as a command the user can paste into any terminal."""
    return " ".join(shlex.quote(a) for a in login_argv())


def reopen_argv(restore: str, python: str, repo: str,
                copilot_home: str = "", ward: Optional[Path] = None
                ) -> List[str]:
    """argv that starts this GUI inside a Cth.ai setup and restores *restore*.

    The display is kept (the GUI needs it), so the CTH.ai splash shows once.
    """
    ward = ward or ward_dir()
    lines = ["#!/bin/sh",
             f"cd {shlex.quote(repo)} || exit 1",
             "unset PYTHONHOME",
             f"PYTHONPATH={shlex.quote(repo)}${{PYTHONPATH:+:$PYTHONPATH}}",
             "export PYTHONPATH"]
    if copilot_home:
        lines += [f"COPILOT_HOME={shlex.quote(copilot_home)}",
                  "export COPILOT_HOME"]
    lines.append("exec " + " ".join(shlex.quote(a) for a in [
        python, "-m", "atpg_coverage_debug_agent", "--restore", restore]))
    script = ward / "cthai_reopen.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(script, 0o700)
    return psetup_argv(str(script), ward)


def explain_error(text: str) -> Optional[str]:
    """A plain-language explanation of a known free-model failure."""
    low = (text or "").lower()
    if ("autobots login" in low or "no cached token" in low
            or "could not obtain a valid token" in low):
        return LOGIN_NEEDED
    if "403" in low:
        return ("The Cth.ai model service refused the request (HTTP 403). "
                "This is usually a no_proxy setting: remove no_proxy/NO_PROXY "
                "from ~/.cshrc, ~/.cshrc.$USER and ~/.aliases, or make sure "
                "it includes .intel.com, .openai.azure.com, .azure-api.net, "
                ".services.ai.azure.com and .p.googleapis.com.")
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return "The free Cth.ai models are busy for you right now. " + LIMITS_NOTE
    if "not found in llm.json" in low:
        return ("This Cth.ai model is no longer offered. Pick another model "
                "from the Free group.")
    return None


class OutputFilter:
    """Strip the Cth.ai banners from a streamed CLI answer.

    Through the Cth setup everything before :data:`BEGIN_MARKER` is setup
    output (kept as :attr:`preamble` for error messages) and the
    :data:`END_MARKER` line carries the real exit status; anything after it
    is dropped. The wrapper's own
    ``Model : cth.ai:<id>`` line and the blank after it are dropped too.
    """

    def __init__(self, wait_for_marker: bool) -> None:
        self._started = not wait_for_marker
        self._buf = ""
        self._skip_blank = False
        self.preamble = ""
        self.rc: Optional[int] = None

    @property
    def started(self) -> bool:
        return self._started

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            kept = self._line(line)
            if kept is not None:
                out.append(kept + "\n")
        return "".join(out)

    def finish(self) -> str:
        rest, self._buf = self._buf, ""
        if not rest:
            return ""
        kept = self._line(rest)
        return kept or ""

    def _line(self, line: str) -> Optional[str]:
        bare = line.rstrip("\r")
        if not self._started:
            if bare.strip() == BEGIN_MARKER:
                self._started = True
            else:
                self.preamble += line + "\n"
            return None
        if self.rc is not None:
            return None  # the Cth setup's own output after the call
        if bare.startswith(END_MARKER):
            try:
                self.rc = int(bare[len(END_MARKER):].rstrip("@"))
            except ValueError:
                self.rc = 1
            return None
        if bare.startswith("Model") and ": cth.ai:" in bare:
            self._skip_blank = True
            return None
        if self._skip_blank:
            self._skip_blank = False
            if not bare.strip():
                return None
        return line
