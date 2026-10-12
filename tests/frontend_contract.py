"""Static contract helpers shared by the frontend regression tests.

Two things the frontend tests need to check without a browser:

* the identifier set ``setup()`` actually exposes to the template
  (``app/static/assets/app-setup.js``, including keys spread in from
  ``useFormatters()``), and
* the root identifiers the template references (``app/static/index.html``).

Vue compiles ``index.html`` at runtime, inside ``with (_ctx)``, and the
public-instance proxy's ``has`` trap answers ``true`` for every key that does
not start with ``_``; a template binding that ``setup()`` never returned
therefore resolves to ``undefined`` instead of raising.  Event bindings
silently attach no listener and interpolations silently render empty, with no
console output at all — Issue #80 was exactly that (「只看自选」's ``@change``).
So the two sets are compared instead of trusted.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"

# Root identifiers that are legal in a template but are not setup() bindings:
# Vue's template globals plus the JS objects the template calls directly.
TEMPLATE_GLOBALS = {
    "$event", "$refs", "$el", "$attrs", "$slots", "$props", "$emit",
    "$options", "$forceUpdate", "$nextTick", "$root", "$data", "$parent",
    "$index",
    "true", "false", "null", "undefined", "in", "of", "typeof", "instanceof",
    "new", "void", "delete", "return", "if", "else", "await",
    "JSON", "Math", "Date", "Number", "String", "Boolean", "Object", "Array",
    "Intl", "parseInt", "parseFloat", "isNaN", "encodeURIComponent",
    "encodeURI", "decodeURIComponent", "toLocaleString", "String",
}

# ``...fmt`` in a return literal: the keys come from the spread source.
_SPREAD = re.compile(r"\.\.\.\s*([A-Za-z_$][\w$]*)")

# Every directive/attribute whose value is a template expression.  ``v-for``
# is scanned separately because its alias declares its own locals.
_EXPR_ATTR = re.compile(
    r"""(?:[\s"'<])"""
    r"""(?::[A-Za-z_][\w:.-]*|@[A-Za-z_][\w:.-]*|v-if|v-else-if|v-show|"""
    r"""v-model|v-html|v-text|v-bind:[A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)\"""",
    re.M,
)
_V_FOR = re.compile(r"""\bv-for\s*=\s*"([^"]*)""")
_MUSTACHE = re.compile(r"\{\{(.*?)\}\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)

# An identifier token: not preceded by a word character or a dot (property
# access / optional chaining).
_IDENT = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*")

_JS_KEYWORDS = {
    "true", "false", "null", "undefined", "in", "of", "typeof", "new",
    "instanceof", "void", "delete", "return", "if", "else", "await", "this",
}


def read_index() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def mask_code(text: str) -> str:
    """Blank string literals, template literals and comments, keeping offsets.

    The result has the same length and line breaks as ``text``, so a
    structural scan can run on the masked copy while the caller still indexes
    the original.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = out[i + 1] = " "
                i += 2
        elif ch in "'\"`":
            quote = ch
            out[i] = " "
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def _matching_brace(text: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced braces")


def _split_top_level(masked_body: str) -> list[str]:
    """Split an object-literal body on commas that close no bracket."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(masked_body):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(masked_body[start:i])
            start = i + 1
    parts.append(masked_body[start:])
    return parts


def _entry_key(entry: str) -> str | None:
    """Key of ``key: value``, name of a shorthand entry, None for a spread."""
    entry = entry.strip()
    if not entry or entry.startswith("..."):
        return None
    depth = 0
    for i, ch in enumerate(entry):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            head = entry[:i].strip()
            break
    else:
        head = entry
    return head if re.fullmatch(r"[A-Za-z_$][\w$]*", head) else None


def _object_keys(js: str, open_index: int, seen: set[str] | None = None) -> set[str]:
    """Keys of the object literal whose ``{`` sits at ``open_index``.

    Spread entries are resolved back to their source (a ``useFormatters()``
    return literal, or another object literal) so the exposed set is complete;
    an unresolvable spread raises instead of silently shrinking the set.
    """
    seen = set() if seen is None else seen
    masked = mask_code(js)
    close_index = _matching_brace(masked, open_index)
    keys: set[str] = set()
    for entry in _split_top_level(masked[open_index + 1:close_index]):
        key = _entry_key(entry)
        if key:
            keys.add(key)
            continue
        spread = _SPREAD.match(entry.strip())
        if spread:
            keys |= _resolve_spread(js, spread.group(1), seen)
    return keys


def _resolve_spread(js: str, ident: str, seen: set[str]) -> set[str]:
    if ident in seen:
        raise ValueError(f"circular spread: {ident}")
    seen.add(ident)

    call = re.search(rf"(?m)^\s*const {re.escape(ident)} = ([A-Za-z_$][\w$]*)\(", js)
    if call:
        return _function_return_keys(js, call.group(1), seen)

    literal = re.search(rf"(?m)^\s*const {re.escape(ident)} = \s*\{{", js)
    if literal:
        return _object_keys(js, js.index("{", literal.start()), seen)

    raise ValueError(
        f"cannot resolve spread ...{ident}: expected `const {ident} = fn(...)` "
        f"or `const {ident} = {{ ... }}`"
    )


def _function_return_keys(js: str, fn_name: str, seen: set[str]) -> set[str]:
    fn = re.search(rf"(?m)^\s*function {re.escape(fn_name)}\(", js)
    if not fn:
        raise ValueError(f"function {fn_name}() not found")
    ret = js.index("return {", fn.end())
    return _object_keys(js, js.index("{", ret), seen)


def exposed_setup_keys(js: str) -> set[str]:
    """The identifier set the component's ``setup()`` hands to the template."""
    marker = "// ── Public API"
    start = js.index(marker)
    ret = js.index("return {", start)
    return _object_keys(js, js.index("{", ret))


def v_for_locals(html: str) -> set[str]:
    """Every ``v-for`` local name (alias and index) in the template."""
    html = _HTML_COMMENT.sub(" ", html)
    locals_: set[str] = set()
    for expr in _V_FOR.findall(html):
        alias = re.split(r"\s+(?:in|of)\s+", expr.strip(), maxsplit=1)[0]
        for name in alias.strip("()").split(","):
            name = name.strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                locals_.add(name)
    return locals_


def _expr_roots(expr: str) -> set[str]:
    """Root identifiers of one template expression, as value references."""
    masked = mask_code(expr)
    found: set[str] = set()
    for match in _IDENT.finditer(masked):
        name = match.group(0)
        if name in _JS_KEYWORDS:
            continue
        before = masked[:match.start()].rstrip()
        after = masked[match.end():].lstrip()
        # Property access (incl. `?.`) and object-literal keys / labels.
        if before.endswith("."):
            continue
        if after.startswith(":"):
            continue
        found.add(name)
    return found


def template_roots(html: str) -> dict[str, set[str]]:
    """Map each template expression to the root identifiers it references."""
    raw = _HTML_COMMENT.sub(" ", html)
    roots: dict[str, set[str]] = {}

    def collect(expr: str) -> None:
        found = _expr_roots(expr)
        if found:
            roots[expr.strip()] = found

    for expr in _V_FOR.findall(raw):
        parts = re.split(r"\s+(?:in|of)\s+", expr.strip(), maxsplit=1)
        if len(parts) == 2:
            collect(parts[1])
    for match in _EXPR_ATTR.finditer(raw):
        collect(match.group(1))
    for match in _MUSTACHE.finditer(raw):
        collect(match.group(1))
    return roots


def dangling_bindings(html: str, exposed: set[str]) -> set[str]:
    """Root template identifiers that ``setup()`` does not expose.

    ``v-for`` locals and Vue/JS globals are subtracted; what is left would
    silently resolve to ``undefined`` at runtime.
    """
    allowed = set(exposed) | v_for_locals(html) | TEMPLATE_GLOBALS
    dangling: set[str] = set()
    for roots in template_roots(html).values():
        dangling |= roots - allowed
    return dangling
