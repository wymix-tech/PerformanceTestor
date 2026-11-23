"""deploy 域服务（P0-1d）：SSH 连通性 / 平台侧构建 / 远程启动 / 运行状态。

SSH 执行体为注入缝（runner）：缺省 ParamikoRunner（paramiko 懒加载，未安装
时显式报错提示 perf-py[deploy]），测试注入 FakeRunner 不依赖真实 SSH。
语义对齐 MCP 工具契约（tests/contract/golden/ai_tools.json 已声明
deploy/start 与 deploy/status）。
"""

from __future__ import annotations

import glob
import io
import os
import shlex
import subprocess

from server.crypto import decrypt
from server.deploy_repo import DeployConnModel


class DeployError(RuntimeError):
    """部署操作失败（SSH 不可达 / 构建失败 / 远程命令非零）。"""


def _validate_port(app_port) -> int:
    """app_port 强校验（P0 修复：远端 shell 拼接前收口，非整型直接拒绝）。"""
    try:
        port = int(app_port)
    except (TypeError, ValueError):
        raise DeployError(f"app_port 非法：{app_port!r}（须为 1~65535 整数）") from None
    if not 1 <= port <= 65535:
        raise DeployError(f"app_port 越界：{port}（须为 1~65535 整数）")
    return port


class ParamikoRunner:
    """paramiko 执行体：exec 返回 (rc, 输出)；upload 走 SFTP。"""

    @staticmethod
    def _client(conn: DeployConnModel):
        try:
            import paramiko
        except ImportError as exc:  # noqa: PLC0415 — 懒加载可选依赖
            raise DeployError(
                "paramiko 未安装：请执行 pip install 'perf-py[deploy]' 后重试") from exc
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = {"hostname": conn.host, "port": conn.port,
                        "username": conn.username, "timeout": 10}
        if conn.auth_type == "privateKey" and conn.private_key:
            # P0 修复：取用点解密（decrypt 对存量明文原样透传，test_conn 明文直传不受影响）
            kwargs["pkey"] = paramiko.RSAKey.from_private_key(
                io.StringIO(decrypt(conn.private_key)))
        else:
            kwargs["password"] = decrypt(conn.password)
        client.connect(**kwargs)
        return client

    def exec(self, conn: DeployConnModel, command: str, timeout: int = 30) -> tuple[int, str]:
        client = self._client(conn)
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
            return rc, out + (f"\n{err}" if err.strip() else "")
        finally:
            client.close()

    def upload(self, conn: DeployConnModel, local_path: str, remote_path: str) -> None:
        client = self._client(conn)
        try:
            with client.open_sftp() as sftp:
                sftp.put(local_path, remote_path)
        finally:
            client.close()


