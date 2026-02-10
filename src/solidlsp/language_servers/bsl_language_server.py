"""
Provides BSL (1C:Enterprise) specific instantiation of the LanguageServer class.
Uses a local Python-based parser and in-memory call graph cache instead of an external Java LSP process.
"""

import hashlib
import logging
import os
import pathlib
import platform
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from overrides import override

from solidlsp import ls_types
from solidlsp.bsl_cache import BSLCache
from solidlsp.bsl_parser import BSLMethod, BSLParser
from solidlsp.ls import DocumentSymbols, SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_utils import TextUtils
from solidlsp.lsp_protocol_handler.lsp_types import InitializeParams
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings

log = logging.getLogger(__name__)


class BslLanguageServer(SolidLanguageServer):
    """
    BSL (1C:Enterprise) language server using a local Python-based parser.

    Instead of launching an external Java bsl-language-server process, this implementation:
    - Parses BSL files using a regex-based Python parser (BSLParser)
    - Builds an in-memory call graph (BSLCache) for references and rename
    - Uses fingerprint-based (MD5) cache validation for incremental updates
    - Indexes files in parallel using ThreadPoolExecutor
    - Stores DocumentSymbols in the standard pickle-based cache for persistence

    All configuration is automatic — no external dependencies required.
    """

    # BSL-specific directories to ignore during file scanning
    _BSL_IGNORED_DIRS = frozenset(
        {
            "build",
            ".bsl-language-server",
            ".vscode",
            ".idea",
            "bin",
            "out",
            "oscript_modules",
            ".cursor",
            ".serena",
            ".git",
        }
    )

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """
        Creates a BslLanguageServer instance. This class is not meant to be instantiated directly.
        Use LanguageServer.create() instead.
        """
        # We pass a dummy command since we don't actually launch a process
        system = platform.system()
        if system == "Windows":
            dummy_cmd: list[str] = ["cmd", "/c", "exit", "0"]
        else:
            dummy_cmd = ["true"]

        super().__init__(
            config,
            repository_root_path,
            ProcessLaunchInfo(cmd=dummy_cmd, cwd=repository_root_path),
            "bsl",
            solidlsp_settings,
        )

        self.server_ready = threading.Event()

        # Settings from custom_settings
        custom_settings = getattr(self, "_custom_settings", {}) or {}
        self.enable_hash_prefiltering: bool = custom_settings.get("enable_hash_prefiltering", True)
        self.file_read_parallelism: int = custom_settings.get("file_read_parallelism", 500)

        # In-memory call graph cache
        self._local_cache = BSLCache()
        # File content cache to avoid re-reading during conversion
        self._file_content_cache: dict[str, str] = {}
        # Track already-converted files for incremental conversion
        self._converted_files: set[str] = set()

        # Concurrency control
        self._processing_files: set[str] = set()
        self._indexing_lock = threading.Lock()

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        """Check if directory should be ignored during file scanning."""
        return super().is_ignored_dirname(dirname) or dirname in self._BSL_IGNORED_DIRS

    def _setup_runtime_dependencies(self, config: LanguageServerConfig, solidlsp_settings: SolidLSPSettings) -> list[str]:
        """Return a no-op command — we don't need an external process."""
        log.info("BSL language server: local cache only mode (no external process)")
        system = platform.system()
        if system == "Windows":
            return ["cmd", "/c", "exit", "0"]
        return ["true"]

    @staticmethod
    def _get_initialize_params(repository_absolute_path: str) -> InitializeParams:
        """Returns the initialize params for the BSL Language Server."""
        root_uri = pathlib.Path(repository_absolute_path).as_uri()
        initialize_params: dict[str, Any] = {
            "locale": "ru",
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True, "dynamicRegistration": True},
                    "completion": {"dynamicRegistration": True, "completionItem": {"snippetSupport": True}},
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "hover": {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]},
                    "formatting": {"dynamicRegistration": True},
                    "rangeFormatting": {"dynamicRegistration": True},
                    "rename": {"dynamicRegistration": True, "prepareSupport": True},
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                },
            },
            "processId": os.getpid(),
            "rootPath": repository_absolute_path,
            "rootUri": root_uri,
            "workspaceFolders": [
                {
                    "uri": root_uri,
                    "name": os.path.basename(repository_absolute_path),
                }
            ],
        }
        return initialize_params  # type: ignore

    @override
    def _start_server(self) -> None:
        """
        Start the BSL Language Server in local cache only mode.
        Finds all BSL files, checks fingerprints, indexes changed files, and saves cache.
        """
        log.info("BSL Language Server: Starting in local cache only mode")

        self.completions_available.set()

        try:
            # 1. Find all BSL files
            all_bsl_files = self._find_all_bsl_files()
            log.info(f"BSL Language Server: Found {len(all_bsl_files)} BSL files")

            if all_bsl_files:
                # 2. Determine files that need indexing
                files_to_index = self._find_files_to_index(all_bsl_files)

                if files_to_index:
                    log.info(f"BSL Language Server: Indexing {len(files_to_index)} files (new or changed)")

                    def save_cache_callback() -> None:
                        try:
                            self.save_cache()
                        except Exception as e:
                            log.warning(f"Failed to save cache during indexing: {e}")

                    results = self._index_files_with_local_parser(files_to_index, save_cache_callback)
                    error_count = sum(1 for r in results.values() if r is not None)
                    if error_count > 0:
                        log.warning(f"BSL Language Server: {error_count} files failed to index")

                    # Convert local cache to DocumentSymbols (only new files)
                    try:
                        self._convert_cache_to_document_symbols(only_new_files=True)
                    except Exception as e:
                        log.warning(f"Failed to convert local cache to DocumentSymbols: {e}")

                    log.info(f"BSL Language Server: Successfully indexed {len(files_to_index) - error_count} files")
                else:
                    log.info("BSL Language Server: All files are up to date")

                # 3. Remove deleted files from cache
                try:
                    removed_count = self._remove_deleted_files_from_cache(all_bsl_files)
                    if removed_count > 0:
                        log.info(f"BSL Language Server: Removed {removed_count} deleted files from cache")
                except Exception as e:
                    log.warning(f"Failed to remove deleted files from cache: {e}")

                # 4. Save cache
                try:
                    self.save_cache()
                    log.info("BSL Language Server: Cache saved successfully")
                except Exception as e:
                    log.warning(f"Failed to save cache: {e}")
            else:
                log.info("BSL Language Server: No BSL files found, skipping indexing")

        except Exception as e:
            log.exception(f"Error during cache update: {e}")

        log.info("BSL Language Server: Local cache mode ready")
        self.server_ready.set()

    @override
    def is_running(self) -> bool:
        """In local cache mode, server is running if it was marked as started."""
        return self.server_started

    @override
    def _save_cache_stats(self) -> None:
        """Save cache stats with BSL-specific data (methods, modules, calls).

        Only writes when the document symbols cache was actually modified.
        If _local_cache has no data (restart with no file changes), preserves
        existing BSL stats from the previous cache_stats.json.
        """
        if not self._document_symbols_cache_is_modified:
            return

        import datetime
        import json

        stats_file = Path(self.repository_root_path) / self._solidlsp_settings.project_data_relative_path / "cache_stats.json"
        try:
            bsl_stats = self._local_cache.get_stats()

            # If _local_cache is empty (no new files parsed), preserve existing BSL stats
            if bsl_stats.get("methods", 0) == 0 and stats_file.exists():
                try:
                    with open(stats_file, encoding="utf-8") as f:
                        existing = json.load(f)
                    if existing.get("bsl") and existing["bsl"].get("methods", 0) > 0:
                        bsl_stats = existing["bsl"]
                except (json.JSONDecodeError, OSError, KeyError):
                    pass

            stats: dict[str, Any] = {
                "indexed_files": len(self._document_symbols_cache),
                "language": self.language_id,
                "last_updated": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "bsl": bsl_stats,
            }
            stats_file.parent.mkdir(parents=True, exist_ok=True)
            with open(stats_file, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
        except Exception as e:
            log.debug("Failed to save cache stats: %s", e)

    # ─── File Discovery ────────────────────────────────────────────────

    def _find_all_bsl_files(self) -> list[str]:
        """
        Recursively collect all .bsl files in the project.

        :return: List of relative paths (normalized with '/') to .bsl files
        """
        bsl_files: list[str] = []
        for root, dirs, files in os.walk(self.repository_root_path):
            dirs[:] = [d for d in dirs if not self.is_ignored_dirname(d)]
            for file in files:
                if not file.endswith(".bsl"):
                    continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, self.repository_root_path).replace("\\", "/")
                if not self.is_ignored_path(rel_path):
                    bsl_files.append(rel_path)
        return bsl_files

    def _find_files_to_index(self, all_files: list[str]) -> list[str]:
        """
        Determine which files need indexing based on hash comparison.

        :return: List of files that are new or have changed
        """
        files_to_index: list[str] = []
        skipped_count = 0

        for file_path in all_files:
            try:
                file_hash = self._compute_file_hash(file_path)
                if not file_hash:
                    files_to_index.append(file_path)
                    continue

                if self.enable_hash_prefiltering and self._is_file_cached(file_path, file_hash):
                    skipped_count += 1
                    continue

                files_to_index.append(file_path)
            except Exception as e:
                log.debug(f"Error checking file {file_path}: {e}, will index it")
                files_to_index.append(file_path)

        if skipped_count > 0:
            log.info(f"BSL: Skipped {skipped_count} unchanged files (hash prefiltering)")

        return files_to_index

    def _remove_deleted_files_from_cache(self, existing_files: list[str]) -> int:
        """
        Remove cache entries for files that no longer exist on disk.

        :return: Number of removed files
        """
        existing_files_set = set(existing_files)
        removed_count = 0
        cache_keys = list(self._document_symbols_cache.keys())

        for cache_key in cache_keys:
            # Handle both string and tuple cache key formats
            relative_file_path = cache_key

            if relative_file_path not in existing_files_set:
                try:
                    del self._document_symbols_cache[cache_key]
                    self._document_symbols_cache_is_modified = True

                    if relative_file_path in self._raw_document_symbols_cache:
                        del self._raw_document_symbols_cache[relative_file_path]
                        self._raw_document_symbols_cache_is_modified = True

                    self._local_cache.remove_file_data(relative_file_path)
                    self._converted_files.discard(relative_file_path)
                    self._file_content_cache.pop(relative_file_path, None)
                    removed_count += 1
                    log.debug(f"Removed deleted file from cache: {relative_file_path}")
                except Exception as e:
                    log.warning(f"Failed to remove deleted file {relative_file_path} from cache: {e}")

        return removed_count

    # ─── Fingerprint System ────────────────────────────────────────────

    def _compute_file_hash(self, relative_file_path: str) -> str:
        """
        Compute MD5 hash of normalized (CRLF→LF) UTF-8 file content.

        :return: Hex digest of the hash, or empty string on failure
        """
        abs_path = os.path.join(self.repository_root_path, relative_file_path)
        try:
            with open(abs_path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            return hashlib.md5(content.encode("utf-8")).hexdigest()
        except Exception as e:
            log.debug(f"Failed to compute hash for {relative_file_path}: {e}")
            return ""

    def _is_file_cached(self, relative_file_path: str, file_hash: str) -> bool:
        """Check if a file is already cached with the given hash."""
        cache_key = relative_file_path
        if cache_key in self._document_symbols_cache:
            cached_hash, _ = self._document_symbols_cache[cache_key]
            return cached_hash == file_hash
        return False

    # ─── Module Path Resolution ────────────────────────────────────────

    def _get_module_for_path(self, fullpath: str, root_path: str) -> str:
        """
        Extract 1C module name from file path.

        For CommonModules: returns the module name (e.g. "ИмяМодуля")
        For other types: returns "ParentType.ObjectName" (e.g. "Документы.ИмяДокумента")
        """
        try:
            if root_path.endswith(("\\", "/")):
                rel_path = fullpath[len(root_path) :]
            else:
                rel_path = fullpath[len(root_path) + 1 :]

            parts = rel_path.replace("\\", "/").split("/")
            hierarchy = len(parts)

            if hierarchy > 3:
                parent_type = parts[hierarchy - 4]
                if parent_type.startswith(("CommonModules", "ОбщиеМодули")):
                    return parts[hierarchy - 3]

                type_mapping = {
                    "Documents": "Документы",
                    "Catalogs": "Справочники",
                    "InformationRegisters": "РегистрыСведений",
                    "AccumulationRegisters": "РегистрыНакопления",
                    "Enums": "Перечисления",
                    "Constants": "Константы",
                    "CommonCommands": "ОбщиеКоманды",
                    "WebServices": "ВебСервисы",
                    "BusinessProcesses": "БизнесПроцессы",
                    "Tasks": "Задачи",
                }
                mapped_type = type_mapping.get(parent_type, parent_type)
                return f"{mapped_type}.{parts[hierarchy - 3]}"

            return ""
        except Exception:
            return ""

    # ─── File Parsing & Indexing ───────────────────────────────────────

    def _parse_file_local(self, relative_file_path: str) -> None:
        """
        Parse a single BSL file and add results to the local cache.
        Also immediately converts to DocumentSymbols for the standard cache.

        :param relative_file_path: Relative path to the .bsl file
        """
        abs_path = os.path.join(self.repository_root_path, relative_file_path)
        if not os.path.exists(abs_path):
            log.debug(f"File not found: {abs_path}")
            return

        try:
            with open(abs_path, encoding="utf-8", errors="ignore") as f:
                source = f.read()

            source = source.replace("\r\n", "\n").replace("\r", "\n")
            file_hash = hashlib.md5(source.encode("utf-8")).hexdigest()

            # Skip if already cached with matching hash
            cache_key = relative_file_path
            if cache_key in self._document_symbols_cache:
                cached_hash, _ = self._document_symbols_cache[cache_key]
                if cached_hash == file_hash:
                    log.debug(f"File {relative_file_path} already cached, skipping")
                    return

            if not source.strip():
                log.debug(f"File {relative_file_path} is empty, skipping")
                return

            log.debug(f"Parsing file: {relative_file_path} ({len(source)} chars)")
            parser = BSLParser()
            parse_result = parser.parse(source)
            log.info(
                f"Parsed: {relative_file_path} — "
                f"{len(parse_result.methods)} methods, "
                f"{len(parse_result.module_vars)} vars, "
                f"{len(parse_result.global_calls)} global calls"
            )

            module = self._get_module_for_path(abs_path, self.repository_root_path)

            # Add to call graph cache
            if parse_result.methods:
                self._local_cache.add_methods_batch([(m, relative_file_path, module) for m in parse_result.methods])

            if parse_result.module_vars:
                self._local_cache.add_module_vars_batch([(v, relative_file_path) for v in parse_result.module_vars.values()])

            if parse_result.global_calls:
                self._local_cache.add_calls_batch([(c, relative_file_path, "GlobalModuleText", module) for c in parse_result.global_calls])

            method_calls: list[tuple[Any, str, str, str]] = []
            for method in parse_result.methods:
                for call in method.calls_position:
                    method_calls.append((call, relative_file_path, method.name, module))
            if method_calls:
                self._local_cache.add_calls_batch(method_calls)

            # Immediately convert to DocumentSymbols
            self._convert_file_to_document_symbols(relative_file_path, abs_path, source, file_hash, parse_result.methods, module)

        except Exception as e:
            log.exception(f"Failed to parse file {relative_file_path} locally: {e}")
            raise

    def _index_files_with_local_parser(
        self,
        file_paths: list[str],
        save_cache_callback: Any = None,
    ) -> dict[str, Exception | None]:
        """
        Mass-index files using the local Python parser with parallel execution.

        :param file_paths: List of relative file paths to index
        :param save_cache_callback: Optional callback to save cache periodically
        :return: Dict mapping file_path -> Exception (if failed) or None (if succeeded)
        """
        total_files = len(file_paths)
        log.info(f"Starting local parser indexing: {total_files} files with {self.file_read_parallelism} workers")

        results: dict[str, Exception | None] = {}
        completed_count = 0
        start_time = time.time()
        file_timeout = 30.0

        with ThreadPoolExecutor(max_workers=self.file_read_parallelism) as executor:
            futures = {executor.submit(self._parse_file_local, fp): fp for fp in file_paths}
            last_logged_percent = -1
            processed_files: set[str] = set()

            # Stuck detection
            stop_event = threading.Event()

            def check_stuck() -> None:
                last_count = completed_count
                time.sleep(60)
                if not stop_event.is_set() and completed_count == last_count:
                    log.warning(
                        f"Possible stuck: no progress in 60s. "
                        f"Completed: {completed_count}/{total_files}, "
                        f"Active: {sum(1 for f in futures if not f.done())}"
                    )

            stuck_checker = threading.Thread(target=check_stuck, daemon=True)
            stuck_checker.start()

            try:
                for future in as_completed(futures):
                    file_path = futures[future]
                    if file_path in processed_files:
                        continue
                    processed_files.add(file_path)

                    try:
                        try:
                            future.result(timeout=file_timeout)
                            results[file_path] = None
                        except TimeoutError:
                            log.warning(f"Timeout ({file_timeout}s) parsing {file_path}")
                            results[file_path] = TimeoutError(f"Timeout after {file_timeout}s")
                            future.cancel()

                        completed_count += 1

                        percent = (completed_count * 100) // total_files if total_files > 0 else 0
                        should_log = percent >= last_logged_percent + 5 or completed_count % 50 == 0 or completed_count == total_files
                        if should_log:
                            elapsed = time.time() - start_time
                            fps = completed_count / elapsed if elapsed > 0 else 0
                            remaining = total_files - completed_count
                            log.info(
                                f"Local parser: {completed_count}/{total_files} ({percent}%) "
                                f"[{fps:.1f} files/sec, {remaining} remaining]"
                            )
                            last_logged_percent = percent

                        if save_cache_callback and completed_count % 200 == 0:
                            try:
                                save_cache_callback()
                            except Exception as e:
                                log.warning(f"Failed to save cache: {e}")

                    except Exception as e:
                        log.exception(f"Unexpected error processing {file_path}: {e}")
                        results[file_path] = e
                        completed_count += 1
            finally:
                stop_event.set()
                pending = [f for f in futures if not f.done()]
                if pending:
                    log.warning(f"{len(pending)} tasks did not complete")
                    for f in pending:
                        fp = futures[f]
                        if fp not in results:
                            results[fp] = Exception("Task did not complete")
                            completed_count += 1

        if save_cache_callback:
            try:
                save_cache_callback()
            except Exception as e:
                log.warning(f"Failed to save final cache: {e}")

        elapsed = time.time() - start_time
        fps = completed_count / elapsed if elapsed > 0 else 0
        stats = self._local_cache.get_stats()
        log.info(
            f"Local parser completed: {completed_count}/{total_files} files "
            f"[{fps:.1f} files/sec, {stats.get('methods', 0)} methods indexed]"
        )
        return results

    # ─── DocumentSymbols Conversion ────────────────────────────────────

    def _create_symbol_from_method(
        self,
        method: BSLMethod,
        file_content: str,
        lines: list[str],
        abs_path: str,
        relative_file_path: str,
    ) -> ls_types.UnifiedSymbolInformation:
        """
        Create a UnifiedSymbolInformation dict from a BSLMethod.
        Shared helper to avoid duplication between batch and single-file conversion.
        """
        kind = 12 if not method.isproc else 6  # 12=Function, 6=Method(Procedure)

        start_line = method.line
        end_line = min(method.endline, len(lines) - 1) if method.endline < len(lines) else len(lines) - 1

        start_char = 0
        if start_line < len(lines):
            name_pos = lines[start_line].find(method.name)
            if name_pos != -1:
                start_char = name_pos

        end_char = len(lines[end_line]) if end_line < len(lines) else 0

        range_obj = ls_types.Range(
            start=ls_types.Position(line=start_line, character=start_char),
            end=ls_types.Position(line=end_line, character=end_char),
        )
        uri = pathlib.Path(abs_path).as_uri()
        location = ls_types.Location(
            uri=uri,
            range=range_obj,
            absolutePath=abs_path,
            relativePath=relative_file_path,
        )

        detail_parts: list[str] = []
        if method.context:
            detail_parts.append(method.context)
        if method.is_export:
            detail_parts.append("Экспорт")
        detail = " | ".join(detail_parts) if detail_parts else None

        body = self._extract_method_body(file_content, method)

        symbol: ls_types.UnifiedSymbolInformation = {  # type: ignore
            "name": method.name,
            "kind": ls_types.SymbolKind(kind),
            "location": location,
            "range": range_obj,
            "selectionRange": range_obj,
            "children": [],
            "body": body,
            "detail": detail or "",
            "description": method.description or None,
        }
        return symbol

    def _convert_file_to_document_symbols(
        self,
        relative_file_path: str,
        abs_path: str,
        file_content: str,
        file_hash: str,
        methods: list[BSLMethod],
        module: str,
    ) -> None:
        """
        Convert a single file's parsed methods to DocumentSymbols and store in cache.
        Called immediately after parsing for incremental cache updates.
        """
        cache_key = relative_file_path

        if not methods:
            document_symbols = DocumentSymbols([])
            self._document_symbols_cache[cache_key] = (file_hash, document_symbols)
            self._document_symbols_cache_is_modified = True
            self._raw_document_symbols_cache[cache_key] = (file_hash, None)
            self._raw_document_symbols_cache_is_modified = True
            return

        lines = file_content.split("\n")
        unified_symbols: list[ls_types.UnifiedSymbolInformation] = []
        for method in methods:
            symbol = self._create_symbol_from_method(method, file_content, lines, abs_path, relative_file_path)
            unified_symbols.append(symbol)

        document_symbols = DocumentSymbols(unified_symbols)
        self._document_symbols_cache[cache_key] = (file_hash, document_symbols)
        self._document_symbols_cache_is_modified = True
        self._raw_document_symbols_cache[cache_key] = (file_hash, None)
        self._raw_document_symbols_cache_is_modified = True
        self._converted_files.add(relative_file_path)

    def _convert_cache_to_document_symbols(self, only_new_files: bool = True) -> None:
        """
        Batch-convert local cache methods to DocumentSymbols format.

        :param only_new_files: If True, only convert files not yet in _converted_files
        """
        methods_by_file: dict[str, list[Any]] = defaultdict(list)
        for method_info in self._local_cache.methods:
            filename = method_info.filename
            if only_new_files and filename in self._converted_files:
                continue
            methods_by_file[filename].append(method_info)

        if not methods_by_file:
            log.debug("No new files to convert to DocumentSymbols")
            return

        log.debug(f"Converting {len(methods_by_file)} files to DocumentSymbols (incremental={only_new_files})")
        conversion_start = time.time()

        for filename, method_infos in methods_by_file.items():
            try:
                abs_path = os.path.join(self.repository_root_path, filename)

                if filename in self._file_content_cache:
                    file_content = self._file_content_cache[filename]
                else:
                    if not os.path.exists(abs_path):
                        continue
                    with open(abs_path, encoding="utf-8", errors="ignore") as f:
                        file_content = f.read()
                    self._file_content_cache[filename] = file_content

                file_hash = hashlib.md5(file_content.encode("utf-8")).hexdigest()
                lines = file_content.split("\n")
                unified_symbols: list[ls_types.UnifiedSymbolInformation] = []

                for method_info in method_infos:
                    symbol = self._create_symbol_from_method(method_info.method, file_content, lines, abs_path, filename)
                    unified_symbols.append(symbol)

                document_symbols = DocumentSymbols(unified_symbols)
                self._document_symbols_cache[filename] = (file_hash, document_symbols)
                self._document_symbols_cache_is_modified = True
                self._raw_document_symbols_cache[filename] = (file_hash, None)
                self._raw_document_symbols_cache_is_modified = True
                self._converted_files.add(filename)

            except Exception as e:
                log.exception(f"Failed to convert cache for {filename}: {e}")

        log.debug(f"Converted {len(methods_by_file)} files in {time.time() - conversion_start:.2f}s")

    @staticmethod
    def _extract_method_body(file_content: str, method: BSLMethod) -> str:
        """Extract the body text of a method from file content."""
        lines = file_content.split("\n")
        start_line = method.line
        end_line = min(method.endline, len(lines) - 1) if method.endline < len(lines) else len(lines) - 1
        if start_line > end_line or start_line >= len(lines):
            return ""
        return "\n".join(lines[start_line : end_line + 1])

    # ─── Optimized Symbol Tree ─────────────────────────────────────────

    @override
    def request_full_symbol_tree(self, within_relative_path: str | None = None) -> list[ls_types.UnifiedSymbolInformation]:
        """
        Optimized symbol tree built directly from cache, grouped by directories.
        Falls back to base implementation if cache is empty.
        """
        # If a specific file is requested, use standard implementation
        if within_relative_path is not None and within_relative_path != "":
            path_obj = Path(within_relative_path)
            if path_obj.is_absolute():
                try:
                    within_abs_path = str(path_obj.resolve())
                    relative_path = str(path_obj.relative_to(Path(self.repository_root_path)))
                except ValueError:
                    within_abs_path = str(path_obj.resolve())
                    relative_path = within_relative_path
            else:
                within_abs_path = os.path.join(self.repository_root_path, within_relative_path)
                relative_path = within_relative_path

            if not os.path.exists(within_abs_path):
                raise FileNotFoundError(f"File or directory not found: {within_abs_path}")
            if os.path.isfile(within_abs_path):
                if self.is_ignored_path(relative_path):
                    log.error("Explicitly passed file is ignored: %s", relative_path)
                    return []
                return self.request_document_symbols(relative_path).root_symbols

        # Build tree from cache
        log.debug("BSL: Building symbol tree from cache")

        cached_files: dict[str, tuple[str, DocumentSymbols]] = {}
        for cache_key, (file_hash, document_symbols) in self._document_symbols_cache.items():
            relative_file_path = cache_key
            if self.is_ignored_path(relative_file_path):
                continue

            # Filter by within_relative_path for directories
            if within_relative_path is not None and within_relative_path != "":
                try:
                    rel_str = str(Path(relative_file_path))
                    within_str = str(Path(within_relative_path))
                    if not (rel_str.startswith((within_str + os.sep, within_str)) or rel_str == within_str):
                        continue
                except Exception:
                    continue

            cached_files[relative_file_path] = (file_hash, document_symbols)

        if not cached_files:
            log.debug("BSL: No cached files, falling back to standard implementation")
            return super().request_full_symbol_tree(within_relative_path=within_relative_path)

        # Group files by directory
        directory_structure: dict[str, list[ls_types.UnifiedSymbolInformation]] = {}

        for relative_file_path, (_file_hash, document_symbols) in cached_files.items():
            file_path_obj = Path(relative_file_path)
            dir_path = str(file_path_obj.parent) if file_path_obj.parent != Path(".") else "."

            # Get file content for range
            file_content = self._file_content_cache.get(relative_file_path, "")
            if not file_content:
                abs_fp = os.path.join(self.repository_root_path, relative_file_path)
                try:
                    with open(abs_fp, encoding="utf-8", errors="ignore") as f:
                        file_content = f.read()
                except Exception:
                    continue

            file_range = self._get_range_from_file_content(file_content)
            file_root_nodes = document_symbols.root_symbols

            file_symbol = ls_types.UnifiedSymbolInformation(  # type: ignore
                name=os.path.splitext(file_path_obj.name)[0],
                kind=ls_types.SymbolKind.File,
                range=file_range,
                selectionRange=file_range,
                location=ls_types.Location(
                    uri=str(pathlib.Path(os.path.join(self.repository_root_path, relative_file_path)).as_uri()),
                    range=file_range,
                    absolutePath=str(os.path.join(self.repository_root_path, relative_file_path)),
                    relativePath=relative_file_path,
                ),
                children=file_root_nodes,
            )
            for child in file_root_nodes:
                child["parent"] = file_symbol

            if dir_path not in directory_structure:
                directory_structure[dir_path] = []
            directory_structure[dir_path].append(file_symbol)

        # Build directory hierarchy
        result: list[ls_types.UnifiedSymbolInformation] = []
        sorted_dirs = sorted(directory_structure.keys(), key=lambda x: (x.count(os.sep), x))
        dir_symbols: dict[str, ls_types.UnifiedSymbolInformation] = {}

        for dir_path in sorted_dirs:
            file_symbols = directory_structure[dir_path]
            current_path = dir_path

            while current_path != ".":
                if current_path not in dir_symbols:
                    if self.is_ignored_path(current_path):
                        break
                    dir_path_obj = Path(current_path)
                    dir_abs_path = os.path.join(self.repository_root_path, current_path)
                    dir_symbol = ls_types.UnifiedSymbolInformation(  # type: ignore
                        name=dir_path_obj.name,
                        kind=ls_types.SymbolKind.Package,
                        location=ls_types.Location(
                            uri=str(pathlib.Path(dir_abs_path).as_uri()),
                            range={"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                            absolutePath=dir_abs_path,
                            relativePath=current_path,
                        ),
                        children=[],
                    )

                    parent_path = str(Path(current_path).parent)
                    if parent_path == current_path:
                        break
                    if parent_path in dir_symbols:
                        parent_sym = dir_symbols[parent_path]
                        dir_symbol["parent"] = parent_sym
                        parent_sym["children"].append(dir_symbol)
                    else:
                        result.append(dir_symbol)

                    dir_symbols[current_path] = dir_symbol
                break

            if dir_path == ".":
                result.extend(file_symbols)
            elif dir_path in dir_symbols:
                dir_sym = dir_symbols[dir_path]
                dir_sym["children"].extend(file_symbols)
                for fs in file_symbols:
                    fs["parent"] = dir_sym
            else:
                result.extend(file_symbols)

        log.debug(f"BSL: Built symbol tree with {len(cached_files)} files in {len(directory_structure)} dirs")
        return result

    # ─── References via Call Graph ─────────────────────────────────────

    @override
    def request_references(self, relative_file_path: str, line: int, column: int) -> list[ls_types.Location]:
        """Find all references to a symbol using the local call graph cache."""
        symbol_name = self._get_symbol_name_at_position(relative_file_path, line, column)
        if not symbol_name:
            log.debug(f"Could not determine symbol at {relative_file_path}:{line}:{column}")
            return []

        log.debug(f"Looking for references to '{symbol_name}' via local cache")

        call_infos = self._local_cache.find_calls(symbol_name)
        if not call_infos:
            log.debug(f"No calls found for '{symbol_name}'")
            return []

        # Find the definition to exclude it from results
        symbol_definition: dict[str, Any] | None = None
        try:
            document_symbols = self.request_document_symbols(relative_file_path)
            for symbol in document_symbols.iter_symbols():
                if symbol.get("name") == symbol_name:
                    symbol_range = symbol.get("range")
                    if symbol_range:
                        start_line = symbol_range["start"]["line"]
                        start_char = symbol_range["start"]["character"]
                        if start_line == line and start_char <= column <= symbol_range["end"]["character"]:
                            symbol_definition = {"filename": relative_file_path, "line": start_line, "character": start_char}
                            break
        except Exception as e:
            log.debug(f"Could not determine symbol definition: {e}")

        references: list[ls_types.Location] = []
        for call_info in call_infos:
            try:
                if self.is_ignored_path(call_info.filename):
                    continue

                # Skip the definition itself
                if (
                    symbol_definition
                    and call_info.filename == symbol_definition["filename"]
                    and call_info.line == symbol_definition["line"]
                    and call_info.character == symbol_definition["character"]
                ):
                    continue

                abs_path = os.path.join(self.repository_root_path, call_info.filename)
                if not os.path.exists(abs_path):
                    continue

                with open(abs_path, encoding="utf-8", errors="ignore") as f:
                    file_lines = f.read().split("\n")

                if call_info.line >= len(file_lines):
                    continue

                call_line = file_lines[call_info.line]
                call_char = call_info.character
                if call_char >= len(call_line):
                    call_char = 0

                name_pos = call_line.find(symbol_name, call_char)
                if name_pos == -1:
                    name_pos = call_char

                end_char = min(name_pos + len(symbol_name), len(call_line))

                range_obj = ls_types.Range(
                    start=ls_types.Position(line=call_info.line, character=name_pos),
                    end=ls_types.Position(line=call_info.line, character=end_char),
                )
                uri = pathlib.Path(abs_path).as_uri()
                location = ls_types.Location(uri=uri, range=range_obj, absolutePath=abs_path, relativePath=call_info.filename)
                references.append(location)

            except Exception as e:
                log.warning(f"Failed to process reference {call_info.filename}:{call_info.line}: {e}")

        log.info(f"Found {len(references)} references to '{symbol_name}'")
        return references

    def _get_symbol_name_at_position(self, relative_file_path: str, line: int, column: int) -> str | None:
        """Determine the symbol name at a given file position."""
        try:
            document_symbols = self.request_document_symbols(relative_file_path)
            for symbol in document_symbols.iter_symbols():
                symbol_range = symbol.get("range")
                if not symbol_range:
                    continue
                start_line = symbol_range["start"]["line"]
                end_line = symbol_range["end"]["line"]
                if start_line <= line <= end_line:
                    if line == start_line and column < symbol_range["start"]["character"]:
                        continue
                    if line == end_line and column > symbol_range["end"]["character"]:
                        continue
                    return symbol.get("name")

            # Fallback: extract identifier from file text
            abs_path = os.path.join(self.repository_root_path, relative_file_path)
            if os.path.exists(abs_path):
                with open(abs_path, encoding="utf-8", errors="ignore") as f:
                    file_lines = f.read().split("\n")
                if line < len(file_lines):
                    file_line = file_lines[line]
                    if column < len(file_line):
                        start = column
                        while start > 0 and (file_line[start - 1].isalnum() or file_line[start - 1] == "_"):
                            start -= 1
                        end = column
                        while end < len(file_line) and (file_line[end].isalnum() or file_line[end] == "_"):
                            end += 1
                        if start < end:
                            name = file_line[start:end]
                            if name and name[0].isalpha():
                                return name

            return None
        except Exception as e:
            log.debug(f"Failed to get symbol name at {relative_file_path}:{line}:{column}: {e}")
            return None

    # ─── Rename via Call Graph ─────────────────────────────────────────

    @override
    def request_rename_symbol_edit(
        self,
        relative_file_path: str,
        line: int,
        column: int,
        new_name: str,
    ) -> ls_types.WorkspaceEdit | None:
        """Workspace-wide rename using the local call graph cache."""
        symbol_name = self._get_symbol_name_at_position(relative_file_path, line, column)
        if not symbol_name:
            log.debug(f"Could not determine symbol at {relative_file_path}:{line}:{column}")
            return None

        log.debug(f"Renaming '{symbol_name}' to '{new_name}' via local cache")

        call_infos = self._local_cache.find_calls(symbol_name)

        # Find the definition
        document_symbols = self.request_document_symbols(relative_file_path)
        symbol_definition: dict[str, Any] | None = None
        for symbol in document_symbols.iter_symbols():
            if symbol.get("name") == symbol_name:
                symbol_range = symbol.get("range")
                if symbol_range:
                    start_line = symbol_range["start"]["line"]
                    start_char = symbol_range["start"]["character"]
                    if start_line == line and start_char <= column <= symbol_range["end"]["character"]:
                        symbol_definition = {"filename": relative_file_path, "range": symbol_range}
                        break

        # Build changes grouped by file URI
        changes: dict[str, list[ls_types.TextEdit]] = {}

        # Add edit for the definition
        if symbol_definition:
            def_uri = pathlib.Path(os.path.join(self.repository_root_path, relative_file_path)).as_uri()
            def_range = symbol_definition["range"]
            abs_path = os.path.join(self.repository_root_path, relative_file_path)
            with open(abs_path, encoding=self._encoding) as f:
                file_lines = f.read().split("\n")

            if def_range["start"]["line"] < len(file_lines):
                def_line = file_lines[def_range["start"]["line"]]
                start_char = def_line.find(symbol_name, def_range["start"]["character"])
                if start_char != -1:
                    end_char = start_char + len(symbol_name)
                    if def_uri not in changes:
                        changes[def_uri] = []
                    changes[def_uri].append(
                        {
                            "range": {
                                "start": {"line": def_range["start"]["line"], "character": start_char},
                                "end": {"line": def_range["start"]["line"], "character": end_char},
                            },
                            "newText": new_name,
                        }
                    )

        # Add edits for all call sites
        for call_info in call_infos:
            call_abs_path = os.path.join(self.repository_root_path, call_info.filename)
            if not os.path.exists(call_abs_path):
                continue

            with open(call_abs_path, encoding=self._encoding) as f:
                call_file_lines = f.read().split("\n")

            if call_info.line < len(call_file_lines):
                call_line = call_file_lines[call_info.line]
                start_char = call_line.find(symbol_name, call_info.character)
                if start_char != -1:
                    end_char = start_char + len(symbol_name)
                    call_uri = pathlib.Path(call_abs_path).as_uri()
                    if call_uri not in changes:
                        changes[call_uri] = []
                    changes[call_uri].append(
                        {
                            "range": {
                                "start": {"line": call_info.line, "character": start_char},
                                "end": {"line": call_info.line, "character": end_char},
                            },
                            "newText": new_name,
                        }
                    )

        if not changes:
            return None

        workspace_edit: ls_types.WorkspaceEdit = {"changes": changes}  # type: ignore
        log.debug(f"WorkspaceEdit: {len(changes)} files, {sum(len(e) for e in changes.values())} edits")
        return workspace_edit

    # ─── Direct File Editing ───────────────────────────────────────────

    @override
    def insert_text_at_position(self, relative_file_path: str, line: int, column: int, text_to_be_inserted: str) -> ls_types.Position:
        """Insert text at a position, then invalidate and re-index the file."""
        absolute_file_path = os.path.join(self.repository_root_path, relative_file_path)
        with open(absolute_file_path, encoding=self._encoding) as f:
            file_content = f.read()

        new_contents, new_l, new_c = TextUtils.insert_text_at_position(file_content, line, column, text_to_be_inserted)

        with open(absolute_file_path, "w", encoding=self._encoding) as f:
            f.write(new_contents)

        self._invalidate_file_cache(relative_file_path)
        return ls_types.Position(line=new_l, character=new_c)

    @override
    def delete_text_between_positions(
        self,
        relative_file_path: str,
        start: ls_types.Position,
        end: ls_types.Position,
    ) -> str:
        """Delete text between positions, then invalidate and re-index the file."""
        absolute_file_path = os.path.join(self.repository_root_path, relative_file_path)
        with open(absolute_file_path, encoding=self._encoding) as f:
            file_content = f.read()

        new_contents, deleted_text = TextUtils.delete_text_between_positions(
            file_content,
            start_line=start["line"],
            start_col=start["character"],
            end_line=end["line"],
            end_col=end["character"],
        )

        with open(absolute_file_path, "w", encoding=self._encoding) as f:
            f.write(new_contents)

        self._invalidate_file_cache(relative_file_path)
        return deleted_text

    @override
    def apply_text_edits_to_file(self, relative_path: str, edits: list[ls_types.TextEdit]) -> None:
        """Apply a list of text edits to a file (sorted from end to start)."""
        absolute_file_path = os.path.join(self.repository_root_path, relative_path)
        with open(absolute_file_path, encoding=self._encoding) as f:
            file_content = f.read()

        sorted_edits = sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True)

        for edit in sorted_edits:
            start_pos = edit["range"]["start"]
            end_pos = edit["range"]["end"]
            file_content, _ = TextUtils.delete_text_between_positions(
                file_content,
                start_line=start_pos["line"],
                start_col=start_pos["character"],
                end_line=end_pos["line"],
                end_col=end_pos["character"],
            )
            file_content, _, _ = TextUtils.insert_text_at_position(file_content, start_pos["line"], start_pos["character"], edit["newText"])

        with open(absolute_file_path, "w", encoding=self._encoding) as f:
            f.write(file_content)

        self._invalidate_file_cache(relative_path)

    # ─── Cache Invalidation ────────────────────────────────────────────

    def _invalidate_file_cache(self, relative_file_path: str) -> None:
        """Invalidate all caches for a file and re-index it."""
        log.debug(f"Invalidating cache for: {relative_file_path}")

        self._document_symbols_cache.pop(relative_file_path, None)
        self._raw_document_symbols_cache.pop(relative_file_path, None)
        self._local_cache.remove_file_data(relative_file_path)
        self._file_content_cache.pop(relative_file_path, None)
        self._converted_files.discard(relative_file_path)

        self._parse_file_local(relative_file_path)
        log.debug(f"Cache invalidated and file reindexed: {relative_file_path}")

    @override
    def stop(self, shutdown_timeout: float = 2.0) -> None:
        """Stop the BSL Language Server."""
        super().stop(shutdown_timeout=shutdown_timeout)
