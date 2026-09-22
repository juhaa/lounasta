"""Minimal PDF text extraction, standard library only.

Enough of the PDF spec to read text out of ordinary, non-scanned documents:
cross-reference-free object scanning, object streams, Flate decoding, per-font
character maps (`/ToUnicode` CMaps, falling back to `/Encoding /Differences`)
and the text-showing operators. Words are placed by their text-matrix position,
so the output keeps the reading order of a page rather than the order in which
the producer happened to emit the strings.

Not a general PDF parser: encrypted files, CID fonts without a `/ToUnicode` map
and scanned images are out of scope. `extract_text()` raises `PdfTextError`
when it cannot produce text.
"""

from __future__ import annotations

import re
import zlib

__all__ = ["PdfTextError", "extract_pages", "extract_text"]


class PdfTextError(Exception):
    pass


# ------------------------------------------------------------------- tokenising


TOKEN_RE = re.compile(
    rb"""
      (?P<name>/[^\s/\[\]<>(){}%]*)
    | (?P<number>[-+]?(?:\d+\.\d*|\.\d+|\d+))
    | (?P<dict_open><<) | (?P<dict_close>>>)
    | (?P<array_open>\[) | (?P<array_close>\])
    | (?P<hexstring><[^>]*>)
    | (?P<string>\()
    | (?P<keyword>[A-Za-z'"*][A-Za-z0-9'"*]*)
    """,
    re.VERBOSE,
)


class Name(str):
    """A PDF name, kept distinct from a string literal."""


class Ref:
    __slots__ = ("num",)

    def __init__(self, num: int):
        self.num = num

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Ref({self.num})"


def read_literal_string(data: bytes, start: int) -> tuple[bytes, int]:
    """Read a `( ... )` string starting just after the opening parenthesis."""
    out = bytearray()
    depth = 1
    i = start
    while i < len(data):
        ch = data[i]
        if ch == 0x5C:  # backslash
            i += 1
            if i >= len(data):
                break
            esc = data[i]
            simple = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
            if esc in simple:
                out.append(simple[esc])
            elif 0x30 <= esc <= 0x37:  # octal, up to three digits
                digits = chr(esc)
                for _ in range(2):
                    if i + 1 < len(data) and 0x30 <= data[i + 1] <= 0x37:
                        i += 1
                        digits += chr(data[i])
                    else:
                        break
                out.append(int(digits, 8) & 0xFF)
            elif esc in (10, 13):  # line continuation
                if esc == 13 and i + 1 < len(data) and data[i + 1] == 10:
                    i += 1
            else:
                out.append(esc)
        elif ch == 0x28:
            depth += 1
            out.append(ch)
        elif ch == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return bytes(out), i


def tokenize(data: bytes):
    """Yield PDF tokens as (kind, value) pairs."""
    i = 0
    length = len(data)
    while i < length:
        ch = data[i]
        if ch in b" \t\r\n\f\x00":
            i += 1
            continue
        if ch == 0x25:  # comment
            end = data.find(b"\n", i)
            i = length if end == -1 else end + 1
            continue
        if ch == 0x28:
            value, i = read_literal_string(data, i + 1)
            yield "string", value
            continue
        match = TOKEN_RE.match(data, i)
        if not match:
            i += 1
            continue
        i = match.end()
        kind = match.lastgroup
        raw = match.group()
        if kind == "name":
            yield "name", Name(decode_name(raw[1:]))
        elif kind == "number":
            text = raw.decode("ascii")
            yield "number", float(text) if ("." in text) else int(text)
        elif kind == "hexstring":
            yield "string", decode_hex_string(raw[1:-1])
        else:
            yield kind, raw.decode("latin-1")


