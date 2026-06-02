import sys
import subprocess
import threading
import os
import signal
import hashlib
import json
import re
import shlex
import time
import traceback
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any

REAL_CLANGD = "/usr/bin/clangd"
exit_event = threading.Event()

# ==================== 全局配置 ====================

REQUEST_TTL_SECONDS = 300.0
CLEANUP_INTERVAL_MESSAGES = 100
MAX_ANALYSIS_CACHE = 64
MAX_RECENT_MESSAGES = 24

# ==================== 状态缓存 ====================

state_lock = threading.RLock()
message_counter = 0

# 语义 token 请求 ID: id -> monotonic_time
semantic_request_ids: dict[Any, float] = {}

# 当前文档内容：uri -> lines
documents: dict[str, list[str]] = {}

# 文档版本：uri -> version
document_versions: dict[str, int] = {}

# 分析缓存：uri -> (version, analysis_result, last_access_time)
analysis_cache: dict[str, tuple[int, list[dict[str, Any]], float]] = {}

workspace_root_path: str | None = None
clangd_stdin = None
clangd_write_lock = threading.RLock()
client_write_lock = threading.RLock()
instance_registry_path: str | None = None
recent_messages = deque(maxlen=MAX_RECENT_MESSAGES)
shutdown_reason: str | None = None

INTERNAL_REQUEST_PREFIX = "__proxy_internal__"
INTERNAL_REQUEST_TIMEOUT_SECONDS = 0.25
DEFINITION_LOOKUP_MISS = object()

internal_request_lock = threading.RLock()
internal_request_counter = 0
thread_context = threading.local()
internal_request_waiters: dict[str, dict[str, Any]] = {}
internal_request_metadata: dict[str, dict[str, Any]] = {}

compile_commands_cache: dict[str, tuple[float, dict[str, str]]] = {}
compile_commands_entries_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
definition_lookup_cache: dict[tuple[str, int, int, int], Any] = {}

# inlayHint 请求缓存：id -> {uri, range, time}
inlay_hint_requests: dict[Any, dict[str, Any]] = {}

# hover 请求缓存：id -> {uri, position, time}
hover_requests: dict[Any, dict[str, Any]] = {}

# definition 请求缓存：id -> {uri, position, time}
definition_requests: dict[Any, dict[str, Any]] = {}

# documentHighlight 请求缓存：id -> {uri, position, time}
document_highlight_requests: dict[Any, dict[str, Any]] = {}

# 你的无下划线字面量后缀 -> 展示类型
SUFFIX_TYPE_MAP = {
    "i8": "i8",
    "u8": "u8",
    "i16": "i16",
    "u16": "u16",
    "i32": "i32",
    "u32": "u32",
    "i64": "i64",
    "u64": "u64",
    "f32": "f32",
    "f64": "f64",
    "usize": "usize",
    "isize": "isize",
}

# 长后缀放前面，避免匹配歧义
SUFFIX_RE = r"usize|isize|i16|u16|i32|u32|i64|u64|f32|f64|i8|u8"

AUTO_ASSIGN_RE = re.compile(
    r"""
    ^
    (?P<indent>\s*)
    (?P<auto_decl>const\s+auto|auto)
    (?P<ptr_ref>\s*[*&]{0,2})?
    \s+
    (?P<name>[A-Za-z_]\w*)
    \s*=\s*
    (?P<expr>.*?)
    \s*;
    """,
    re.VERBOSE,
)

LITERAL_WITH_SUFFIX_RE = re.compile(
    rf"""
    ^\s*
    (?:
        0[xX][0-9A-Fa-f']+
      | 0[bB][01']+
      | [0-9][0-9']*(?:\.[0-9']*)?(?:[eE][+-]?[0-9']+)?
      | \.[0-9']+(?:[eE][+-]?[0-9']+)?
    )
    (?P<suffix>{SUFFIX_RE})
    \s*$
    """,
    re.VERBOSE,
)

IDENT_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*$")

# 标准 C++ 整数字面量：
#   123
#   123u
#   123U
#   123l
#   123ll
#   123ul
#   0xff
#   0b1010
STANDARD_INT_LITERAL_RE = re.compile(
    r"""
    ^\s*
    (?P<body>
        0[xX][0-9A-Fa-f']+
      | 0[bB][01']+
      | [0-9][0-9']*
    )
    (?P<suffix>
        [uU]?[lL]{0,2}
      | [lL]{1,2}[uU]?
    )?
    \s*$
    """,
    re.VERBOSE,
)

# 标准 C++ 浮点字面量：
#   3.14
#   3.14f
#   1e3
#   1e3f
#   .5
STANDARD_FLOAT_LITERAL_RE = re.compile(
    r"""
    ^\s*
    (?P<body>
        [0-9][0-9']*\.[0-9']*(?:[eE][+-]?[0-9']+)?
      | \.[0-9']+(?:[eE][+-]?[0-9']+)?
      | [0-9][0-9']*[eE][+-]?[0-9']+
    )
    (?P<suffix>[fFlL])?
    \s*$
    """,
    re.VERBOSE,
)

FUNCTION_DECL_RE = re.compile(
    r"""
    ^\s*
    (?:(?:inline|constexpr|static|extern|virtual|friend)\s+)*
    (?P<ret>[A-Za-z_:\s][A-Za-z0-9_:\s*&<>]*?)
    \s+
    (?P<name>[A-Za-z_]\w*)
    \s*\([^;{}()]*\)\s*
    (?:(?:const|noexcept)\s*)*
    (?:\{|;)$
    """,
    re.VERBOSE,
)

VARIABLE_DECL_RE = re.compile(
    r"""
    ^\s*
    (?:(?:inline|constexpr|static|extern)\s+)*
    (?P<type>[A-Za-z_:\s][A-Za-z0-9_:\s*&<>]*?)
    \s+
    (?P<name>[A-Za-z_]\w*)
    \s*(?:=\s*[^;{}()]*)?;
    \s*$
    """,
    re.VERBOSE,
)

INCLUDE_RE = re.compile(r'^\s*#\s*include\s*(?P<delim>[<"])(?P<path>[^">]+)[">]')
USING_SYMBOL_RE = re.compile(
    r'^\s*using\s+(?P<qualified>(?:(?:[A-Za-z_]\w*)::)+(?P<name>[A-Za-z_]\w*))\s*;\s*$'
)
USING_NAMESPACE_RE = re.compile(
    r'^\s*using\s+namespace\s+(?P<namespace>(?:(?:[A-Za-z_]\w*)::)*[A-Za-z_]\w*)\s*;\s*$'
)
NAMESPACE_OPEN_RE = re.compile(r'^\s*namespace\s+(?P<name>[A-Za-z_]\w*)\s*\{')

I32_MAX = 2**31 - 1
U32_MAX = 2**32 - 1
I64_MAX = 2**63 - 1
U64_MAX = 2**64 - 1

TYPE_INFO_MAP = {
    "i8": {"kind": "int", "bits": 8, "signed": True},
    "u8": {"kind": "int", "bits": 8, "signed": False},
    "i16": {"kind": "int", "bits": 16, "signed": True},
    "u16": {"kind": "int", "bits": 16, "signed": False},
    "i32": {"kind": "int", "bits": 32, "signed": True},
    "u32": {"kind": "int", "bits": 32, "signed": False},
    "i64": {"kind": "int", "bits": 64, "signed": True},
    "u64": {"kind": "int", "bits": 64, "signed": False},
    "isize": {"kind": "int", "bits": 64, "signed": True},
    "usize": {"kind": "int", "bits": 64, "signed": False},
    "f32": {"kind": "float", "bits": 32, "signed": None},
    "f64": {"kind": "float", "bits": 64, "signed": None},
    "long double": {"kind": "float", "bits": 80, "signed": None},
}

TYPE_ALIAS_MAP = {
    "base::i8": "i8",
    "base::u8": "u8",
    "base::i16": "i16",
    "base::u16": "u16",
    "base::i32": "i32",
    "base::u32": "u32",
    "base::i64": "i64",
    "base::u64": "u64",
    "base::isize": "isize",
    "base::usize": "usize",
    "base::f32": "f32",
    "base::f64": "f64",
    "int": "i32",
    "unsigned": "u32",
    "unsigned int": "u32",
    "long": "i64",
    "unsigned long": "u64",
    "long long": "i64",
    "unsigned long long": "u64",
    "float": "f32",
    "double": "f64",
    "std::size_t": "usize",
    "size_t": "usize",
    "std::ptrdiff_t": "isize",
    "ptrdiff_t": "isize",
}

SIGNED_INT_BY_BITS = {
    8: "i8",
    16: "i16",
    32: "i32",
    64: "i64",
}

UNSIGNED_INT_BY_BITS = {
    8: "u8",
    16: "u16",
    32: "u32",
    64: "u64",
}

FLOAT_BY_BITS = {
    32: "f32",
    64: "f64",
    80: "long double",
}

SOURCE_FILE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
}


@dataclass(frozen=True)
class ExprType:
    name: str
    kind: str
    bits: int
    signed: bool | None = None


@dataclass(frozen=True)
class ExprDiagnostic:
    message: str
    start: int
    end: int
    severity: int = 1


@dataclass(frozen=True)
class LiteralInfo:
    kind: str
    standard: bool
    suffix: str = ""
    int_value: int | None = None


@dataclass
class ExprNode:
    type_name: str | None
    start: int
    end: int
    diagnostics: list[ExprDiagnostic] = field(default_factory=list)
    literal_info: LiteralInfo | None = None


@dataclass
class ExprResult:
    type_name: str | None
    diagnostics: list[ExprDiagnostic] = field(default_factory=list)


def uri_to_path(uri: str) -> str | None:
    if not uri.startswith("file://"):
        return None

    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    if os.name == "nt" and path.startswith("/"):
        path = path[1:]
    return path


def path_to_uri(path: str) -> str:
    normalized = os.path.abspath(path)
    return "file://" + urllib.parse.quote(normalized)


def normalize_workspace_path(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(path))


def log_proxy_message(message: str):
    print(f"[代理日志] {message}", file=sys.stderr, flush=True)


def summarize_lsp_message(msg: dict[str, Any]) -> str:
    msg_id = msg.get("id", "NO_ID")
    method = msg.get("method", "RESPONSE/UNKNOWN")
    parts = [f"id={msg_id}", f"method={method}"]

    params = msg.get("params")
    if isinstance(params, dict):
        text_document = params.get("textDocument")
        if isinstance(text_document, dict):
            uri = text_document.get("uri")
            if isinstance(uri, str):
                parts.append(f"uri={uri}")
            version = text_document.get("version")
            if isinstance(version, int):
                parts.append(f"version={version}")

        position = params.get("position")
        if isinstance(position, dict):
            line_no = position.get("line")
            ch = position.get("character")
            if isinstance(line_no, int) and isinstance(ch, int):
                parts.append(f"pos={line_no}:{ch}")

        req_range = params.get("range")
        if isinstance(req_range, dict):
            start = req_range.get("start")
            end = req_range.get("end")
            if isinstance(start, dict) and isinstance(end, dict):
                s_line = start.get("line")
                s_ch = start.get("character")
                e_line = end.get("line")
                e_ch = end.get("character")
                if all(isinstance(v, int) for v in (s_line, s_ch, e_line, e_ch)):
                    parts.append(f"range={s_line}:{s_ch}-{e_line}:{e_ch}")

    result = msg.get("result")
    if result is not None:
        parts.append(f"result_type={type(result).__name__}")

    error = msg.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        parts.append(f"error={code}:{message}")

    return ", ".join(parts)


