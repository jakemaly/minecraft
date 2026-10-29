"""Run-scoped agent workspace and compact file/shell tools."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any


TOOL_NAMES = ("read", "write", "edit", "bash", "grep", "find", "ls")
_OUTPUT_LIMIT = 12_000
_REVISION_LIMIT = 24_000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded(text: str, limit: int = _OUTPUT_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n… output truncated; use a narrower query or request full output", True


class Workspace:
    """Owns one agent's files, command execution, and revision events."""

    def __init__(self, root: str | Path, *, events=None, bridge_socket: str | Path | None = None, shared_skills: str | Path | None = None):
        self.root = Path(root).resolve()
        self.events = events
        self.bridge_socket = Path(bridge_socket).resolve() if bridge_socket else None
        self.shared_skills = Path(shared_skills).resolve() if shared_skills else None
        self._lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_container_idfile: Path | None = None
        from .sandbox import probe
        self.sandbox = probe()

    def initialize(self, *, mineflayer_docs: str | Path | None = None, pathfinder_docs: str | Path | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        for directory in ("scripts", "skills", "docs/mineflayer", "docs/pathfinder", "bin", ".runtime"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / "plan.md").touch(exist_ok=True)
        (self.root / "memory.md").touch(exist_ok=True)
        (self.root / "skills/README.md").write_text(
            "# Reusable skills\n\n"
            "Create one folder per ability, with a `SKILL.md` that starts with a one-sentence description and then gives the reliable procedure. "
            "Add a `test.sh` that checks the procedure's scripts, run it, then publish the version with `mc-skill publish skills/<name>`. "
            "Published versions appear under `library/` in later trials. Discover skills with `find` and read the selected `SKILL.md` before use.\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "# Agent workspace\n\n"
            "Use `scripts/` for programs, `skills/` for tested reusable abilities, "
            "and `docs/` for the installed Mineflayer APIs. Record verified "
            "learnings in `memory.md`; keep `plan.md` focused on the active goal.\n",
            encoding="utf-8",
        )
        if mineflayer_docs:
            self._copy_docs(Path(mineflayer_docs), self.root / "docs/mineflayer")
        if pathfinder_docs:
            self._copy_docs(Path(pathfinder_docs), self.root / "docs/pathfinder")
        if self.shared_skills and self.shared_skills.is_dir():
            library = self.root / "skills/library"
            library.mkdir(parents=True, exist_ok=True)
            for source in sorted(self.shared_skills.iterdir()):
                if source.is_dir() and not source.is_symlink():
                    destination = library / source.name
                    if not destination.exists():
                        shutil.copytree(source, destination, symlinks=False)
        (self.root / "docs/mineflayer/agent-quick-reference.md").write_text(
            "# Mineflayer and pathfinder quick reference\n\n"
            "These return different shapes: `bot.findBlocks(options)` returns an array of `Vec3` positions (`x`, `y`, `z` directly); "
            "`bot.findBlock(options)` returns a `Block` or `null`, whose coordinates are in `block.position`.\n\n"
            "```js\n"
            "const positions = bot.findBlocks({ matching: block => block.name.includes('log'), maxDistance: 32 });\n"
            "const pos = positions[0]; // Vec3: pos.x, pos.y, pos.z\n"
            "if (pos) {\n"
            "  const block = bot.blockAt(pos); // Block or null\n"
            "  const { goals } = require('mineflayer-pathfinder');\n"
            "  await bot.pathfinder.goto(new goals.GoalNear(pos.x, pos.y, pos.z, 2));\n"
            "}\n"
            "```\n\n"
            "`bot.inventory.items()` returns the inventory item array. `bot.recipesFor(itemTypeId, metadata, minResultCount, craftingTable)` returns recipes; pass a recipe object to `await bot.craft(recipe, count, craftingTable)`, never an item stack. `bot.registry.itemsByName.oak_planks.id` is the item type ID for oak planks.\n\n"
            "`GoalBlock(x, y, z)` asks the bot to stand inside that exact block position; use `GoalNear` when approaching a solid block. "
            "See the adjacent version-pinned package references for full options and errors.\n",
            encoding="utf-8",
        )
        doc_revisions = {
            "mineflayer": self._source_revision(Path(mineflayer_docs) if mineflayer_docs else None, "mineflayer"),
            "pathfinder": self._source_revision(Path(pathfinder_docs) if pathfinder_docs else None, "mineflayer-pathfinder"),
        }
        (self.root / "docs/README.md").write_text(
            "# Pinned API references\n\n"
            f"- Mineflayer: `{doc_revisions['mineflayer']}` in `mineflayer/`\n"
            f"- mineflayer-pathfinder: `{doc_revisions['pathfinder']}` in `pathfinder/`\n\n"
            "These files were copied from the exact npm packages installed for this run.\n",
            encoding="utf-8",
        )
        (self.root / ".runtime/docs-manifest.json").write_text(json.dumps(doc_revisions, indent=2) + "\n", encoding="utf-8")
        self._install_minecraft_runner()

    @staticmethod
    def _source_revision(source: Path | None, package_name: str) -> str:
        if source is None:
            return "unavailable"
        candidates = [source / "package.json"] if source.is_dir() else []
        candidates.extend((source.parent / "package.json", source.parent.parent / "package.json"))
        for package_file in candidates:
            try:
                package = json.loads(package_file.read_text(encoding="utf-8"))
                version = package.get("version")
                if package.get("name") == package_name and version:
                    return f"{package_name}@{version}"
            except (OSError, json.JSONDecodeError):
                pass
        return "unavailable"

    @staticmethod
    def _copy_docs(source: Path, destination: Path) -> None:
        source = source.resolve()
        if source.is_dir():
            for path in source.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(source)
                if len(relative.parts) > 1:
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        elif source.is_file():
            (destination / source.name).write_bytes(source.read_bytes())

    def _install_minecraft_runner(self) -> None:
        script = r'''#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

function fail(message) { process.stderr.write(`mc-run: ${message}\n`); process.exit(2); }
const args = process.argv.slice(2);
const positional = [];
for (let index = 0; index < args.length; index += 1) {
  if (args[index] === '--timeout') { index += 1; continue; }
  if (args[index].startsWith('--')) fail(`unknown option ${args[index]}`);
  positional.push(args[index]);
}
const scriptName = positional[0];
if (positional.length > 1) fail('only one script path is accepted');
const timeoutIndex = args.indexOf('--timeout');
const timeout = timeoutIndex >= 0 ? Number(args[timeoutIndex + 1]) : 60;
if (!scriptName) fail('usage: mc-run scripts/name.js [--timeout seconds]');
if (!Number.isFinite(timeout) || timeout < 1 || timeout > 43200) fail('timeout must be between 1 second and 12 hours');
const root = fs.realpathSync(process.cwd());
const target = path.resolve(root, scriptName);
if (!(target === root || target.startsWith(`${root}${path.sep}`)) || !fs.statSync(target, { throwIfNoEntry: false } )?.isFile()) fail('script must be a readable file inside this workspace');
const socketPath = process.env.MINECRAFT_AGENT_SOCKET;
if (!socketPath) fail('live Mineflayer session is unavailable');
const request = { version: 1, method: 'run_program', source: fs.readFileSync(target, 'utf8'), script: path.relative(root, target), timeout_seconds: timeout };
const client = net.createConnection(socketPath);
let data = '';
const timer = setTimeout(() => { client.destroy(); fail(`program response exceeded ${timeout + 10} seconds`); }, (timeout + 10) * 1000);
client.setEncoding('utf8');
client.once('connect', () => client.end(`${JSON.stringify(request)}\n`));
client.on('data', (chunk) => {
  data += chunk;
  if (Buffer.byteLength(data) > 1_000_000) { client.destroy(); fail('response exceeded 1 MB'); }
});
client.once('error', (error) => { clearTimeout(timer); fail(error.message); });
client.once('close', () => {
  clearTimeout(timer);
  try {
    const response = JSON.parse(data);
    process.stdout.write(`${JSON.stringify(response)}\n`);
    if (response.status !== 'completed') process.exitCode = 1;
  } catch (error) { fail(`invalid program response: ${error.message}`); }
});
'''
        path = self.root / "bin/mc-run"
        path.write_text(script, encoding="utf-8")
        path.chmod(0o700)

    def environment(self) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if not any(secret in key.lower() for secret in ("token", "secret", "password", "api_key", "credential", "authorization"))
        }
        bridge_root = Path(__file__).resolve().parents[2] / "bridge"
        env["PATH"] = os.pathsep.join((str(self.root / "bin"), str(bridge_root / "node_modules/.bin"), str(self.root / ".venv/bin"), env.get("PATH", "/usr/bin:/bin")))
        env["HOME"] = str(self.root)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (env.get("PYTHONPATH", ""), str(Path(__file__).resolve().parents[1]))))
        env["NODE_PATH"] = str(bridge_root / "node_modules")
        if self.bridge_socket:
            env["MINECRAFT_AGENT_SOCKET"] = str(self.bridge_socket)
        return env

    def _sandbox_command(self, command: str, timeout_seconds: float) -> tuple[list[str], Path | None]:
        """Confine shell helpers to the run workspace and the bot capability socket."""
        import shutil

        bwrap = shutil.which("bwrap")
        prlimit = shutil.which("prlimit")
        if not self.sandbox.available:
            return ["bash", "-c", command], None

        bridge_root = Path(__file__).resolve().parents[2] / "bridge"
        env = self.environment()
        if self.sandbox.backend == "bwrap" and bwrap and prlimit:
            argv = [
                prlimit,
                f"--cpu={max(3, int(timeout_seconds) + 5)}",
                "--nproc=64",
                "--fsize=1073741824",
                "--",
                bwrap,
                "--die-with-parent", "--new-session", "--unshare-all",
                "--ro-bind", "/usr", "/usr",
            ]
            for source in ("/lib", "/lib64", "/bin"):
                resolved = Path(source).resolve()
                if resolved.exists():
                    argv.extend(("--ro-bind", str(resolved), source))
            argv.extend(("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"))
            argv.extend(("--bind", str(self.root), "/work"))
            argv.extend(("--ro-bind", str(bridge_root / "node_modules"), "/node_modules"))
            if self.bridge_socket:
                argv.extend(("--ro-bind", str(self.bridge_socket), "/run/minecraft.sock"))
            argv.extend(("--chdir", "/work", "--clearenv"))
            for name, value in (("PATH", "/work/bin:/usr/local/bin:/usr/bin:/bin"), ("HOME", "/work"), ("LANG", env.get("LANG", "C.UTF-8")), ("NODE_PATH", "/node_modules")):
                argv.extend(("--setenv", name, value))
            if self.bridge_socket:
                argv.extend(("--setenv", "MINECRAFT_AGENT_SOCKET", "/run/minecraft.sock"))
            version = env.get("MINECRAFT_VERSION")
            if version:
                argv.extend(("--setenv", "MINECRAFT_VERSION", version))
            argv.extend(("--", "bash", "-c", command))
            return argv, None

        if self.sandbox.backend == "docker" and self.sandbox.image:
            docker = shutil.which("docker")
            if not docker:
                return ["bash", "-c", command], None
            cid_dir = self.root.parent / ".runtime"
            cid_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            cidfile = cid_dir / f"shell-{uuid.uuid4().hex}.cid"
            argv = [
                docker, "run", "--rm", "--pull=never", "--cidfile", str(cidfile),
                "--network=none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "64",
                "--memory", os.getenv("MINECRAFT_AGENT_SANDBOX_MEMORY", "2g"), "--cpus", "2",
                "--ulimit", f"cpu={max(3, int(timeout_seconds) + 5)}:{max(3, int(timeout_seconds) + 5)}",
                "--ulimit", "fsize=1073741824:1073741824", "--ulimit", "nofile=1024:1024",
                "--volume", f"{self.root}:/work:rw",
                "--volume", f"{bridge_root / 'node_modules'}:/node_modules:ro",
                "--workdir", "/work", "--env", "HOME=/work", "--env", "NODE_PATH=/node_modules",
                "--env", "PATH=/work/bin:/usr/local/bin:/usr/bin:/bin", "--entrypoint", "/usr/bin/bash",
            ]
            if self.bridge_socket and self.bridge_socket.exists():
                argv.extend(("--volume", f"{self.bridge_socket}:/run/minecraft.sock:ro", "--env", "MINECRAFT_AGENT_SOCKET=/run/minecraft.sock"))
            if env.get("MINECRAFT_VERSION"):
                argv.extend(("--env", f"MINECRAFT_VERSION={env['MINECRAFT_VERSION']}"))
            argv.extend((self.sandbox.image, "-c", command))
            return argv, cidfile
        return ["bash", "-c", command], None

    def _kill_sandbox_container(self, cidfile: Path | None) -> None:
        if not cidfile:
            return
        try:
            container_id = cidfile.read_text(encoding="ascii").strip()
        except OSError:
            return
        if container_id:
            docker = shutil.which("docker")
            if docker:
                try:
                    subprocess.run([docker, "kill", container_id], capture_output=True, timeout=5, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass

    def _resolve(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("path must be a non-empty workspace-relative string")
        candidate = (self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path must stay inside this run's workspace")
        return candidate

    def _snapshot(self) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_symlink() or ".history" in path.parts:
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative.startswith(".runtime/"):
                continue
            try:
                result[relative] = path.read_bytes()
            except OSError:
                continue
        return result

    def _record_revision(self, tool: str, before: dict[str, bytes], after: dict[str, bytes]) -> None:
        changed = []
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path), after.get(path)
            if old == new:
                continue
            old_hash = _sha256(old) if old is not None else None
            new_hash = _sha256(new) if new is not None else None
            diff = ""
            if path.endswith((".js", ".py", ".md", ".json", ".sh")) and (old or new):
                old_lines = (old or b"").decode("utf-8", errors="replace").splitlines()
                new_lines = (new or b"").decode("utf-8", errors="replace").splitlines()
                diff, _ = _bounded("\n".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")), _REVISION_LIMIT)
            changed.append({"path": path, "old_sha256": old_hash, "sha256": new_hash, "diff": diff})
        if changed and self.events:
            self.events.append("workspace_revision", {"tool": tool, "files": changed})

    def execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in TOOL_NAMES:
            return {"ok": False, "error": {"code": "unknown_tool", "message": f"tool must be one of {', '.join(TOOL_NAMES)}"}}
        started = time.monotonic()
        try:
            result = getattr(self, f"_{tool}")(**arguments)
            result.setdefault("ok", True)
        except Exception as exc:
            result = {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)[:500]}}
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        if self.events:
            self.events.append("workspace_tool", {"tool": tool, "arguments": arguments, "result": result})
        return result

    def _read(self, path: str, full: bool = False) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        visible, truncated = (content, False) if full else _bounded(content)
        return {"path": target.relative_to(self.root).as_posix(), "content": visible, "truncated": truncated}

    def _write(self, path: str, content: str) -> dict[str, Any]:
        target = self._resolve(path)
        if not isinstance(content, str):
            raise ValueError("content must be text")
        before = self._snapshot()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        after = self._snapshot()
        self._record_revision("write", before, after)
        return {"path": target.relative_to(self.root).as_posix(), "bytes": target.stat().st_size, "sha256": _sha256(target.read_bytes())}

    def _edit(self, path: str, old: str, new: str, replace_all: bool = False) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        content = target.read_text(encoding="utf-8")
        count = content.count(old)
        if not old or count == 0:
            raise ValueError("edit target text was not found")
        if count != 1 and not replace_all:
            raise ValueError(f"edit target occurs {count} times; set replace_all to true")
        before = self._snapshot()
        updated = content.replace(old, new, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        after = self._snapshot()
        self._record_revision("edit", before, after)
        return {"path": target.relative_to(self.root).as_posix(), "replacements": count if replace_all else 1, "sha256": _sha256(target.read_bytes())}

    def _bash(self, command: str, timeout_seconds: float = 60) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty shell command")
        timeout_seconds = float(timeout_seconds)
        if timeout_seconds < 1 or timeout_seconds > 43200:
            raise ValueError("timeout_seconds must be between 1 and 12 hours")
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"invalid shell command: {exc}") from exc
        if len(tokens) == 3 and tokens[:2] == ["mc-skill", "publish"]:
            return self._publish_skill(tokens[2], timeout_seconds)
        return self._run_shell(command, timeout_seconds)

    def _run_shell(self, command: str, timeout_seconds: float) -> dict[str, Any]:
        before = self._snapshot()
        sandbox_argv, cidfile = self._sandbox_command(command, timeout_seconds)
        process = subprocess.Popen(
            sandbox_argv, cwd=self.root, env=self.environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        with self._lock:
            self._active_process = process
            self._active_container_idfile = cidfile
        status = "completed"
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            status = "timeout"
            try:
                self._kill_sandbox_container(cidfile)
                os.killpg(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill_sandbox_container(cidfile)
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None
                    self._active_container_idfile = None
            if cidfile:
                cidfile.unlink(missing_ok=True)
        after = self._snapshot()
        self._record_revision("bash", before, after)
        out, out_truncated = _bounded(stdout)
        err, err_truncated = _bounded(stderr)
        return {"status": status, "exit_code": process.returncode, "stdout": out, "stderr": err, "stdout_truncated": out_truncated, "stderr_truncated": err_truncated, "sandbox": self.sandbox.reason}

    def _publish_skill(self, path: str, timeout_seconds: float) -> dict[str, Any]:
        if self.shared_skills is None:
            raise RuntimeError("shared skill library is not configured")
        source = self._resolve(path)
        library_root = self.root / "skills/library"
        if not source.is_dir() or not source.is_relative_to(self.root / "skills") or source.is_relative_to(library_root):
            raise ValueError("publish a skill directory beneath skills/<name>")
        skill_file = source / "SKILL.md"
        test_file = source / "test.sh"
        if not skill_file.is_file() or not skill_file.read_text(encoding="utf-8", errors="replace").strip():
            raise ValueError("skill directory must contain a non-empty SKILL.md")
        if not test_file.is_file():
            raise ValueError("skill directory must contain test.sh; publish only tested skills")
        for item in source.rglob("*"):
            if item.is_symlink():
                raise ValueError("skill publication refuses symlinks")
        test_command = f"bash {shlex.quote(test_file.relative_to(self.root).as_posix())}"
        test_result = self._run_shell(test_command, timeout_seconds)
        if test_result.get("status") != "completed" or test_result.get("exit_code") != 0:
            if self.events:
                self.events.append("skill_test_failed", {"path": source.relative_to(self.root).as_posix(), "command": test_command, "result": test_result})
            return {"status": "test_failed", "test": test_result}
        digest = hashlib.sha256()
        files = []
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source).as_posix()
            file_hash = _sha256(item.read_bytes())
            files.append({"path": relative, "sha256": file_hash, "bytes": item.stat().st_size})
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_hash.encode("ascii"))
            digest.update(b"\n")
        tree_hash = digest.hexdigest()
        slug = source.name
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", slug):
            raise ValueError("skill directory name must use lowercase letters, digits, underscores, or hyphens")
        library_entry = self.shared_skills / slug
        existing = sorted(library_entry.glob("v[0-9][0-9][0-9]-*")) if library_entry.exists() else []
        version = len(existing) + 1
        target = library_entry / f"v{version:03d}-{tree_hash[:10]}"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(source, target, symlinks=False)
        manifest = {
            "schema_version": 1, "name": slug, "version": version,
            "sha256": tree_hash, "source_run_workspace": str(source),
            "test": {"command": test_command, "status": "passed", "exit_code": 0, "stdout": test_result.get("stdout", "")[:2000]},
            "files": files,
        }
        (target / "skill-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if self.events:
            self.events.append("skill_published", {"name": slug, "version": version, "sha256": tree_hash, "path": str(target), "test": manifest["test"]})
        return {"status": "published", "name": slug, "version": version, "sha256": tree_hash, "path": str(target), "test": manifest["test"]}

    def _grep(self, pattern: str, path: str = ".", ignore_case: bool = False, max_matches: int = 50) -> dict[str, Any]:
        root = self._resolve(path)
        if not root.exists():
            raise FileNotFoundError(f"path not found: {path}")
        flags = re.IGNORECASE if ignore_case else 0
        query = re.compile(pattern, flags)
        matches = []
        for file in ([root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())):
            if any(part in {".runtime", ".history", "node_modules", ".git"} for part in file.parts):
                continue
            try:
                with file.open(encoding="utf-8", errors="replace") as stream:
                    for number, line in enumerate(stream, 1):
                        if query.search(line):
                            matches.append({"path": file.relative_to(self.root).as_posix(), "line": number, "text": line.rstrip()[:500]})
                            if len(matches) >= max(1, min(int(max_matches), 200)):
                                return {"matches": matches, "truncated": True}
            except OSError:
                continue
        return {"matches": matches, "empty": not matches, "truncated": False}

    def _find(self, path: str = ".", pattern: str = "**/*", max_results: int = 100) -> dict[str, Any]:
        root = self._resolve(path)
        if not root.exists():
            raise FileNotFoundError(f"path not found: {path}")
        candidates = [root] if root.is_file() else sorted(root.glob(pattern))
        found = []
        for item in candidates:
            if any(part in {".runtime", ".history", "node_modules", ".git"} for part in item.parts):
                continue
            found.append({"path": item.relative_to(self.root).as_posix(), "kind": "directory" if item.is_dir() else "file", "bytes": item.stat().st_size if item.is_file() else None})
            if len(found) >= max(1, min(int(max_results), 500)):
                break
        return {"matches": found, "empty": not found, "truncated": len(candidates) > len(found)}

    def _ls(self, path: str = ".", all: bool = False) -> dict[str, Any]:
        root = self._resolve(path)
        if not root.exists():
            raise FileNotFoundError(f"path not found: {path}")
        if root.is_file():
            return {"entries": [{"name": root.name, "kind": "file", "bytes": root.stat().st_size}]}
        entries = []
        for item in sorted(root.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())):
            if not all and item.name.startswith("."):
                continue
            entries.append({"name": item.name, "kind": "directory" if item.is_dir() else "file", "bytes": item.stat().st_size if item.is_file() else None})
        return {"entries": entries, "empty": not entries}

    def cancel_active(self) -> None:
        with self._lock:
            process = self._active_process
            cidfile = self._active_container_idfile
        if process and process.poll() is None:
            self._kill_sandbox_container(cidfile)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._kill_sandbox_container(cidfile)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if cidfile:
            cidfile.unlink(missing_ok=True)


def tool_schemas() -> list[dict[str, Any]]:
    """Small, stable tool descriptions included in the model's system prefix."""
    return [
        {"name": "read", "description": "Read a workspace file; use full=true to avoid default truncation.", "parameters": {"path": "string", "full": "boolean?"}},
        {"name": "write", "description": "Create or replace a workspace file.", "parameters": {"path": "string", "content": "string"}},
        {"name": "edit", "description": "Replace exact text in one workspace file.", "parameters": {"path": "string", "old": "string", "new": "string", "replace_all": "boolean?"}},
        {"name": "bash", "description": "Run a shell command from the workspace; timeout defaults to 60 seconds. Set it at least 10 seconds longer than an mc-run script timeout.", "parameters": {"command": "string", "timeout_seconds": "number?"}},
        {"name": "grep", "description": "Search workspace text by regular expression.", "parameters": {"pattern": "string", "path": "string?", "ignore_case": "boolean?", "max_matches": "integer?"}},
        {"name": "find", "description": "Find workspace paths by glob.", "parameters": {"path": "string?", "pattern": "string?", "max_results": "integer?"}},
        {"name": "ls", "description": "List one workspace directory; all=true includes hidden entries.", "parameters": {"path": "string?", "all": "boolean?"}},
    ]


def run_program_socket(socket_path: str | Path, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """Small socket client used by `mc-run` integration tests and notebook controls."""
    path = Path(socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout + 10)
        client.connect(str(path))
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        data = bytearray()
        while chunk := client.recv(64 * 1024):
            data.extend(chunk)
            if len(data) > 1_000_000:
                raise ValueError("program response exceeds 1 MB")
    return json.loads(data)
