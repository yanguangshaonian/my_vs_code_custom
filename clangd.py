import sys
import subprocess
import threading
import os
import signal
import json
import re
import time
from typing import Any

REAL_CLANGD = "/usr/bin/clangd"
exit_event = threading.Event()

# ==================== 全局配置 ====================

REQUEST_TTL_SECONDS = 30.0
CLEANUP_INTERVAL_MESSAGES = 100
MAX_ANALYSIS_CACHE = 64

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

# inlayHint 请求缓存：id -> {uri, range, time}
inlay_hint_requests: dict[Any, dict[str, Any]] = {}

# hover 请求缓存：id -> {uri, position, time}
hover_requests: dict[Any, dict[str, Any]] = {}

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

I32_MAX = 2**31 - 1
U32_MAX = 2**32 - 1
I64_MAX = 2**63 - 1
U64_MAX = 2**64 - 1

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

        for req_id, req_time in list(semantic_request_ids.items()):
            if now - req_time > REQUEST_TTL_SECONDS:
                semantic_request_ids.pop(req_id, None)


# ==================== 文档同步 ====================

def split_lines(text: str) -> list[str]:
    return text.split("\n")


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
        expr = inner
    return expr


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
    expr = strip_outer_parens(expr)

    # 1. 你的自定义后缀：555u32 / 1.23f32
    m = LITERAL_WITH_SUFFIX_RE.match(expr)
    if m:
        suffix = m.group("suffix")
        return SUFFIX_TYPE_MAP.get(suffix)

    # 2. 标准浮点字面量：3.14 / 3.14f / 1e3
    m = STANDARD_FLOAT_LITERAL_RE.match(expr)
    if m:
        return infer_standard_float_type(m.group("suffix"))

    # 3. 标准整数字面量：123 / 123u / 0xff
    m = STANDARD_INT_LITERAL_RE.match(expr)
    if m:
        return infer_standard_int_type(
            m.group("body"),
            m.group("suffix"),
        )

    # 4. 简单变量传播：auto b = a;
    m = IDENT_RE.match(expr)
    if m:
        name = m.group(1)
        return known_vars.get(name)

    return None


def analyze_lines_uncached(lines: list[str]) -> list[dict[str, Any]]:
    known_vars: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    for line_no, line in enumerate(lines):
        m = AUTO_ASSIGN_RE.match(line)
        if not m:
            continue

        name = m.group("name")
        expr = m.group("expr")
        auto_decl = m.group("auto_decl")
        ptr_ref = m.group("ptr_ref")

        base_type = infer_expr_type(expr, known_vars)
        if not base_type:
            continue

        display_type = decorate_type_by_auto_decl(base_type, auto_decl, ptr_ref)

        var_start = m.start("name")
        var_end = m.end("name")

        known_vars[name] = display_type

        results.append({
            "line": line_no,
            "var_start": var_start,
            "var_end": var_end,
            "name": name,
            "type": display_type,
            "line_text": line,
        })

    return results


def get_analysis(uri: str) -> list[dict[str, Any]]:
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

    result = analyze_lines_uncached(lines_snapshot)

    with state_lock:
        current_version = document_versions.get(uri, 0)

        # 只有版本没变才写入缓存
        if current_version == version:
            analysis_cache[uri] = (version, result, time.monotonic())
            trim_analysis_cache_locked()

    return result


def make_custom_inlay_hints(uri: str, req_range: dict[str, Any]) -> list[dict[str, Any]]:
    items = get_analysis(uri)
    if not items:
        return []

    start_line = req_range["start"]["line"]
    end_line = req_range["end"]["line"]

    hints: list[dict[str, Any]] = []

    for item in items:
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

    custom_hints = make_custom_inlay_hints(req["uri"], req["range"])
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

def find_custom_var_at_position(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    line = position["line"]
    ch = position["character"]

    for item in get_analysis(uri):
        if item["line"] != line:
            continue

        if item["var_start"] <= ch <= item["var_end"]:
            return item

    return None


def patch_hover_response(msg: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    item = find_custom_var_at_position(req["uri"], req["position"])
    if not item:
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

    # ==================== VSCode -> Clangd ====================
    if direction == "VSCode -> Clangd":
        now = time.monotonic()

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
                    analysis_cache.pop(uri, None)

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

                    analysis_cache.pop(uri, None)

        # didClose：释放缓存
        elif method == "textDocument/didClose":
            td = msg.get("params", {}).get("textDocument", {})
            uri = td.get("uri")

            if uri:
                with state_lock:
                    documents.pop(uri, None)
                    document_versions.pop(uri, None)
                    analysis_cache.pop(uri, None)

        # cancelRequest：清理被取消请求，防止长时间运行积累
        elif method == "$/cancelRequest":
            cancel_id = msg.get("params", {}).get("id")
            if cancel_id is not None:
                with state_lock:
                    inlay_hint_requests.pop(cancel_id, None)
                    hover_requests.pop(cancel_id, None)
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

def transfer_with_hook(fd_in: int, fd_out: int, direction: str):
    """带 LSP 报文解析的 I/O 引擎"""
    try:
        f_in = os.fdopen(fd_in, "rb", buffering=0)
        f_out = os.fdopen(fd_out, "wb", buffering=0)

        while not exit_event.is_set():
            content_length = None

            # 1. 读取 LSP header
            while True:
                line = f_in.readline()
                if not line:
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
            body = f_in.read(content_length)
            if len(body) < content_length:
                break

            # 3. 解析并 Hook
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception as e:
                print(f"JSON Decode Error: {e}", file=sys.stderr, flush=True)

                header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
                f_out.write(header)
                f_out.write(body)
                f_out.flush()
                continue

            try:
                msg = hook_message(direction, msg)
            except Exception as e:
                print(f"Hook Error: {e}", file=sys.stderr, flush=True)

            # 4. 重组 LSP 消息
            new_body = json.dumps(
                msg,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            new_header = f"Content-Length: {len(new_body)}\r\n\r\n".encode("utf-8")

            f_out.write(new_header)
            f_out.write(new_body)
            f_out.flush()

    except Exception as e:
        print(f"Transfer Error [{direction}]: {e}", file=sys.stderr, flush=True)
    finally:
        exit_event.set()


# ==================== main ====================

def main():
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

    t1 = threading.Thread(
        target=transfer_with_hook,
        args=(sys.stdin.fileno(), p.stdin.fileno(), "VSCode -> Clangd"),
        daemon=True,
    )

    t2 = threading.Thread(
        target=transfer_with_hook,
        args=(p.stdout.fileno(), sys.stdout.fileno(), "Clangd -> VSCode"),
        daemon=True,
    )

    t1.start()
    t2.start()

    try:
        while p.poll() is None and not exit_event.is_set():
            exit_event.wait(timeout=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        exit_event.set()

        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=1)

        t1.join(timeout=1.0)
        t2.join(timeout=1.0)

        return p.returncode if p.returncode is not None else 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda signum, frame: exit_event.set())
    sys.exit(main())