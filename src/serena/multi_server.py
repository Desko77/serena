"""
Multi-server manager for running multiple Serena MCP server processes in parallel.

Each project gets its own isolated server process, enabling concurrent work
from different MCP clients or browser windows.
"""

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)


def _get_process_memory_rss_mb(pid: int) -> float | None:
    """Read RSS memory (in MB) for a given PID from /proc/{pid}/status.

    Returns None on non-Linux systems or if the process info is unavailable.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:    123456 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        return round(int(parts[1]) / 1024, 1)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return None


def _get_process_tree_rss_mb(pid: int) -> float | None:
    """Read RSS memory (in MB) for a PID and all its child processes.

    Walks /proc to find child processes recursively, then sums VmRSS.
    Returns None on non-Linux systems.
    """
    try:
        # Collect all descendants by walking /proc/*/status for PPid
        all_pids: set[int] = {pid}
        # Iterate until no new children found
        changed = True
        while changed:
            changed = False
            try:
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    child_pid = int(entry)
                    if child_pid in all_pids:
                        continue
                    try:
                        with open(f"/proc/{child_pid}/status", encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("PPid:"):
                                    ppid = int(line.split()[1])
                                    if ppid in all_pids:
                                        all_pids.add(child_pid)
                                        changed = True
                                    break
                    except (FileNotFoundError, PermissionError, OSError, ValueError):
                        continue
            except (FileNotFoundError, PermissionError):
                break

        total_kb = 0
        for p in all_pids:
            try:
                with open(f"/proc/{p}/status", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            parts = line.split()
                            if len(parts) >= 2:
                                total_kb += int(parts[1])
                            break
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                continue

        return round(total_kb / 1024, 1) if total_kb > 0 else None
    except Exception:
        return None


def _get_system_memory_info() -> dict[str, Any]:
    """Read system memory info from /proc/meminfo.

    Returns dict with total_mb, available_mb, used_mb. Values are None on non-Linux.
    """
    result: dict[str, Any] = {"total_mb": None, "available_mb": None, "used_mb": None}
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                for key in ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached"):
                    if line.startswith(key + ":"):
                        parts = line.split()
                        if len(parts) >= 2:
                            values[key] = int(parts[1])
        if "MemTotal" in values:
            total = values["MemTotal"]
            result["total_mb"] = round(total / 1024, 1)
            if "MemAvailable" in values:
                avail = values["MemAvailable"]
                result["available_mb"] = round(avail / 1024, 1)
                result["used_mb"] = round((total - avail) / 1024, 1)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return result


def _get_project_file_stats(project_path: str) -> dict[str, Any]:
    """Walk a project directory and collect file statistics.

    Returns dict with total_files, source_files (by extension), total_size_mb.
    """
    total_files = 0
    total_size = 0
    ext_counts: dict[str, int] = {}
    source_extensions = {".bsl", ".os", ".py", ".ts", ".js", ".go", ".java", ".cs", ".rs", ".kt", ".rb", ".php"}

    try:
        for root, _dirs, files in os.walk(project_path):
            # Skip hidden directories and common non-source dirs
            basename = os.path.basename(root)
            if basename.startswith(".") and basename != ".":
                continue
            for fname in files:
                total_files += 1
                fpath = os.path.join(root, fname)
                try:
                    total_size += os.path.getsize(fpath)
                except OSError:
                    pass
                ext = os.path.splitext(fname)[1].lower()
                if ext in source_extensions:
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
    except (PermissionError, OSError) as e:
        log.warning("Failed to walk project directory %s: %s", project_path, e)

    source_files = sum(ext_counts.values())
    return {
        "total_files": total_files,
        "source_files": source_files,
        "source_by_extension": dict(sorted(ext_counts.items(), key=lambda x: -x[1])),
        "total_size_mb": round(total_size / (1024 * 1024), 1),
    }


def _read_cache_stats(project_path: str) -> dict[str, Any] | None:
    """Read cache_stats.json from a project's .serena directory.

    Returns the parsed dict if found, None otherwise.
    """
    stats_file = os.path.join(project_path, ".serena", "cache_stats.json")
    if not os.path.exists(stats_file):
        return None
    try:
        with open(stats_file, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.debug("Failed to read cache stats from %s: %s", stats_file, e)
        return None


@dataclass
class ManagedServerProcess:
    """Wrapper around a subprocess running a single Serena MCP server for one project."""

    project_name: str
    project_path: str
    port: int
    transport: Literal["sse", "streamable-http"]
    host: str = "0.0.0.0"
    context: str | None = None
    modes: tuple[str, ...] = ()
    log_level: str | None = None
    auto_restart: bool = True
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _start_time: float | None = field(default=None, init=False, repr=False)
    _restart_count: int = field(default=0, init=False, repr=False)
    _stdout_file: Any = field(default=None, init=False, repr=False)
    _stderr_file: Any = field(default=None, init=False, repr=False)

    def _build_command(self) -> list[str]:
        cmd = [
            "serena-mcp-server",
            "--project",
            self.project_path,
            "--transport",
            self.transport,
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        if self.context:
            cmd.extend(["--context", self.context])
        for mode in self.modes:
            cmd.extend(["--mode", mode])
        if self.log_level:
            cmd.extend(["--log-level", self.log_level])
        return cmd

    def start(self) -> None:
        """Start the server subprocess."""
        if self._process is not None and self._process.poll() is None:
            log.warning("Server for %s is already running (pid %d)", self.project_name, self._process.pid)
            return

        cmd = self._build_command()
        log.info("Starting server for %s on port %d: %s", self.project_name, self.port, " ".join(cmd))

        # Redirect child output to log files for debugging
        log_dir = os.path.join(os.path.expanduser("~"), ".serena", "logs", "multi-server")
        os.makedirs(log_dir, exist_ok=True)
        stdout_path = os.path.join(log_dir, f"{self.project_name}.stdout.log")
        stderr_path = os.path.join(log_dir, f"{self.project_name}.stderr.log")
        self._stdout_file = open(stdout_path, "a")  # noqa: SIM115
        self._stderr_file = open(stderr_path, "a")  # noqa: SIM115
        self._process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._stdout_file, stderr=self._stderr_file)
        self._start_time = time.time()
        log.info("Server for %s started with pid %d", self.project_name, self._process.pid)

    def stop(self, timeout: float = 10) -> None:
        """Stop the server subprocess gracefully, then force-kill if needed."""
        if self._process is None or self._process.poll() is not None:
            log.debug("Server for %s is not running", self.project_name)
            self._process = None
            return

        pid = self._process.pid
        log.info("Stopping server for %s (pid %d)", self.project_name, pid)

        try:
            self._process.terminate()
            self._process.wait(timeout=timeout)
            log.info("Server for %s stopped gracefully", self.project_name)
        except subprocess.TimeoutExpired:
            log.warning("Server for %s did not stop in %ds, killing", self.project_name, timeout)
            self._process.kill()
            self._process.wait(timeout=5)
        finally:
            self._process = None
            self._close_log_files()

    def is_alive(self) -> bool:
        """Check if the subprocess is still running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def restart(self) -> bool:
        """Restart the server with exponential backoff. Returns True if restart succeeded."""
        max_retries = 3
        if self._restart_count >= max_retries:
            log.error("Server for %s has exceeded max restart attempts (%d)", self.project_name, max_retries)
            return False

        backoff = min(2**self._restart_count, 30)
        log.info("Restarting server for %s (attempt %d, backoff %ds)", self.project_name, self._restart_count + 1, backoff)
        time.sleep(backoff)

        self.stop(timeout=5)
        self.start()
        self._restart_count += 1
        return True

    def reset_restart_count(self) -> None:
        """Reset the restart counter (e.g., after a period of stable operation)."""
        self._restart_count = 0

    def _close_log_files(self) -> None:
        for f in (self._stdout_file, self._stderr_file):
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
        self._stdout_file = None
        self._stderr_file = None

    @property
    def pid(self) -> int | None:
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        return None

    @property
    def uptime(self) -> float | None:
        if self._start_time is not None and self.is_alive():
            return time.time() - self._start_time
        return None

    @property
    def status(self) -> str:
        if self.is_alive():
            return "running"
        if self._process is not None:
            return "crashed"
        if not self.auto_restart:
            return "stopped"
        return "stopped"

    def to_dict(self) -> dict[str, Any]:
        uptime = self.uptime
        pid = self.pid
        return {
            "project_name": self.project_name,
            "project_path": self.project_path,
            "port": self.port,
            "transport": self.transport,
            "host": self.host,
            "status": self.status,
            "pid": pid,
            "uptime_seconds": round(uptime, 1) if uptime is not None else None,
            "auto_restart": self.auto_restart,
            "memory_rss_mb": _get_process_memory_rss_mb(pid) if pid else None,
        }


