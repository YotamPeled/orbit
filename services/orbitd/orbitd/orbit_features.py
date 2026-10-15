"""The features registry and the one reading of an instance's `features:` block.

An Orbit platform is `core` plus optional features (logs, traces, ...). The registry
(provisioning/features.yaml, vendored with the staged tree it describes) says what each
feature owns: the files it renders, the Applications it applies, the MCP tools it
exposes, and what it needs before it can be turned on. instance/config.yaml says which
are on. Everything that asks "is logs on here" asks this module: the scaffolder, the
generator, the verify stages and orbitd, so they cannot answer differently.

Reading `features:` (states()):
  * no `features:` key at all: every feature whose requirements are met is on. That is
    what an instance rendered before features existed, so an upgrade changes nothing.
  * a `features:` mapping: exactly the features set to `true` are on; one left out is
    off. Turning on a feature whose requirement is missing is refused by name, never
    rendered half.
"""
import pathlib
import re

import yaml

REGISTRY_REL = pathlib.PurePosixPath("provisioning/features.yaml")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# a block a feature owns inside a file another feature owns:
#   # orbit:feature logs
#   ...lines rendered only while logs is on...
#   # orbit:end logs
BLOCK_RE = re.compile(r"^[ \t]*# orbit:(feature|end) ([a-z][a-z0-9-]*)[ \t]*\r?$")
SPEC_KEYS = {"always", "summary", "requires", "secrets", "apps", "consumers", "paths", "tools", "memory_mib",
             "generated"}


class FeatureError(Exception):
    """A registry or a `features:` block that cannot mean anything; str() is one sentence."""


class RegistryMissing(FeatureError):
    """The tree carries no registry at all (it was vendored before features existed)."""


def load_registry(root) -> dict:
    """The registry of the tree under `root`, validated: {name: spec}, declaration order."""
    path = pathlib.Path(root) / REGISTRY_REL
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RegistryMissing(f"{REGISTRY_REL} is missing: this tree is not a faithful copy of "
                           f"the core's template (run `make core-sync`)") from None
    features = (doc or {}).get("features")
    if not isinstance(features, dict) or not features:
        raise FeatureError(f"{REGISTRY_REL} declares no features")
    for name, spec in features.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise FeatureError(f"{REGISTRY_REL}: {name!r} is not a feature name")
        if not isinstance(spec, dict) or set(spec) - SPEC_KEYS:
            raise FeatureError(f"{REGISTRY_REL}: feature {name} carries keys the registry does "
                               f"not know: {sorted(set(spec or {}) - SPEC_KEYS)}")
        for key in ("requires", "secrets", "apps", "consumers", "paths", "tools", "generated"):
            if not isinstance(spec.setdefault(key, []), list):
                raise FeatureError(f"{REGISTRY_REL}: feature {name}: {key} must be a list")
        spec["always"] = bool(spec.get("always", False))
        if spec["always"] and spec["requires"]:
            raise FeatureError(f"{REGISTRY_REL}: feature {name} is always on and cannot require anything")
    for name, spec in features.items():
        for req in spec["requires"]:
            if req != "bucket" and req not in features:
                raise FeatureError(f"{REGISTRY_REL}: feature {name} requires {req!r}, which is "
                                   f"neither `bucket` nor a feature")
    return features


def has_bucket(config: dict) -> bool:
    return bool((((config or {}).get("objectStorage") or {}).get("buckets") or {}).get("platform"))


def missing_requirement(name: str, registry: dict, config: dict, on: dict):
    """The first requirement of `name` that is not met, as a sentence; None when all are."""
    for req in registry[name]["requires"]:
        if req == "bucket":
            if not has_bucket(config):
                return ("it needs a bucket: declare objectStorage.buckets.platform and "
                        "objectStorage.endpoint in instance/config.yaml first")
        elif not on.get(req, False):
            return f"it needs the {req} feature on"
    return None


