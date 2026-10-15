#!/usr/bin/env python3
"""No personal string, no secret and no second author in this repository.

  python3 scripts/personal_strings.py               the working tree (tracked and new files)
  python3 scripts/personal_strings.py --staged      what is staged for the next commit
  python3 scripts/personal_strings.py --push <old> <new>
                                                    every commit a push would publish: its
                                                    files, its message and its author
  python3 scripts/personal_strings.py --message <file>
                                                    a commit message (the commit-msg hook)
  python3 scripts/personal_strings.py --add <string>...
                                                    forbid more strings (stored hashed)
  python3 scripts/personal_strings.py --author <email>
                                                    set the one author this repository takes

Exit 0 when clean, 1 with every hit named (file, line, why: never the string itself).

What it refuses:
- The owner's personal strings: names, addresses, domains, hosts, buckets and ids of the
  instance Orbit was built and tested on. They are held below only as salted SHA-256 hashes,
  so this file does not spell out what it forbids. A word that contains one is refused too.
- Any personal mail address, a home-directory path, a public IPv4 address outside the
  documentation ranges and the few public resolvers and fixtures named below.
- Credentials by their shape: private keys with a body, cloud and Git-host tokens, age
  secret keys, bot and API tokens.
- A commit by anyone but the repository's author, and a `Co-authored-by:` trailer.

The hooks in .githooks run it before every commit and every push (scripts/local-tests.sh
sets `core.hooksPath`), and scripts/test_personal_strings.py runs it on the whole tree.
"""
import hashlib
import ipaddress
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SALT = "orbit/personal-strings/v1/7f3c9a51e2"

# length:sha256(SALT + string)[:20] of each forbidden string, lowercase.
# Kept between the markers by --add; do not edit by hand.
# BEGIN HASHED
HASHED = frozenset({
    "10:1db1abe9d19e5de676f8",
    "10:39af2c433363d9d1c6d4",
    "10:3db0939d4b4f9ad7cbb6",
    "10:5baff81815658d5a975d",
    "10:8883a3cc8e397d01c539",
    "17:399bac4050ee97173c64",
    "18:275321344b36e2e17107",
    "32:28bcce5f8c4aa1a90e7b",
    "32:40d856c4a50e5bb3d91d",
    "32:bee03a82d3ff2e78fd5f",
    "5:1cbdb59492c0005cc700",
    "5:4810ced10eef7998a01a",
    "62:d4f084001ef30646e17f",
    "6:3e22568064c7d94bf7cf",
    "6:857a25b4bad64954577d",
    "7:1ccf6a8e4c6b254c78c4",
    "7:8e12c14cfbbe4be5d3ff",
    "7:df238a746f06a0cebee7",
    "8:4b153a2d546a09b6d1e4",
    "8:675f0c022f137cb85077",
    "8:7e0c72853a2e74e07fd8",
    "9:445f37dfda7199b8867d",
    "9:dcc6a037c37a013bb1d0",
})
# END HASHED

# sha256(SALT + email)[:20] of the one author this repository takes. Set by --author.
# BEGIN AUTHOR
AUTHOR = "87ffdadba40d4aa56567"
# END AUTHOR

# The repository's own public home: the only places the owner's account name may appear.
# (The name is written a letter at a time so this line does not trip the list itself.)
_ACCOUNT = "y[o]t[a]m[p]e[l]e[d]"
ALLOWED_SPANS = re.compile(
    rf"ghcr\.io/{_ACCOUNT}/(?:orbitd|orbit-desk)(?![a-z0-9-])"
    rf"|github\.com[/:]{_ACCOUNT}/orbit(?![a-z0-9-])",
    re.I)

# Public addresses that are no one's: resolvers, the address example.com had, fixtures.
ALLOWED_PUBLIC_IPV4 = {
    "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9",
    "185.12.64.1", "185.12.64.2",          # Hetzner's resolvers
    "93.184.216.34",                       # example.com
    "1.2.3.4", "65.21.7.8", "37.0.0.1",    # fixtures in tests
    "13.0.0.0", "64.0.0.0",                # range bounds in tests
}
HOME_NAMES_ALLOWED = {"someone", "me", "u", "orbit", "runner", "<user>", "you"}

