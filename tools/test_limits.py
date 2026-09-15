"""The notice wordings the bridge must (and must not) read as a usage limit.

Run from anywhere:  .venv/bin/python tools/test_limits.py
Reads alan's classifier; touches no account state and makes no API calls."""
import pathlib, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from types import SimpleNamespace
import claude_bridge as cb, router

FABLE_CAP = ("You've reached your Fable limit. Switch to another model, or manage usage "
             "credits at claude.ai/settings/usage?from=cc_cli_limit_message, to keep going.")
FABLE_5 = "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
FIVE_H = "5-hour limit reached ∙ resets 3am"
SESSION = "You've hit your session limit · resets 9:10pm (UTC)"
PROSE = "I looked at why the chat didn't switch: the notice 'You've reached your Fable limit' was not matched."

# the notice that leaked through is now detected, and names its family
for t in (FABLE_CAP, FABLE_5, FIVE_H, SESSION):
    assert cb.parse_limit_reset(t) is not None, t
assert router.limit_family(FABLE_CAP) == "fable"
assert router.limit_family(FIVE_H) is None
print("detected:", ", ".join(repr(t[:28]) for t in (FABLE_CAP, FABLE_5, FIVE_H, SESSION)))

# a successful result never counts, whatever its text says
def result(text, is_error, subtype="success"):
    return SimpleNamespace(result=text, is_error=is_error, subtype=subtype)

assert cb.result_limit_reset(result(FABLE_CAP, False)) is None, "success result parked the chat"
assert cb.result_limit_reset(result(PROSE, False)) is None, "prose in a reply parked the chat"
# An *error* result quoting a notice stays ambiguous and is read as a limit,
# as in alan: the false positive costs a retry, the false negative leaks the
# notice to the user and stalls the chat — which is the bug this fixes.
assert cb.result_limit_reset(result(PROSE, True)) is not None
assert cb.result_limit_reset(result(FABLE_CAP, True)) > time.time()
assert cb.result_limit_reset(result(FIVE_H, False, "error_during_execution")) > time.time()
print("gating: only error results count; prose never does")
print("ALL OK")
