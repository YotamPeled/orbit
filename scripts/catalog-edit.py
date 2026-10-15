#!/usr/bin/env python3
"""Replace or remove ONE project's entry in catalog/projects.yaml, leaving every other
line of the file as it was.

    catalog-edit.py set <project>      < the new entry, as JSON or YAML, on stdin
    catalog-edit.py remove <project>

scripts/catalog-commit.sh runs this in a fresh clone (the orbit_catalog_set and
orbit_project_remove tools); it never commits or pushes anything itself.

The catalog is a hand-written file: comments, ordering and the other projects' own
formatting are theirs to keep. So the edit is a splice of the one entry's lines, located
by the YAML parser's own marks, and it is proven before it is written: the file is parsed
again and must equal the old document with exactly that one entry replaced (or gone).
Anything else is refused and the file is not touched.

`set` only replaces an entry that exists. A new project needs more than a catalog entry
(its namespaces, its pull secret), which is onboarding's job. The entry's `id` cannot
change. Comments inside the replaced entry are not kept: the new entry is written fresh.
`remove` also drops the project's lines from catalog/tags.yaml and, for a project with a
database, writes down in catalog/retired.yaml the archive generation it leaves in the
bucket, so that a project made under the name again archives under the next
(catalog-sync.py retired_archives).
"""
import json
import re
import sys

import yaml

CATALOG = "catalog/projects.yaml"
TAGS = "catalog/tags.yaml"
RETIRED = "catalog/retired.yaml"
RETIRED_HEAD = """\
# Written by `catalog-edit.py remove` (orbit_project_remove). Not edited by hand.
# A removed project's database backups and WAL stay in the bucket. Each line is the
# highest archive generation a removed project of that name wrote under; a project made
# under the name again archives under the next one, with nothing to remember.
"""
ID = re.compile(r"[a-z][a-z0-9-]{0,62}")


class Refused(Exception):
    """One plain sentence; nothing was written."""


def _entry_lines(text: str, project: str) -> tuple[int, int, int]:
    """(first line, last line, column of the dash) of the project's entry, 0-based and
    inclusive: from its `- ` to its last line that is neither blank nor a comment."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as e:
        raise Refused(f"{CATALOG} is not YAML ({e.__class__.__name__})") from e
    if not isinstance(root, yaml.MappingNode):
        raise Refused(f"{CATALOG} is not a mapping")
    projects = next((value for key, value in root.value if getattr(key, "value", None) == "projects"), None)
    if not isinstance(projects, yaml.SequenceNode) or projects.flow_style:
        raise Refused(f"{CATALOG} has no block list under `projects:`")
    found = []
    for item in projects.value:
        if not isinstance(item, yaml.MappingNode):
            continue
        ident = next((value.value for key, value in item.value
                      if getattr(key, "value", None) == "id" and isinstance(value, yaml.ScalarNode)), None)
        if ident == project:
            found.append(item)
    if not found:
        raise Refused(f"project {project} is not in the catalog")
    if len(found) > 1:
        raise Refused(f"the catalog has more than one project {project}")
    item = found[0]
    if item.flow_style:
        raise Refused(f"project {project}'s entry is written on one line ({{...}}); rewrite it as a block first")
    lines = text.split("\n")
    first = item.start_mark.line
    head = lines[first]
    dash = head.find("- ")
    if dash < 0 or head[:dash].strip() or dash + 2 != item.start_mark.column:
        raise Refused(f"project {project}'s entry does not start on its own `- ` line")
    last = item.end_mark.line if item.end_mark.column > 0 else item.end_mark.line - 1
    last = min(last, len(lines) - 1)
    while last > first and (not lines[last].strip() or lines[last].lstrip().startswith("#")
                            or len(lines[last]) - len(lines[last].lstrip()) <= dash):
        last -= 1
    return first, last, dash


class _Indented(yaml.SafeDumper):
    """Lists indented under their key, the way the catalog is written by hand."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _block(entry: dict, dash: int) -> list[str]:
    ordered = {"id": entry["id"], **{k: v for k, v in entry.items() if k != "id"}}
    dumped = yaml.dump(ordered, Dumper=_Indented, sort_keys=False, default_flow_style=False,
                       allow_unicode=True, width=1000)
    body = dumped.rstrip("\n").split("\n")
    pad = " " * dash
    return [f"{pad}- {body[0]}"] + [f"{pad}  {line}" if line else "" for line in body[1:]]


