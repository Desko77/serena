"""
Python parser for BSL (1C:Enterprise) code for mass indexing.
Provides regex-based parsing of procedures, functions, module variables, and call positions.
"""

import re
from dataclasses import dataclass, field


@dataclass
class BSLParam:
    """Parameter of a procedure or function."""

    name: str
    byval: bool  # True if "Знач" (ByVal), False if by reference
    default: str | None = None


@dataclass
class BSLCallPosition:
    """Position of a procedure/function call in code."""

    call: str  # Name of the called procedure/function
    line: int  # Line number (0-based)
    character: int  # Character position in line (0-based)


@dataclass
class BSLModuleVar:
    """Module-level variable."""

    name: str
    is_export: bool
    description: str = ""


@dataclass
class BSLMethod:
    """BSL procedure or function."""

    name: str
    line: int  # Start line number (0-based)
    endline: int  # End line number (0-based)
    isproc: bool  # True for procedure, False for function
    is_export: bool
    params: list[BSLParam] = field(default_factory=list)
    description: str = ""
    context: str = ""  # НаСервере, НаКлиенте, НаСервереБезКонтекста, or empty
    calls_position: list[BSLCallPosition] = field(default_factory=list)


@dataclass
class BSLParseResult:
    """Result of BSL code parsing."""

    methods: list[BSLMethod] = field(default_factory=list)
    global_calls: list[BSLCallPosition] = field(default_factory=list)
    module_vars: dict[str, BSLModuleVar] = field(default_factory=dict)


