"""Importing whatever is dropped: files, folders, and archives, holding anything.

``Intake`` walks every path it is given. Archives (zip, tar, gz, and, with the
system's bsdtar, 7z and rar) are unpacked, archives inside them too, up to a few
levels deep. Chat exports are recognised by their content wherever they sit and
go to their importers: a ChatGPT or Claude ``conversations.json``, a Gemini
``MyActivity.json``, Claude Code and Codex CLI sessions, and ChatLore archives.
Every other file ChatLore can read becomes a note
(``chatlore.importers.documents``). The rest is skipped, and each skipped file
is listed with the reason, so nothing disappears silently.

Unpacking is limited in total size and file count and never writes outside its
folder, so a hostile archive can neither fill the disk nor overwrite anything.
"""

from __future__ import annotations

import gzip
import os
import platform
import re
import shutil
import subprocess
import tarfile
import zipfile
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from chatlore.archive import is_archive
from chatlore.importers import IMPORTERS, ImportIssue, IssueSink, detect_source
from chatlore.importers.agents import session_kind
from chatlore.importers.base import ImporterError, load_json, report
from chatlore.importers.documents import MEDIA, DocumentError, read_document, readable
from chatlore.importers.gemini import _is_gemini
from chatlore.models import Conversation, SourceKind

MAX_UNPACKED = 2_000_000_000
"""Bytes all archives together may unpack to."""
MAX_FILES = 100_000
"""Files one import may hold."""
MAX_DEPTH = 4
"""How deep archives may sit inside archives."""

ARCHIVES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".txz",
    ".tar",
    ".zip",
    ".7z",
    ".rar",
    ".gz",
)
_SKIPPED_DIRS = frozenset(
    {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".obsidian",
     ".trash", ".Trash", "__MACOSX", ".idea", ".vscode"}
)  # fmt: skip
_SKIPPED_FILES = frozenset({".ds_store", "thumbs.db", "desktop.ini", "archive_browser.html"})
_EXPORT_FILES = {"conversations.json", "myactivity.json"}


