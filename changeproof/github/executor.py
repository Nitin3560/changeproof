import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4


class ExecutionTimeout(RuntimeError):
    pass


class DockerExecutor:
    def command(self, *args, timeout=30, check=True):
        try:
            result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeout("Container execution deadline exceeded") from exc
        if check and result.returncode:
            raise RuntimeError("Docker operation failed")
        return result

    def execute(self, snapshot, target, output, policy):
        snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
        output.mkdir(parents=True)
        output.chmod(0o777)
        runtime = self.command("image", "inspect", "--format", "{{.Id}}", policy.runtime_image).stdout.strip()
        database = self.command("image", "inspect", "--format", "{{.Id}}", policy.database_image).stdout.strip()
        name = "changeproof-" + uuid4().hex
        network, db, runner = name + "-net", name + "-db", name + "-run"
        try:
            self.command("network", "create", "--internal", network)
            self.command("run", "-d", "--pull", "never", "--name", db, "--network", network,
                "--network-alias", "db", "--read-only", "--user", "70:70", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", "256m", "--cpus", "0.5",
                "--pids-limit", "128", "--log-driver", "none",
                "--tmpfs", "/var/lib/postgresql/data:rw,size=128m,uid=70,gid=70,mode=0700",
                "--tmpfs", "/var/run/postgresql:rw,size=16m,uid=70,gid=70",
                "--tmpfs", "/tmp:rw,size=16m", "-e", "POSTGRES_USER=changeproof",
                "-e", "POSTGRES_PASSWORD=benchmark", "-e", "POSTGRES_DB=changeproof", database)
            for _ in range(30):
                ready = self.command("exec", db, "pg_isready", "-U", "changeproof", timeout=5, check=False)
                if ready.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Disposable database did not become ready")
            self.command("run", "-d", "--pull", "never", "--name", runner, "--network", network,
                "--read-only", "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "512m", "--cpus", "1", "--pids-limit", "128", "--log-driver", "none",
                "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--workdir", "/tmp",
                "--mount", f"type=bind,src={snapshot},dst=/source,readonly",
                "--mount", f"type=bind,src={target.config_path},dst=/policy/config.yaml,readonly",
                "--mount", f"type=bind,src={output},dst=/out",
                "-e", "DATABASE_URL=postgresql://changeproof:benchmark@db:5432/changeproof",
                "--entrypoint", "timeout", runtime, "--signal=KILL", str(policy.timeout_seconds) + "s",
                "python", "-I", "-m", "changeproof.github.entrypoint",
                "--adapter", target.adapter_path, "--package", target.source_package)
            waited = self.command("wait", runner, timeout=policy.timeout_seconds + 10)
            exit_code = int(waited.stdout.strip())
            reports = list(output.glob("run-*/report.json"))
            if len(reports) != 1 or reports[0].is_symlink() or reports[0].stat().st_size > 10_000_000:
                raise RuntimeError("Verifier did not produce a bounded report")
            size, count = 0, 0
            for path in output.rglob("*"):
                if path.is_symlink():
                    raise RuntimeError("Report artifacts must not contain links")
                if path.is_file():
                    count += 1
                    size += path.stat().st_size
            if size > 30_000_000 or count > 1000:
                raise RuntimeError("Report artifacts exceed collection bounds")
            report = json.loads(reports[0].read_text())
            report["runtime_images"] = {"verifier": runtime, "database": database}
            reports[0].write_text(json.dumps(report, indent=2) + "\n")
            return reports[0], exit_code
        finally:
            try:
                self.command("rm", "-f", runner, db, check=False)
            finally:
                self.command("network", "rm", network, check=False)