def _projects(text: str) -> tuple[dict, list]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise Refused(f"{CATALOG} is not YAML ({e.__class__.__name__})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
        raise Refused(f"{CATALOG} has no `projects` list")
    return doc, doc["projects"]


def edit(text: str, project: str, entry: dict | None) -> str:
    """The catalog text with the project's entry replaced by `entry`, or removed (None)."""
    if not ID.fullmatch(project or ""):
        raise Refused("the project is not a project id")
    if entry is not None:
        if not isinstance(entry, dict):
            raise Refused("the new entry is not a mapping")
        if entry.get("id") != project:
            raise Refused(f"the new entry's id is not {project}: an entry's id cannot change")
        try:
            json.dumps(entry)
        except (TypeError, ValueError) as e:
            raise Refused("the new entry holds a value that is not plain data") from e
    before, was = _projects(text)
    first, last, dash = _entry_lines(text, project)
    if entry is not None and next((p for p in was if isinstance(p, dict) and p.get("id") == project), None) == entry:
        return text          # the same entry: not one byte moves (its comments stay too)
    lines = text.split("\n")
    new = lines[:first] + (_block(entry, dash) if entry is not None else []) + lines[last + 1:]
    if entry is None and len(was) == 1:
        # the only project: a bare `projects:` would read as null, so the key says "none"
        key = max((i for i in range(first) if lines[i].rstrip() == "projects:"), default=None)
        if key is None:
            raise Refused(f"{project} is the catalog's only project and its `projects:` line "
                          f"is not written plainly; nothing was written")
        new[key] = "projects: []"
    out = "\n".join(new)
    # the proof: everything but that one entry parses to exactly what it was
    try:
        after, now = _projects(out)
    except Refused as e:
        raise Refused(f"the edited catalog would not read as a catalog ({e}); nothing was written") from e
    want = [entry if p.get("id") == project else p for p in was if isinstance(p, dict)]
    want = [p for p in want if p is not None]
    rest_before = {k: v for k, v in before.items() if k != "projects"}
    rest_after = {k: v for k, v in after.items() if k != "projects"}
    if now != want or rest_before != rest_after:
        raise Refused("the edit would change more than that one entry; nothing was written")
    return out


def drop_tags(text: str, project: str) -> str:
    """catalog/tags.yaml without the removed project's `<project>/<env>/<component>` lines."""
    doc = yaml.safe_load(text) or {}
    tags = doc.get("tags") if isinstance(doc, dict) else None
    if not isinstance(tags, dict) or not any(str(k).startswith(project + "/") for k in tags):
        return text
    key = re.compile(r"""^\s+["']?%s/[^:"']+["']?\s*:""" % re.escape(project))
    out = "\n".join(line for line in text.split("\n") if not key.match(line))
    after = yaml.safe_load(out) or {}
    want = {k: v for k, v in tags.items() if not str(k).startswith(project + "/")}
    if (after.get("tags") or {}) != want or {k: v for k, v in after.items() if k != "tags"} != \
            {k: v for k, v in doc.items() if k != "tags"}:
        raise Refused(f"{TAGS} could not be edited line by line; nothing was written")
    return out


def retire(text: str | None, project: str, entry: dict) -> str | None:
    """catalog/retired.yaml with the removed project's archive generation written down, or
    None when the project had no database (it leaves nothing in the bucket)."""
    db = entry.get("database") if isinstance(entry, dict) else None
    if db is None:
        return None
    doc = yaml.safe_load(text) if text else {}
    retired = doc.get("retired") if isinstance(doc, dict) else None
    if text and not isinstance(retired, dict):
        raise Refused(f"{RETIRED} is not `retired:` with a map of project id to generation; nothing was written")
    retired = dict(retired or {})
    before = retired.get(project, 0)
    named = db.get("archiveGeneration") if isinstance(db, dict) else None
    if not isinstance(before, int) or (named is not None and not isinstance(named, int)):
        raise Refused(f"{RETIRED} or the entry's archiveGeneration is not a whole number; nothing was written")
    # what catalog-sync.py rendered for it: the generation it named, else the one after
    # the last removed project of the name, else the first
    retired[project] = max(before, named if named is not None else before + 1)
    return RETIRED_HEAD + yaml.safe_dump({"retired": dict(sorted(retired.items()))}, default_flow_style=False)


def changed(old: dict, new: dict, at: str = "") -> list[str]:
    """The paths at which two entries differ, for a confirmation a person can read."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in list(old) + [k for k in new if k not in old]:
            where = f"{at}.{key}" if at else str(key)
            if key not in new:
                out.append(f"{where} (removed)")
            elif key not in old:
                out.append(f"{where} (added)")
            else:
                out += changed(old[key], new[key], where)
        return out
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        out = []
        for i, (a, b) in enumerate(zip(old, new)):
            name = a.get("name") if isinstance(a, dict) and isinstance(b, dict) and a.get("name") == b.get("name") else None
            out += changed(a, b, f"{at}[{name if name else i}]")
        return out
    return [] if old == new else [at or "(the entry)"]


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("set", "remove"):
        print("usage: catalog-edit.py set|remove <project>", file=sys.stderr)
        return 2
    verb, project = argv[1], argv[2]
    try:
        text = open(CATALOG, encoding="utf-8").read()
        entry = None
        if verb == "set":
            try:
                entry = yaml.safe_load(sys.stdin.read())
            except yaml.YAMLError as e:
                raise Refused("the new entry on stdin is not JSON or YAML") from e
        out = edit(text, project, entry)
        tags_out = retired_out = None
        if verb == "remove":
            was = next((p for p in (yaml.safe_load(text) or {}).get("projects") or []
                        if isinstance(p, dict) and p.get("id") == project), {})
            try:
                retired_text = open(RETIRED, encoding="utf-8").read()
            except FileNotFoundError:
                retired_text = None
            retired_out = retire(retired_text, project, was)
            try:
                tags_text = open(TAGS, encoding="utf-8").read()
            except FileNotFoundError:
                tags_text = None
            if tags_text is not None:
                tags_out = drop_tags(tags_text, project)
    except Refused as e:
        print(str(e))
        return 1
    except OSError as e:
        print(f"{CATALOG} cannot be read ({e.__class__.__name__})")
        return 1
    with open(CATALOG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    if tags_out is not None:
        with open(TAGS, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(tags_out)
    if retired_out is not None:
        with open(RETIRED, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(retired_out)
    print(f"{verb} {project}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
