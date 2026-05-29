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