def remember_recent_message(direction: str, msg: dict[str, Any]):
    with state_lock:
        recent_messages.append({
            "time": time.strftime("%H:%M:%S"),
            "direction": direction,
            "summary": summarize_lsp_message(msg),
        })


def format_recent_messages(limit: int = 8) -> str:
    with state_lock:
        items = list(recent_messages)[-limit:]
    if not items:
        return "无"
    return "\n".join(
        f"  - {item['time']} | {item['direction']} | {item['summary']}"
        for item in items
    )


def format_exception_trace() -> str:
    return "".join(traceback.format_exc()).strip()


def log_exception_with_context(title: str, exc: BaseException, **context: Any):
    parts = [f"{key}={value}" for key, value in context.items()]
    detail = ", ".join(parts)
    if detail:
        log_proxy_message(f"{title}: type={type(exc).__name__}, error={exc}, {detail}")
    else:
        log_proxy_message(f"{title}: type={type(exc).__name__}, error={exc}")

    trace = format_exception_trace()
    if trace and trace != "NoneType: None":
        print(trace, file=sys.stderr, flush=True)

    print(
        "[代理日志] 最近消息:\n" + format_recent_messages(),
        file=sys.stderr,
        flush=True,
    )


def install_thread_exception_logging():
    def _threading_excepthook(args: threading.ExceptHookArgs):
        parts = [
            f"thread={args.thread.name if args.thread else 'unknown'}",
            f"type={args.exc_type.__name__ if args.exc_type else 'unknown'}",
            f"error={args.exc_value}",
        ]
        log_proxy_message("线程异常: " + ", ".join(parts))
        if args.exc_traceback is not None:
            print(
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)).strip(),
                file=sys.stderr,
                flush=True,
            )
        print(
            "[代理日志] 最近消息:\n" + format_recent_messages(),
            file=sys.stderr,
            flush=True,
        )

    threading.excepthook = _threading_excepthook


def request_shutdown(reason: str):
    global shutdown_reason
    should_log_recent = False
    with state_lock:
        if shutdown_reason is None:
            shutdown_reason = reason
            should_log_recent = True

    if should_log_recent:
        log_proxy_message(f"设置退出事件: reason={reason}")
        print(
            "[代理日志] 最近消息:\n" + format_recent_messages(),
            file=sys.stderr,
            flush=True,
        )

    exit_event.set()


