import sys
import subprocess
import threading
import os
import signal
import json
import re
from typing import Any

REAL_CLANGD = "/usr/bin/clangd"
exit_event = threading.Event()

# ==================== 状态缓存 ====================

# 语义 token 请求 ID
semantic_request_ids = set()

# 当前文档内容：uri -> lines
documents: dict[str, list[str]] = {}

# inlayHint 请求缓存：id -> {uri, range}
inlay_hint_requests: dict[Any, dict[str, Any]] = {}

# hover 请求缓存：id -> {uri, position}
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

# 匹配：
#   auto x = expr;
#   const auto x = expr;
#   auto& x = expr;
#   auto *x = expr; 这里只做基础支持
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

# 匹配带你自定义后缀的数字字面量：
#   123u32
#   0xffu32
#   0b1010u32
#   1.23f32
#   1e3f64
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


# ==================== 文档同步 ====================

def split_lines(text: str) -> list[str]:
    # 用 split('\n') 保留末尾空行，符合 LSP 行号模型
    return text.split("\n")


def apply_change(lines: list[str], change: dict[str, Any]) -> list[str]:
    """
    应用 VS Code 发来的 didChange。
    注意：LSP character 是 UTF-16 code unit。
    这里假设 C++ 源码主体是 ASCII/UTF-8 常规字符，足够用于你的变量行匹配。
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

    # 防御：如果 VS Code 发来的行号超过缓存，补齐
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


def infer_expr_type(expr: str, known_vars: dict[str, str]) -> str | None:
    expr = strip_outer_parens(expr)

    # 1. 字面量后缀：555u32 / 1.23f32
    m = LITERAL_WITH_SUFFIX_RE.match(expr)
    if m:
        suffix = m.group("suffix")
        return SUFFIX_TYPE_MAP.get(suffix)

    # 2. 简单变量传播：auto b = a;
    m = IDENT_RE.match(expr)
    if m:
        name = m.group(1)
        return known_vars.get(name)

    return None


def analyze_document(uri: str) -> list[dict[str, Any]]:
    """
    扫描文档，找出我们能推断的 auto 变量类型。
    返回：
    [
      {
        "line": int,
        "var_start": int,
        "var_end": int,
        "name": str,
        "type": str,
      }
    ]
    """
    lines = documents.get(uri)
    if not lines:
        return []

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


def make_custom_inlay_hints(uri: str, req_range: dict[str, Any]) -> list[dict[str, Any]]:
    items = analyze_document(uri)
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
            "kind": 1,  # Type
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

    # 避免重复。若 clangd 已经在同位置给了类型 hint，则不再追加。
    existing_positions = set()
    for h in result:
        if not isinstance(h, dict):
            continue
        pos = h.get("position", {})
        existing_positions.add((
            pos.get("line"),
            pos.get("character"),
            h.get("kind"),
        ))

    for h in custom_hints:
        key = (
            h["position"]["line"],
            h["position"]["character"],
            h["kind"],
        )
        if key not in existing_positions:
            result.append(h)
            existing_positions.add(key)

    return msg


def find_custom_var_at_position(uri: str, position: dict[str, int]) -> dict[str, Any] | None:
    line = position["line"]
    ch = position["character"]

    for item in analyze_document(uri):
        if item["line"] != line:
            continue

        # 鼠标在变量名范围内即可
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
    """
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
        # didOpen：保存完整文本
        if method == "textDocument/didOpen":
            td = msg.get("params", {}).get("textDocument", {})
            uri = td.get("uri")
            text = td.get("text")
            if uri and text is not None:
                documents[uri] = split_lines(text)

        # didChange：增量更新文本
        elif method == "textDocument/didChange":
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            if uri:
                if uri not in documents:
                    documents[uri] = [""]

                for change in params.get("contentChanges", []):
                    documents[uri] = apply_change(documents[uri], change)

        # didClose：释放缓存
        elif method == "textDocument/didClose":
            td = msg.get("params", {}).get("textDocument", {})
            uri = td.get("uri")
            if uri:
                documents.pop(uri, None)

        # 记录 inlayHint 请求
        elif method == "textDocument/inlayHint" and "id" in msg:
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            req_range = params.get("range")
            if uri and req_range:
                inlay_hint_requests[msg["id"]] = {
                    "uri": uri,
                    "range": req_range,
                }

        # 记录 hover 请求
        elif method == "textDocument/hover" and "id" in msg:
            params = msg.get("params", {})
            td = params.get("textDocument", {})
            uri = td.get("uri")
            pos = params.get("position")
            if uri and pos:
                hover_requests[msg["id"]] = {
                    "uri": uri,
                    "position": pos,
                }

        # 原来的 semanticTokens 观察逻辑
        if "semanticTokens" in method:
            if "id" in msg:
                semantic_request_ids.add(msg["id"])
                print(
                    f">>> [!] 拦截到语义令牌请求 ID={msg['id']}",
                    file=sys.stderr,
                    flush=True,
                )

    # ==================== Clangd -> VSCode ====================
    elif direction == "Clangd -> VSCode":
        # 修改 inlayHint 响应
        if msg.get("id") in inlay_hint_requests:
            req = inlay_hint_requests.pop(msg["id"])
            msg = patch_inlay_hint_response(msg, req)

        # 修改 hover 响应
        if msg.get("id") in hover_requests:
            req = hover_requests.pop(msg["id"])
            msg = patch_hover_response(msg, req)

        # 原来的 semanticTokens 观察逻辑
        if msg.get("id") in semantic_request_ids:
            semantic_request_ids.remove(msg["id"])
            result = msg.get("result", {})

            # full 返回 data；delta 返回 edits
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
                # 原样转发
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