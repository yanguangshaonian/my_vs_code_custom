# import sys
# import subprocess
# import threading
# import os
# import signal
# import json  # 新增 json

# REAL_CLANGD = "/usr/bin/clangd"
# exit_event = threading.Event()

# # ==================== HOOK 相关代码 ====================
# # 用于存储拦截到的语义令牌请求的 ID，以便在 response 中匹配
# # ==================== HOOK 相关代码 ====================
# semantic_request_ids = set()

# def hook_message(direction: str, msg: dict) -> dict:
#     """
#     开发调试阶段：打印所有经过的消息，不放过任何死角
#     """
#     # 1. 提取关键信息用于摘要
#     msg_id = msg.get("id", "NO_ID")
#     method = msg.get("method", "RESPONSE/UNKNOWN")
    
#     # 2. 将整个报文转为字符串，如果太长就截断，保证能看清全貌又不至于崩盘
#     raw_str = json.dumps(msg, ensure_ascii=False)
#     if len(raw_str) > 500:
#         raw_str = raw_str[:500] + f" ... <TRUNCATED, Total {len(raw_str)} bytes>"
        
#     # 3. 无差别输出所有日志
#     print(f"[{direction}] ID:{msg_id} | METHOD:{method} | RAW: {raw_str}", file=sys.stderr, flush=True)

#     # 4. 试探性拦截逻辑（保留这个壳子，一旦上面的全量日志里出现了我们想要的，立刻就能定位）
#     if direction == "VSCode -> Clangd" and "semanticTokens" in method:
#         if "id" in msg:
#             semantic_request_ids.add(msg["id"])
#             print(f">>> [!] 拦截到语义令牌请求 ID={msg['id']}", file=sys.stderr, flush=True)

#     elif direction == "Clangd -> VSCode" and msg.get("id") in semantic_request_ids:
#         semantic_request_ids.remove(msg["id"])
#         data = msg.get("result", {}).get("data", [])
#         print(f">>> [!] 拦截到语义令牌响应，长度: {len(data)}", file=sys.stderr, flush=True)

#     return msg


# def transfer_with_hook(fd_in: int, fd_out: int, direction: str):
#     """带 LSP 报文解析的 I/O 引擎"""
#     try:
#         # 包装成带缓冲的二进制流，方便按行读取 HTTP Header
#         f_in = os.fdopen(fd_in, 'rb')
#         f_out = os.fdopen(fd_out, 'wb')
        
#         while not exit_event.is_set():
#             # 1. 解析头，找 Content-Length
#             content_length = 0
#             while True:
#                 line = f_in.readline()
#                 if not line: return # EOF，结束
#                 if line == b'\r\n': break
#                 if line.startswith(b'Content-Length:'):
#                     content_length = int(line.split(b':')[1].strip())
            
#             # 2. 精确读取完整的 JSON 数据体
#             body = f_in.read(content_length)
#             if len(body) < content_length: break
                
#             # 3. 反序列化
#             msg = json.loads(body.decode('utf-8'))
            
#             # 4. >>>> 触发 HOOK <<<<
#             try:
#                 msg = hook_message(direction, msg)
#             except Exception as e:
#                 print(f"Hook Error: {e}", file=sys.stderr)
            
#             # 5. 组装新报文发出去
#             new_body = json.dumps(msg, separators=(',', ':')).encode('utf-8')
#             new_header = f"Content-Length: {len(new_body)}\r\n\r\n".encode('utf-8')
            
#             f_out.write(new_header)
#             f_out.write(new_body)
#             f_out.flush()
#     except Exception:
#         pass
#     finally:
#         exit_event.set()

# def main():
#     try:
#         p = subprocess.Popen(
#             [REAL_CLANGD] + sys.argv[1:],
#             stdin=subprocess.PIPE,
#             stdout=subprocess.PIPE,
#             stderr=sys.stderr, 
#             bufsize=0          
#         )
#     except FileNotFoundError:
#         sys.stderr.write(f"Error: Clangd not found at {REAL_CLANGD}\n")
#         return 1
    
#     # 注入你的带 Hook 的 transfer 引擎，并附带请求方向参数
#     t1 = threading.Thread(target=transfer_with_hook, args=(sys.stdin.fileno(), p.stdin.fileno(), "VSCode -> Clangd")) # type: ignore
#     t2 = threading.Thread(target=transfer_with_hook, args=(p.stdout.fileno(), sys.stdout.fileno(), "Clangd -> VSCode")) # type: ignore

#     t1.start()
#     t2.start()
    
#     try:
#         while p.poll() is None and not exit_event.is_set():
#             exit_event.wait(timeout=0.1) 
#     except KeyboardInterrupt:
#         pass 
#     finally:
#         exit_event.set()
#         if p.poll() is None:
#             p.terminate()
#             p.wait(timeout=1)
#         t1.join(timeout=1.0)
#         t2.join(timeout=1.0)
#         return p.returncode

# if __name__ == "__main__":
#     signal.signal(signal.SIGINT, lambda signum, frame: exit_event.set())
#     sys.exit(main())







import sys
import subprocess
import threading
import os
import signal

# 1. 你的 Clangd 真实路径
REAL_CLANGD = "/usr/bin/clangd"

# 用于在任何一端断开时，通知其他组件安全退出
exit_event = threading.Event()

def transfer(fd_in: int, fd_out: int):
    """纯粹的 I/O 数据转发"""
    try:
        while not exit_event.is_set():
            data = os.read(fd_in, 65536)
            if not data:
                break # 收到 EOF，对端已关闭
            os.write(fd_out, data)
    except Exception:
        pass
    finally:
        exit_event.set()

def main():
    try:
        p = subprocess.Popen(
            [REAL_CLANGD] + sys.argv[1:],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr, # 错误输出直接接管给 VS Code
            bufsize=0          # 禁用缓冲，降低延迟
        )
    except FileNotFoundError:
        sys.stderr.write(f"Error: Clangd not found at {REAL_CLANGD}\n")
        return 1
    
    # 启动两个纯粹的数据传输线程
    t1 = threading.Thread(target=transfer, args=(sys.stdin.fileno(), p.stdin.fileno())) # type: ignore
    t2 = threading.Thread(target=transfer, args=(p.stdout.fileno(), sys.stdout.fileno())) # type: ignore

    t1.start()
    t2.start()
    
    try:
        # 等待子进程退出 或 I/O 线程触发退出信号
        while p.poll() is None and not exit_event.is_set():
            exit_event.wait(timeout=0.1) 
    except KeyboardInterrupt:
        pass # 触发下方的 finally 安全退出
    finally:
        # 确保所有组件退出
        exit_event.set()
        
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=1)
            
        t1.join(timeout=1.0)
        t2.join(timeout=1.0)
        
        return p.returncode

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda signum, frame: exit_event.set())
    sys.exit(main())