def is_explicit(config: dict) -> bool:
    """True when the instance declares `features:` (anything left out of it is off)."""
    return "features" in (config or {})


def states(config: dict, registry: dict) -> dict:
    """{feature: on?} for an instance config, in registry order. Raises FeatureError."""
    config = config or {}
    on = {name: True for name, spec in registry.items() if spec["always"]}
    optional = [name for name, spec in registry.items() if not spec["always"]]
    if not is_explicit(config):
        # the pre-features reading: on wherever the requirements are met. Resolved to a
        # fixpoint so a feature requiring another follows it either way round.
        changed = True
        while changed:
            changed = False
            for name in optional:
                if name not in on and missing_requirement(name, registry, config, on) is None:
                    on[name] = changed = True
        return {name: on.get(name, False) for name in registry}
    declared = config["features"]
    if declared is None:
        declared = {}
    if not isinstance(declared, dict):
        raise FeatureError("features: must be a mapping of feature name to true or false")
    for name, value in declared.items():
        if name not in registry:
            raise FeatureError(f"features.{name} is not a feature this core has; the features "
                               f"are: {', '.join(optional)}")
        if registry[name]["always"]:
            raise FeatureError(f"features.{name} cannot be set: {name} is always on")
        if not isinstance(value, bool):
            raise FeatureError(f"features.{name} must be true or false, not {value!r}")
    for name in optional:
        on[name] = declared.get(name, False)
    for name in optional:
        if on[name]:
            why = missing_requirement(name, registry, config, on)
            if why:
                raise FeatureError(f"features.{name} is on but {why}, or turn {name} off")
    return {name: on.get(name, False) for name in registry}


def generated_with(app: str, registry: dict) -> frozenset:
    """`namespace/name` of the Secrets the software of `app` makes for itself and owns (a
    feature's `generated`): they go when the app is retired, and nothing else holds them."""
    return frozenset(str(ref) for spec in registry.values() if app in spec["apps"]
                     for ref in spec.get("generated") or [])


def owners(rel: str, registry: dict) -> list:
    """Every feature whose `paths` cover a path relative to clusters/ (a directory prefix
    covers what is under it)."""
    rel = str(rel).replace("\\", "/")
    return [name for name, spec in registry.items()
            if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in spec["paths"])]


def apply_blocks(text: str, on: dict, where: str = "a staged file") -> str:
    """`text` without its marker lines, and without the lines of every block whose feature
    is off. Blocks nest; a block is kept only when every feature around it is on."""
    out, stack = [], []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        m = BLOCK_RE.match(line.rstrip("\n"))
        if not m:
            if all(on[name] for name in stack):
                out.append(line)
            continue
        kind, name = m.groups()
        if name not in on:
            raise FeatureError(f"{where}:{number}: `orbit:{kind} {name}` names no feature")
        if kind == "feature":
            stack.append(name)
        elif not stack or stack[-1] != name:
            raise FeatureError(f"{where}:{number}: `orbit:end {name}` closes a block that is not open")
        else:
            stack.pop()
    if stack:
        raise FeatureError(f"{where}: `orbit:feature {stack[-1]}` is never closed")
    return "".join(out)


def block_features(text: str) -> set:
    """Every feature a file carries a block for."""
    return {m.group(2) for m in map(BLOCK_RE.match, text.splitlines()) if m}


def tool_owner(tool: str, registry: dict):
    """The optional feature that owns an MCP tool, or None (a core tool)."""
    for name, spec in registry.items():
        if tool in spec["tools"] and not spec["always"]:
            return name
    return None


