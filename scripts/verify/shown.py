"""The one display rule for every diagnostic row the verify stages print.

A row repeats a name it read from outside (a file name, an object name, a Secret key, a
label value, a hostname) only when the name FITS: it is a string, within the shape's
length limit, matches the shape's grammar in full, and is not of a credential's shape.
Anything else is described, never repeated. Twenty-six review rounds found the same
defect at one site after another (a token passes a name grammar; a value reaches a row
before its shape is checked); this module is the single implementation those sites share.

Stage scripts import it from their python blocks (the stage exports PYTHONPATH with this
directory); am-routes.py, am-inline-credentials.py and no-instance-strings.py sit beside
it; instance-init.py adds this directory to sys.path. It vendors with the stages
(core-manifest.txt).
"""
import re

_RUN = re.compile(r"[A-Za-z0-9]+")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")


def tokenish(s):
    """An identifier of a credential's shape: a run of 16+ letters and digits holding both
    (ghp_…, a hex or base64 body). Such a value is never repeated, whatever grammar it
    passes."""
    return any(len(r) >= 16 and _LETTER.search(r) and _DIGIT.search(r)
               for r in _RUN.findall(s if isinstance(s, str) else ""))


# shape -> (grammar the whole value must match, length limit)
SHAPES = {
    # a Kubernetes object name (DNS subdomain)
    "object-name": (re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?"), 253),
    # a namespace: a DNS label, no dots
    "namespace": (re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"), 63),
    # one path component
    "file-name": (re.compile(r"[A-Za-z0-9._-]{1,255}"), 255),
    # a Secret / ConfigMap data key, a config key
    "key": (re.compile(r"[A-Za-z0-9._-]{1,253}"), 253),
    # a label value: an alertname, a severity, an environment key
    "label": (re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,99}"), 100),
    # a Prometheus rule / alert name
    "alert-name": (re.compile(r"[A-Za-z_:][A-Za-z0-9_:.-]{0,99}"), 100),
    # a DNS hostname, label by label
    "hostname": (re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"), 253),
    # one hostname label
    "host-label": (re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"), 63),
    # a receiver, time-interval or other config-level name
    "name": (re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"), 100),
    # a GitHub owner, or a ghcr.io/<owner> registry
    "owner": (re.compile(r"(?:ghcr\.io/)?[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"), 47),
    # a bare word (a kind, a type key)
    "word": (re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}"), 64),
    # an address range as an operator wrote it: the characters a CIDR of either family
    # can carry and nothing else (a value that parsed as one is safe to repeat)
    "cidr": (re.compile(r"[0-9A-Fa-f:.]{1,45}/[0-9]{1,3}"), 49),
    # what someone writes where a cron schedule goes: the five fields, an @-macro, or a
    # phrase like "every tuesday". Words, digits, spaces and cron's own punctuation; a
    # refusal that hides the value hides the mistake (QA of v1.0.0, F25). A credential
    # pasted there is still not repeated: `fits` refuses a credential's shape first.
    "schedule": (re.compile(r"[A-Za-z0-9@*/,?#-]+( [A-Za-z0-9@*/,?#-]+){0,9}"), 100),
}


def fits(value, shape):
    """True when value may be repeated as a <shape>: a string within the limit, matching
    the grammar in full, and not of a credential's shape."""
    grammar, limit = SHAPES[shape]
    return (isinstance(value, str) and len(value) <= limit
            and grammar.fullmatch(value) is not None and not tokenish(value))


def described(shape, what="name"):
    """What a row prints instead of a value that does not fit."""
    return "(%s %s not of %s %s shape, or of a credential's shape)" % (
        "an" if what[:1].lower() in "aeiou" else "a", what,
        "an" if shape[:1].lower() in "aeiou" else "a", shape)


def shown(value, shape, what="name"):
    """The value itself when it fits the shape, else its description."""
    return value if fits(value, shape) else described(shape, what)


def shown_parts(value, shape="file-name", what="name", sep="/"):
    """A path (or a dotted host / field) component by component: each component is
    repeated when it fits, described when it does not. A leading empty component (an
    absolute path's root) is kept as is."""
    parts = str(value).split(sep)
    return sep.join(c if (i == 0 and c == "" and sep == "/") or fits(c, shape) else described(shape, what)
                    for i, c in enumerate(parts))


def shown_list(values, shape, what="name"):
    """(the values that fit, in order; a count note for the rest, or "")."""
    values = list(values)
    good = [v for v in values if fits(v, shape)]
    odd = len(values) - len(good)
    return good, ("(+%d %s(s) not of %s %s shape, or of a credential's shape)"
                  % (odd, what, "an" if shape[:1].lower() in "aeiou" else "a", shape) if odd else "")