def decode_name(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw):
            try:
                out.append(int(raw[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(raw[i])
        i += 1
    return out.decode("latin-1")


def decode_hex_string(raw: bytes) -> bytes:
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii"))


# --------------------------------------------------------------------- objects


def parse_object(tokens: list, index: int):
    """Parse one object from a token list, returning (value, next index)."""
    kind, value = tokens[index]
    if kind == "dict_open":
        result = {}
        index += 1
        while index < len(tokens) and tokens[index][0] != "dict_close":
            if tokens[index][0] != "name":
                index += 1  # malformed key; skip it
                continue
            key = tokens[index][1]
            value, index = parse_object(tokens, index + 1)
            result[key] = value
        return result, index + 1
    if kind == "array_open":
        items = []
        index += 1
        while index < len(tokens) and tokens[index][0] != "array_close":
            item, index = parse_object(tokens, index)
            items.append(item)
        return items, index + 1
    if kind == "number":
        # "12 0 R" is a reference; "12 0 obj" is handled by the caller.
        if (
            isinstance(value, int)
            and index + 2 < len(tokens)
            and tokens[index + 1] == ("number", 0)
            and tokens[index + 2] == ("keyword", "R")
        ):
            return Ref(value), index + 3
        return value, index + 1
    if kind == "keyword":
        if value == "true":
            return True, index + 1
        if value == "false":
            return False, index + 1
        return None, index + 1
    return value, index + 1


class Document:
    """All objects of a PDF, indexed by object number."""

    def __init__(self, data: bytes):
        self.data = data
        self.objects: dict[int, object] = {}
        self.streams: dict[int, bytes] = {}
        self._scan_objects()
        self._expand_object_streams()

    # -- loading ----------------------------------------------------------

    def _scan_objects(self) -> None:
        for match in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", self.data):
            num = int(match.group(1))
            end = self.data.find(b"endobj", match.end())
            body = self.data[match.end() : end if end != -1 else len(self.data)]

            stream_at = body.find(b"stream")
            head = body[:stream_at] if stream_at != -1 else body
            tokens = list(tokenize(head))
            value = parse_object(tokens, 0)[0] if tokens else None
            self.objects[num] = value

            if stream_at != -1:
                start = stream_at + len(b"stream")
                if body[start : start + 2] == b"\r\n":
                    start += 2
                elif body[start : start + 1] in (b"\n", b"\r"):
                    start += 1
                stop = body.find(b"endstream", start)
                self.streams[num] = body[start : stop if stop != -1 else len(body)]

    def _expand_object_streams(self) -> None:
        for num, obj in list(self.objects.items()):
            if not isinstance(obj, dict) or obj.get("Type") != "ObjStm":
                continue
            try:
                data = self.stream_data(num)
            except PdfTextError:
                continue
            count = int(self.resolve(obj.get("N")) or 0)
            first = int(self.resolve(obj.get("First")) or 0)
            header = data[:first].split()
            for i in range(count):
                try:
                    obj_num = int(header[2 * i])
                    offset = int(header[2 * i + 1])
                except (IndexError, ValueError):
                    break
                tokens = list(tokenize(data[first + offset :]))
                if not tokens:
                    continue
                value, _ = parse_object(tokens, 0)
                self.objects.setdefault(obj_num, value)

    # -- access -----------------------------------------------------------

    def resolve(self, value):
        seen = 0
        while isinstance(value, Ref) and seen < 32:
            value = self.objects.get(value.num)
            seen += 1
        return value

    def stream_data(self, num: int) -> bytes:
        raw = self.streams.get(num)
        if raw is None:
            raise PdfTextError(f"object {num} has no stream")
        meta = self.resolve(self.objects.get(num)) or {}
        filters = self.resolve(meta.get("Filter"))
        filters = [filters] if isinstance(filters, str) else list(filters or [])

        data = raw
        for name in filters:
            if name in ("FlateDecode", "Fl"):
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompressobj().decompress(data)  # truncated tail
            elif name in ("ASCIIHexDecode", "AHx"):
                data = decode_hex_string(data.split(b">")[0])
            else:
                raise PdfTextError(f"unsupported stream filter: {name}")

        predictor = self.resolve((self.resolve(meta.get("DecodeParms")) or {}).get("Predictor") if isinstance(self.resolve(meta.get("DecodeParms")), dict) else None)
        if predictor and int(predictor) >= 10:
            raise PdfTextError("PNG-predicted streams are not supported")
        return data

    def pages(self) -> list[dict]:
        pages = [
            obj
            for obj in self.objects.values()
            if isinstance(obj, dict) and obj.get("Type") == "Page"
        ]
        # Prefer the order given by the page tree when there is one.
        tree = [
            obj
            for obj in self.objects.values()
            if isinstance(obj, dict) and obj.get("Type") == "Pages" and obj.get("Kids")
        ]
        if tree:
            ordered = []
            for node in tree:
                for kid in self.resolve(node.get("Kids")) or []:
                    target = self.resolve(kid)
                    if isinstance(target, dict) and target.get("Type") == "Page":
                        ordered.append(target)
            if ordered:
                seen = {id(page) for page in ordered}
                ordered += [page for page in pages if id(page) not in seen]
                return ordered
        return pages

    def content_bytes(self, page: dict) -> bytes:
        contents = page.get("Contents")
        refs = contents if isinstance(contents, list) else [contents]
        chunks = []
        for ref in refs:
            if isinstance(ref, Ref):
                try:
                    chunks.append(self.stream_data(ref.num))
                except PdfTextError:
                    continue
        return b"\n".join(chunks)


# ----------------------------------------------------------------------- fonts

GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-",
    "period": ".", "slash": "/", "zero": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">",
    "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\",
    "bracketright": "]", "underscore": "_", "braceleft": "{", "bar": "|",
    "braceright": "}", "endash": "–", "emdash": "—", "bullet": "•",
    "quoteleft": "‘", "quoteright": "’", "quotedblleft": "“",
    "quotedblright": "”", "Euro": "€", "degree": "°",
    "adieresis": "ä", "Adieresis": "Ä", "odieresis": "ö", "Odieresis": "Ö",
    "aring": "å", "Aring": "Å", "udieresis": "ü", "Udieresis": "Ü",
    "eacute": "é", "Eacute": "É", "ccedilla": "ç", "germandbls": "ß",
}


def glyph_to_char(name: str) -> str:
    if name in GLYPH_NAMES:
        return GLYPH_NAMES[name]
    if len(name) == 1:
        return name
    match = re.fullmatch(r"uni([0-9A-Fa-f]{4})", name)
    if match:
        return chr(int(match.group(1), 16))
    return ""


def parse_tounicode(data: bytes) -> dict[int, str]:
    """Read the bfchar/bfrange sections of a ToUnicode CMap."""
    text = data.decode("latin-1", "replace")
    mapping: dict[int, str] = {}

    def to_text(hex_digits: str) -> str:
        raw = bytes.fromhex(hex_digits) if len(hex_digits) % 2 == 0 else b""
        try:
            return raw.decode("utf-16-be").replace("\x00", "")
        except UnicodeDecodeError:
            return ""

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = to_text(dst)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for low, high, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            start, end, base = int(low, 16), int(high, 16), int(dst, 16)
            for offset in range(min(end - start + 1, 65536)):
                mapping[start + offset] = chr(base + offset)
        for low, high, items in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S
        ):
            start = int(low, 16)
            for offset, dst in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", items)):
                if start + offset <= int(high, 16):
                    mapping[start + offset] = to_text(dst)
    return mapping


class Font:
    """Maps character codes of one font to text."""

    def __init__(self, doc: Document, font_dict: dict):
        self.two_byte = False
        self.map: dict[int, str] = {}

        subtype = doc.resolve(font_dict.get("Subtype"))
        if subtype == "Type0":
            self.two_byte = True

        tounicode = font_dict.get("ToUnicode")
        if isinstance(tounicode, Ref):
            try:
                self.map.update(parse_tounicode(doc.stream_data(tounicode.num)))
            except PdfTextError:
                pass

        encoding = doc.resolve(font_dict.get("Encoding"))
        if isinstance(encoding, dict):
            differences = doc.resolve(encoding.get("Differences")) or []
            code = 0
            for item in differences:
                if isinstance(item, (int, float)):
                    code = int(item)
                else:
                    self.map.setdefault(code, glyph_to_char(str(item)))
                    code += 1

    def decode(self, raw: bytes) -> str:
        if self.two_byte:
            codes = [
                int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw) - 1, 2)
            ]
        else:
            codes = list(raw)
        if self.map:
            return "".join(self.map.get(code, "") for code in codes)
        return bytes(codes).decode("latin-1", "replace") if not self.two_byte else ""