@dataclass(frozen=True, slots=True)
class Skipped:
    """A file that was not imported, and why."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class _Export:
    kind: SourceKind
    path: Path
    shown: str


@dataclass(slots=True)
class Intake:
    """What is in the paths given, found by ``scan`` and read by ``conversations``.

    ``workdir`` is an empty folder for unpacked archives; the caller removes it.
    """

    workdir: Path
    max_unpacked: int = MAX_UNPACKED
    max_files: int = MAX_FILES
    exports: list[_Export] = field(default_factory=list)
    archives: list[tuple[Path, str]] = field(default_factory=list)
    documents: list[tuple[Path, str]] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    sources: Counter[str] = field(default_factory=Counter)
    _unpacked: int = 0
    _files: int = 0
    _folders: int = 0

    # -- finding what is there ---------------------------------------------------

    def scan(self, paths: Sequence[Path]) -> None:
        """Find everything importable under ``paths``, unpacking archives on the way."""
        for path in paths:
            if not path.exists():
                raise ImporterError(f"{path} does not exist")
            # Files keep their path within the folder given, as the Markdown importer
            # always named them, so importing a folder again updates the same notes.
            if path.is_dir():
                self._folder(path, "", 0)
            else:
                self._file(path, path.name, 0)

    def _folder(self, root: Path, shown: str, depth: int) -> None:
        for folder, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames if name not in _SKIPPED_DIRS and not name.startswith(".")
            )
            here = Path(folder)
            prefix = _join(shown, here.relative_to(root).as_posix())
            claimed = self._exports_in(here, filenames, prefix)
            for name in sorted(filenames):
                if name.lower() in _EXPORT_FILES:
                    continue
                if claimed is not None and Path(name).suffix.lower() in {".json", ".html"}:
                    self.skipped.append(
                        Skipped(_join(prefix, name), f"part of the {claimed} export")
                    )
                    continue
                self._file(here / name, _join(prefix, name), depth)

    def _exports_in(self, folder: Path, filenames: list[str], prefix: str) -> str | None:
        """Take the chat exports in one folder. Returns the export's name when one is there."""
        claimed: str | None = None
        for name in filenames:
            lower = name.lower()
            if lower not in _EXPORT_FILES:
                continue
            path, shown = folder / name, _join(prefix, name)
            if not self._count():
                return claimed
            if lower == "myactivity.json":
                if _has_gemini(path):
                    self.exports.append(_Export(SourceKind.GEMINI, path, shown))
                    claimed = "Gemini"
                else:
                    self.skipped.append(Skipped(shown, "activity for another Google product"))
                continue
            try:
                kind = detect_source(path)
            except ImporterError:
                self.documents.append((path, shown))  # a conversations.json of some other kind
                continue
            self.exports.append(_Export(kind, path, shown))
            claimed = "ChatGPT" if kind is SourceKind.CHATGPT else kind.value.title()
        return claimed

    def _file(self, path: Path, shown: str, depth: int) -> None:
        lower = path.name.lower()
        if lower in _SKIPPED_FILES or lower.startswith("._"):
            return
        if not self._count():
            return
        if lower in _EXPORT_FILES:
            self._exports_in(
                path.parent, [path.name], shown.rsplit("/", 1)[0] if "/" in shown else ""
            )
            return
        if lower.endswith(ARCHIVES) and not lower.endswith((".docx", ".xlsx", ".pptx")):
            self._archive(path, shown, depth)
            return
        if path.suffix.lower() == ".jsonl" and (kind := session_kind(path)) is not None:
            self.exports.append(_Export(kind, path, shown))
            return
        if path.suffix.lower() in MEDIA:
            self.skipped.append(Skipped(shown, "images, audio, video, and programs are not read"))
            return
        if not readable(path):
            self.skipped.append(Skipped(shown, "not a kind of file ChatLore reads"))
            return
        self.documents.append((path, shown))

    def _archive(self, path: Path, shown: str, depth: int) -> None:
        if lower_is_zip(path) and is_archive(path):
            self.archives.append((path, shown))
            return
        if depth >= MAX_DEPTH:
            self.skipped.append(Skipped(shown, "archives nested too deep"))
            return
        self._folders += 1
        target = self.workdir / f"{self._folders:05d}"
        target.mkdir(parents=True)
        try:
            self._unpack(path, target)
        except UnpackError as error:
            self.skipped.append(Skipped(shown, str(error)))
            return
        if path.name.lower().endswith(".gz") and not path.name.lower().endswith(".tar.gz"):
            for inner in sorted(target.iterdir()):
                self._file(
                    inner,
                    _join(shown.rsplit("/", 1)[0] if "/" in shown else "", inner.name),
                    depth + 1,
                )
            return
        self._folder(target, shown, depth + 1)

    def _unpack(self, path: Path, target: Path) -> None:
        lower = path.name.lower()
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = [member for member in archive.infolist() if not member.is_dir()]
                self._reserve(sum(member.file_size for member in members), len(members))
                for member in members:
                    archive.extract(member, target)  # zipfile keeps members inside the target
            return
        if lower.endswith((".tar", ".tgz", ".tbz2", ".txz", ".tar.gz", ".tar.bz2", ".tar.xz")):
            try:
                with tarfile.open(path) as bundle:
                    entries = [entry for entry in bundle.getmembers() if entry.isfile()]
                    self._reserve(sum(entry.size for entry in entries), len(entries))
                    bundle.extractall(target, members=entries, filter="data")
            except (tarfile.TarError, OSError) as error:
                raise UnpackError(f"the archive could not be unpacked: {error}") from error
            return
        if lower.endswith(".gz"):
            name = path.name[: -len(".gz")] or "unpacked"
            with gzip.open(path, "rb") as source, (target / name).open("wb") as out:
                while piece := source.read(1 << 20):
                    self._reserve(len(piece), 0)
                    out.write(piece)
            self._reserve(0, 1)
            return
        self._bsdtar(path, target)

    def _bsdtar(self, path: Path, target: Path) -> None:
        tool = bsdtar()
        if tool is None:
            raise UnpackError(
                f"{path.suffix} archives need bsdtar, which is not installed here "
                "(it comes with Windows and macOS; on Linux install libarchive-tools)"
            )
        listing = subprocess.run(
            [tool, "-tvf", str(path)], capture_output=True, text=True, errors="replace", check=False
        )
        if listing.returncode != 0:
            raise UnpackError(f"the archive could not be read: {listing.stderr.strip()[:200]}")
        sizes = [_listed_size(line) for line in listing.stdout.splitlines() if line.startswith("-")]
        self._reserve(sum(sizes), len(sizes))
        # bsdtar refuses absolute paths and ".." by default, so nothing lands outside target.
        unpacked = subprocess.run(
            [tool, "-xf", str(path), "-C", str(target)],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if unpacked.returncode != 0:
            raise UnpackError(f"the archive could not be unpacked: {unpacked.stderr.strip()[:200]}")

    def _reserve(self, size: int, files: int) -> None:
        self._unpacked += size
        self._files += files
        if self._unpacked > self.max_unpacked:
            raise ImporterError(
                f"the archives unpack to more than {self.max_unpacked // 1_000_000_000} GB"
            )
        if self._files > self.max_files:
            raise ImporterError(f"more than {self.max_files:,} files in one import")

    def _count(self) -> bool:
        self._files += 1
        if self._files > self.max_files:
            raise ImporterError(f"more than {self.max_files:,} files in one import")
        return True

    # -- reading it ----------------------------------------------------------------

    def conversations(self, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        """Every conversation found, chat exports first, then documents as notes.

        ChatLore archives are listed in ``archives`` and imported by the caller,
        since they bring a graph and caches along. ``sources`` counts what each
        source yielded.
        """
        for export in self.exports:
            try:
                for conversation in IMPORTERS[export.kind].parse(export.path, on_issue):
                    self.sources[conversation.source.value] += 1
                    yield conversation
            except ImporterError as error:
                self.skipped.append(Skipped(export.shown, str(error)))
        for path, shown in self.documents:
            try:
                found = read_document(path, shown)
            except DocumentError as error:
                self.skipped.append(Skipped(shown, str(error)))
                continue
            for conversation in found:
                self.sources[conversation.source.value] += 1
                yield conversation

    def found_anything(self) -> bool:
        return bool(self.exports or self.archives or self.documents)


class UnpackError(Exception):
    """One archive cannot be unpacked; the import goes on without it."""


def lower_is_zip(path: Path) -> bool:
    return path.name.lower().endswith(".zip") and zipfile.is_zipfile(path)


def _join(prefix: str, name: str) -> str:
    parts = [part for part in (prefix, name) if part and part != "."]
    return "/".join(parts)


def _has_gemini(path: Path) -> bool:
    try:
        data = load_json(str(path), path.read_bytes())
    except ImporterError:
        return False
    return isinstance(data, list) and any(_is_gemini(entry) for entry in data)


def _listed_size(line: str) -> int:
    fields = line.split()
    return int(fields[4]) if len(fields) > 4 and fields[4].isdigit() else 0


@cache
def bsdtar() -> str | None:
    """A bsdtar on this machine, which unpacks 7z and rar as well as the rest."""
    candidates = [shutil.which("bsdtar")]
    if platform.system() == "Windows":
        candidates.append(
            str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "tar.exe")
        )
    candidates += ["/usr/bin/tar", shutil.which("tar")]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            version = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "bsdtar" in version:
            return candidate
    return None


def describe(skipped: Sequence[Skipped], limit: int = 10) -> list[str]:
    """Skipped files for people to read, with identical reasons grouped."""
    grouped: dict[str, list[str]] = {}
    for item in skipped:
        grouped.setdefault(re.sub(r"\d+", "N", item.reason), []).append(item.path)
    lines = []
    for reason, paths in list(grouped.items())[:limit]:
        lines.append(
            f"{paths[0]}: {reason}" if len(paths) == 1 else f"{len(paths)} files: {reason}"
        )
    return lines


__all__ = [
    "ARCHIVES",
    "MAX_FILES",
    "MAX_UNPACKED",
    "ImportIssue",
    "Intake",
    "Skipped",
    "UnpackError",
    "bsdtar",
    "describe",
    "report",
]
