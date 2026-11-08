"""启动真实 HTTP 服务验证部署入口；使用临时库，不调用外部接口。"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, DB_PATH=str(Path(tmp) / "smoke.db"), DEEPSEEK_API_KEY="", BOCHA_API_KEY="")
        with open(Path(tmp) / "server.log", "w+") as logs:
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                     "--port", str(port), "--workers", "1"], cwd=root, env=env, stdout=logs, stderr=logs)
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError("server exited")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                            assert response.status == 200
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("startup timeout")
                for route in ["/", "/healthz", "/login", "/graph", "/about"]:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=3) as response:
                        assert response.status == 200, route
                        print(f"PASS HTTP 200 {route}")
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                logs.seek(0)
                output = logs.read()
                assert "Application startup complete." in output, output
                assert "Application shutdown complete." in output, output
                print("PASS startup / graceful shutdown")


if __name__ == "__main__":
    main()