def page_fonts(doc: Document, page: dict) -> dict[str, Font]:
    resources = doc.resolve(page.get("Resources")) or {}
    fonts = doc.resolve(resources.get("Font")) or {}
    out = {}
    for name, ref in fonts.items():
        font_dict = doc.resolve(ref)
        if isinstance(font_dict, dict):
            out[name] = Font(doc, font_dict)
    return out


# -------------------------------------------------------------------- content


def multiply(a: tuple, b: tuple) -> tuple:
    """Multiply two PDF matrices given as (a, b, c, d, e, f)."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (
        a0 * b0 + a1 * b2,
        a0 * b1 + a1 * b3,
        a2 * b0 + a3 * b2,
        a2 * b1 + a3 * b3,
        a4 * b0 + a5 * b2 + b4,
        a4 * b1 + a5 * b3 + b5,
    )


def page_fragments(doc: Document, page: dict) -> list[tuple[float, float, str]]:
    """Return (x, y, text) for every string shown on the page."""
    fonts = page_fonts(doc, page)
    tokens = list(tokenize(doc.content_bytes(page)))

    fragments: list[tuple[float, float, str]] = []
    stack: list[tuple] = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    text_matrix = line_matrix = ctm
    font = None
    font_size = 1.0
    leading = 0.0
    operands: list = []

    def show(raw: bytes) -> None:
        nonlocal text_matrix
        text = font.decode(raw) if font else raw.decode("latin-1", "replace")
        if not text:
            return
        matrix = multiply(text_matrix, ctm)
        fragments.append((matrix[4], matrix[5], text))

    for kind, value in tokens:
        if kind != "keyword":
            if kind == "array_open":
                operands.append("[")
            elif kind == "array_close":
                items = []
                while operands and operands[-1] != "[":
                    items.append(operands.pop())
                if operands:
                    operands.pop()
                operands.append(list(reversed(items)))
            else:
                operands.append(value)
            continue

        op = value
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else ctm
        elif op == "cm" and len(operands) >= 6:
            ctm = multiply(tuple(float(n) for n in operands[-6:]), ctm)
        elif op == "BT":
            text_matrix = line_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        elif op == "Tf" and len(operands) >= 2:
            font = fonts.get(str(operands[-2]))
            try:
                font_size = float(operands[-1])
            except (TypeError, ValueError):
                font_size = 1.0
        elif op == "TL" and operands:
            leading = float(operands[-1])
        elif op in ("Td", "TD") and len(operands) >= 2:
            tx, ty = float(operands[-2]), float(operands[-1])
            if op == "TD":
                leading = -ty
            line_matrix = multiply((1.0, 0.0, 0.0, 1.0, tx, ty), line_matrix)
            text_matrix = line_matrix
        elif op == "Tm" and len(operands) >= 6:
            line_matrix = text_matrix = tuple(float(n) for n in operands[-6:])
        elif op == "T*":
            line_matrix = multiply((1.0, 0.0, 0.0, 1.0, 0.0, -leading), line_matrix)
            text_matrix = line_matrix
        elif op == "Tj" and operands:
            if isinstance(operands[-1], bytes):
                show(operands[-1])
        elif op in ("'", '"') and operands:
            line_matrix = multiply((1.0, 0.0, 0.0, 1.0, 0.0, -leading), line_matrix)
            text_matrix = line_matrix
            if isinstance(operands[-1], bytes):
                show(operands[-1])
        elif op == "TJ" and operands:
            items = operands[-1] if isinstance(operands[-1], list) else []
            parts = []
            for item in items:
                if isinstance(item, bytes):
                    parts.append(font.decode(item) if font else item.decode("latin-1", "replace"))
                elif isinstance(item, (int, float)) and item <= -120:
                    parts.append(" ")  # a wide negative kern reads as a space
            text = "".join(parts)
            if text:
                matrix = multiply(text_matrix, ctm)
                fragments.append((matrix[4], matrix[5], text))
        operands = []

    return fragments


def fragments_to_lines(fragments: list[tuple[float, float, str]], tolerance: float = 3.0) -> list[str]:
    """Group fragments into visual lines, top to bottom, left to right."""
    lines: list[list[tuple[float, float, str]]] = []
    for fragment in sorted(fragments, key=lambda f: (-f[1], f[0])):
        for line in lines:
            if abs(line[0][1] - fragment[1]) <= tolerance:
                line.append(fragment)
                break
        else:
            lines.append([fragment])

    out = []
    for line in lines:
        line.sort(key=lambda f: f[0])
        text = ""
        previous_x = None
        for x, _y, fragment_text in line:
            if previous_x is not None and x - previous_x > 8 and not text.endswith(" "):
                text += " "
            text += fragment_text
            previous_x = x
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            out.append(text)
    return out


def extract_pages(data: bytes) -> list[list[str]]:
    """Extract one list of text lines per page."""
    if not data.startswith(b"%PDF"):
        raise PdfTextError("not a PDF file")
    if b"/Encrypt" in data:
        raise PdfTextError("encrypted PDFs are not supported")

    doc = Document(data)
    pages = doc.pages()
    if not pages:
        raise PdfTextError("no pages found")

    out = [fragments_to_lines(page_fragments(doc, page)) for page in pages]
    if not any(out):
        raise PdfTextError("no extractable text (the PDF may be a scan)")
    return out


def extract_text(data: bytes) -> str:
    return "\n\n".join("\n".join(page) for page in extract_pages(data) if page)