RULES = [
    ("a personal mail address",
     re.compile(r"[a-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|msn|yahoo|icloud|me|mac|"
                r"proton|protonmail|pm|gmx|aol|yandex|walla)\.[a-z.]{2,}", re.I)),
    ("a GitHub account's no-reply address",
     re.compile(r"\b\d+\+[a-z0-9-]+@users\.noreply\.github\.com", re.I)),
    ("a private key with a body",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
                r"[ \t]*\r?\n(?:[A-Za-z0-9+/=:\- ]*\r?\n)?[A-Za-z0-9+/=]{60,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("an age secret key", re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{58}\b")),
    ("a Telegram bot token", re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_-]{33}\b")),
    ("an Anthropic or OpenAI key", re.compile(r"\bsk-(?:ant-[a-z0-9]+-|proj-)?[A-Za-z0-9_-]{32,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
PLACEHOLDER = re.compile(r"PLANTED|EXAMPLE|FAKE|REDACTED|XXXX", re.I)
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w./])")   # a CIDR range is no one's host
HOME = re.compile(r"(?:\b[a-z]:(?:\\\\|\\|/)+users(?:\\\\|\\|/)+|/c/users/|/home/|(?<![\w.])/users/)([a-z0-9_][^\\/\s'\"`:;,)|]*)", re.I)
WORD = re.compile(r"[a-z0-9]+")
DOTTED = re.compile(r"[a-z0-9](?:[a-z0-9._@-]*[a-z0-9])?")
CO_AUTHOR = re.compile(r"^\s*co-authored-by\s*:", re.I | re.M)


def _h(s: str) -> str:
    return hashlib.sha256((SALT + s.lower()).encode("utf-8")).hexdigest()[:20]


def _lengths() -> list[int]:
    return sorted({int(e.split(":", 1)[0]) for e in HASHED})


_SEEN: dict[str, bool] = {}


def _word_is_personal(word: str, lengths: list[int]) -> bool:
    if word in _SEEN:
        return _SEEN[word]
    hit = False
    for n in lengths:
        if n > len(word):
            break
        for i in range(len(word) - n + 1):
            if f"{n}:{_h(word[i:i + n])}" in HASHED:
                hit = True
                break
        if hit:
            break
    _SEEN[word] = hit
    return hit


def _line_hits(line: str, lengths: list[int]) -> list[str]:
    why = []
    low = ALLOWED_SPANS.sub(" ", line.lower())
    if lengths and (any(_word_is_personal(w, lengths) for w in set(WORD.findall(low)))
                    or any(f"{len(d)}:{_h(d)}" in HASHED for d in set(DOTTED.findall(low)))):
        why.append("a personal string (one of the hashed list)")
    for name, rx in RULES:
        if any(not PLACEHOLDER.search(m.group(0)) for m in rx.finditer(line)):
            why.append(name)
    for m in IPV4.finditer(line):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip.is_global and m.group(1) not in ALLOWED_PUBLIC_IPV4:
            why.append("a public IPv4 address (allow a no-one's address in ALLOWED_PUBLIC_IPV4)")
            break
    for m in HOME.finditer(line):
        name = m.group(1).lower()
        if name not in HOME_NAMES_ALLOWED and not name.startswith(("$", "%", "<", "{", "~")):
            why.append("a home-directory path that names a person's account")
            break
    return why


def scan_text(text: str) -> list[tuple[int, str]]:
    """(line number, why) for every hit in `text`."""
    lengths = _lengths()
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for why in _line_hits(line, lengths):
            hits.append((n, why))
    # a private key's body is on the line after its header
    for name, rx in RULES:
        if name == "a private key with a body":
            for m in rx.finditer(text):
                if not PLACEHOLDER.search(m.group(0)):
                    hits.append((text.count("\n", 0, m.start()) + 1, name))
    return sorted(set(hits))


def _git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(("git", "-C", str(ROOT)) + args, input=data, capture_output=True, check=True).stdout


def _blobs(ids: list[str]) -> list[bytes]:
    """The contents of the blobs `ids`, in order, through one `git cat-file --batch`."""
    if not ids:
        return []
    out = _git("cat-file", "--batch", data=("\n".join(ids) + "\n").encode())
    contents, i = [], 0
    for _ in ids:
        header_end = out.index(b"\n", i)
        size = int(out[i:header_end].split()[2])
        contents.append(out[header_end + 1:header_end + 1 + size])
        i = header_end + 1 + size + 1
    return contents


def _decode(raw: bytes) -> str | None:
    if b"\0" in raw[:8000]:
        return None
    return raw.decode("utf-8", errors="replace")


