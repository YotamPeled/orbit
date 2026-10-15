"""One place that answers what this repository ships as a document, for every contract that
judges documents.

WHY THIS FILE EXISTS. Two contracts were each reading their own list of documents, and both
lists were the same walk: the skills under one tree, plus one document under `docs/`. A
second tracked skill tree holds six more documents beside the first, and `docs/` holds
eighteen. Each contract therefore judged a fraction of what a reader is handed while its
docstring claimed it judged all of it, and a reviewer found the same blindness twice, in two
files, within a day. The front's rule is that a class found twice means the design is wrong.
So the walk is not patched in two places: the inventory moves here, is derived from what git
tracks rather than from a path a file was told about, and every contract that judges
documents takes its list from this module and says which kinds it judges and why.

WHAT A KIND MEANS.

  - A **skill** is a document a working session is handed as instructions. Every tracked
    `SKILL.md`, in any tree, at any depth. Nothing about the tree's name is assumed: a third
    tree is covered the day someone adds it.
  - A **document that names a tool** is a document under `docs/` that puts at least one of
    this surface's tool names in front of a reader. That is derived from the surface's own
    listing, not from a list here, so a document starts being judged the moment it starts
    talking about the surface, and stops when it stops.
  - **Every tracked markdown** is the widest kind, for the contract that asks whether a rule
    is restated anywhere -- a restatement in a design note is still a copy that can go stale,
    even though a design note promises a reader nothing.

WHAT THIS IS NOT. It is not the surface's own prose. That is driven, not read off disk, and
it lives beside these as `surface_documents`: a tool description is read by a caller before
any call, so the front's standing ruling puts it on the judged side of the line.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess


def repo_root() -> pathlib.Path:
    import orbitd

    return pathlib.Path(orbitd.__file__).parents[3]


def _tracked(pattern: str) -> list:
    """What git tracks, matching `pattern`. Tracked is the honest definition of shipped: a
    file nobody committed is not in front of any reader, and a file somebody committed is,
    whichever directory it went into. Falls back to walking the tree when git cannot answer
    -- a source archive with no repository still has to be judgeable -- and the guard below
    fails loudly if the fallback finds nothing either."""
    root = repo_root()
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", pattern],
                             capture_output=True, text=True, timeout=30, check=True)
        paths = [root / p for p in out.stdout.split("\0") if p]
        if paths:
            return sorted(paths)
    except Exception:
        pass
    return sorted(p for p in root.rglob(pattern.replace("**/", ""))
                  if ".venv" not in p.parts and ".git" not in p.parts
                  and "node_modules" not in p.parts)


def skill_document_paths() -> list:
    """Where the shipped skills are, for the rare case that needs the file rather than its
    text -- copying a tree to plant a mutation in it, say."""
    return _tracked("**/SKILL.md")


def skill_documents() -> list:
    """Every shipped skill, whole, labelled by the tree it sits in so a reader of a failure
    knows which of two same-named documents it is. No paragraph is skipped and no claim is
    lifted out of the prose: a contract searches these literally, so there is nothing for a
    spelling to walk past."""
    paths = _tracked("**/SKILL.md")
    root = repo_root()
    documents = [(f"skill {path.parent.relative_to(root)}", path.read_text())
                 for path in paths]
    assert len(documents) > 4, (
        f"only {len(documents)} shipped skills found; the inventory is broken, and every "
        "contract that reads it is now judging almost nothing")
    trees = {label.split("/")[0] for label, _ in documents}
    assert len(trees) >= 2, (
        f"every shipped skill was found under one tree ({trees}); this inventory exists "
        "because a walk that saw one tree of two claimed to see them all")
    return documents


def names_a_tool(tool_names):
    """A pattern that matches a name of one of this surface's tools, or anything shaped like
    one under the prefix those names share. A document naming a tool this surface USED to
    offer is talking about the surface too, and it is the document most likely to be telling
    a reader to call something that is no longer there."""
    names = sorted({str(n) for n in tool_names if str(n).strip()})
    assert names, "the surface listed no tools, so no document can be selected by naming one"
    shared = os.path.commonprefix(names)
    shapes = [re.escape(n) for n in names]
    if len(shared) >= 4:
        shapes.append(re.escape(shared) + r"[a-z][a-z_]{2,}")
    return re.compile(r"\b(?:%s)\b" % "|".join(shapes))


def documents_that_name_a_tool(tool_names) -> list:
    """Every markdown document under `docs/` that puts at least one of this surface's tool
    names in front of a reader, labelled by path.

    Derived from the surface's own listing rather than from a list here, so which documents
    are judged follows what the surface offers. A document that never names a tool is a
    design note or a plan: it promises a caller nothing, and a contract about what a caller
    is told has no business reading it."""
    pattern = names_a_tool(tool_names)
    root = repo_root()
    documents = []
    for path in _tracked("docs/**/*.md") + _tracked("docs/*.md"):
        text = path.read_text(errors="replace")
        if pattern.search(text):
            documents.append((f"doc {path.relative_to(root)}", text))
    documents = sorted(set(documents))
    assert documents, (
        "no document under docs/ names a single tool this surface offers, which would mean "
        "either the listing or the walk is broken")
    return documents


def every_tracked_markdown() -> list:
    """Every markdown document this repository ships, labelled by path. The widest kind, for
    a contract asking whether something is restated anywhere at all."""
    root = repo_root()
    documents = [(f"markdown {path.relative_to(root)}", path.read_text(errors="replace"))
                 for path in _tracked("**/*.md")]
    assert len(documents) > 20, (
        f"only {len(documents)} markdown documents found; the inventory is broken")
    return documents


# ---------------------------------------------------------------- a scope, not a walk

#: The documents whose subject is THIS surface's protocol: what a caller sends, what comes
#: back, and what a refusal looks like. A person decides this, one line per document, and
#: the guards below hold the decision honest.
#:
#: Why a decision and not a derivation. Two questions were riding on one list of documents.
#: "Does this document hand a reader a word the surface never produces?" is about a literal
#: string and is true of any document anywhere -- it runs over the whole inventory. "Does
#: this document name a status the surface never answers with?" is about a three-digit
#: number, and a three-digit number only means a status when the document is talking about
#: a call to this surface. A document about a tenant's own application is full of them: a
#: base image's user id, a rolling update that answers 502, an address like 203.0.113.10,
#: a pull request numbered 103. Measured over the full inventory, that half produced
#: thirty-one lines and every one of them was wrong. Proximity to a tool name was tried as
#: a derivation and dropped nineteen of the twenty-three real lines it is catching today,
#: because a document about the protocol states a status in prose without naming a tool
#: beside it. So the subject of a document is a decision, it is recorded here once, and a
#: reviewer can read the whole of it in four lines.
PROTOCOL_SUBJECT = (
    "docs/ORBITD.md",
    "skills/orbit-diagnose/SKILL.md",
    "skills/orbit-operator/SKILL.md",
    "skills/orbit-rollback/SKILL.md",
    "skills/orbit-ship/SKILL.md",
    "skills/orbit-admin/SKILL.md",
    "skills/orbit-deploy-review/SKILL.md",
    "skills/orbit-budget-review/SKILL.md",
    "skills/orbit-investigate-alert/SKILL.md",
)


def documents_whose_subject_is_the_protocol(tool_names) -> list:
    """The scope above, read, with every guard the decision needs.

    A named document must exist, must be tracked, and must name a tool of this surface --
    a document that stops talking about the surface has stopped being in scope and the
    line naming it is stale. The scope must not be empty, and it must not have quietly
    become the whole inventory either: if every document in the repository ends up here,
    the distinction this scope exists to make has been lost."""
    root = repo_root()
    named = {str(path.relative_to(root)) for path in _tracked("**/*.md")}
    pattern = names_a_tool(tool_names)
    documents = []
    missing, silent = [], []
    for relative in PROTOCOL_SUBJECT:
        if relative not in named:
            missing.append(relative)
            continue
        text = (root / relative).read_text()
        if not pattern.search(text):
            silent.append(relative)
            continue
        documents.append((f"protocol {relative}", text))
    assert not silent, (
        f"a document named as describing this surface's protocol never names a tool of it, "
        f"so either it has stopped being about this surface or the line naming it is "
        f"stale: {silent}")
    assert not missing, (
        f"a document named as describing this surface's protocol is not shipped: {missing}")
    assert documents, "the protocol scope is empty, so the status half asks nothing"
    assert len(documents) < len(_tracked("**/*.md")), (
        "every document in the repository is in the protocol scope, so the scope no longer "
        "distinguishes anything")
    return documents


# ------------------------------------------------- the surface's own prose

PROSE_KEYS = frozenset({"description", "title", "instructions"})


def _schema_prose(schema, into: list) -> None:
    """Every description and title sentence anywhere in one JSON Schema, in order."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in PROSE_KEYS and isinstance(value, str):
                if value.strip():
                    into.append(value)
            else:
                _schema_prose(value, into)
    elif isinstance(schema, (list, tuple)):
        for value in schema:
            _schema_prose(value, into)