class DeployService:
    """部署编排：连通性 / 构建 / 启动 / 状态（SSH 执行体可注入）。"""

    def __init__(self, conn_repo, source_repo, runner=None,
                 platform_base_url: str = "http://127.0.0.1:8000"):
        self._conns = conn_repo
        self._sources = source_repo
        self._runner = runner if runner is not None else ParamikoRunner()
        self._platform_base_url = platform_base_url.rstrip("/")

    # ------------------------------------------------------------ 连通性

    def test_conn(self, config: dict) -> dict:
        """探活（不落库）：临时构造连接对象执行 echo。"""
        conn = DeployConnModel(
            conn_name=str(config.get("connName") or "probe"),
            host=str(config.get("host") or ""),
            port=int(config.get("port") or 22),
            username=str(config.get("username") or "root"),
            auth_type=str(config.get("authType") or "password"),
            password=config.get("password"),
            private_key=config.get("privateKey"),
            work_dir=str(config.get("workDir") or "/opt/perftestor"))
        if not conn.host:
            raise DeployError("host 必填")
        rc, out = self._runner.exec(conn, "echo ok", timeout=10)
        return {"reachable": rc == 0 and "ok" in out}

    def test_conn_by_id(self, conn_id: int) -> dict:
        conn = self._conns.find_by_id(conn_id)
        if conn is None:
            raise DeployError(f"deploy conn not found: {conn_id}")
        rc, out = self._runner.exec(conn, "echo ok", timeout=10)
        return {"reachable": rc == 0 and "ok" in out}

    # ------------------------------------------------------------ 平台侧构建

    def build(self, repo_id: int, module: str | None = None) -> dict:
        """平台侧 mvn package 指定仓库；探测 target/*.jar 返回绝对路径。"""
        repo = self._sources.repo_by_id(repo_id)
        if repo is None:
            raise DeployError(f"source repo not found: {repo_id}")
        src = repo.source_path
        if not src or not os.path.isdir(src):
            raise DeployError(f"仓库本地路径不可用: {src}")
        cmd = ["mvn", "-q", "clean", "package", "-DskipTests"]
        if module:
            cmd += ["-pl", module]
        proc = subprocess.run(cmd, cwd=src, capture_output=True, text=True,
                              timeout=1800)
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr)[-2000:]
            raise DeployError(f"构建失败（exit={proc.returncode}）：{tail}")
        jars = sorted(
            glob.glob(os.path.join(src, "**", "target", "*.jar"), recursive=True),
            key=os.path.getmtime, reverse=True)
        jars = [j for j in jars
                if not j.endswith("-sources.jar") and "original-" not in os.path.basename(j)]
        if not jars:
            raise DeployError("构建成功但未探测到 target/*.jar")
        return {"jarPath": jars[0]}

    # ------------------------------------------------------------ 远程启动

    def start(self, conn_id: int, jar_path: str, agent_path: str | None = None,
              app_port: int = 8081, app_name: str | None = None) -> dict:
        """上传编译包（与 agent 自动挂载）并远程 nohup 启动。

        agent 未提供时若远端 work_dir 已存在 perf-agent.jar 则复用；
        reportUrl 指向平台 /api/agent/report（AGENT_REPORT_URL 可覆盖语义
        由平台侧部署环境决定）。
        """
        conn = self._conns.find_by_id(conn_id)
        if conn is None:
            raise DeployError(f"deploy conn not found: {conn_id}")
        if not os.path.isfile(jar_path):
            raise DeployError(f"jar 不存在: {jar_path}")
        port = _validate_port(app_port)
        work = conn.work_dir
        remote_jar = f"{work}/app.jar"
        remote_agent = f"{work}/perf-agent.jar"
        # P0 修复：所有拼接到远端 shell 的变量一律经 shlex.quote 转义（防命令注入）
        self._runner.exec(conn, f"mkdir -p {shlex.quote(work)}")
        self._runner.upload(conn, jar_path, remote_jar)
        if agent_path and os.path.isfile(agent_path):
            self._runner.upload(conn, agent_path, remote_agent)
        else:
            rc, _ = self._runner.exec(
                conn, f"test -f {shlex.quote(remote_agent)} && echo ok")
            if rc != 0:
                raise DeployError(
                    f"未提供 agentPath 且远端不存在 {remote_agent}（请先在平台构建"
                    " perf-agent 并传入 agentPath）")
        report_url = os.environ.get("AGENT_REPORT_URL",
                                    f"{self._platform_base_url}/api/agent/report")
        name = app_name or conn.conn_name
        javaagent = (f"{remote_agent}=reportUrl={report_url}"
                     f",appName={name},appPort={port}")
        launch = (f"cd {shlex.quote(work)} && nohup java "
                  f"-javaagent:{shlex.quote(javaagent)}"
                  f" -jar {shlex.quote(remote_jar)} --server.port={port}"
                  f" > {shlex.quote(f'{work}/app.log')} 2>&1 & echo $!")
        rc, out = self._runner.exec(conn, launch, timeout=30)
        if rc != 0:
            raise DeployError(f"远程启动失败：{out[-1000:]}")
        pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        return {"connId": conn_id, "pid": pid, "jarPath": remote_jar,
                "agentPath": remote_agent, "appPort": port,
                "logPath": f"{work}/app.log"}

    # ------------------------------------------------------------ 运行状态

    def status(self, conn_id: int, app_port: int = 8081) -> dict:
        conn = self._conns.find_by_id(conn_id)
        if conn is None:
            raise DeployError(f"deploy conn not found: {conn_id}")
        port = _validate_port(app_port)
        work = conn.work_dir
        # P0 修复：pgrep 匹配串与日志路径同样经 shlex.quote 转义
        rc, out = self._runner.exec(
            conn, f"pgrep -f {shlex.quote(f'java .*{work}/app.jar')} || true",
            timeout=15)
        pid = out.strip().splitlines()[0].strip() if out.strip() else ""
        running = bool(pid)
        rc, health = self._runner.exec(
            conn, f"curl -s -o /dev/null -w '%{{http_code}}' "
                  f"--max-time 5 http://127.0.0.1:{port}/actuator/health || true",
            timeout=15)
        health = health.strip().splitlines()[-1] if health.strip() else ""
        rc, log_tail = self._runner.exec(
            conn, f"tail -n 50 {shlex.quote(f'{work}/app.log')} 2>/dev/null || true",
            timeout=15)
        return {"connId": conn_id, "running": running, "pid": pid,
                "healthStatus": health if health in ("200", "401", "404") else
                ("up" if "200" in health else ""),
                "logTail": log_tail[-4000:]}

