"""Route Claude sessions across several subscriptions.

The bot used to run every session on one login (the host `~/.claude`, or the
token captured by /login). alan (`/home/ubuntu/alan`) already keeps the
registry of subscriptions this box may use (`secrets/accounts.toml`) and a
flock-protected record of which ones are currently capped
(`state/accounts.json`); its `accounts.py` is stdlib-only, so this module
loads it by path and reuses both. Sharing that state is the point: an
account alan capped a minute ago must not be probed again here, and a limit
this bot walks into parks the account for alan too.

Selection policy, cooldown detection and per-model caps all come from alan
(see its accounts.py): among accounts with allowance, the one whose 7-day
window resets soonest wins, so the allowance about to expire is spent first.

`kind = "cursor"` accounts are listed but never picked: they only run through
the cursor-agent CLI, not the Claude Agent SDK this bridge speaks.
"""
import functools
import importlib.util
import logging
import re
import sys
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)

# Families a limit notice can name ("You've reached your Fable 5 limit"),
# used to record a per-model cap instead of parking the whole subscription.
_FAMILY_RE = re.compile(r"\b(fable|opus|sonnet|haiku)\b", re.I)


@functools.lru_cache(maxsize=1)
def _alan():
    """alan's accounts module, or None when it is not installed here."""
    path = Path(config.ALAN_ACCOUNTS)
    if not path.is_file():
        log.info("account routing off: %s not found", path)
        return None
    try:
        spec = importlib.util.spec_from_file_location("alan_accounts", path)
        mod = importlib.util.module_from_spec(spec)
        # Registered before exec: @dataclass resolves annotations through
        # sys.modules[cls.__module__] and blows up on a module that is not
        # there yet.
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("alan_accounts", None)
        log.exception("could not load alan accounts module from %s", path)
        return None
    return mod


@functools.lru_cache(maxsize=1)
def _alan_core():
    """alan's main module (for its limit classifier), or None.

    Loaded lazily — it pulls in the Agent SDK — and only once the bot has to
    read a limit notice. alan's own `accounts` import is bound to the module
    this router already uses, so both sides share one registry object rather
    than loading a second copy of it.
    """
    mod = _alan()
    if mod is None:
        return None
    path = Path(config.ALAN_ACCOUNTS).with_name("alan.py")
    if not path.is_file():
        return None
    try:
        sys.modules.setdefault("accounts", mod)
        spec = importlib.util.spec_from_file_location("alan_core", path)
        core = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = core
        spec.loader.exec_module(core)
    except Exception:
        sys.modules.pop("alan_core", None)
        log.exception("could not load alan's limit classifier from %s", path)
        return None
    return core


def classify_limit(text: str | None, model: str | None = None):
    """alan's reading of a limit notice: (scope, model, reset_epoch), or None
    when `text` is not one. It knows the wordings this bot's own regex misses
    — above all the per-model cap ("You've reached your Fable limit"), which
    reads as an ordinary reply to the old pattern list and so never triggered
    a switch. Returns None when alan is not installed; the caller falls back
    to its own patterns."""
    core = _alan_core()
    if core is None:
        return None
    try:
        return core.classify_limit(text, model)
    except Exception:
        log.exception("alan's limit classifier failed on %r", (text or "")[:120])
        return None


def available() -> bool:
    """True when routing is possible (alan's registry has a Claude account)."""
    return bool(usable())


def accounts() -> list:
    """Every registered account, cursor ones included (they are display-only
    here). Empty list when routing is unavailable."""
    mod = _alan()
    if mod is None:
        return []
    try:
        return mod.load_accounts()
    except Exception:
        log.exception("could not read the account registry")
        return []


def usable() -> list:
    """Accounts this bot can actually run on (Claude logins/tokens)."""
    return [a for a in accounts() if not a.cursor]


def names() -> list[str]:
    return [a.name for a in usable()]


def get(name: str | None):
    if not name:
        return None
    return next((a for a in usable() if a.name == name), None)


def resolve(name: str | None) -> str | None:
    """The registered slug for a name typed in a chat ('Solar Dev', 'solar
    dev', 'SOLAR-DEV' -> 'solar-dev'), or the input unchanged when no account
    matches. Pins are always stored as slugs."""
    mod = _alan()
    if mod is None or not name:
        return name
    try:
        return mod.resolve(name)
    except Exception:
        log.exception("could not resolve the account name %r", name)
        return name


def title(name: str | None) -> str | None:
    """How to spell an account in a message ('solar-dev' -> 'Solar Dev')."""
    a = get(name)
    return a.title if a is not None else name


def env_for(acct) -> dict[str, str]:
    """Environment that makes the CLI use `acct`.

    A login-dir account gets CLAUDE_CONFIG_DIR, a token account
    CLAUDE_CODE_OAUTH_TOKEN; the config dir is always spelled out (the
    account's own, else the host's) and a token is blanked unless the account
    has one — an inherited token would otherwise win over the config dir and
    silently run the session on the wrong account. Blanking must not be
    symmetric: an empty CLAUDE_CODE_OAUTH_TOKEN is ignored by the CLI, but an
    empty CLAUDE_CONFIG_DIR is taken as a real (empty) path and lands on
    "Not logged in" (verified against the bundled CLI 2026-09-11).
    """
    env = {"CLAUDE_CONFIG_DIR": str(acct.dir), "CLAUDE_CODE_OAUTH_TOKEN": ""}
    env.update(acct.env())
    return env