def tool_prose(tool: dict) -> str:
    """What a caller reads on one listing entry: its description, its titles, and every
    description and title sentence in its input and output schemas. The output schema
    counts too: a model docstring rides the listing the same way a field description
    does, and a code planted in either would vouch for a skill naming it."""
    prose = []
    for key in ("description", "title"):
        value = tool.get(key)
        if isinstance(value, str) and value.strip():
            prose.append(value)
    annotations = tool.get("annotations")
    if isinstance(annotations, dict):
        title = annotations.get("title")
        if isinstance(title, str) and title.strip():
            prose.append(title)
    for key in ("inputSchema", "input_schema", "outputSchema", "output_schema"):
        if key in tool:
            _schema_prose(tool[key], prose)
    return "\n".join(prose)


def surface_documents(listed: dict, discovered: dict, resources: dict,
                           templates: dict) -> list:
    """The surface's own prose as searched documents, one per tool, resource and
    template plus the instructions string, each labelled so a reader can act on an
    offence. Driven at the role that hides nothing, counted per route rather than in
    total, so losing a whole route goes red instead of shrinking a number that is
    still in the hundreds."""
    documents = []
    body = listed.get("result", listed) if isinstance(listed, dict) else {}
    tools = body.get("tools", []) if isinstance(body, dict) else []
    assert isinstance(tools, list) and tools, (
        "tools/list answered with no tools, so its prose is not being measured")
    for tool in tools:
        name = tool.get("name", "?") if isinstance(tool, dict) else "?"
        prose = tool_prose(tool) if isinstance(tool, dict) else ""
        assert prose.strip(), (
            f"tool {name} contributed no prose, so its document would judge nothing")
        documents.append((f"tool {name}", prose))
    body = discovered.get("result", discovered) if isinstance(discovered, dict) else {}
    instructions = body.get("instructions", "") if isinstance(body, dict) else ""
    assert isinstance(instructions, str) and len(instructions) > 50, (
        "server/discover answered with no instructions string, so it is not being measured")
    documents.append(("server instructions", instructions))
    body = resources.get("result", resources) if isinstance(resources, dict) else {}
    entries = body.get("resources", []) if isinstance(body, dict) else []
    assert isinstance(entries, list) and entries, (
        "resources/list answered with no resources, so their prose is not being measured")
    for entry in entries:
        uri = entry.get("uri", "?") if isinstance(entry, dict) else "?"
        description = entry.get("description", "") if isinstance(entry, dict) else ""
        assert isinstance(description, str) and description.strip(), (
            f"resource {uri} carries no description, so its document would judge nothing")
        documents.append((f"resource {uri}", description))
    body = templates.get("result", templates) if isinstance(templates, dict) else {}
    entries = body.get("resourceTemplates", body.get("resource_templates", [])) \
        if isinstance(body, dict) else []
    assert isinstance(entries, list) and entries, (
        "resources/templates/list answered with no templates, "
        "so their prose is not being measured")
    for entry in entries:
        uri = (entry.get("uriTemplate", entry.get("uri_template", "?"))
               if isinstance(entry, dict) else "?")
        description = entry.get("description", "") if isinstance(entry, dict) else ""
        assert isinstance(description, str) and description.strip(), (
            f"resource template {uri} carries no description, "
            "so its document would judge nothing")
        documents.append((f"resource template {uri}", description))
    return documents