# ---------------------------------------------------------------------------------------
# Switching one feature in the TEXT of instance/config.yaml. The file is hand-written and
# commented, so only the top-level `features:` block is rewritten (or added at the end when
# the instance never had one) and every other byte stays. The first switch on an instance
# without a block writes every optional feature's CURRENT state into the new block, so
# naming them changes nothing but the one that was asked for. The result is parsed again
# before it is returned: it must read as the same config with exactly the wanted features,
# or the edit is refused.
_BLOCK_START = re.compile(r"^features:[ \t]*(.*?)[ \t]*\r?$")
HEADER = "# Which optional features run (provisioning/features.yaml describes each one).\n"


def _block_span(lines: list):
    """[start, end) of the top-level `features:` block in `lines`, or None."""
    for i, line in enumerate(lines):
        if _BLOCK_START.match(line):
            j = i + 1
            while j < len(lines) and lines[j][:1] in (" ", "\t") and lines[j].strip():
                j += 1
            return i, j
    return None


def _render(want: dict) -> list:
    if not want:
        return ["features: {}\n"]
    return ["features:\n"] + [f"  {name}: {'true' if on else 'false'}\n" for name, on in want.items()]


def set_feature(config_text: str, registry: dict, feature: str, enabled: bool) -> tuple:
    """(new config text, the optional features' states it declares). Raises
    FeatureError with one sentence when the switch cannot be made."""
    try:
        config = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as e:
        raise FeatureError(f"instance/config.yaml is not YAML ({e.__class__.__name__})") from e
    if not isinstance(config, dict):
        raise FeatureError("instance/config.yaml is not a mapping")
    optional = [name for name, spec in registry.items() if not spec["always"]]
    if feature not in registry:
        raise FeatureError(f"{feature} is not a feature this core has; the features are: {', '.join(optional)}")
    if registry[feature]["always"]:
        raise FeatureError(f"{feature} is always on and cannot be switched")
    current = states(config, registry)
    want = {name: current[name] for name in optional}
    want[feature] = bool(enabled)
    if not enabled:
        for name in optional:
            if want[name] and feature in registry[name]["requires"]:
                raise FeatureError(f"{feature} cannot be turned off while {name} is on: {name} needs it")
    return _rewrite(config_text, config, registry, want), want


def declare(config_text: str, registry: dict, desired: dict) -> tuple:
    """(new config text, the optional features' states) with the `features:` block set
    to exactly `desired`: a feature it leaves out is off. Onboarding writes an instance's
    first block with this. Raises FeatureError with one sentence when the set cannot be:
    an unknown name, a value that is not true or false, a feature on without what it needs."""
    try:
        config = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as e:
        raise FeatureError(f"instance/config.yaml is not YAML ({e.__class__.__name__})") from e
    if not isinstance(config, dict):
        raise FeatureError("instance/config.yaml is not a mapping")
    optional = [name for name, spec in registry.items() if not spec["always"]]
    for name, value in (desired or {}).items():
        if name not in registry or registry[name]["always"]:
            raise FeatureError(f"{name} is not a feature that can be switched; the features are: {', '.join(optional)}")
        if not isinstance(value, bool):
            raise FeatureError(f"features.{name} must be true or false")
    want = {name: bool((desired or {}).get(name, False)) for name in optional}
    return _rewrite(config_text, config, registry, want), want


def _rewrite(config_text: str, config: dict, registry: dict, want: dict) -> str:
    """`config_text` with its features block replaced by `want`, proven to change nothing else."""
    after = {key: value for key, value in config.items() if key != "features"}
    after["features"] = dict(want)
    states(after, registry)          # a feature turned on without what it needs is refused here

    lines = config_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    span = _block_span(lines)
    if span is None:
        lines += (["\n"] if lines and lines[-1].strip() else []) + [HEADER] + _render(want)
    else:
        lines[span[0]:span[1]] = _render(want)
    new_text = "".join(lines)
    try:
        reread = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise FeatureError("the edited instance/config.yaml would not parse; nothing was written") from e
    if reread != after:
        raise FeatureError("the edit would change more than the features block; nothing was written "
                                 "(is `features:` written in a form this command cannot rewrite?)")
    return new_text