def pick(pin: str | None, model: str | None = None):
    """The account a session should connect on, or None when routing is off.

    A pin is honoured even while the account is capped — the request then
    doubles as the probe that finds out whether the limit has lifted, which
    is what the retry loop expects. With no pin and every account capped,
    the first registered account that is not held in reserve plays that role
    (an account keeping a model's allowance for the user is the last one to
    probe with — see alan's accounts.reserved()).
    """
    mod = _alan()
    if mod is None:
        return None
    try:
        # avoid_cooldown=False: alan would refresh usage over HTTP, and this
        # runs on the event loop. Snapshots are refreshed in the background
        # (refresh_usage) and limit marks are written the moment one is hit,
        # so the cached view is what routing needs anyway.
        chosen = mod.pick(override=pin, avoid_cooldown=False, model=model)
    except SystemExit as e:            # unknown pin
        log.warning("account pin rejected: %s", e)
        chosen = None
    except Exception:
        log.exception("account selection failed")
        return None
    if chosen is not None and not chosen.cursor:
        return chosen
    return get(pin) or _fallback()


def _fallback():
    """The account to probe with when every one of them is capped: the first
    registered one, but a reserved account only if it is the only one left."""
    free = usable()
    if not free:
        return None
    mod = _alan()
    try:
        spare = [a for a in free if mod.reserved(a.name)[0] is None]
    except Exception:            # older alan without reserved()
        spare = free
    return (spare or free)[0]


def next_account(pin: str | None, model: str | None, current: str | None) -> str | None:
    """Another subscription with allowance for `model`, or None when the chat
    should wait out the limit instead (pinned chat, or nothing left)."""
    if pin or _alan() is None:
        return None                     # a pin means: this account or wait
    try:
        chosen = _alan().pick(override=None, avoid_cooldown=False, model=model)
    except Exception:
        log.exception("account selection failed")
        return None
    if chosen is None or chosen.cursor or chosen.name == current:
        return None
    return chosen.name


def limit_family(text: str | None) -> str | None:
    """The model family a limit notice names ('Fable 5 limit' -> 'fable'), or
    None for a notice about the account's own window ('5-hour limit reached')."""
    m = _FAMILY_RE.search(text or "")
    return m.group(1).lower() if m else None


def mark_limited(name: str, until: float, text: str | None = None) -> None:
    """Record a usage limit hit on `name`. A notice naming a model family caps
    that family alone (the account keeps serving every other model); anything
    else parks the account until `until`."""
    mod = _alan()
    if mod is None:
        return
    try:
        fam = limit_family(text)
        if fam:
            mod.mark_model_limited(name, fam, until, message=text)
        else:
            mod.mark_limited(name, until)
        if text:
            mod.record_limit_message(name, text, fam,
                                     scope="model" if fam else "account")
    except Exception:
        log.exception("could not record the limit on %s", name)


def record_rate_limit(name: str, info, model: str | None = None) -> None:
    """Hand the CLI's streamed rate-limit event to alan: it keeps the usage
    picture warm and, for a rejection, opens the right window (account-wide
    for five_hour/seven_day, per-model for seven_day_opus and friends)."""
    mod = _alan()
    if mod is None:
        return
    try:
        mod.record_rate_limit(name, info, model)
    except Exception:
        log.exception("could not record the rate-limit event for %s", name)


def refresh_usage() -> None:
    """Refresh every account's usage snapshot (blocking HTTP — call from a
    thread). alan caches each for 60s, so calling this per connect is cheap."""
    mod = _alan()
    if mod is None:
        return
    for a in usable():
        try:
            mod.refresh_usage(a, timeout=6)
        except Exception:
            log.warning("usage refresh failed for %s", a.name, exc_info=True)


def _state_line(acct) -> str:
    """'available' or why the account cannot run right now."""
    mod = _alan()
    if acct.cursor:
        return "cursor-agent only — not usable from this bot"
    try:
        until, why = mod.cooldown(acct.name)
    except Exception:
        return "unknown"
    parts = []
    if until:
        when = time.strftime('%H:%M UTC', time.gmtime(until))
        # A reserved account is not out of quota: it is kept free for the
        # user until a day before its reserved window resets.
        held = mod.reserved(acct.name)[0] if hasattr(mod, "reserved") else None
        parts.append(f"{why}, {'free again' if held else 'resets'} {when}")
    else:
        parts.append("available")
    try:
        caps = mod.model_cooldowns(acct.name)
    except Exception:
        caps = {}
    for c in caps.values():
        parts.append(f"{c['label']} capped until "
                     f"{time.strftime('%H:%M UTC', time.gmtime(c['until']))}")
    try:
        snap = mod._read_state().get(acct.name, {}).get("usage") or {}
        used = [f"{nm} {w['pct']:.0f}%"
                for k, nm in (("five_hour", "5h"), ("seven_day", "7d"))
                if (w := snap.get(k)) and w.get("pct") is not None]
        if used:
            parts.append(" · ".join(used))
    except Exception:
        pass
    return ", ".join(parts)


def overview(pin: str | None, current: str | None) -> str:
    """Multi-line account listing for /account and /status (plain text)."""
    if _alan() is None:
        return (f"Account routing is off — {config.ALAN_ACCOUNTS} not found.\n"
                "Sessions run on the host Claude login (or the /login token).")
    lines = ["Subscriptions (registry and limit state shared with alan):"]
    for a in accounts():
        marks = []
        if a.name == current:
            marks.append("this chat")
        if a.name == pin:
            marks.append("pinned")
        tag = f"  ← {', '.join(marks)}" if marks else ""
        label = f" ({a.label})" if a.label != a.title else ""
        slug = f" · /account {a.name}" if a.title != a.name else ""
        lines.append(f"• {a.title}{label}{slug} — {_state_line(a)}{tag}")
    lines.append("")
    lines.append(f"Routing: {'pinned to ' + pin if pin else 'auto'} "
                 "(soonest weekly reset first; switches on a usage limit)")
    lines.append("Usage: /account auto | " + " | ".join(names()))
    return "\n".join(lines)