def compute_instance_registry_path(root_uri: str | None, process_id: int | None) -> str:
    compile_dir = parse_compile_commands_dir_arg() or ""
    key_source = f"{root_uri or ''}|{process_id if isinstance(process_id, int) else ''}|{compile_dir}"
    key_hash = hashlib.sha1(key_source.encode("utf-8")).hexdigest()
    return os.path.join("/tmp", f"clangd_py_instance_{key_hash}.pid")


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_looks_like_proxy(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [part.decode("utf-8", errors="replace") for part in f.read().split(b"\0") if part]
    except Exception:
        return False

    script_path = os.path.abspath(__file__)
    return any(part == script_path for part in parts)


def cleanup_instance_registry():
    global instance_registry_path
    path = instance_registry_path
    if not path:
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            recorded_pid = int(f.read().strip())
    except Exception:
        recorded_pid = None

    if recorded_pid == os.getpid():
        try:
            os.remove(path)
        except OSError:
            pass

    instance_registry_path = None


def register_proxy_instance(root_uri: str | None, process_id: int | None):
    global instance_registry_path
    registry_path = compute_instance_registry_path(root_uri, process_id)

    previous_pid = None
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            previous_pid = int(f.read().strip())
    except Exception:
        previous_pid = None

    if (
        isinstance(previous_pid, int)
        and previous_pid != os.getpid()
        and is_pid_alive(previous_pid)
        and process_looks_like_proxy(previous_pid)
    ):
        log_proxy_message(
            f"准备清理旧代理实例: previous_pid={previous_pid}, current_pid={os.getpid()}, registry={registry_path}"
        )
        try:
            os.kill(previous_pid, signal.SIGTERM)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if not is_pid_alive(previous_pid):
                    break
                time.sleep(0.05)
            if is_pid_alive(previous_pid):
                os.kill(previous_pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        with open(registry_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        return

    instance_registry_path = registry_path
    log_proxy_message(
        f"注册代理实例: pid={os.getpid()}, process_id={process_id}, registry={registry_path}"
    )


def parse_compile_commands_dir_arg() -> str | None:
    args = sys.argv[1:]
    for idx, arg in enumerate(args):
        if arg.startswith("--compile-commands-dir="):
            return arg.split("=", 1)[1]
        if arg == "--compile-commands-dir" and idx + 1 < len(args):
            return args[idx + 1]
    return None


def discover_compile_commands_path(uri: str | None) -> str | None:
    base_path = workspace_root_path
    if not base_path and uri:
        file_path = uri_to_path(uri)
        if file_path:
            base_path = os.path.dirname(file_path)
    if not base_path:
        base_path = os.getcwd()

    base_path = normalize_workspace_path(base_path)
    if not base_path:
        return None

    configured_dir = parse_compile_commands_dir_arg()
    if configured_dir:
        configured_dir = normalize_workspace_path(
            configured_dir if os.path.isabs(configured_dir) else os.path.join(base_path, configured_dir)
        )
        if configured_dir:
            candidate = configured_dir
            if os.path.isdir(candidate):
                candidate = os.path.join(candidate, "compile_commands.json")
            if os.path.isfile(candidate):
                return candidate

    search_root = base_path
    for current in [search_root, *list(iter_parent_dirs(search_root))]:
        direct = os.path.join(current, "compile_commands.json")
        if os.path.isfile(direct):
            return direct

        try:
            with os.scandir(current) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    child_candidate = os.path.join(entry.path, "compile_commands.json")
                    if os.path.isfile(child_candidate):
                        return child_candidate
        except OSError:
            continue

    return None


def iter_parent_dirs(path: str):
    current = os.path.abspath(path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        yield parent
        current = parent


def parse_include_dirs(command: str) -> list[str]:
    include_dirs: list[str] = []
    try:
        parts = shlex.split(command)
    except ValueError:
        return include_dirs

    idx = 0
    while idx < len(parts):
        part = parts[idx]
        if part == "-I" and idx + 1 < len(parts):
            include_dirs.append(parts[idx + 1])
            idx += 2
            continue
        if part.startswith("-I") and len(part) > 2:
            include_dirs.append(part[2:])
            idx += 1
            continue
        if part == "-isystem" and idx + 1 < len(parts):
            include_dirs.append(parts[idx + 1])
            idx += 2
            continue
        idx += 1

    return include_dirs


def load_compile_commands_entries(compile_commands_path: str) -> list[dict[str, Any]]:
    try:
        mtime = os.path.getmtime(compile_commands_path)
    except OSError:
        return []

    cached = compile_commands_entries_cache.get(compile_commands_path)
    if cached and cached[0] == mtime:
        return list(cached[1])

    try:
        with open(compile_commands_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return []

    normalized_entries = [entry for entry in entries if isinstance(entry, dict)]
    compile_commands_entries_cache[compile_commands_path] = (mtime, list(normalized_entries))
    return normalized_entries


def extract_entry_command(entry: dict[str, Any]) -> str:
    command = entry.get("command")
    if isinstance(command, str):
        return command

    arguments = entry.get("arguments")
    if not isinstance(arguments, list):
        return ""

    rendered_parts: list[str] = []
    for arg in arguments:
        rendered_parts.append(shlex.quote(str(arg)))
    return " ".join(rendered_parts)


def find_compile_command_entry(entries: list[dict[str, Any]], file_path: str) -> dict[str, Any] | None:
    normalized_target = normalize_workspace_path(file_path)
    if not normalized_target:
        return None

    for entry in entries:
        candidate = normalize_workspace_path(entry.get("file"))
        if candidate == normalized_target:
            return entry

    return None


def collect_compile_include_dirs(uri: str) -> list[str]:
    file_path = uri_to_path(uri)
    if not file_path:
        return []

    include_dirs: list[str] = []
    current_dir = normalize_workspace_path(os.path.dirname(file_path))
    if current_dir:
        include_dirs.append(current_dir)

    compile_commands_path = discover_compile_commands_path(uri)
    if not compile_commands_path:
        return include_dirs

    entries = load_compile_commands_entries(compile_commands_path)
    entry = find_compile_command_entry(entries, file_path)
    if entry is None:
        return include_dirs

    directory = normalize_workspace_path(entry.get("directory"))
    command = extract_entry_command(entry)
    for include_dir in parse_include_dirs(command):
        include_path = normalize_workspace_path(
            include_dir if os.path.isabs(include_dir) else os.path.join(directory or "", include_dir)
        )
        if include_path and include_path not in include_dirs:
            include_dirs.append(include_path)

    return include_dirs


def resolve_include_path(include_name: str, current_dir: str, include_dirs: list[str], prefer_current_dir: bool) -> str | None:
    search_dirs: list[str] = []
    if prefer_current_dir:
        search_dirs.append(current_dir)
    search_dirs.extend(include_dirs)

    for search_dir in search_dirs:
        candidate = normalize_workspace_path(os.path.join(search_dir, include_name))
        if candidate and os.path.isfile(candidate):
            return candidate

    return None


def collect_included_workspace_files(uri: str) -> list[str]:
    root_file = uri_to_path(uri)
    if not root_file:
        return []

    include_dirs = collect_compile_include_dirs(uri)
    if not include_dirs:
        return []

    root = normalize_workspace_path(workspace_root_path) if workspace_root_path else None
    visited: set[str] = set()
    discovered: set[str] = set()
    pending = [normalize_workspace_path(root_file) or root_file]

    while pending:
        current = pending.pop()
        if not current or current in visited:
            continue
        visited.add(current)

        try:
            with open(current, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue

        current_dir = os.path.dirname(current)
        for line in lines:
            match = INCLUDE_RE.match(line)
            if not match:
                continue

            include_path = resolve_include_path(
                match.group("path"),
                current_dir,
                include_dirs,
                match.group("delim") == "\"",
            )
            if not include_path or not should_index_path(include_path, root):
                continue

            discovered.add(include_path)
            if include_path not in visited:
                pending.append(include_path)

    return sorted(discovered)


def is_source_like_file(path: str) -> bool:
    return os.path.splitext(path)[1] in SOURCE_FILE_EXTENSIONS


def should_index_path(path: str, root: str | None) -> bool:
    if not is_source_like_file(path):
        return False
    if root is None:
        return True

    try:
        common = os.path.commonpath([root, path])
    except ValueError:
        return False

    return common == root


def collect_known_functions_from_lines(lines: list[str]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}

    for line in lines:
        m = FUNCTION_DECL_RE.match(line.strip())
        if not m:
            continue

        name = m.group("name")
        if name in {"if", "for", "while", "switch", "return"}:
            continue

        ret_type = normalize_type_name(m.group("ret"))
        if not ret_type:
            continue

        if name not in candidates:
            candidates[name] = set()
        candidates[name].add(ret_type)

    known_functions: dict[str, str] = {}
    for name, ret_types in candidates.items():
        if len(ret_types) != 1:
            continue
        known_functions[name] = next(iter(ret_types))

    return known_functions


def collect_known_functions(lines: list[str]) -> dict[str, str]:
    return collect_known_functions_from_lines(lines)


def collect_using_targets(lines: list[str]) -> tuple[dict[str, set[str]], set[str]]:
    direct_symbols: dict[str, set[str]] = {}
    namespaces: set[str] = set()

    for line in lines:
        stripped = line.strip()
        symbol_match = USING_SYMBOL_RE.match(stripped)
        if symbol_match:
            name = symbol_match.group("name")
            qualified = symbol_match.group("qualified")
            direct_symbols.setdefault(name, set()).add(qualified)
            continue

        namespace_match = USING_NAMESPACE_RE.match(stripped)
        if namespace_match:
            namespaces.add(namespace_match.group("namespace"))

    return direct_symbols, namespaces


def collect_known_variables_from_lines(lines: list[str], uri: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    namespace_stack: list[tuple[str, int]] = []
    brace_depth = 0
    skip_prefixes = (
        "using ",
        "using\t",
        "return ",
        "if ",
        "if(",
        "for ",
        "for(",
        "while ",
        "while(",
        "switch ",
        "switch(",
        "class ",
        "struct ",
        "enum ",
        "typedef ",
        "template<",
        "#",
    )

    for line_no, line in enumerate(lines):
        stripped = line.strip()
        current_namespace = "::".join(name for name, _ in namespace_stack if name)
        current_namespace_depth = namespace_stack[-1][1] if namespace_stack else 0

        if stripped and brace_depth == current_namespace_depth:
            if not stripped.startswith(skip_prefixes) and "(" not in stripped:
                match = VARIABLE_DECL_RE.match(stripped)
                if match:
                    decl_type = normalize_type_name(match.group("type"))
                    if decl_type:
                        name = match.group("name")
                        qualified_name = f"{current_namespace}::{name}" if current_namespace else name
                        start = line.find(name)
                        if start >= 0:
                            items.append({
                                "uri": uri,
                                "line": line_no,
                                "var_start": start,
                                "var_end": start + len(name),
                                "name": name,
                                "qualified_name": qualified_name,
                                "type": decl_type,
                            })

        namespace_match = NAMESPACE_OPEN_RE.match(stripped)
        brace_depth += line.count("{")
        brace_depth -= line.count("}")
        if namespace_match:
            namespace_stack.append((namespace_match.group("name"), brace_depth))

        while namespace_stack and brace_depth < namespace_stack[-1][1]:
            namespace_stack.pop()

    return items


def dedupe_definition_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int, int]] = set()
    deduped: list[dict[str, Any]] = []

    for item in items:
        item_uri = item.get("uri")
        line_no = item.get("line")
        start = item.get("var_start")
        if not isinstance(item_uri, str) or not isinstance(line_no, int) or not isinstance(start, int):
            continue
        key = (item_uri, line_no, start)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def build_included_variable_index(uri: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    qualified: dict[str, list[dict[str, Any]]] = {}
    unqualified: dict[str, list[dict[str, Any]]] = {}

    for include_path in collect_included_workspace_files(uri):
        include_uri = path_to_uri(include_path)
        try:
            with open(include_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue

        for item in collect_known_variables_from_lines(lines, include_uri):
            qualified_name = item.get("qualified_name")
            name = item.get("name")
            if isinstance(qualified_name, str):
                qualified.setdefault(qualified_name, []).append(item)
            if isinstance(name, str):
                unqualified.setdefault(name, []).append(item)

    return {
        "qualified": qualified,
        "unqualified": unqualified,
    }


def find_cross_file_definition_item(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    symbol = read_symbol_under_position(uri, position)
    if not symbol:
        return None

    index = build_included_variable_index(uri)
    qualified_index = index.get("qualified", {})
    unqualified_index = index.get("unqualified", {})
    normalized_symbol = symbol.lstrip(":")

    if "::" in normalized_symbol:
        exact_matches = dedupe_definition_items(list(qualified_index.get(normalized_symbol, [])))
        if len(exact_matches) == 1:
            return exact_matches[0]
        return None

    lines = read_document_lines(uri) or []
    direct_symbols, using_namespaces = collect_using_targets(lines)
    alias_matches: list[dict[str, Any]] = []
    for qualified_name in sorted(direct_symbols.get(normalized_symbol, set())):
        alias_matches.extend(qualified_index.get(qualified_name, []))
    for namespace_name in sorted(using_namespaces):
        alias_matches.extend(qualified_index.get(f"{namespace_name}::{normalized_symbol}", []))

    alias_matches = dedupe_definition_items(alias_matches)
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        return None

    basename_matches = dedupe_definition_items(list(unqualified_index.get(normalized_symbol, [])))
    if len(basename_matches) == 1:
        return basename_matches[0]

    return None


def build_project_function_index(uri: str | None) -> dict[str, str]:
    compile_commands_path = discover_compile_commands_path(uri)
    if not compile_commands_path:
        return {}

    try:
        mtime = os.path.getmtime(compile_commands_path)
    except OSError:
        return {}

    cached = compile_commands_cache.get(compile_commands_path)
    if cached and cached[0] == mtime:
        return dict(cached[1])

    try:
        with open(compile_commands_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return {}

    root = workspace_root_path
    if root:
        root = normalize_workspace_path(root)

    candidate_files: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        file_path = normalize_workspace_path(entry.get("file"))
        if file_path and should_index_path(file_path, root):
            candidate_files.add(file_path)

        directory = normalize_workspace_path(entry.get("directory"))
        for include_dir in parse_include_dirs(entry.get("command", "")):
            include_path = normalize_workspace_path(
                include_dir if os.path.isabs(include_dir) else os.path.join(directory or "", include_dir)
            )
            if not include_path or not os.path.isdir(include_path):
                continue
            if root and not should_index_path(os.path.join(include_path, "x.hpp"), root):
                continue

            for dirpath, _, filenames in os.walk(include_path):
                for filename in filenames:
                    path = os.path.join(dirpath, filename)
                    if should_index_path(path, root):
                        candidate_files.add(path)
                if len(candidate_files) > 4096:
                    break
            if len(candidate_files) > 4096:
                break

    known_functions: dict[str, str] = {}
    for path in sorted(candidate_files):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        known_functions.update(collect_known_functions_from_lines(lines))

    compile_commands_cache[compile_commands_path] = (mtime, dict(known_functions))
    return known_functions


def next_internal_request_id() -> str:
    global internal_request_counter
    with internal_request_lock:
        internal_request_counter += 1
        return f"{INTERNAL_REQUEST_PREFIX}{internal_request_counter}"


def send_lsp_payload(fd, msg: dict[str, Any]):
    body = json.dumps(
        msg,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    fd.write(header)
    fd.write(body)
    fd.flush()


def write_raw_payload(fd, body: bytes):
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    fd.write(header)
    fd.write(body)
    fd.flush()


def write_payload_with_lock(fd, msg: dict[str, Any], lock: threading.RLock):
    with lock:
        send_lsp_payload(fd, msg)


def read_exactly(fd, total_size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < total_size:
        chunk = fd.read(total_size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def request_clangd(
    method: str,
    params: dict[str, Any],
    timeout: float = INTERNAL_REQUEST_TIMEOUT_SECONDS,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if clangd_stdin is None:
        return None

    req_id = next_internal_request_id()
    waiter = {
        "event": threading.Event(),
        "response": None,
    }

    with internal_request_lock:
        internal_request_waiters[req_id] = waiter
        if metadata is not None:
            tagged = dict(metadata)
            tagged["time"] = time.monotonic()
            internal_request_metadata[req_id] = tagged

    try:
        write_payload_with_lock(clangd_stdin, {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }, clangd_write_lock)

        if not waiter["event"].wait(timeout=timeout):
            log_proxy_message(
                f"内部请求超时: req_id={req_id}, method={method}, timeout={timeout}, metadata={metadata}"
            )
            print(
                "[代理日志] 最近消息:\n" + format_recent_messages(),
                file=sys.stderr,
                flush=True,
            )
            return None

        return waiter["response"]
    except Exception as e:
        log_exception_with_context(
            "内部请求异常",
            e,
            req_id=req_id,
            method=method,
            timeout=timeout,
            metadata=metadata,
        )
        raise
    finally:
        with internal_request_lock:
            internal_request_waiters.pop(req_id, None)


def is_clangd_response_thread() -> bool:
    return getattr(thread_context, "direction", None) == "Clangd -> VSCode"


def read_document_lines(uri: str) -> list[str] | None:
    with state_lock:
        lines = documents.get(uri)
        if lines is not None:
            return list(lines)

    path = uri_to_path(uri)
    if not path:
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except Exception:
        return None


def is_identifier_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def read_symbol_under_position(uri: str, position: dict[str, int]) -> str | None:
    lines = read_document_lines(uri)
    if not lines:
        return None

    line_no = position.get("line")
    ch = position.get("character")
    if not isinstance(line_no, int) or not isinstance(ch, int):
        return None
    if line_no < 0 or line_no >= len(lines):
        return None

    line = lines[line_no]
    if not line:
        return None

    cursor = ch
    if cursor >= len(line):
        cursor = len(line) - 1
    if cursor < 0:
        return None

    if not is_identifier_char(line[cursor]):
        left = cursor - 1
        right = cursor + 1
        if left >= 0 and is_identifier_char(line[left]):
            cursor = left
        elif right < len(line) and is_identifier_char(line[right]):
            cursor = right
        else:
            return None

    start = cursor
    while start > 0 and is_identifier_char(line[start - 1]):
        start -= 1

    end = cursor + 1
    while end < len(line) and is_identifier_char(line[end]):
        end += 1

    qualified_start = start
    while qualified_start >= 2:
        if line[qualified_start - 1] != ":" or line[qualified_start - 2] != ":":
            break
        prefix_end = qualified_start - 2
        prefix_start = prefix_end
        while prefix_start > 0 and is_identifier_char(line[prefix_start - 1]):
            prefix_start -= 1
        if prefix_start == prefix_end:
            break
        qualified_start = prefix_start

    if qualified_start >= end:
        return None
    return line[qualified_start:end]


def extract_function_signature(lines: list[str], line_no: int) -> str | None:
    if line_no < 0 or line_no >= len(lines):
        return None

    buffer = lines[line_no].strip()
    if not buffer:
        return None

    if FUNCTION_DECL_RE.match(buffer):
        return buffer

    tail = line_no + 1
    while tail < len(lines) and tail < line_no + 6:
        segment = lines[tail].strip()
        if not segment:
            tail += 1
            continue
        buffer = f"{buffer} {segment}".strip()
        if FUNCTION_DECL_RE.match(buffer):
            return buffer
        if "{" in segment or ";" in segment:
            break
        tail += 1

    return buffer if FUNCTION_DECL_RE.match(buffer) else None


def resolve_definition_return_type(uri: str, line_no: int, name: str) -> str | None:
    lines = read_document_lines(uri)
    if not lines:
        return None

    signature = extract_function_signature(lines, line_no)
    if not signature:
        return None

    m = FUNCTION_DECL_RE.match(signature)
    target_name = unqualified_name(name)
    if not m or unqualified_name(m.group("name")) != target_name:
        return None

    return normalize_type_name(m.group("ret"))


def lookup_function_type_via_clangd(uri: str | None, line_no: int | None, char_no: int, version: int, name: str) -> str | None:
    return lookup_function_type_via_clangd_cached(uri, line_no, char_no, version, name, True)


def lookup_function_type_via_clangd_cached(
    uri: str | None,
    line_no: int | None,
    char_no: int,
    version: int,
    name: str,
    allow_request: bool,
) -> str | None:
    if not uri or line_no is None:
        return None

    cache_key = (uri, line_no, char_no, version)
    if cache_key in definition_lookup_cache:
        cached = definition_lookup_cache[cache_key]
        if cached is DEFINITION_LOOKUP_MISS:
            return None
        return cached

    if not allow_request or is_clangd_response_thread():
        return None

    response = request_clangd("textDocument/definition", {
        "textDocument": {"uri": uri},
        "position": {"line": line_no, "character": char_no},
    }, metadata={
        "kind": "definition_lookup",
        "cache_key": cache_key,
        "name": name,
    })
    result_type = None

    if response and "result" in response:
        result = response.get("result")
        locations = result if isinstance(result, list) else [result]
        for location in locations:
            if not isinstance(location, dict):
                continue
            target_uri = location.get("uri")
            target_range = location.get("range", {})
            start = target_range.get("start", {})
            target_line = start.get("line")
            if not target_uri or not isinstance(target_line, int):
                continue
            result_type = resolve_definition_return_type(target_uri, target_line, name)
            if result_type:
                break

    if result_type is None:
        hover_response = request_clangd("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line_no, "character": char_no},
        }, metadata={
            "kind": "hover_lookup",
            "cache_key": cache_key,
            "name": name,
        })
        if hover_response and "result" in hover_response:
            result_type = resolve_hover_return_type(hover_response.get("result", {}), name)

    if result_type is not None:
        definition_lookup_cache[cache_key] = result_type
    else:
        definition_lookup_cache[cache_key] = DEFINITION_LOOKUP_MISS
    return result_type


def handle_internal_definition_response(meta: dict[str, Any], msg: dict[str, Any]):
    cache_key = meta.get("cache_key")
    name = meta.get("name")
    if not isinstance(cache_key, tuple) or not isinstance(name, str):
        return

    result = msg.get("result")
    locations = result if isinstance(result, list) else [result]
    for location in locations:
        if not isinstance(location, dict):
            continue
        target_uri = location.get("uri")
        target_range = location.get("range", {})
        start = target_range.get("start", {})
        target_line = start.get("line")
        if not target_uri or not isinstance(target_line, int):
            continue
        result_type = resolve_definition_return_type(target_uri, target_line, name)
        if result_type is not None:
            definition_lookup_cache[cache_key] = result_type
            return


def handle_internal_hover_response(meta: dict[str, Any], msg: dict[str, Any]):
    cache_key = meta.get("cache_key")
    name = meta.get("name")
    if not isinstance(cache_key, tuple) or not isinstance(name, str):
        return

    result = msg.get("result")
    if not isinstance(result, dict):
        return

    result_type = resolve_hover_return_type(result, name)
    if result_type is not None:
        definition_lookup_cache[cache_key] = result_type

# ==================== 缓存清理 ====================

def trim_analysis_cache_locked():
    if len(analysis_cache) <= MAX_ANALYSIS_CACHE:
        return

    victims = sorted(
        analysis_cache.items(),
        key=lambda item: item[1][2],
    )

    remove_count = len(analysis_cache) - MAX_ANALYSIS_CACHE
    for uri, _ in victims[:remove_count]:
        analysis_cache.pop(uri, None)


def cleanup_request_caches_if_needed():
    global message_counter

    with state_lock:
        message_counter += 1
        if message_counter % CLEANUP_INTERVAL_MESSAGES != 0:
            return

        now = time.monotonic()

        for req_id, req in list(inlay_hint_requests.items()):
            if now - req.get("time", now) > REQUEST_TTL_SECONDS:
                inlay_hint_requests.pop(req_id, None)

        for req_id, req in list(hover_requests.items()):
            if now - req.get("time", now) > REQUEST_TTL_SECONDS:
                hover_requests.pop(req_id, None)

        for req_id, req in list(definition_requests.items()):
            if now - req.get("time", now) > REQUEST_TTL_SECONDS:
                definition_requests.pop(req_id, None)

        for req_id, req in list(document_highlight_requests.items()):
            if now - req.get("time", now) > REQUEST_TTL_SECONDS:
                document_highlight_requests.pop(req_id, None)

        for req_id, req_time in list(semantic_request_ids.items()):
            if now - req_time > REQUEST_TTL_SECONDS:
                semantic_request_ids.pop(req_id, None)

        for req_id, meta in list(internal_request_metadata.items()):
            if now - meta.get("time", now) > REQUEST_TTL_SECONDS:
                internal_request_metadata.pop(req_id, None)


# ==================== 文档同步 ====================

def split_lines(text: str) -> list[str]:
    return text.split("\n")


def invalidate_uri_caches(uri: str):
    analysis_cache.pop(uri, None)

    stale_definition_keys = [
        key for key in definition_lookup_cache
        if isinstance(key, tuple) and len(key) >= 1 and key[0] == uri
    ]
    for key in stale_definition_keys:
        definition_lookup_cache.pop(key, None)


def apply_change(lines: list[str], change: dict[str, Any]) -> list[str]:
    """
    应用 VS Code 发来的 didChange。
    注意：LSP character 是 UTF-16 code unit。
    当前实现按 Python 字符索引处理，对纯 ASCII C++ 主体足够。
    """
    text = change.get("text", "")

    # 全量更新
    if "range" not in change:
        return split_lines(text)

    if not lines:
        lines = [""]

    r = change["range"]
    start = r["start"]
    end = r["end"]

    start_line = start["line"]
    start_char = start["character"]
    end_line = end["line"]
    end_char = end["character"]

    while len(lines) <= max(start_line, end_line):
        lines.append("")

    before = lines[start_line][:start_char]
    after = lines[end_line][end_char:]

    new_lines = text.split("\n")

    if len(new_lines) == 1:
        replacement = [before + new_lines[0] + after]
    else:
        replacement = new_lines
        replacement[0] = before + replacement[0]
        replacement[-1] = replacement[-1] + after

    return lines[:start_line] + replacement + lines[end_line + 1:]


# ==================== 自定义类型推断 ====================

def strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        inner = expr[1:-1].strip()
        if not inner:
            break
        balance = 0
        valid = True
        for idx, ch in enumerate(expr):
            if ch == "(":
                balance += 1
            elif ch == ")":
                balance -= 1
                if balance == 0 and idx != len(expr) - 1:
                    valid = False
                    break
        if not valid or balance != 0:
            break
        expr = inner
    return expr


def normalize_type_name(type_name: str | None) -> str | None:
    if not type_name:
        return None

    clean = type_name.strip()
    if not clean:
        return None

    clean = clean.replace("&", " ")
    clean = clean.replace("*", " ")
    clean = " ".join(clean.split())

    tokens = [token for token in clean.split() if token not in {"const", "volatile"}]
    if not tokens:
        return None

    normalized = " ".join(tokens)
    if normalized in TYPE_INFO_MAP:
        return normalized

    return TYPE_ALIAS_MAP.get(normalized)


def unqualified_name(name: str | None) -> str | None:
    if not name:
        return None

    clean = name.strip()
    if not clean:
        return None

    while clean.startswith("::"):
        clean = clean[2:]

    if not clean:
        return None

    return clean.rsplit("::", 1)[-1]


def unqualified_name_offset(name: str | None) -> int:
    if not name:
        return 0

    clean = name.strip()
    if not clean:
        return 0

    marker = clean.rfind("::")
    if marker < 0:
        return 0

    return marker + 2


def strip_template_suffix(name: str | None) -> str | None:
    if not name:
        return None

    clean = name.strip()
    if not clean:
        return None

    marker = clean.find("<")
    if marker >= 0:
        clean = clean[:marker]

    return clean


def extract_hover_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""

    contents = result.get("contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value")
        return value if isinstance(value, str) else ""
    if isinstance(contents, list):
        parts: list[str] = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                value = item.get("value")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return ""


FUNCTION_SIGNATURE_RE = re.compile(
    r"(?P<ret>[A-Za-z_:\s*&<>]+?)\s+(?P<name>[~A-Za-z_][A-Za-z0-9_:<>]*)\s*\(",
)
HOVER_RESULT_TYPE_RE = re.compile(r"→\s+`([^`]+)`")


def resolve_hover_return_type(result: dict[str, Any], name: str) -> str | None:
    hover_text = extract_hover_text(result)
    if not hover_text:
        return None

    target_name = strip_template_suffix(unqualified_name(name))
    if not target_name:
        return None

    direct_result = HOVER_RESULT_TYPE_RE.search(hover_text)
    if direct_result is not None:
        result_type = normalize_type_name(direct_result.group(1))
        if result_type is not None:
            return result_type

    for match in FUNCTION_SIGNATURE_RE.finditer(hover_text):
        candidate_name = strip_template_suffix(unqualified_name(match.group("name")))
        if candidate_name != target_name:
            continue

        result_type = normalize_type_name(match.group("ret"))
        if result_type is not None:
            return result_type

    return None


def get_expr_type(type_name: str | None) -> ExprType | None:
    normalized = normalize_type_name(type_name)
    if not normalized:
        return None

    meta = TYPE_INFO_MAP.get(normalized)
    if not meta:
        return None

    return ExprType(
        name=normalized,
        kind=meta["kind"],
        bits=meta["bits"],
        signed=meta["signed"],
    )


def promote_int_type(bits: int, is_signed: bool) -> str | None:
    if is_signed:
        return SIGNED_INT_BY_BITS.get(bits)
    return UNSIGNED_INT_BY_BITS.get(bits)


def promote_float_type(bits: int) -> str | None:
    return FLOAT_BY_BITS.get(bits)


def max_int_value_for_type(expr_type: ExprType) -> int | None:
    if expr_type.kind != "int":
        return None
    if expr_type.signed:
        return (1 << (expr_type.bits - 1)) - 1
    return (1 << expr_type.bits) - 1


def coerce_literal_type(node: ExprNode, other_type_name: str | None) -> str | None:
    literal = node.literal_info
    if literal is None or not literal.standard:
        return node.type_name

    other = get_expr_type(other_type_name)
    if other is None:
        return node.type_name

    if literal.kind == "int":
        if literal.suffix:
            return node.type_name
        if other.kind != "int" or literal.int_value is None:
            return node.type_name
        max_value = max_int_value_for_type(other)
        if max_value is None or literal.int_value > max_value:
            return node.type_name
        return other.name

    if literal.kind == "float":
        if literal.suffix:
            return node.type_name
        if other.kind == "float" and other.name == "f32":
            return other.name

    return node.type_name


def infer_binary_result_type(op: str, left_type: str | None, right_type: str | None) -> tuple[str | None, str | None]:
    left = get_expr_type(left_type)
    right = get_expr_type(right_type)
    if left is None or right is None:
        return None, None

    if left.kind == "float" or right.kind == "float":
        bits = max(left.bits, right.bits)
        result = promote_float_type(bits)
        return result, None

    if left.signed != right.signed:
        return None, f"不支持混合有符号与无符号整数: {left.name} {op} {right.name}"

    bits = max(left.bits, right.bits)
    if left.name in {"isize", "usize"} and left.bits == bits:
        return left.name, None
    if right.name in {"isize", "usize"} and right.bits == bits:
        return right.name, None

    result = promote_int_type(bits, bool(left.signed))
    return result, None


def read_identifier(expr: str, start: int) -> tuple[str, int] | None:
    if start >= len(expr):
        return None

    ch = expr[start]
    if not (ch.isalpha() or ch == "_"):
        return None

    pos = start + 1
    while pos < len(expr):
        ch = expr[pos]
        if ch.isalnum() or ch == "_":
            pos += 1
            continue
        if expr.startswith("::", pos):
            pos += 2
            if pos >= len(expr):
                break
            next_ch = expr[pos]
            if not (next_ch.isalpha() or next_ch == "_"):
                break
            pos += 1
            while pos < len(expr):
                nested = expr[pos]
                if nested.isalnum() or nested == "_":
                    pos += 1
                    continue
                break
            continue
        break

    return expr[start:pos], pos


def read_atom(expr: str, start: int) -> tuple[str, int] | None:
    if start >= len(expr):
        return None

    ident = read_identifier(expr, start)
    if ident is not None:
        return ident

    pos = start
    while pos < len(expr):
        ch = expr[pos]
        if ch.isspace() or ch in "+-*/()<>,":
            break
        pos += 1

    if pos == start:
        return None

    return expr[start:pos], pos


def infer_atom_type(text: str, known_vars: dict[str, str]) -> str | None:
    text = text.strip()
    if not text:
        return None

    m = LITERAL_WITH_SUFFIX_RE.match(text)
    if m:
        suffix = m.group("suffix")
        return SUFFIX_TYPE_MAP.get(suffix)

    m = STANDARD_FLOAT_LITERAL_RE.match(text)
    if m:
        return infer_standard_float_type(m.group("suffix"))

    m = STANDARD_INT_LITERAL_RE.match(text)
    if m:
        return infer_standard_int_type(
            m.group("body"),
            m.group("suffix"),
        )

    m = IDENT_RE.match(text)
    if m:
        name = m.group(1)
        return normalize_type_name(known_vars.get(name))

    return normalize_type_name(text)


def infer_atom_node(text: str, known_vars: dict[str, str], start: int, end: int) -> ExprNode:
    stripped = text.strip()
    type_name = infer_atom_type(stripped, known_vars)
    literal_info = None

    suffix_match = LITERAL_WITH_SUFFIX_RE.match(stripped)
    if suffix_match:
        suffix = suffix_match.group("suffix") or ""
        literal_kind = "float" if suffix.lower().startswith("f") else "int"
        literal_info = LiteralInfo(kind=literal_kind, standard=False, suffix=suffix.lower())
    else:
        float_match = STANDARD_FLOAT_LITERAL_RE.match(stripped)
        if float_match:
            literal_info = LiteralInfo(
                kind="float",
                standard=True,
                suffix=(float_match.group("suffix") or "").lower(),
            )
        else:
            int_match = STANDARD_INT_LITERAL_RE.match(stripped)
            if int_match:
                int_value = None
                try:
                    int_value, _ = parse_int_literal_value(int_match.group("body"))
                except Exception:
                    int_value = None
                literal_info = LiteralInfo(
                    kind="int",
                    standard=True,
                    suffix=(int_match.group("suffix") or "").lower(),
                    int_value=int_value,
                )

    return ExprNode(
        type_name=type_name,
        start=start,
        end=end,
        diagnostics=[],
        literal_info=literal_info,
    )


def collect_known_functions(lines: list[str]) -> dict[str, str]:
    known_functions: dict[str, str] = {}

    for line in lines:
        m = FUNCTION_DECL_RE.match(line.strip())
        if not m:
            continue

        name = m.group("name")
        if name in {"if", "for", "while", "switch", "return"}:
            continue

        ret_type = normalize_type_name(m.group("ret"))
        if not ret_type:
            continue

        known_functions[name] = ret_type

    return known_functions


class ExprParser:
    def __init__(
        self,
        expr: str,
        known_vars: dict[str, str],
        known_functions: dict[str, str],
        uri: str | None = None,
        line_no: int | None = None,
        base_char: int = 0,
        version: int = 0,
        allow_active_query: bool = True,
    ):
        self.expr = expr
        self.known_vars = known_vars
        self.known_functions = known_functions
        self.uri = uri
        self.line_no = line_no
        self.base_char = base_char
        self.version = version
        self.allow_active_query = allow_active_query
        self.pos = 0

    def skip_ws(self):
        while self.pos < len(self.expr) and self.expr[self.pos].isspace():
            self.pos += 1

    def peek(self) -> str | None:
        if self.pos >= len(self.expr):
            return None
        return self.expr[self.pos]

    def consume(self, text: str) -> bool:
        if self.expr.startswith(text, self.pos):
            self.pos += len(text)
            return True
        return False

    def parse(self) -> ExprResult:
        node = self.parse_add_sub()
        if node is None:
            return ExprResult(None, [])

        self.skip_ws()
        if self.pos != len(self.expr):
            return ExprResult(None, node.diagnostics)

        return ExprResult(node.type_name, node.diagnostics)

    def parse_add_sub(self) -> ExprNode | None:
        node = self.parse_mul_div()
        if node is None:
            return None

        while True:
            self.skip_ws()
            op = self.peek()
            if op not in {"+", "-"}:
                break

            self.pos += 1
            right = self.parse_mul_div()
            if right is None:
                return node

            node = self.combine_binary(node, right, op)

        return node

    def parse_mul_div(self) -> ExprNode | None:
        node = self.parse_unary()
        if node is None:
            return None

        while True:
            self.skip_ws()
            op = self.peek()
            if op not in {"*", "/"}:
                break

            self.pos += 1
            right = self.parse_unary()
            if right is None:
                return node

            node = self.combine_binary(node, right, op)

        return node

    def parse_unary(self) -> ExprNode | None:
        self.skip_ws()
        start = self.pos
        op = self.peek()
        if op not in {"+", "-"}:
            return self.parse_primary()

        self.pos += 1
        inner = self.parse_unary()
        if inner is None:
            return None

        return ExprNode(
            type_name=inner.type_name,
            start=start,
            end=inner.end,
            diagnostics=list(inner.diagnostics),
            literal_info=inner.literal_info,
        )

    def parse_primary(self) -> ExprNode | None:
        self.skip_ws()
        start = self.pos

        if self.consume("static_cast"):
            return self.parse_static_cast(start)

        if self.consume("("):
            inner = self.parse_add_sub()
            self.skip_ws()
            if self.peek() == ")":
                self.pos += 1

            if inner is None:
                return None

            node = ExprNode(
                type_name=inner.type_name,
                start=start,
                end=self.pos,
                diagnostics=list(inner.diagnostics),
                literal_info=inner.literal_info,
            )
            return self.parse_postfix(node)

        ident = read_identifier(self.expr, self.pos)
        if ident is not None:
            name, end = ident
            self.pos = end
            self.skip_ws()
            if self.peek() == "(":
                node = self.parse_call(start, name, start)
                return self.parse_postfix(node)

            node = infer_atom_node(name, self.known_vars, start, end)
            return self.parse_postfix(node)

        atom = read_atom(self.expr, self.pos)
        if atom is None:
            return None

        text, end = atom
        self.pos = end
        node = infer_atom_node(text, self.known_vars, start, end)
        return self.parse_postfix(node)

    def parse_postfix(self, node: ExprNode) -> ExprNode:
        while True:
            self.skip_ws()
            if self.consume("->") or self.consume("."):
                self.skip_ws()
                member_start = self.pos
                ident = read_identifier(self.expr, self.pos)
                if ident is None:
                    return ExprNode(
                        type_name=None,
                        start=node.start,
                        end=self.pos,
                        diagnostics=list(node.diagnostics),
                        literal_info=None,
                    )

                member_name, member_end = ident
                self.pos = member_end
                self.skip_ws()
                if self.peek() != "(":
                    return ExprNode(
                        type_name=None,
                        start=node.start,
                        end=self.pos,
                        diagnostics=list(node.diagnostics),
                        literal_info=None,
                    )

                member_node = self.parse_call(node.start, member_name, member_start, True)
                node = ExprNode(
                    type_name=member_node.type_name,
                    start=node.start,
                    end=member_node.end,
                    diagnostics=list(node.diagnostics) + list(member_node.diagnostics),
                    literal_info=None,
                )
                continue

            break

        return node

    def parse_call(
        self,
        start: int,
        name: str,
        lookup_start: int | None = None,
        force_active_query: bool = False,
    ) -> ExprNode:
        diagnostics: list[ExprDiagnostic] = []
        result_type = self.known_functions.get(name)
        if result_type is None:
            result_type = self.known_functions.get(unqualified_name(name) or "")
        lookup_origin = start if lookup_start is None else lookup_start
        lookup_char = self.base_char + lookup_origin + unqualified_name_offset(name)
        should_query_clangd = force_active_query or "::" in name or result_type is None
        if self.peek() != "(":
            return ExprNode(
                type_name=result_type,
                start=start,
                end=self.pos,
                diagnostics=diagnostics,
                literal_info=None,
            )

        self.pos += 1
        self.skip_ws()
        if self.peek() == ")":
            self.pos += 1
            if should_query_clangd:
                active_result = lookup_function_type_via_clangd_cached(
                    self.uri,
                    self.line_no,
                    lookup_char,
                    self.version,
                    name,
                    self.allow_active_query,
                )
                if active_result is not None:
                    result_type = active_result
            return ExprNode(
                type_name=result_type,
                start=start,
                end=self.pos,
                diagnostics=diagnostics,
                literal_info=None,
            )

        while self.pos < len(self.expr):
            arg = self.parse_add_sub()
            if arg is not None:
                diagnostics.extend(arg.diagnostics)

            self.skip_ws()
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                self.skip_ws()
                continue
            if ch == ")":
                self.pos += 1
                break
            break

        if should_query_clangd:
            active_result = lookup_function_type_via_clangd_cached(
                self.uri,
                self.line_no,
                lookup_char,
                self.version,
                name,
                self.allow_active_query,
            )
            if active_result is not None:
                result_type = active_result

        return ExprNode(
            type_name=result_type,
            start=start,
            end=self.pos,
            diagnostics=diagnostics,
            literal_info=None,
        )

    def parse_static_cast(self, start: int) -> ExprNode | None:
        self.skip_ws()
        if self.peek() != "<":
            return None

        self.pos += 1
        type_start = self.pos
        depth = 1
        while self.pos < len(self.expr):
            ch = self.expr[self.pos]
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
                if depth == 0:
                    break
            self.pos += 1

        if self.pos >= len(self.expr):
            return None

        target_type = normalize_type_name(self.expr[type_start:self.pos])
        self.pos += 1

        self.skip_ws()
        if self.peek() != "(":
            return None

        self.pos += 1
        depth = 1
        expr_start = self.pos
        while self.pos < len(self.expr):
            ch = self.expr[self.pos]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            self.pos += 1

        inner_text = self.expr[expr_start:self.pos]
        if inner_text.strip():
            inner_parser = ExprParser(
                inner_text,
                self.known_vars,
                self.known_functions,
                self.uri,
                self.line_no,
                self.base_char + expr_start,
                self.version,
            )
            inner_parser.parse()

        if self.pos < len(self.expr) and self.expr[self.pos] == ")":
            self.pos += 1

        return ExprNode(
            type_name=target_type,
            start=start,
            end=self.pos,
            diagnostics=[],
            literal_info=None,
        )

    def combine_binary(self, left: ExprNode, right: ExprNode, op: str) -> ExprNode:
        diagnostics = list(left.diagnostics)
        diagnostics.extend(right.diagnostics)

        left_type_name = coerce_literal_type(left, right.type_name)
        right_type_name = coerce_literal_type(right, left.type_name)
        result_type, message = infer_binary_result_type(op, left_type_name, right_type_name)
        if message:
            diagnostics.append(
                ExprDiagnostic(
                    message=message,
                    start=left.start,
                    end=right.end,
                )
            )

        return ExprNode(
            type_name=result_type,
            start=left.start,
            end=right.end,
            diagnostics=diagnostics,
            literal_info=None,
        )


def decorate_type_by_auto_decl(base_type: str, auto_decl: str, ptr_ref: str | None) -> str:
    typ = base_type

    if auto_decl.strip().startswith("const "):
        typ = "const " + typ

    ptr_ref = (ptr_ref or "").strip()
    if ptr_ref:
        typ = typ + " " + ptr_ref

    return typ


def parse_int_literal_value(body: str) -> tuple[int, str]:
    clean = body.replace("'", "")

    if clean.startswith(("0x", "0X")):
        return int(clean, 16), "hex"

    if clean.startswith(("0b", "0B")):
        return int(clean, 2), "bin"

    return int(clean, 10), "dec"


def infer_standard_int_type(body: str, suffix: str | None) -> str | None:
    try:
        value, base_kind = parse_int_literal_value(body)
    except Exception:
        return None

    suffix = (suffix or "").lower()

    is_unsigned = "u" in suffix
    has_long = "l" in suffix

    # 123u / 123U
    if is_unsigned and not has_long:
        if value <= U32_MAX:
            return "u32"
        if value <= U64_MAX:
            return "u64"
        return None

    # 123l / 123ll
    if has_long and not is_unsigned:
        if value <= I64_MAX:
            return "i64"
        if value <= U64_MAX:
            return "u64"
        return None

    # 123ul / 123ull
    if has_long and is_unsigned:
        if value <= U64_MAX:
            return "u64"
        return None

    # 无后缀十进制：C++ 优先 int -> long -> long long
    if base_kind == "dec":
        if value <= I32_MAX:
            return "i32"
        if value <= I64_MAX:
            return "i64"
        return None

    # 无后缀 hex/bin：C++ 会在 signed/unsigned 间选择
    if value <= I32_MAX:
        return "i32"
    if value <= U32_MAX:
        return "u32"
    if value <= I64_MAX:
        return "i64"
    if value <= U64_MAX:
        return "u64"

    return None


def infer_standard_float_type(suffix: str | None) -> str | None:
    suffix = (suffix or "").lower()

    # 3.14f
    if suffix == "f":
        return "f32"

    # 3.14
    if suffix == "":
        return "f64"

    # 3.14L 是 long double，你的类型模块里暂时没有对应别名
    if suffix == "l":
        return "long double"

    return None


def infer_expr_type(expr: str, known_vars: dict[str, str]) -> str | None:
    return infer_expr_result(expr, known_vars, {}).type_name


def infer_expr_result(
    expr: str,
    known_vars: dict[str, str],
    known_functions: dict[str, str],
    uri: str | None = None,
    line_no: int | None = None,
    base_char: int = 0,
    version: int = 0,
    allow_active_query: bool = True,
) -> ExprResult:
    expr = strip_outer_parens(expr)
    parser = ExprParser(
        expr,
        known_vars,
        known_functions,
        uri,
        line_no,
        base_char,
        version,
        allow_active_query,
    )
    return parser.parse()


def analyze_lines_uncached(
    lines: list[str],
    uri: str | None = None,
    version: int = 0,
    allow_active_query: bool = True,
) -> list[dict[str, Any]]:
    known_vars: dict[str, str] = {}
    known_functions = build_project_function_index(uri)
    known_functions.update(collect_known_functions(lines))
    results: list[dict[str, Any]] = []

    for line_no, line in enumerate(lines):
        m = AUTO_ASSIGN_RE.match(line)
        if not m:
            continue

        name = m.group("name")
        expr = m.group("expr")
        auto_decl = m.group("auto_decl")
        ptr_ref = m.group("ptr_ref")
        expr_start = m.start("expr")

        expr_result = infer_expr_result(
            expr,
            known_vars,
            known_functions,
            uri=uri,
            line_no=line_no,
            base_char=expr_start,
            version=version,
            allow_active_query=allow_active_query,
        )
        base_type = expr_result.type_name
        display_type = None
        if base_type:
            display_type = decorate_type_by_auto_decl(base_type, auto_decl, ptr_ref)

        var_start = m.start("name")
        var_end = m.end("name")

        if display_type:
            known_vars[name] = display_type

        diagnostics = []
        for diag in expr_result.diagnostics:
            diagnostics.append({
                "line": line_no,
                "start": expr_start + diag.start,
                "end": expr_start + max(diag.end, diag.start + 1),
                "message": diag.message,
                "severity": diag.severity,
            })

        if display_type is None and not diagnostics:
            continue

        results.append({
            "line": line_no,
            "var_start": var_start,
            "var_end": var_end,
            "name": name,
            "type": display_type,
            "line_text": line,
            "diagnostics": diagnostics,
        })

    return results


def get_analysis(uri: str, allow_active_query: bool = True) -> list[dict[str, Any]]:
    """
    带版本缓存的文档分析。
    同一版本内，inlayHint / hover 复用分析结果，不重复全文件扫描。
    """
    now = time.monotonic()

    with state_lock:
        version = document_versions.get(uri, 0)
        cached = analysis_cache.get(uri)

        if cached and cached[0] == version:
            analysis_cache[uri] = (cached[0], cached[1], now)
            return cached[1]

        lines = documents.get(uri)
        if not lines:
            return []

        # 拷贝一份，避免分析时被 didChange 改动
        lines_snapshot = list(lines)

    if not allow_active_query:
        return analyze_lines_uncached(
            lines_snapshot,
            uri,
            version,
            allow_active_query=False,
        )

    result = analyze_lines_uncached(lines_snapshot, uri, version)

    with state_lock:
        current_version = document_versions.get(uri, 0)

        # 只有版本没变才写入缓存
        if current_version == version:
            analysis_cache[uri] = (version, result, time.monotonic())
            trim_analysis_cache_locked()

    return result


def warm_analysis(uri: str):
    try:
        get_analysis(uri)
    except Exception as e:
        print(f"Warm analysis error for {uri}: {e}", file=sys.stderr, flush=True)


def recompute_analysis_without_requests(uri: str) -> list[dict[str, Any]]:
    with state_lock:
        version = document_versions.get(uri, 0)
        lines = documents.get(uri)
        if not lines:
            return []
        lines_snapshot = list(lines)

    return analyze_lines_uncached(
        lines_snapshot,
        uri,
        version,
        allow_active_query=False,
    )


def make_custom_inlay_hints(uri: str, req_range: dict[str, Any], allow_active_query: bool = True) -> list[dict[str, Any]]:
    items = get_analysis(uri, allow_active_query=allow_active_query)
    if not items:
        return []

    start_line = req_range["start"]["line"]
    end_line = req_range["end"]["line"]

    hints: list[dict[str, Any]] = []

    for item in items:
        if not item.get("type"):
            continue

        line_no = item["line"]
        if line_no < start_line or line_no > end_line:
            continue

        hints.append({
            "kind": 1,
            "label": [{"value": f": {item['type']}"}],
            "paddingLeft": False,
            "paddingRight": False,
            "position": {
                "line": line_no,
                "character": item["var_end"],
            },
        })

    return hints


def patch_inlay_hint_response(msg: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    result = msg.get("result")
    if not isinstance(result, list):
        return msg

    custom_hints = make_custom_inlay_hints(req["uri"], req["range"], allow_active_query=False)
    if not custom_hints:
        return msg

    custom_by_position = {
        (
            h["position"]["line"],
            h["position"]["character"],
            h["kind"],
        ): h
        for h in custom_hints
    }

    # 1. 如果 clangd 已经在同位置给了类型提示，直接替换 label
    for h in result:
        if not isinstance(h, dict):
            continue

        pos = h.get("position", {})
        key = (
            pos.get("line"),
            pos.get("character"),
            h.get("kind"),
        )

        custom = custom_by_position.pop(key, None)
        if custom is None:
            continue

        h["label"] = custom["label"]
        h["paddingLeft"] = custom.get("paddingLeft", False)
        h["paddingRight"] = custom.get("paddingRight", False)

    # 2. clangd 没给的位置，追加我们自己的 hint
    for h in custom_by_position.values():
        result.append(h)

    return msg


def make_custom_diagnostics(uri: str, allow_active_query: bool = True) -> list[dict[str, Any]]:
    diagnostics = []

    for item in get_analysis(uri, allow_active_query=allow_active_query):
        for diag in item.get("diagnostics", []):
            diagnostics.append({
                "range": {
                    "start": {
                        "line": diag["line"],
                        "character": diag["start"],
                    },
                    "end": {
                        "line": diag["line"],
                        "character": diag["end"],
                    },
                },
                "severity": diag["severity"],
                "source": "clangd.py",
                "message": diag["message"],
            })

    return diagnostics


def patch_publish_diagnostics_response(msg: dict[str, Any]) -> dict[str, Any]:
    params = msg.get("params")
    if not isinstance(params, dict):
        return msg

    uri = params.get("uri")
    diagnostics = params.get("diagnostics")
    if not uri or not isinstance(diagnostics, list):
        return msg

    custom_diagnostics = make_custom_diagnostics(uri, allow_active_query=False)
    if not custom_diagnostics:
        return msg

    params["diagnostics"] = diagnostics + custom_diagnostics
    return msg

def find_custom_var_at_position(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    line = position["line"]
    ch = position["character"]

    with state_lock:
        cached = analysis_cache.get(uri)
        items = list(cached[1]) if cached else []

    for item in items:
        if item["line"] != line:
            continue

        if item["var_start"] <= ch <= item["var_end"]:
            return item

    return None


def find_custom_var_at_position_uncached(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    line = position["line"]
    ch = position["character"]

    for item in recompute_analysis_without_requests(uri):
        if item["line"] != line:
            continue

        if item["var_start"] <= ch <= item["var_end"]:
            return item

    return None


def read_identifier_under_position(uri: str, position: dict[str, int]) -> str | None:
    return unqualified_name(read_symbol_under_position(uri, position))


def collect_local_auto_declarations(uri: str) -> list[dict[str, Any]]:
    lines = read_document_lines(uri)
    if not lines:
        return []

    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines):
        match = AUTO_ASSIGN_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        items.append({
            "uri": uri,
            "line": line_no,
            "var_start": match.start("name"),
            "var_end": match.end("name"),
            "name": name,
        })
    return items


def find_local_definition_by_name(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    target_name = read_identifier_under_position(uri, position)
    if not target_name:
        return None

    line_no = position.get("line")
    ch = position.get("character")
    if not isinstance(line_no, int) or not isinstance(ch, int):
        return None

    items = collect_local_auto_declarations(uri)
    best_item = None
    for item in items:
        if item.get("name") != target_name:
            continue
        item_line = item.get("line")
        item_start = item.get("var_start")
        if not isinstance(item_line, int) or not isinstance(item_start, int):
            continue
        if item_line > line_no:
            continue
        if item_line == line_no and item_start > ch:
            continue
        if best_item is None:
            best_item = item
            continue
        best_line = best_item.get("line", -1)
        best_start = best_item.get("var_start", -1)
        if item_line > best_line or (item_line == best_line and item_start > best_start):
            best_item = item

    return best_item


def find_local_definition_item(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    item = find_local_definition_by_name(uri, position)
    if item is not None:
        return item
    return find_cross_file_definition_item(uri, position)


def make_definition_location(uri: str, item: dict[str, Any]) -> dict[str, Any]:
    target_uri = item.get("uri") if isinstance(item.get("uri"), str) else uri
    return {
        "uri": target_uri,
        "range": {
            "start": {
                "line": item["line"],
                "character": item["var_start"],
            },
            "end": {
                "line": item["line"],
                "character": item["var_end"],
            },
        },
    }


def is_same_location(lhs: dict[str, Any], rhs: dict[str, Any]) -> bool:
    lhs_start = lhs.get("range", {}).get("start", {})
    rhs_start = rhs.get("range", {}).get("start", {})
    return (
        lhs.get("uri") == rhs.get("uri")
        and lhs_start.get("line") == rhs_start.get("line")
        and lhs_start.get("character") == rhs_start.get("character")
    )


def is_using_alias_location(uri: str, location: dict[str, Any], symbol_name: str | None) -> bool:
    if location.get("uri") != uri or not symbol_name:
        return False

    start = location.get("range", {}).get("start", {})
    line_no = start.get("line")
    if not isinstance(line_no, int):
        return False

    lines = read_document_lines(uri) or []
    if line_no < 0 or line_no >= len(lines):
        return False

    match = USING_SYMBOL_RE.match(lines[line_no].strip())
    if not match:
        return False

    return match.group("name") == symbol_name


def patch_definition_response(msg: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    uri = req.get("uri")
    position = req.get("position")
    if not isinstance(uri, str) or not isinstance(position, dict):
        return msg

    result = msg.get("result")
    cross_file_item = find_cross_file_definition_item(uri, position)
    if isinstance(result, list) and result and cross_file_item is not None:
        cross_file_location = make_definition_location(uri, cross_file_item)
        symbol_name = read_identifier_under_position(uri, position)
        reordered = [cross_file_location]
        saw_cross_file = False
        local_alias_only = True
        for location in result:
            if not isinstance(location, dict):
                continue
            if is_same_location(location, cross_file_location):
                saw_cross_file = True
                continue
            reordered.append(location)
            if not is_using_alias_location(uri, location, symbol_name):
                local_alias_only = False
        if saw_cross_file or local_alias_only:
            msg["result"] = reordered
        return msg

    if result not in (None, []):
        return msg

    item = find_local_definition_item(uri, position)
    if item is None:
        return msg

    msg["result"] = [make_definition_location(uri, item)]
    return msg


def patch_document_highlight_response(msg: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    result = msg.get("result")
    if result not in (None, []):
        return msg

    uri = req.get("uri")
    position = req.get("position")
    if not isinstance(uri, str) or not isinstance(position, dict):
        return msg

    item = find_local_definition_item(uri, position)
    if item is None:
        return msg

    with state_lock:
        lines = list(documents.get(uri, []))
    if not lines:
        read_lines = read_document_lines(uri)
        lines = read_lines or []

    highlights: list[dict[str, Any]] = []
    item_uri = item.get("uri") if isinstance(item.get("uri"), str) else uri
    if item_uri == uri:
        highlights.append({
            "range": make_definition_location(uri, item)["range"],
            "kind": 1,
        })

    token_name = unqualified_name(read_symbol_under_position(uri, position))
    if not token_name:
        token_name = item.get("name")
    if isinstance(token_name, str) and lines:
        token_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token_name)}(?![A-Za-z0-9_])")
        for line_no, line_text in enumerate(lines):
            for match in token_re.finditer(line_text):
                highlights.append({
                    "range": {
                        "start": {"line": line_no, "character": match.start()},
                        "end": {"line": line_no, "character": match.end()},
                    },
                    "kind": 2 if item_uri == uri and line_no == item["line"] and match.start() == item["var_start"] else 1,
                })

    seen: set[tuple[int, int, int]] = set()
    deduped: list[dict[str, Any]] = []
    for highlight in highlights:
        highlight_range = highlight.get("range", {})
        start = highlight_range.get("start", {})
        end = highlight_range.get("end", {})
        key = (
            start.get("line", -1),
            start.get("character", -1),
            end.get("character", -1),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(highlight)

    msg["result"] = deduped
    return msg


def patch_hover_response(msg: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    item = find_custom_var_at_position(req["uri"], req["position"])
    if (not item or not item.get("type")) and req.get("uri"):
        item = find_custom_var_at_position_uncached(req["uri"], req["position"])
    if not item or not item.get("type"):
        return msg

    original_result = msg.get("result")
    if original_result is None:
        return msg

    name = item["name"]
    typ = item["type"]
    line_text = item.get("line_text", "").strip()

    msg["result"] = {
        "contents": {
            "kind": "markdown",
            "value": (
                f"### variable `{name}`  \n\n"
                f"---\n"
                f"Type: `{typ}`  \n\n"
                f"---\n"
                f"```cpp\n"
                f"// patched by clangd.py\n"
                f"{line_text}\n"
                f"```"
            ),
        },
        "range": {
            "start": {
                "line": item["line"],
                "character": item["var_start"],
            },
            "end": {
                "line": item["line"],
                "character": item["var_end"],
            },
        },
    }

    return msg


# ==================== HOOK 主逻辑 ====================

def hook_message(direction: str, msg: dict[str, Any]) -> dict[str, Any]:
    """
    代理层 Hook：
    1. 打印 LSP 日志。
    2. 维护文档内容。
    3. 修改 inlayHint / hover 返回值。
    4. 增加版本缓存、取消请求清理、TTL 清理。
    """
    cleanup_request_caches_if_needed()

    msg_id = msg.get("id", "NO_ID")
    method = msg.get("method", "RESPONSE/UNKNOWN")

    raw_str = json.dumps(msg, ensure_ascii=False)
    if len(raw_str) > 500:
        raw_str = raw_str[:500] + f" ... <TRUNCATED, Total {len(raw_str)} bytes>"

    print(
        f"[{direction}] ID:{msg_id} | METHOD:{method} | RAW: {raw_str}",
        file=sys.stderr,
        flush=True,
    )
    remember_recent_message(direction, msg)

    # ==================== VSCode -> Clangd ====================
    if direction == "VSCode -> Clangd":
        now = time.monotonic()

        if method == "initialize":
            params = msg.get("params", {})
            root_uri = params.get("rootUri")
            root_path = uri_to_path(root_uri) if isinstance(root_uri, str) else None
            if root_path:
                global workspace_root_path
                workspace_root_path = normalize_workspace_path(root_path)
            register_proxy_instance(root_uri if isinstance(root_uri, str) else None, params.get("processId"))

        # didOpen：保存完整文本
        if method == "textDocument/didOpen":
            td = msg.get("params", {}).get("textDocument", {})
            uri = td.get("uri")
            text = td.get("text")
            version = td.get("version", 0)

            if uri and text is not None:
                with state_lock:
                    documents[uri] = split_lines(text)
                    document_versions[uri] = int(version) if isinstance(version, int) else 0
                    invalidate_uri_caches(uri)

        # didChange：增量更新文本，并失效分析缓存
        elif method == "textDocument/didChange":
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            version = td.get("version")

            if uri:
                with state_lock:
                    if uri not in documents:
                        documents[uri] = [""]

                    lines = documents[uri]
                    for change in params.get("contentChanges", []):
                        lines = apply_change(lines, change)

                    documents[uri] = lines

                    if isinstance(version, int):
                        document_versions[uri] = version
                    else:
                        document_versions[uri] = document_versions.get(uri, 0) + 1

                    invalidate_uri_caches(uri)

        # didSave：用磁盘或客户端提供的最终文本刷新缓存，避免保存后仍然使用旧文档快照
        elif method == "textDocument/didSave":
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")

            if uri:
                saved_text = params.get("text")
                if not isinstance(saved_text, str):
                    path = uri_to_path(uri)
                    if path:
                        try:
                            with open(path, "r", encoding="utf-8") as f:
                                saved_text = f.read()
                        except Exception:
                            saved_text = None

                with state_lock:
                    if isinstance(saved_text, str):
                        documents[uri] = split_lines(saved_text)
                    elif uri not in documents:
                        documents[uri] = [""]
                    document_versions[uri] = document_versions.get(uri, 0) + 1
                    invalidate_uri_caches(uri)

        # didClose：释放缓存
        elif method == "textDocument/didClose":
            td = msg.get("params", {}).get("textDocument", {})
            uri = td.get("uri")

            if uri:
                with state_lock:
                    documents.pop(uri, None)
                    document_versions.pop(uri, None)
                    invalidate_uri_caches(uri)

        # cancelRequest：清理被取消请求，防止长时间运行积累
        elif method == "$/cancelRequest":
            cancel_id = msg.get("params", {}).get("id")
            if cancel_id is not None:
                with state_lock:
                    inlay_hint_requests.pop(cancel_id, None)
                    hover_requests.pop(cancel_id, None)
                    definition_requests.pop(cancel_id, None)
                    document_highlight_requests.pop(cancel_id, None)
                    semantic_request_ids.pop(cancel_id, None)

        # 记录 inlayHint 请求
        elif method == "textDocument/inlayHint" and "id" in msg:
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            req_range = params.get("range")

            if uri and req_range:
                with state_lock:
                    inlay_hint_requests[msg["id"]] = {
                        "uri": uri,
                        "range": req_range,
                        "time": now,
                    }
                warm_analysis(uri)

        # 记录 hover 请求
        elif method == "textDocument/hover" and "id" in msg:
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            pos = params.get("position")

            if uri and pos:
                with state_lock:
                    hover_requests[msg["id"]] = {
                        "uri": uri,
                        "position": pos,
                        "time": now,
                    }
                warm_analysis(uri)

        # 记录 definition 请求
        elif method == "textDocument/definition" and "id" in msg:
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            pos = params.get("position")

            if uri and pos:
                with state_lock:
                    definition_requests[msg["id"]] = {
                        "uri": uri,
                        "position": pos,
                        "time": now,
                    }

        # 记录 documentHighlight 请求
        elif method == "textDocument/documentHighlight" and "id" in msg:
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            pos = params.get("position")

            if uri and pos:
                with state_lock:
                    document_highlight_requests[msg["id"]] = {
                        "uri": uri,
                        "position": pos,
                        "time": now,
                    }

        # semanticTokens 观察逻辑
        if "semanticTokens" in method:
            if "id" in msg:
                with state_lock:
                    semantic_request_ids[msg["id"]] = now

                print(
                    f">>> [!] 拦截到语义令牌请求 ID={msg['id']}",
                    file=sys.stderr,
                    flush=True,
                )

    # ==================== Clangd -> VSCode ====================
    elif direction == "Clangd -> VSCode":
        req_id = msg.get("id")
        if isinstance(req_id, str) and req_id.startswith(INTERNAL_REQUEST_PREFIX):
            with internal_request_lock:
                meta = internal_request_metadata.pop(req_id, None)
                waiter = internal_request_waiters.get(req_id)
                if meta and meta.get("kind") == "definition_lookup":
                    handle_internal_definition_response(meta, msg)
                if meta and meta.get("kind") == "hover_lookup":
                    handle_internal_hover_response(meta, msg)
                if waiter is not None:
                    waiter["response"] = msg
                    waiter["event"].set()
            return None

        if method == "textDocument/publishDiagnostics":
            msg = patch_publish_diagnostics_response(msg)

        # 修改 inlayHint 响应
        req = None
        with state_lock:
            if req_id in inlay_hint_requests:
                req = inlay_hint_requests.pop(req_id, None)

        if req is not None:
            msg = patch_inlay_hint_response(msg, req)

        # 修改 hover 响应
        req = None
        with state_lock:
            if req_id in hover_requests:
                req = hover_requests.pop(req_id, None)
        if req is not None:
            msg = patch_hover_response(msg, req)

        # 修改 definition 响应
        req = None
        with state_lock:
            if req_id in definition_requests:
                req = definition_requests.pop(req_id, None)
        if req is not None:
            msg = patch_definition_response(msg, req)

        # 修改 documentHighlight 响应
        req = None
        with state_lock:
            if req_id in document_highlight_requests:
                req = document_highlight_requests.pop(req_id, None)
        if req is not None:
            msg = patch_document_highlight_response(msg, req)

        # semanticTokens 观察逻辑
        semantic_hit = False
        with state_lock:
            if req_id in semantic_request_ids:
                semantic_request_ids.pop(req_id, None)
                semantic_hit = True

        if semantic_hit:
            result = msg.get("result", {})

            if isinstance(result, dict):
                if "data" in result:
                    data_len = len(result.get("data", []))
                elif "edits" in result:
                    data_len = sum(
                        len(edit.get("data", []))
                        for edit in result.get("edits", [])
                        if isinstance(edit, dict)
                    )
                else:
                    data_len = 0
            else:
                data_len = 0

            print(
                f">>> [!] 拦截到语义令牌响应，长度: {data_len}",
                file=sys.stderr,
                flush=True,
            )

    return msg


# ==================== LSP 转发引擎 ====================

def send_vscode_payload(msg: dict[str, Any]):
    try:
        write_payload_with_lock(sys.stdout.buffer, msg, client_write_lock)
    except Exception as e:
        log_exception_with_context(
            "写回 VSCode 异常",
            e,
            summary=summarize_lsp_message(msg),
        )
        raise


def maybe_short_circuit_client_request(msg: dict[str, Any]) -> bool:
    method = msg.get("method")
    req_id = msg.get("id")
    if method not in {"textDocument/definition", "textDocument/documentHighlight"} or req_id is None:
        return False

    params = msg.get("params", {})
    td = params.get("textDocument", {})
    uri = td.get("uri")
    position = params.get("position")
    if not isinstance(uri, str) or not isinstance(position, dict):
        return False

    item = find_local_definition_item(uri, position)
    if item is None:
        return False

    if method == "textDocument/definition":
        with state_lock:
            definition_requests.pop(req_id, None)
        send_vscode_payload({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": [make_definition_location(uri, item)],
        })
        return True

    with state_lock:
        document_highlight_requests.pop(req_id, None)
    response = patch_document_highlight_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": [],
    }, {
        "uri": uri,
        "position": position,
        "time": time.monotonic(),
    })
    send_vscode_payload(response)
    return True


def transfer_with_hook(fd_in: int, fd_out: int, direction: str):
    """带 LSP 报文解析的 I/O 引擎"""
    last_summary = "无"
    last_body_len = 0
    try:
        thread_context.direction = direction
        # 用 dup 后的 fd 构造局部 file object, 避免线程退出时把共享 pipe 直接关掉.
        f_in = os.fdopen(os.dup(fd_in), "rb", buffering=0)
        f_out = os.fdopen(os.dup(fd_out), "wb", buffering=0)

        while not exit_event.is_set():
            content_length = None

            # 1. 读取 LSP header
            while True:
                line = f_in.readline()
                if not line:
                    log_proxy_message(
                        f"转发线程读到 EOF: direction={direction}, fd_in={fd_in}, fd_out={fd_out}, last_summary={last_summary}, exit_set={exit_event.is_set()}"
                    )
                    request_shutdown(f"eof:{direction}")
                    return

                if line in (b"\r\n", b"\n"):
                    break

                lower = line.lower()
                if lower.startswith(b"content-length:"):
                    try:
                        content_length = int(line.split(b":", 1)[1].strip())
                    except Exception:
                        content_length = None

            if content_length is None:
                continue

            # 2. 读取 JSON body
            body = read_exactly(f_in, content_length)
            if len(body) < content_length:
                log_proxy_message(
                    f"转发线程读到短包: direction={direction}, expected={content_length}, actual={len(body)}, last_summary={last_summary}, exit_set={exit_event.is_set()}"
                )
                request_shutdown(f"short-read:{direction}")
                break

            # 3. 解析并 Hook
            try:
                msg = json.loads(body.decode("utf-8"))
                last_summary = summarize_lsp_message(msg)
                last_body_len = len(body)
            except Exception as e:
                log_exception_with_context(
                    "JSON 解码异常",
                    e,
                    direction=direction,
                    body_len=len(body),
                )

                if direction == "VSCode -> Clangd":
                    with clangd_write_lock:
                        write_raw_payload(f_out, body)
                else:
                    with client_write_lock:
                        write_raw_payload(f_out, body)
                continue

            try:
                msg = hook_message(direction, msg)
            except Exception as e:
                log_exception_with_context(
                    "Hook 异常",
                    e,
                    direction=direction,
                    summary=last_summary,
                )

            if msg is None:
                continue

            if direction == "VSCode -> Clangd" and maybe_short_circuit_client_request(msg):
                continue

            # 4. 重组 LSP 消息
            new_body = json.dumps(
                msg,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            last_summary = summarize_lsp_message(msg)
            last_body_len = len(new_body)

            new_header = f"Content-Length: {len(new_body)}\r\n\r\n".encode("utf-8")
            if direction == "VSCode -> Clangd":
                with clangd_write_lock:
                    f_out.write(new_header)
                    f_out.write(new_body)
                    f_out.flush()
            else:
                with client_write_lock:
                    f_out.write(new_header)
                    f_out.write(new_body)
                    f_out.flush()

    except Exception as e:
        log_exception_with_context(
            "转发线程异常",
            e,
            direction=direction,
            fd_in=fd_in,
            fd_out=fd_out,
            last_summary=last_summary,
            last_body_len=last_body_len,
            exit_set=exit_event.is_set(),
        )
        request_shutdown(f"transfer-exception:{direction}:{type(e).__name__}")


# ==================== main ====================

def main():
    global clangd_stdin
    try:
        p = subprocess.Popen(
            [REAL_CLANGD] + sys.argv[1:],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
        )
    except FileNotFoundError:
        sys.stderr.write(f"Error: Clangd not found at {REAL_CLANGD}\n")
        return 1

    if p.stdin is None or p.stdout is None:
        sys.stderr.write("Error: Failed to open clangd stdio pipes\n")
        return 1

    clangd_stdin = p.stdin
    log_proxy_message(
        f"启动代理: proxy_pid={os.getpid()}, clangd_pid={p.pid}, argv={sys.argv[1:]}"
    )

    t1 = threading.Thread(
        target=transfer_with_hook,
        args=(sys.stdin.fileno(), p.stdin.fileno(), "VSCode -> Clangd"),
        name="proxy-vscode-to-clangd",
        daemon=True,
    )

    t2 = threading.Thread(
        target=transfer_with_hook,
        args=(p.stdout.fileno(), sys.stdout.fileno(), "Clangd -> VSCode"),
        name="proxy-clangd-to-vscode",
        daemon=True,
    )

    t1.start()
    t2.start()

    try:
        while p.poll() is None and not exit_event.is_set():
            exit_event.wait(timeout=0.1)
    except KeyboardInterrupt:
        request_shutdown("keyboard-interrupt")
    finally:
        if not exit_event.is_set() and p.poll() is not None:
            request_shutdown(f"clangd-exited:returncode={p.poll()}")

        if p.poll() is None:
            log_proxy_message(
                f"准备终止 clangd 子进程: clangd_pid={p.pid}, shutdown_reason={shutdown_reason}"
            )
            p.terminate()
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                log_proxy_message(f"clangd 子进程未及时退出, 准备 kill: clangd_pid={p.pid}")
                p.kill()
                p.wait(timeout=1)

        t1.join(timeout=1.0)
        t2.join(timeout=1.0)
        cleanup_instance_registry()
        log_proxy_message(
            f"代理退出: proxy_pid={os.getpid()}, clangd_pid={p.pid}, returncode={p.returncode}, exit_set={exit_event.is_set()}, shutdown_reason={shutdown_reason}"
        )

        return p.returncode if p.returncode is not None else 0


def handle_exit_signal(signum: int, frame):
    signame = signal.Signals(signum).name
    log_proxy_message(
        f"收到退出信号: signum={signum}, signame={signame}, exit_set_before={exit_event.is_set()}"
    )
    request_shutdown(f"signal:{signame}")


if __name__ == "__main__":
    install_thread_exception_logging()
    signal.signal(signal.SIGINT, handle_exit_signal)
    signal.signal(signal.SIGTERM, handle_exit_signal)
    signal.signal(signal.SIGHUP, handle_exit_signal)
    sys.exit(main())