def scan_files(named: list[tuple[str, bytes]]) -> list[str]:
    out = []
    for path, raw in named:
        text = _decode(raw)
        if text is None:
            continue
        out += [f"{path}:{n}: {why}" for n, why in scan_text(text)]
        out += [f"{path}: the file's path: {why}" for _, why in scan_text(path)]
    return out


def working_tree() -> list[tuple[str, bytes]]:
    paths = _git("ls-files", "-z", "-co", "--exclude-standard").decode().split("\0")
    return [(p, (ROOT / p).read_bytes()) for p in paths if p and (ROOT / p).is_file()]


def staged() -> list[tuple[str, bytes]]:
    rows = [r.split("\t", 1) for r in _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
            .decode().split("\0") if r]
    paths = [r[0] for r in rows]
    ids = [f":{p}" for p in paths]
    return list(zip(paths, _blobs(ids)))


def author_problem(email: str) -> str | None:
    if AUTHOR and _h(email.strip().strip("<>")) != AUTHOR:
        return (f"the author {email!r} is not this repository's author: it takes commits from one "
                f"author only (scripts/personal_strings.py --author)")
    return None


def message_problems(message: str) -> list[str]:
    out = [f"commit message line {n}: {why}" for n, why in scan_text(message)]
    if CO_AUTHOR.search(message):
        out.append("commit message: a Co-authored-by trailer (this repository has one author)")
    return out


def push(old: str, new: str) -> list[str]:
    """Every commit `new` brings that the remote does not have."""
    zero = "0" * 40
    rng = [new, "--not", "--remotes"] if old == zero else [f"{old}..{new}"]
    commits = _git("rev-list", *rng).decode().split()
    out = []
    for c in commits:
        meta = _git("show", "-s", "--format=%ae%x00%ce%x00%B", c).decode("utf-8", "replace").split("\0", 2)
        for who in meta[:2]:
            p = author_problem(who)
            if p:
                out.append(f"commit {c[:10]}: {p}")
        out += [f"commit {c[:10]}: {m}" for m in message_problems(meta[2])]
        rows = _git("diff-tree", "-r", "--root", "--no-commit-id", "-z", "--diff-filter=ACMR", c).decode().split("\0")
        pairs = [(rows[i + 1], rows[i].split()[3]) for i in range(0, len(rows) - 1, 2) if rows[i].startswith(":")]
        out += [f"commit {c[:10]}: {h}" for h in scan_files([(p, b) for (p, _), b in zip(pairs, _blobs([i for _, i in pairs]))])]
    return out


def _rewrite(marker: str, body: str) -> None:
    me = pathlib.Path(__file__)
    t = me.read_text(encoding="utf-8")
    a, b = f"# BEGIN {marker}\n", f"# END {marker}\n"
    i, j = t.index(a) + len(a), t.index(b)
    me.write_text(t[:i] + body + t[j:], encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    if argv[:1] == ["--add"]:
        entries = set(HASHED) | {f"{len(s.strip())}:{_h(s.strip())}" for s in argv[1:] if s.strip()}
        _rewrite("HASHED", "HASHED = frozenset({\n" + "".join(f'    "{e}",\n' for e in sorted(entries)) + "})\n")
        print(f"{len(entries)} hashed strings")
        return 0
    if argv[:1] == ["--author"] and len(argv) == 2:
        _rewrite("AUTHOR", f'AUTHOR = "{_h(argv[1].strip())}"\n')
        return 0
    if argv[:1] == ["--staged"]:
        hits = scan_files(staged())
        ident = _git("var", "GIT_AUTHOR_IDENT").decode()
        p = author_problem(ident[ident.find("<") + 1:ident.find(">")])
        hits += [p] if p else []
    elif argv[:1] == ["--push"] and len(argv) == 3:
        hits = push(argv[1], argv[2])
    elif argv[:1] == ["--message"] and len(argv) == 2:
        text = pathlib.Path(argv[1]).read_text(encoding="utf-8", errors="replace")
        hits = message_problems("\n".join(l for l in text.splitlines() if not l.startswith("#")))
    elif not argv:
        hits = scan_files(working_tree())
    else:
        print(__doc__)
        return 2
    for h in hits:
        print(h)
    if hits:
        print(f"\nrefused: {len(hits)} hit(s). Take the string out (a placeholder such as example.com,"
              f" 203.0.113.x or ~ reads the same); see scripts/personal_strings.py.", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
