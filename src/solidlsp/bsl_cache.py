"""
In-memory cache for BSL symbols (analogous to LokiJS).
Provides fast search and indexing of symbols with thread safety.
"""

import re
import threading
from dataclasses import dataclass
from typing import Any

from solidlsp.bsl_parser import BSLCallPosition, BSLMethod, BSLModuleVar


@dataclass
class BSLCallInfo:
    """Information about a procedure/function call."""

    filename: str
    call: str
    line: int
    character: int
    method_name: str  # Name of the method containing the call
    module: str = ""  # Module the file belongs to


@dataclass
class BSLModuleInfo:
    """Module metadata."""

    filename: str
    module: str  # Module name (e.g. "ОбщиеМодули.ИмяМодуля")
    type: str = ""  # Module type (ObjectModule, ManagerModule, CommonModule, etc.)
    parenttype: str = ""  # Parent type (CommonModules, Documents, etc.)
    project: str = ""  # Project path


@dataclass
class BSLMethodInfo:
    """Method information with file context."""

    method: BSLMethod
    filename: str
    module: str = ""  # Module the method belongs to


class BSLCache:
    """
    In-memory database for BSL symbol cache.
    Thread-safe for concurrent read/write access.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.methods: list[BSLMethodInfo] = []
        self.module_vars: dict[str, list[BSLModuleVar]] = {}  # filename -> list of vars
        self.calls: dict[str, list[BSLCallInfo]] = {}  # call_name -> list of calls
        self.modules: list[BSLModuleInfo] = []

        # Indices for fast lookup
        self._method_name_index: dict[str, list[int]] = {}  # name -> list of indices
        self._method_module_index: dict[str, list[int]] = {}  # module -> list of indices
        self._method_export_index: list[int] = []  # indices of exported methods

    def add_method(self, method: BSLMethod, filename: str, module: str = "") -> None:
        """Add a method to the cache (thread-safe)."""
        with self._lock:
            method_info = BSLMethodInfo(method=method, filename=filename, module=module)
            index = len(self.methods)
            self.methods.append(method_info)

            method_name_lower = method.name.lower()
            if method_name_lower not in self._method_name_index:
                self._method_name_index[method_name_lower] = []
            self._method_name_index[method_name_lower].append(index)

            if module:
                module_lower = module.lower()
                if module_lower not in self._method_module_index:
                    self._method_module_index[module_lower] = []
                self._method_module_index[module_lower].append(index)

            if method.is_export:
                self._method_export_index.append(index)

    def add_module_var(self, var: BSLModuleVar, filename: str) -> None:
        """Add a module variable to the cache (thread-safe)."""
        with self._lock:
            if filename not in self.module_vars:
                self.module_vars[filename] = []
            self.module_vars[filename].append(var)

    def add_call(self, call: BSLCallPosition, filename: str, method_name: str, module: str = "") -> None:
        """Add a call to the cache (thread-safe)."""
        with self._lock:
            call_name = call.call
            if call_name not in self.calls:
                self.calls[call_name] = []
            call_info = BSLCallInfo(
                filename=filename,
                call=call_name,
                line=call.line,
                character=call.character,
                method_name=method_name,
                module=module,
            )
            self.calls[call_name].append(call_info)

    def add_methods_batch(self, methods_data: list[tuple[BSLMethod, str, str]]) -> None:
        """Add multiple methods to the cache in a batch (thread-safe)."""
        with self._lock:
            for method, filename, module in methods_data:
                method_info = BSLMethodInfo(method=method, filename=filename, module=module)
                index = len(self.methods)
                self.methods.append(method_info)

                method_name_lower = method.name.lower()
                if method_name_lower not in self._method_name_index:
                    self._method_name_index[method_name_lower] = []
                self._method_name_index[method_name_lower].append(index)

                if module:
                    module_lower = module.lower()
                    if module_lower not in self._method_module_index:
                        self._method_module_index[module_lower] = []
                    self._method_module_index[module_lower].append(index)

                if method.is_export:
                    self._method_export_index.append(index)

    def add_module_vars_batch(self, vars_data: list[tuple[BSLModuleVar, str]]) -> None:
        """Add multiple module variables to the cache in a batch (thread-safe)."""
        with self._lock:
            for var, filename in vars_data:
                if filename not in self.module_vars:
                    self.module_vars[filename] = []
                self.module_vars[filename].append(var)

    def add_calls_batch(self, calls_data: list[tuple[BSLCallPosition, str, str, str]]) -> None:
        """Add multiple calls to the cache in a batch (thread-safe)."""
        with self._lock:
            for call, filename, method_name, module in calls_data:
                call_name = call.call
                if call_name not in self.calls:
                    self.calls[call_name] = []
                call_info = BSLCallInfo(
                    filename=filename,
                    call=call_name,
                    line=call.line,
                    character=call.character,
                    method_name=method_name,
                    module=module,
                )
                self.calls[call_name].append(call_info)

    def add_module(self, module_info: BSLModuleInfo) -> None:
        """Add module metadata to the cache (thread-safe)."""
        with self._lock:
            self.modules.append(module_info)

    def find_methods(self, query: dict[str, Any] | None = None) -> list[BSLMethodInfo]:
        """
        Search for methods by query (analogous to LokiJS .find()).

        Supported query fields:
        - name: exact name or regex pattern (dict with $regex key)
        - module: module name or regex pattern
        - is_export / isExport: True/False for exported methods
        - context: context (НаСервере, НаКлиенте, etc.)
        - isproc: True for procedures, False for functions
        """
        if query is None or not query:
            return self.methods.copy()

        candidate_indices: set[int] | None = None

        # Filter by name
        if "name" in query:
            name_pattern = query["name"]
            if isinstance(name_pattern, dict) and "$regex" in name_pattern:
                pattern = re.compile(name_pattern["$regex"], re.IGNORECASE)
                name_indices: set[int] = set()
                for name, indices in self._method_name_index.items():
                    if pattern.search(name):
                        name_indices.update(indices)
                candidate_indices = name_indices if candidate_indices is None else candidate_indices & name_indices
            else:
                name_lower = str(name_pattern).lower()
                name_indices = set(self._method_name_index.get(name_lower, []))
                candidate_indices = name_indices if candidate_indices is None else candidate_indices & name_indices

        # Filter by module
        if "module" in query:
            module_pattern = query["module"]
            if isinstance(module_pattern, dict) and "$regex" in module_pattern:
                pattern = re.compile(module_pattern["$regex"], re.IGNORECASE)
                module_indices: set[int] = set()
                for module, indices in self._method_module_index.items():
                    if pattern.search(module):
                        module_indices.update(indices)
                candidate_indices = module_indices if candidate_indices is None else candidate_indices & module_indices
            else:
                module_lower = str(module_pattern).lower()
                module_indices = set(self._method_module_index.get(module_lower, []))
                candidate_indices = module_indices if candidate_indices is None else candidate_indices & module_indices

        # Filter by export
        if "is_export" in query or "isExport" in query:
            is_export = query.get("is_export", query.get("isExport", False))
            export_indices = set(self._method_export_index)
            if candidate_indices is not None:
                if is_export:
                    candidate_indices &= export_indices
                else:
                    candidate_indices -= export_indices
            else:
                if is_export:
                    candidate_indices = export_indices
                else:
                    candidate_indices = set(range(len(self.methods))) - export_indices

        if candidate_indices is None:
            candidate_indices = set(range(len(self.methods)))

        # Apply remaining filters that require checking method objects
        results: list[BSLMethodInfo] = []
        for idx in candidate_indices:
            if idx >= len(self.methods):
                continue
            method_info = self.methods[idx]
            method = method_info.method

            if "context" in query and method.context != query["context"]:
                continue
            if "isproc" in query and method.isproc != query["isproc"]:
                continue

            results.append(method_info)

        return results

    def find_calls(self, call_name: str) -> list[BSLCallInfo]:
        """Find all calls to a procedure/function."""
        return self.calls.get(call_name, []).copy()

    def find_methods_by_module(self, module: str) -> list[BSLMethodInfo]:
        """Find all methods in the given module."""
        return self.find_methods({"module": module})

    def find_exported_methods(self, module: str | None = None) -> list[BSLMethodInfo]:
        """Find all exported methods, optionally filtered by module."""
        query: dict[str, Any] = {"is_export": True}
        if module:
            query["module"] = module
        return self.find_methods(query)

    def clear(self) -> None:
        """Clear the entire cache (thread-safe)."""
        with self._lock:
            self.methods.clear()
            self.module_vars.clear()
            self.calls.clear()
            self.modules.clear()
            self._method_name_index.clear()
            self._method_module_index.clear()
            self._method_export_index.clear()

    def remove_file_data(self, filename: str) -> None:
        """Remove all data for a specific file from the cache (thread-safe)."""
        with self._lock:
            # 1. Remove methods (iterate in reverse to preserve indices)
            indices_to_remove: list[int] = []
            for idx in range(len(self.methods) - 1, -1, -1):
                if self.methods[idx].filename == filename:
                    indices_to_remove.append(idx)

            for idx in indices_to_remove:
                self.methods.pop(idx)

            # Rebuild all indices after removal
            self._rebuild_indices()

            # 2. Remove module variables
            self.module_vars.pop(filename, None)

            # 3. Remove calls
            calls_to_remove: list[str] = []
            for call_name, call_list in self.calls.items():
                filtered_calls = [call for call in call_list if call.filename != filename]
                if not filtered_calls:
                    calls_to_remove.append(call_name)
                else:
                    self.calls[call_name] = filtered_calls

            for call_name in calls_to_remove:
                self.calls.pop(call_name, None)

            # 4. Remove modules
            self.modules = [module for module in self.modules if module.filename != filename]

    def _rebuild_indices(self) -> None:
        """Rebuild all indices after changes to the methods list. Must be called under lock."""
        self._method_name_index.clear()
        self._method_module_index.clear()
        self._method_export_index.clear()

        for idx, method_info in enumerate(self.methods):
            method = method_info.method

            method_name_lower = method.name.lower()
            if method_name_lower not in self._method_name_index:
                self._method_name_index[method_name_lower] = []
            self._method_name_index[method_name_lower].append(idx)

            if method_info.module:
                module_lower = method_info.module.lower()
                if module_lower not in self._method_module_index:
                    self._method_module_index[module_lower] = []
                self._method_module_index[module_lower].append(idx)

            if method.is_export:
                self._method_export_index.append(idx)

    def get_stats(self) -> dict[str, int]:
        """Get cache statistics."""
        return {
            "methods": len(self.methods),
            "exported_methods": len(self._method_export_index),
            "module_vars": sum(len(vars_list) for vars_list in self.module_vars.values()),
            "calls": sum(len(calls_list) for calls_list in self.calls.values()),
            "unique_calls": len(self.calls),
            "modules": len(self.modules),
        }