class MultiServerManager:
    """Manages N ManagedServerProcess instances with monitoring and control."""

    CONTROL_FILE_NAME = "multi_server.json"
    STABLE_PERIOD = 60  # seconds of uptime to consider process stable

    def __init__(self) -> None:
        self._servers: dict[str, ManagedServerProcess] = {}
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()

    def add_server(
        self,
        project_name: str,
        project_path: str,
        port: int,
        transport: Literal["sse", "streamable-http"] = "sse",
        host: str = "0.0.0.0",
        context: str | None = None,
        modes: tuple[str, ...] = (),
        log_level: str | None = None,
    ) -> None:
        """Register a new server to be managed."""
        with self._lock:
            if project_name in self._servers:
                raise ValueError(f"Server for project '{project_name}' already registered")
            self._servers[project_name] = ManagedServerProcess(
                project_name=project_name,
                project_path=project_path,
                port=port,
                transport=transport,
                host=host,
                context=context,
                modes=modes,
                log_level=log_level,
            )

    def start_all(self) -> None:
        """Start all registered servers and print a summary table."""
        with self._lock:
            for server in self._servers.values():
                if server.auto_restart and not server.is_alive():
                    server.start()

        self._print_status_table()
        self._save_control_file()

    def stop_server(self, project_name: str) -> None:
        """Stop a specific server (disables auto-restart)."""
        with self._lock:
            server = self._servers.get(project_name)
            if server is None:
                raise ValueError(f"Unknown project: {project_name}")
            server.auto_restart = False
            server.stop()
        self._save_control_file()
        log.info("Server for %s stopped and auto-restart disabled", project_name)

    def start_server(self, project_name: str) -> None:
        """Start a previously stopped server (re-enables auto-restart)."""
        with self._lock:
            server = self._servers.get(project_name)
            if server is None:
                raise ValueError(f"Unknown project: {project_name}")
            server.auto_restart = True
            server.start()
        self._save_control_file()
        log.info("Server for %s started and auto-restart enabled", project_name)

    def restart_server(self, project_name: str) -> None:
        """Restart a specific server."""
        with self._lock:
            server = self._servers.get(project_name)
            if server is None:
                raise ValueError(f"Unknown project: {project_name}")
            server.stop()
            server.auto_restart = True
            server.reset_restart_count()
            server.start()
        self._save_control_file()
        log.info("Server for %s restarted", project_name)

    def list_servers(self) -> list[dict[str, Any]]:
        """Get status of all servers."""
        with self._lock:
            return [server.to_dict() for server in self._servers.values()]

    def remove_server(self, project_name: str) -> None:
        """Stop and remove a server from management (does not modify serena_config.yml)."""
        with self._lock:
            server = self._servers.get(project_name)
            if server is None:
                raise ValueError(f"Unknown project: {project_name}")
            server.auto_restart = False
            server.stop()
            del self._servers[project_name]
        self._save_control_file()
        log.info("Server for %s removed", project_name)

    def get_server_logs(self, project_name: str, log_type: str = "stderr", num_lines: int = 200) -> list[str]:
        """Read the last N lines from a server's log file.

        Args:
            project_name: Name of the managed project.
            log_type: "stderr" or "stdout".
            num_lines: Maximum number of lines to return.

        Returns:
            List of log lines (most recent last).

        """
        with self._lock:
            if project_name not in self._servers:
                raise ValueError(f"Unknown project: {project_name}")

        if log_type not in ("stderr", "stdout"):
            raise ValueError(f"Invalid log type: {log_type}. Must be 'stderr' or 'stdout'.")

        log_dir = os.path.join(os.path.expanduser("~"), ".serena", "logs", "multi-server")
        log_path = os.path.join(log_dir, f"{project_name}.{log_type}.log")

        if not os.path.exists(log_path):
            return []

        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return [line.rstrip("\n") for line in lines[-num_lines:]]
        except OSError as e:
            log.warning("Failed to read log file %s: %s", log_path, e)
            return []

    def get_server_stats(self, project_name: str) -> dict[str, Any]:
        """Get project statistics. Reads cache_stats.json if available (instant), falls back to os.walk()."""
        with self._lock:
            server = self._servers.get(project_name)
            if server is None:
                raise ValueError(f"Unknown project: {project_name}")
            project_path = server.project_path
            pid = server.pid

        result: dict[str, Any] = {"memory_tree_rss_mb": _get_process_tree_rss_mb(pid) if pid else None}

        # Try reading cache_stats.json (written by language server after indexing)
        cache_stats = _read_cache_stats(project_path)
        if cache_stats:
            result["indexed_files"] = cache_stats.get("indexed_files")
            result["language"] = cache_stats.get("language")
            result["last_updated"] = cache_stats.get("last_updated")
            result["bsl"] = cache_stats.get("bsl")
            result["source"] = "cache"
        else:
            # Fallback: walk the directory (slow for large projects)
            file_stats = _get_project_file_stats(project_path)
            result.update(file_stats)
            result["source"] = "filesystem"

        return result

    @staticmethod
    def get_system_stats() -> dict[str, Any]:
        """Get system-level memory and CPU info."""
        mem = _get_system_memory_info()
        # CPU count
        cpu_count: int | None = None
        try:
            cpu_count = os.cpu_count()
        except Exception:
            pass
        # Load average (Linux)
        load_avg: tuple[float, ...] | None = None
        try:
            load_avg = os.getloadavg()  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
        return {
            "memory": mem,
            "cpu_count": cpu_count,
            "load_average": list(load_avg) if load_avg else None,
        }

    def find_free_port(self, host: str = "0.0.0.0") -> int:
        """Find a free port starting after the highest used port.

        Checks ports sequentially with socket.bind() to ensure availability.
        """
        with self._lock:
            used_ports = {s.port for s in self._servers.values()}

        start_port = max(used_ports) + 1 if used_ports else 9200
        for port in range(start_port, start_port + 100):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind((host, port))
                    return port
            except OSError:
                continue
        raise RuntimeError(f"No free port found in range {start_port}-{start_port + 99}")

    def add_and_start_server(
        self,
        project_path: str,
        transport: Literal["sse", "streamable-http"] | None = None,
        host: str | None = None,
        context: str | None = None,
        modes: tuple[str, ...] = (),
        log_level: str | None = None,
    ) -> dict[str, Any]:
        """Add a new project and start its server.

        Automatically assigns a free port and deduplicates project names.

        Returns:
            Server info dict from to_dict().

        """
        base_name = os.path.basename(project_path.rstrip("/\\"))
        if not base_name:
            raise ValueError(f"Cannot derive project name from path: {project_path}")

        # Deduplicate name
        project_name = base_name
        counter = 1
        with self._lock:
            existing_names = set(self._servers.keys())
        while project_name in existing_names:
            counter += 1
            project_name = f"{base_name}_{counter}"

        # Defaults from existing servers
        if transport is None:
            with self._lock:
                first = next(iter(self._servers.values()), None)
            transport = first.transport if first else "sse"
        if host is None:
            with self._lock:
                first = next(iter(self._servers.values()), None)
            host = first.host if first else "0.0.0.0"

        port = self.find_free_port(host)

        self.add_server(
            project_name=project_name,
            project_path=project_path,
            port=port,
            transport=transport,
            host=host,
            context=context,
            modes=modes,
            log_level=log_level,
        )
        self.start_server(project_name)

        with self._lock:
            return self._servers[project_name].to_dict()

    def shutdown(self) -> None:
        """Stop all managed servers and clean up."""
        log.info("Shutting down all servers...")
        self._shutdown_event.set()
        with self._lock:
            for server in self._servers.values():
                server.stop()
        self._remove_control_file()
        log.info("All servers stopped")

    def run_forever(self) -> None:
        """Monitor loop: restart crashed processes, process control commands."""

        # Install signal handlers
        def _signal_handler(signum: int, frame: Any) -> None:
            log.info("Received signal %d, shutting down...", signum)
            self.shutdown()

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        log.info("Multi-server manager running. Monitoring %d server(s)...", len(self._servers))

        # Give child processes time to start before first health check
        self._shutdown_event.wait(timeout=10)

        while not self._shutdown_event.is_set():
            # Snapshot servers under lock, release immediately to avoid blocking async handlers
            with self._lock:
                servers_snapshot = list(self._servers.values())

            for server in servers_snapshot:
                if not server.is_alive() and server.auto_restart:
                    rc = server._process.returncode if server._process is not None else "?"
                    log.warning(
                        "Server for %s has died (rc=%s, check ~/.serena/logs/multi-server/), attempting restart...",
                        server.project_name,
                        rc,
                    )
                    if not server.restart():
                        log.error("Server for %s failed to restart, disabling auto-restart", server.project_name)
                        server.auto_restart = False

                # Reset restart count after stable period
                if server.is_alive() and server.uptime is not None and server.uptime > self.STABLE_PERIOD:
                    server.reset_restart_count()

            # Process control commands from file
            self._process_control_commands()

            self._shutdown_event.wait(timeout=5)

    def _print_status_table(self) -> None:
        """Print a formatted status table to stdout."""
        servers = self.list_servers()
        if not servers:
            print("No servers registered.")
            return

        # Calculate column widths
        name_w = max(len(s["project_name"]) for s in servers)
        name_w = max(name_w, 7)  # "Project" header
        port_w = 5
        status_w = 8
        pid_w = 6
        transport_w = max(len(s["transport"]) for s in servers)
        transport_w = max(transport_w, 9)

        header = f"{'Project':<{name_w}}  {'Port':<{port_w}}  {'Status':<{status_w}}  {'PID':<{pid_w}}  {'Transport':<{transport_w}}"
        separator = "-" * len(header)
        print(separator)
        print(header)
        print(separator)
        for s in servers:
            pid_str = str(s["pid"]) if s["pid"] else "-"
            print(
                f"{s['project_name']:<{name_w}}  {s['port']:<{port_w}}  {s['status']:<{status_w}}  {pid_str:<{pid_w}}  {s['transport']:<{transport_w}}"
            )
        print(separator)

    def _get_control_file_path(self) -> str:
        """Get path to the control file for inter-process communication."""
        from serena.constants import SERENA_MANAGED_DIR_IN_HOME

        return os.path.join(SERENA_MANAGED_DIR_IN_HOME, self.CONTROL_FILE_NAME)

    def _save_control_file(self) -> None:
        """Write current state to the control file."""
        control_path = self._get_control_file_path()
        os.makedirs(os.path.dirname(control_path), exist_ok=True)
        data = {
            "pid": os.getpid(),
            "servers": self.list_servers(),
            "commands": [],  # CLI writes commands here, manager reads and clears
        }
        try:
            with open(control_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning("Failed to write control file: %s", e)

    def _remove_control_file(self) -> None:
        """Remove the control file on shutdown."""
        control_path = self._get_control_file_path()
        try:
            if os.path.exists(control_path):
                os.remove(control_path)
        except Exception as e:
            log.warning("Failed to remove control file: %s", e)

    def _process_control_commands(self) -> None:
        """Read and execute commands from the control file."""
        control_path = self._get_control_file_path()
        try:
            if not os.path.exists(control_path):
                return
            with open(control_path) as f:
                data = json.load(f)

            commands = data.get("commands", [])
            if not commands:
                return

            for cmd in commands:
                action = cmd.get("action")
                project = cmd.get("project")
                try:
                    if action == "stop" and project:
                        self.stop_server(project)
                    elif action == "start" and project:
                        self.start_server(project)
                    elif action == "restart" and project:
                        self.restart_server(project)
                    else:
                        log.warning("Unknown control command: %s", cmd)
                except Exception as e:
                    log.error("Failed to execute control command %s: %s", cmd, e)

            # Clear processed commands and update state
            self._save_control_file()

        except (json.JSONDecodeError, FileNotFoundError, PermissionError) as e:
            log.debug("Failed to read control file: %s", e)


class AdminServer:
    """Admin web UI and API server for managing multi-server instances.

    Built on Starlette + uvicorn (already installed as dependencies of the mcp package).
    Runs in a background daemon thread.
    """

    def __init__(self, manager: MultiServerManager, host: str, port: int) -> None:
        self._manager = manager
        self._host = host
        self._port = port
        self._uvicorn_server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._html_cache: str = self._load_html()

    @staticmethod
    def _load_html() -> str:
        from serena.constants import SERENA_ADMIN_DIR

        html_path = os.path.join(SERENA_ADMIN_DIR, "admin.html")
        try:
            with open(html_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            log.warning("Admin HTML not found at %s, using fallback", html_path)
            return "<html><body><h1>Serena Admin</h1><p>admin.html not found</p></body></html>"

    def _create_app(self) -> Starlette:
        routes = [
            Route("/", self._handle_index, methods=["GET"]),
            Route("/admin/system", self._handle_system, methods=["GET"]),
            Route("/admin/available-projects", self._handle_available_projects, methods=["GET"]),
            Route("/admin/servers", self._handle_servers, methods=["GET", "POST"]),
            Route("/admin/servers/{name:path}/logs", self._handle_logs, methods=["GET"]),
            Route("/admin/servers/{name:path}/stats", self._handle_stats, methods=["GET"]),
            Route("/admin/servers/{name:path}/{action}", self._handle_action, methods=["POST"]),
            Route("/admin/servers/{name:path}", self._handle_delete, methods=["DELETE"]),
        ]
        return Starlette(routes=routes)

    async def _handle_index(self, request: Request) -> HTMLResponse:
        return HTMLResponse(self._html_cache)

    async def _handle_servers(self, request: Request) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse(self._manager.list_servers())

        # POST — add new project
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        path = body.get("path")
        if not path:
            return JSONResponse({"error": "Missing required field: path"}, status_code=400)

        try:
            result = await asyncio.to_thread(
                self._manager.add_and_start_server,
                project_path=path,
                transport=body.get("transport"),
                host=body.get("host"),
                context=body.get("context"),
                modes=tuple(body["modes"]) if "modes" in body else (),
                log_level=body.get("log_level"),
            )
            return JSONResponse(result, status_code=201)
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            log.exception("Failed to add server")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_logs(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        log_type = request.query_params.get("type", "stderr")
        try:
            num_lines = int(request.query_params.get("lines", "200"))
        except ValueError:
            return JSONResponse({"error": "Invalid 'lines' parameter"}, status_code=400)

        try:
            lines = await asyncio.to_thread(self._manager.get_server_logs, name, log_type, num_lines)
            return JSONResponse({"lines": lines})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    async def _handle_stats(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            stats = await asyncio.to_thread(self._manager.get_server_stats, name)
            return JSONResponse(stats)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    async def _handle_system(self, request: Request) -> JSONResponse:
        stats = await asyncio.to_thread(self._manager.get_system_stats)
        return JSONResponse(stats)

    async def _handle_available_projects(self, request: Request) -> JSONResponse:
        """List directories under SERENA_PROJECTS_DIR not yet managed as servers."""

        def _scan() -> list[dict[str, Any]]:
            projects_dir = os.environ.get("SERENA_PROJECTS_DIR", "/projects")
            if not os.path.isdir(projects_dir):
                return []
            managed_paths = {s["project_path"] for s in self._manager.list_servers()}
            available: list[dict[str, Any]] = []
            for entry in sorted(os.listdir(projects_dir)):
                full_path = os.path.join(projects_dir, entry)
                if os.path.isdir(full_path) and full_path not in managed_paths:
                    has_project_yml = os.path.isfile(os.path.join(full_path, ".serena", "project.yml"))
                    available.append({"path": full_path, "name": entry, "initialized": has_project_yml})
            return available

        result = await asyncio.to_thread(_scan)
        return JSONResponse(result)

    async def _handle_action(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        action = request.path_params["action"]
        try:
            if action == "stop":
                await asyncio.to_thread(self._manager.stop_server, name)
            elif action == "start":
                await asyncio.to_thread(self._manager.start_server, name)
            elif action == "restart":
                await asyncio.to_thread(self._manager.restart_server, name)
            else:
                return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)
            return JSONResponse({"status": "ok", "action": action, "project": name})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    async def _handle_delete(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            await asyncio.to_thread(self._manager.remove_server, name)
            return JSONResponse({"status": "ok", "removed": name})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    def start(self) -> None:
        """Start the admin Starlette/uvicorn server in a background thread."""
        app = self._create_app()
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn_server.run, daemon=True)
        self._thread.start()
        log.info("Admin UI running on http://%s:%d/", self._host, self._port)

    def stop(self) -> None:
        """Stop the admin server."""
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            self._uvicorn_server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


def send_control_command(action: str, project: str | None = None) -> dict[str, Any] | None:
    """Send a control command to a running multi-server manager via the control file.

    Returns the current server list, or None if the manager is not running.
    """
    from serena.constants import SERENA_MANAGED_DIR_IN_HOME

    control_path = os.path.join(SERENA_MANAGED_DIR_IN_HOME, MultiServerManager.CONTROL_FILE_NAME)

    if not os.path.exists(control_path):
        return None

    try:
        with open(control_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    # Check if manager is still alive
    manager_pid = data.get("pid")
    if manager_pid and not _is_pid_alive(manager_pid):
        # Manager is dead, clean up
        try:
            os.remove(control_path)
        except OSError:
            pass
        return None

    if action == "status":
        return data

    # Append command
    commands = data.get("commands", [])
    cmd: dict[str, str] = {"action": action}
    if project:
        cmd["project"] = project
    commands.append(cmd)
    data["commands"] = commands

    try:
        with open(control_path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log.error("Failed to write control command: %s", e)
        return None

    # Wait for the command to be processed
    time.sleep(2)

    # Re-read to get updated state
    try:
        with open(control_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