class BSLParser:
    """Regex-based parser for BSL code."""

    PROC_PATTERN = re.compile(
        r"^\s*(?:&НаСервере|&НаКлиенте|&НаСервереБезКонтекста)?\s*(?:Экспорт\s+)?Процедура\s+([а-яёА-ЯЁ\w]+)\s*\(",
        re.IGNORECASE | re.MULTILINE,
    )

    FUNC_PATTERN = re.compile(
        r"^\s*(?:&НаСервере|&НаКлиенте|&НаСервереБезКонтекста)?\s*(?:Экспорт\s+)?Функция\s+([а-яёА-ЯЁ\w]+)\s*\(",
        re.IGNORECASE | re.MULTILINE,
    )

    PROC_END_PATTERN = re.compile(r"^\s*КонецПроцедуры", re.IGNORECASE | re.MULTILINE)
    FUNC_END_PATTERN = re.compile(r"^\s*КонецФункции", re.IGNORECASE | re.MULTILINE)

    CONTEXT_PATTERN = re.compile(r"&(НаСервере|НаКлиенте|НаСервереБезКонтекста)", re.IGNORECASE)
    EXPORT_PATTERN = re.compile(r"\bЭкспорт\b", re.IGNORECASE)

    PARAM_PATTERN = re.compile(
        r"(?:Знач\s+)?([а-яёА-ЯЁ\w]+)(?:\s*=\s*([^,)]+))?",
        re.IGNORECASE,
    )

    MODULE_VAR_PATTERN = re.compile(
        r"^\s*Перем\s+([а-яёА-ЯЁ\w]+)(?:\s+Экспорт)?\s*;",
        re.IGNORECASE | re.MULTILINE,
    )

    CALL_PATTERN = re.compile(
        r"\b([а-яёА-ЯЁ\w]+)\s*\(",
        re.IGNORECASE,
    )

    # Keywords that should not be treated as calls
    KEYWORDS: set[str] = {
        "если",
        "иначе",
        "иначеесли",
        "конецесли",
        "пока",
        "конеццикла",
        "для",
        "каждого",
        "из",
        "цикл",
        "процедура",
        "функция",
        "конецпроцедуры",
        "конецфункции",
        "возврат",
        "прервать",
        "продолжить",
        "попытка",
        "исключение",
        "вызватьисключение",
        "новый",
        "тип",
        "типзнч",
        "неопределено",
        "истина",
        "ложь",
        "сообщить",
        "сообщениепользователю",
        "пустаястрока",
        "стршаблон",
        "насервере",
        "наклиенте",
        "насерверебезконтекста",
        "экспорт",
        "знач",
        "перем",
        "конецобласти",
        "область",
    }

    def parse(self, source: str) -> BSLParseResult:
        """
        Parse BSL code and return a structured result.

        :param source: BSL source code
        :return: BSLParseResult with extracted data
        """
        result = BSLParseResult()
        lines = source.split("\n")

        result.module_vars = self._parse_module_vars(source, lines)
        result.methods = self._parse_methods(source, lines)
        result.global_calls = self._parse_global_calls(lines, result.methods)

        for method in result.methods:
            method.calls_position = self._parse_method_calls(lines, method)

        return result

    def _parse_module_vars(self, source: str, lines: list[str]) -> dict[str, BSLModuleVar]:
        """Extract module-level variables."""
        vars_dict: dict[str, BSLModuleVar] = {}

        for match in self.MODULE_VAR_PATTERN.finditer(source):
            var_name = match.group(1)
            var_line = source[: match.start()].count("\n")
            var_text = match.group(0)
            is_export = "Экспорт" in var_text or "экспорт" in var_text
            description = self._extract_description_before(lines, var_line)

            vars_dict[var_name] = BSLModuleVar(
                name=var_name,
                is_export=is_export,
                description=description,
            )

        return vars_dict

    def _parse_methods(self, source: str, lines: list[str]) -> list[BSLMethod]:
        """Extract procedures and functions."""
        methods: list[BSLMethod] = []

        for match in self.PROC_PATTERN.finditer(source):
            method = self._parse_method_from_match(source, lines, match, is_proc=True)
            if method:
                methods.append(method)

        for match in self.FUNC_PATTERN.finditer(source):
            method = self._parse_method_from_match(source, lines, match, is_proc=False)
            if method:
                methods.append(method)

        methods.sort(key=lambda m: m.line)
        return methods

    def _parse_method_from_match(
        self,
        source: str,
        lines: list[str],
        match: re.Match[str],
        is_proc: bool,
    ) -> BSLMethod | None:
        """Create BSLMethod from a regex match."""
        method_name = match.group(1)
        start_pos = match.start()
        start_line = source[:start_pos].count("\n")

        # Verify start_line points to the line containing the match
        for offset in range(3):
            check_line = start_line + offset
            if check_line < len(lines) and method_name in lines[check_line]:
                line_text = lines[check_line]
                if ("Процедура" in line_text or "Функция" in line_text) and method_name in line_text:
                    start_line = check_line
                    break

        declaration_line = lines[start_line] if start_line < len(lines) else ""
        context = self._extract_context(declaration_line)
        is_export = bool(self.EXPORT_PATTERN.search(declaration_line))
        params = self._extract_params(source, start_pos, lines, start_line)
        end_line = self._find_method_end(source, start_line, is_proc)
        if end_line is None:
            return None

        description = self._extract_description_before(lines, start_line)

        return BSLMethod(
            name=method_name,
            line=start_line,
            endline=end_line,
            isproc=is_proc,
            is_export=is_export,
            params=params,
            description=description,
            context=context,
        )

    def _extract_context(self, line: str) -> str:
        """Extract context directive from a line (НаСервере, НаКлиенте, etc.)."""
        match = self.CONTEXT_PATTERN.search(line)
        context_map = {
            "насервере": "НаСервере",
            "наклиенте": "НаКлиенте",
            "насерверебезконтекста": "НаСервереБезКонтекста",
        }
        if match:
            return context_map.get(match.group(1).lower(), "")
        return ""

    def _extract_params(
        self,
        source: str,
        start_pos: int,
        lines: list[str],
        start_line: int,
    ) -> list[BSLParam]:
        """Extract procedure/function parameters."""
        params: list[BSLParam] = []
        declaration_line = lines[start_line] if start_line < len(lines) else ""

        paren_start = declaration_line.find("(")
        if paren_start == -1:
            return params

        paren_end = declaration_line.find(")", paren_start + 1)
        if paren_end == -1:
            search_start = start_pos + paren_start + 1
            paren_end_pos = source.find(")", search_start)
            if paren_end_pos == -1:
                return params
            params_text = source[search_start:paren_end_pos]
        else:
            params_text = declaration_line[paren_start + 1 : paren_end]

        if params_text.strip():
            for param_match in self.PARAM_PATTERN.finditer(params_text):
                param_name = param_match.group(1)
                byval = "Знач" in param_match.group(0) or "знач" in param_match.group(0)
                default = param_match.group(2) if param_match.lastindex and param_match.lastindex >= 2 and param_match.group(2) else None

                params.append(
                    BSLParam(
                        name=param_name,
                        byval=byval,
                        default=default.strip() if default else None,
                    )
                )

        return params

    def _find_method_end(self, source: str, start_line: int, is_proc: bool) -> int | None:
        """Find the end line of a procedure/function."""
        lines = source.split("\n")
        end_pattern = self.PROC_END_PATTERN if is_proc else self.FUNC_END_PATTERN

        depth = 1
        for i in range(start_line + 1, len(lines)):
            line = lines[i]
            proc_match = self.PROC_PATTERN.search(line)
            func_match = self.FUNC_PATTERN.search(line)
            if proc_match or func_match:
                match_obj = proc_match if proc_match else func_match
                assert match_obj is not None
                match_pos = match_obj.start()
                if match_pos == len(line) - len(line.lstrip()):
                    depth += 1
            elif end_pattern.search(line):
                depth -= 1
                if depth == 0:
                    return i

        return None

    def _extract_description_before(self, lines: list[str], line_num: int) -> str:
        """Extract comment description before a method/variable declaration."""
        description_lines: list[str] = []

        for i in range(max(0, line_num - 20), line_num):
            line = lines[i].strip()
            if not description_lines and not line:
                continue
            if line.startswith("//"):
                description_lines.append(line[2:].strip())
            elif "/*" in line:
                if "*/" in line:
                    comment = line[line.find("/*") + 2 : line.find("*/")].strip()
                    if comment:
                        description_lines.append(comment)
            elif line:
                break

        description_lines.reverse()
        return "\n".join(description_lines).strip()

    def _parse_global_calls(
        self,
        lines: list[str],
        methods: list[BSLMethod],
    ) -> list[BSLCallPosition]:
        """Extract procedure/function calls at module level (outside methods)."""
        calls: list[BSLCallPosition] = []

        method_ranges: set[int] = set()
        for method in methods:
            for line_num in range(method.line, method.endline + 1):
                method_ranges.add(line_num)

        for line_num, line in enumerate(lines):
            if line_num in method_ranges:
                continue
            for match in self.CALL_PATTERN.finditer(line):
                call_name = match.group(1)
                if call_name.lower() in self.KEYWORDS:
                    continue
                calls.append(BSLCallPosition(call=call_name, line=line_num, character=match.start()))

        return calls

    def _parse_method_calls(
        self,
        lines: list[str],
        method: BSLMethod,
    ) -> list[BSLCallPosition]:
        """Extract procedure/function calls within a method."""
        calls: list[BSLCallPosition] = []
        method_lines = lines[method.line : method.endline + 1]

        for line_offset, line in enumerate(method_lines):
            line_num = method.line + line_offset
            for match in self.CALL_PATTERN.finditer(line):
                call_name = match.group(1)
                if call_name.lower() in self.KEYWORDS:
                    continue
                if call_name == method.name:
                    continue
                calls.append(BSLCallPosition(call=call_name, line=line_num, character=match.start()))

        return calls
