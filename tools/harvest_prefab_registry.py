"""
Find Rec Room's prefab registry in the game files and extract every prefab ID -> type name.

The PrefabIds.csv from an export is used as a set of anchors: the object that holds most of those
IDs and names is almost certainly the registry. Its entries are then extracted and checked against
the known names, so you can tell whether the extraction is right.

Usage:
    pip install UnityPy pyahocorasick
    python harvest_prefab_registry.py --anchors PrefabIds.csv --install "path/to/RecRoom"

Outputs (in --out-dir):
    prefab_registry.csv    Guid,Base64 Guid,Type rows, loadable in the inspector
    registry_report.json   Where the registry was found, how well it matched the anchors, raw context
    containers/            Raw bytes and readable fields of the best candidate objects, for manual inspection
"""

import argparse
import base64
import csv
import json
import math
import re
import struct
import sys
import uuid
from collections import Counter, defaultdict
from functools import reduce
from pathlib import Path

import ahocorasick

CHUNK_SIZE = 64 * 1024 * 1024
BUNDLE_SIGNATURES = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
# The final Steam client's engine version. Its bundles have the version stripped, so UnityPy needs to be told.
DEFAULT_UNITY_VERSION = "6000.0.27f1"
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{3,}")
GUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
MIN_ANCHORS_FOR_REGISTRY = 3
CONTAINERS_TO_KEEP = 5


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

def load_anchors(csv_path):
    anchors = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                guid = uuid.UUID((row.get("Guid") or "").strip())
            except ValueError:
                continue
            type_name = (row.get("Type") or "").strip()
            if type_name:
                anchors[str(guid)] = type_name
    if not anchors:
        sys.exit(f"No usable rows in {csv_path}")
    return anchors


def unity_guid_text(raw_bytes):
    return "".join(f"{byte & 0x0F:x}{byte >> 4:x}" for byte in raw_bytes)


def guid_patterns(guid_text):
    guid = uuid.UUID(guid_text)
    return {
        "raw bytes": guid.bytes_le,
        "big-endian bytes": guid.bytes,
        "guid string": str(guid).encode("ascii"),
        "GUID STRING": str(guid).upper().encode("ascii"),
        "hex string": guid.hex.encode("ascii"),
        "unity asset guid": unity_guid_text(guid.bytes_le).encode("ascii"),
        "unity asset guid (big-endian)": unity_guid_text(guid.bytes).encode("ascii"),
        "utf-16 guid string": str(guid).encode("utf-16-le"),
    }


def build_automaton(anchors):
    automaton = ahocorasick.Automaton()
    longest = 0
    names_added = set()
    for guid_text, type_name in anchors.items():
        for form, pattern in guid_patterns(guid_text).items():
            automaton.add_word(pattern.decode("latin-1"), ("guid", guid_text, form, len(pattern)))
            longest = max(longest, len(pattern))
        if len(type_name) >= 6 and type_name not in names_added:
            names_added.add(type_name)
            for form, pattern in (("name", type_name.encode("ascii")), ("utf-16 name", type_name.encode("utf-16-le"))):
                automaton.add_word(pattern.decode("latin-1"), ("name", type_name, form, len(pattern)))
                longest = max(longest, len(pattern))
    automaton.make_automaton()
    return automaton, longest


# ---------------------------------------------------------------------------
# Locating: which file or bundle object holds the most anchors
# ---------------------------------------------------------------------------

class Container:
    def __init__(self, file_path, object_label=None, path_id=None):
        self.file_path = str(file_path)
        self.object_label = object_label
        self.path_id = path_id
        self.guid_hits = defaultdict(list)  # guid -> [(offset, form)]
        self.name_hits = defaultdict(list)  # type name -> [(offset, form)]
        self.raw_data = None
        self.typetree = None

    @property
    def key(self):
        return (self.file_path, self.path_id)

    @property
    def label(self):
        return f"{self.file_path}{f' :: {self.object_label}' if self.object_label else ''}"

    @property
    def score(self):
        return len(self.guid_hits) * 2 + len(self.name_hits)


def record_matches(container, text, automaton, offset_base=0, skip_before=0):
    for end_index, (kind, value, form, length) in automaton.iter(text):
        start_index = end_index - length + 1
        if start_index + length <= skip_before:
            continue
        target = container.guid_hits if kind == "guid" else container.name_hits
        if len(target[value]) < 20:
            target[value].append((offset_base + start_index, form))


def scan_plain_file(path, automaton, longest):
    container = Container(path)
    overlap = longest - 1
    carried = b""
    file_offset = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            data = carried + chunk
            record_matches(container, data.decode("latin-1"), automaton, file_offset - len(carried), len(carried))
            carried = data[-overlap:] if overlap > 0 else b""
            file_offset += len(chunk)
    return [container] if container.guid_hits or container.name_hits else []


def object_label(unity_object):
    try:
        name = unity_object.peek_name()
    except Exception:
        name = None
    return f"{unity_object.type.name} #{unity_object.path_id}{f' {name!r}' if name else ''}"


def scan_bundle(path, automaton):
    import UnityPy

    containers = []
    environment = UnityPy.load(str(path))
    for unity_object in environment.objects:
        try:
            data = bytes(unity_object.get_raw_data())
        except Exception:
            continue
        container = Container(path, object_label(unity_object), unity_object.path_id)
        record_matches(container, data.decode("latin-1"), automaton)
        if container.guid_hits or container.name_hits:
            container.raw_data = data
            if len(container.guid_hits) >= MIN_ANCHORS_FOR_REGISTRY:
                try:
                    container.typetree = unity_object.read_typetree()
                except Exception:
                    container.typetree = None
            containers.append(container)
    return containers


def is_unity_bundle(path):
    with open(path, "rb") as handle:
        return handle.read(16).startswith(BUNDLE_SIGNATURES)


def locate_containers(install_dir, automaton, longest, unpack_bundles):
    containers = []
    files = [path for path in Path(install_dir).rglob("*") if path.is_file()]
    bundles_failed = 0
    for index, path in enumerate(files, start=1):
        print(f"[{index}/{len(files)}] {path}"[:160].ljust(160), end="\r", flush=True)
        try:
            if unpack_bundles and is_unity_bundle(path):
                try:
                    containers.extend(scan_bundle(path, automaton))
                except Exception:
                    bundles_failed += 1
            else:
                containers.extend(scan_plain_file(path, automaton, longest))
        except OSError:
            continue
    print()
    if bundles_failed:
        print(f"{bundles_failed} bundles couldn't be opened")
    return sorted(containers, key=lambda container: -container.score)


# ---------------------------------------------------------------------------
# Extracting from readable fields (bundles with type trees)
# ---------------------------------------------------------------------------

def int_readings(values):
    """A GUID split into ints can be packed a few ways; return each packing as 16 bytes, keyed by its rule."""
    readings = {}
    if len(values) == 4 and all(isinstance(value, int) for value in values):
        for fmt in ("<IIII", ">IIII"):
            readings[fmt] = struct.pack(fmt, *[value & 0xFFFFFFFF for value in values])
    elif len(values) == 2 and all(isinstance(value, int) for value in values):
        for fmt in ("<QQ", ">QQ"):
            readings[fmt] = struct.pack(fmt, *[value & 0xFFFFFFFFFFFFFFFF for value in values])
    elif len(values) == 16 and all(isinstance(value, int) and 0 <= value < 256 for value in values):
        readings["bytes"] = bytes(values)
    return readings


def guid_readings(node):
    """Every GUID this typetree node could represent, keyed by how it was read."""
    readings = {}
    if isinstance(node, str) and GUID_TEXT.match(node):
        compact = node.replace("-", "").lower()
        readings["text"] = str(uuid.UUID(compact))
        swapped = bytes(int(compact[index + 1] + compact[index], 16) for index in range(0, 32, 2))
        readings["unity text"] = str(uuid.UUID(bytes_le=swapped))
        readings["unity text big-endian"] = str(uuid.UUID(bytes=swapped))
    else:
        values = list(node.values()) if isinstance(node, dict) else node if isinstance(node, list) else None
        if values is not None:
            for rule, packed in int_readings(values).items():
                readings[f"{rule} little-endian guid"] = str(uuid.UUID(bytes_le=packed))
                readings[f"{rule} big-endian guid"] = str(uuid.UUID(bytes=packed))
    return readings


def walk_typetree(node, path=()):
    """Yield (path, node, parent) for every node in a typetree."""
    stack = [(node, path, None)]
    while stack:
        current, current_path, parent = stack.pop()
        yield current_path, current, parent
        if isinstance(current, dict):
            for key, value in current.items():
                stack.append((value, current_path + (str(key),), current))
        elif isinstance(current, list):
            for index, value in enumerate(current):
                stack.append((value, current_path + (index,), current))


def path_shape(path):
    return tuple("#" if isinstance(part, int) else part for part in path)


def node_at(tree, path):
    node = tree
    for part in path:
        node = node[part]
    return node


def entry_root_path(path):
    """The entry an ID belongs to: everything up to the first list index (the whole tree if there's none)."""
    for index, part in enumerate(path):
        if isinstance(part, int):
            return path[:index + 1]
    return ()


def flatten_fields(node, prefix=()):
    """Every scalar field in an entry, keyed by its path. Long numeric arrays (raw bytes) are skipped."""
    fields = {}
    if isinstance(node, dict):
        for key, value in node.items():
            fields.update(flatten_fields(value, prefix + (str(key),)))
    elif isinstance(node, list):
        if len(node) <= 16 and not all(isinstance(value, int) for value in node):
            for index, value in enumerate(node):
                fields.update(flatten_fields(value, prefix + (index,)))
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        fields[prefix] = node
    return fields


def normalized_name(text):
    """DELAY_NODE, DelayNode and "Delay Node" all become delaynode."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def names_match(anchor_name, extracted_name):
    anchor, extracted = normalized_name(anchor_name), normalized_name(extracted_name)
    return bool(anchor) and bool(extracted) and (anchor in extracted or extracted in anchor)


POINTER_KEYS = {"m_PathID", "m_FileID"}


def field_label(path):
    return ".".join(str(part) for part in path) or "(root)"


def learn_number_field(entries, anchors):
    """An int field that is the same for the same type name and different between type names."""
    candidate_keys = set()
    for guid, fields in entries:
        if guid in anchors:
            candidate_keys.update(key for key, value in fields.items() if isinstance(value, int))
    best_key, best_score = None, 0
    for key in candidate_keys:
        if any(part in POINTER_KEYS for part in key if isinstance(part, str)):
            continue  # Object pointers are unique per object, so they'd look like perfect type numbers.
        anchor_values = [fields[key] for guid, fields in entries if guid in anchors and key in fields]
        if not anchor_values or max(abs(value) for value in anchor_values) > 1_000_000:
            continue
        values_by_name = defaultdict(set)
        names_by_value = defaultdict(set)
        for guid, fields in entries:
            if guid in anchors and key in fields:
                values_by_name[anchors[guid]].add(fields[key])
                names_by_value[fields[key]].add(anchors[guid])
        consistent = sum(1 for values in values_by_name.values() if len(values) == 1)
        distinct = sum(1 for names in names_by_value.values() if len(names) == 1)
        score = min(consistent, distinct)
        if score > best_score:
            best_key, best_score = key, score
    return best_key, best_score


def extract_from_typetree(typetree, anchors):
    """Find GUID-shaped values, learn the reading rule and the naming field from the anchors, apply them to every entry.
    Returns (rows, note, asset_names, sample_fields)."""
    anchor_set = set(anchors)
    found = []
    for path, node, parent in walk_typetree(typetree):
        readings = guid_readings(node)
        if readings:
            found.append((path, readings))

    rule_votes = Counter()
    shape_votes = Counter()
    for path, readings in found:
        for rule, guid in readings.items():
            if guid in anchor_set:
                rule_votes[rule] += 1
                shape_votes[path_shape(path)] += 1
    if not rule_votes:
        return {}, "no GUID-shaped values in the readable fields match the anchors", {}, None
    rule = rule_votes.most_common(1)[0][0]
    shape = shape_votes.most_common(1)[0][0]
    shape_text = "/".join(str(part) for part in shape)

    entries = []
    for path, readings in found:
        if path_shape(path) != shape or rule not in readings:
            continue
        root_path = entry_root_path(path)
        guid_path_inside = tuple(path[len(root_path):])
        fields = flatten_fields(node_at(typetree, root_path))
        fields = {key: value for key, value in fields.items() if key[:len(guid_path_inside)] != guid_path_inside}
        entries.append((readings[rule], fields))

    sample_fields = next((fields for guid, fields in entries if guid in anchor_set), None)
    asset_names = {}
    for guid, fields in entries:
        asset_names[guid] = str(fields.get(("__prefab_name",)) or fields.get(("m_Name",)) or "")

    name_votes = Counter()
    for guid, fields in entries:
        if guid in anchor_set:
            for key, value in fields.items():
                if isinstance(value, str) and names_match(anchors[guid], value):
                    name_votes[key] += 1
    if name_votes and name_votes.most_common(1)[0][1] >= MIN_ANCHORS_FOR_REGISTRY:
        name_key, votes = name_votes.most_common(1)[0]
        rows = {guid: str(fields.get(name_key, "")) for guid, fields in entries}
        return rows, f"typetree {shape_text}, read as {rule}, name in '{field_label(name_key)}' ({votes} anchors agree)", asset_names, sample_fields

    number_key, score = learn_number_field(entries, anchors)
    if number_key is not None and score >= MIN_ANCHORS_FOR_REGISTRY:
        enum_names = {fields[number_key]: anchors[guid] for guid, fields in entries if guid in anchor_set and number_key in fields}
        rows = {}
        for guid, fields in entries:
            number = fields.get(number_key)
            if number in enum_names:
                rows[guid] = enum_names[number]
            else:
                asset_name = asset_names.get(guid)
                rows[guid] = f"{asset_name} (UNKNOWN_TYPE_{number})" if asset_name else f"UNKNOWN_TYPE_{number}"
        return rows, (f"typetree {shape_text}, read as {rule}, type number in '{field_label(number_key)}' "
                      f"({score} anchor types line up, {len(enum_names)} numbers named from the anchors, so this can't be checked independently)"), asset_names, sample_fields

    rows = {guid: asset_names.get(guid, "") for guid, fields in entries}
    return rows, f"typetree {shape_text}, read as {rule}, no field lines up with the anchor names; using object names", asset_names, sample_fields


# ---------------------------------------------------------------------------
# Extracting from raw bytes (fixed-size entries)
# ---------------------------------------------------------------------------

def guess_stride(offsets):
    if len(offsets) < MIN_ANCHORS_FOR_REGISTRY:
        return None
    gaps = [second - first for first, second in zip(offsets, offsets[1:]) if second != first]
    if not gaps:
        return None
    stride = reduce(math.gcd, gaps)
    if stride < 16 or stride > 4096 or max(gaps) // stride > 512:
        return None
    return stride


def printable_runs(data, start, end):
    return [(start + match.start(), match.group().decode("ascii")) for match in PRINTABLE_RUN.finditer(data[max(0, start):end])]


def learn_name_position(data, anchor_entries, anchors, stride):
    """For each anchor, find its known name among the strings after the GUID and learn which one it is."""
    positions = Counter()
    for offset, guid_text in anchor_entries:
        runs = printable_runs(data, offset + 16, offset + stride)
        for index, (run_offset, text) in enumerate(runs):
            if anchors[guid_text] in text:
                positions[("after", index)] += 1
        runs_before = printable_runs(data, offset - stride + 16, offset)
        for index, (run_offset, text) in enumerate(reversed(runs_before)):
            if anchors[guid_text] in text:
                positions[("before", index)] += 1
    return positions.most_common(1)[0] if positions else (None, 0)


NAME_BYTES = re.compile(rb"^[A-Za-z0-9_ .\-]+$")


def read_prefixed_string(data, position):
    """Unity strings are an int32 length followed by the bytes, padded to 4."""
    if position < 0 or position + 4 > len(data):
        return None, None
    length = struct.unpack_from("<i", data, position)[0]
    if not 1 <= length <= 128 or position + 4 + length > len(data):
        return None, None
    text = data[position + 4:position + 4 + length]
    if not NAME_BYTES.match(text):
        return None, None
    end = position + 4 + length
    return text.decode("ascii"), end + (-end % 4)


def is_v4_guid(entry_bytes, form):
    if len(entry_bytes) != 16:
        return False
    reading = uuid.UUID(bytes_le=entry_bytes) if form == "raw bytes" else uuid.UUID(bytes=entry_bytes)
    return reading.version == 4 and reading.variant == uuid.RFC_4122


def guid_text_from(entry_bytes, form):
    return str(uuid.UUID(bytes_le=entry_bytes) if form == "raw bytes" else uuid.UUID(bytes=entry_bytes))


def extract_prefixed_names(data, container, anchors, form):
    """Learn from the anchors whether each name is a Unity string right after or before its ID, then scan for that shape."""
    after_votes = 0
    before_gaps = Counter()
    for guid_text, hits in container.guid_hits.items():
        for offset, hit_form in hits:
            if hit_form != form:
                continue
            name, _ = read_prefixed_string(data, offset + 16)
            if name and anchors[guid_text].lower() in name.lower():
                after_votes += 1
            for back in range(4, 260, 4):
                name, name_end = read_prefixed_string(data, offset - back)
                if name and anchors[guid_text].lower() in name.lower() and name_end <= offset:
                    before_gaps[offset - name_end] += 1
                    break

    rows = {}
    if after_votes >= MIN_ANCHORS_FOR_REGISTRY:
        for position in range(0, len(data) - 20, 4):
            entry_bytes = data[position:position + 16]
            if not is_v4_guid(entry_bytes, form):
                continue
            name, _ = read_prefixed_string(data, position + 16)
            if name:
                rows[guid_text_from(entry_bytes, form)] = name
        return rows, f"each ID is followed by its name as a Unity string ({after_votes} anchors agree)"

    if before_gaps and before_gaps.most_common(1)[0][1] >= MIN_ANCHORS_FOR_REGISTRY:
        gap, votes = before_gaps.most_common(1)[0]
        for position in range(0, len(data) - 8, 4):
            name, name_end = read_prefixed_string(data, position)
            if not name:
                continue
            entry_bytes = data[name_end + gap:name_end + gap + 16]
            if is_v4_guid(entry_bytes, form):
                rows[guid_text_from(entry_bytes, form)] = name
        return rows, f"each name is a Unity string {gap} bytes before its ID ({votes} anchors agree)"

    return {}, "names aren't stored as Unity strings next to the IDs"


def extract_raw_table(data, container, anchors, form):
    anchor_entries = sorted((offset, guid) for guid, hits in container.guid_hits.items() for offset, hit_form in hits if hit_form == form)
    offsets = sorted(set(offset for offset, _ in anchor_entries))
    stride = guess_stride(offsets)
    if stride is None:
        return {}, "anchors aren't evenly spaced, so this isn't a fixed-size table"

    def looks_like_guid(entry_bytes):
        if len(entry_bytes) != 16 or entry_bytes.count(0) > 3:
            return False
        reading = uuid.UUID(bytes_le=entry_bytes) if form == "raw bytes" else uuid.UUID(bytes=entry_bytes)
        return reading.version == 4

    for candidate in range(16, stride, 4):
        if stride % candidate == 0 and all(looks_like_guid(data[position:position + 16]) for position in range(offsets[0], offsets[-1] + 1, candidate)):
            stride = candidate
            break

    (name_rule, votes) = learn_name_position(data, anchor_entries, anchors, stride)
    rows = {}
    for direction in (1, -1):
        position = offsets[0] if direction == 1 else offsets[0] - stride
        misses = 0
        while 0 <= position <= len(data) - 16 and misses < 2:
            entry_bytes = data[position:position + 16]
            if not looks_like_guid(entry_bytes):
                misses += 1
            else:
                misses = 0
                guid = str(uuid.UUID(bytes_le=entry_bytes) if form == "raw bytes" else uuid.UUID(bytes=entry_bytes))
                name = ""
                if name_rule:
                    side, index = name_rule
                    runs = printable_runs(data, position + 16, position + stride) if side == "after" else list(reversed(printable_runs(data, position - stride + 16, position)))
                    name = runs[index][1] if index < len(runs) else ""
                rows[guid] = name
            position += stride * direction
    note = f"{stride}-byte entries" + (f", name is string {name_rule[1] + 1} {name_rule[0]} each ID ({votes} anchors agree)" if name_rule else ", names not found next to IDs")
    return rows, note


# ---------------------------------------------------------------------------
# Extracting from one definition object per prefab
# ---------------------------------------------------------------------------

# A MonoBehaviour's serialized data starts with m_GameObject (PPtr), m_Enabled, m_Script (PPtr) and m_Name.
SCRIPT_POINTER_OFFSET = 16
NAME_OFFSET = 28


def read_script_pointer(data):
    if len(data) < NAME_OFFSET:
        return None
    file_id, path_id = struct.unpack_from("<iq", data, SCRIPT_POINTER_OFFSET)
    return (file_id, path_id)


def read_object_name(data):
    """m_Name, or "" if it's empty; also where the object's own fields begin."""
    if len(data) < NAME_OFFSET + 4:
        return None, None
    length = struct.unpack_from("<i", data, NAME_OFFSET)[0]
    if length == 0:
        return "", NAME_OFFSET + 4
    name, name_end = read_prefixed_string(data, NAME_OFFSET)
    return name, name_end


def find_anchor_offsets(data, anchors):
    found = []
    for guid_text in anchors:
        guid = uuid.UUID(guid_text)
        for form, pattern in (("raw bytes", guid.bytes_le), ("big-endian bytes", guid.bytes)):
            offset = data.find(pattern)
            if offset != -1:
                found.append((guid_text, form, offset))
    return found


def learn_type_field(anchor_records):
    """Find an int32 near the ID that is the same for the same type name and differs between type names."""
    best_offset, best_score = None, 0
    for relative in range(-64, 128, 4):
        if 0 <= relative < 16:
            continue
        values_by_name = defaultdict(set)
        names_by_value = defaultdict(set)
        readable = 0
        for data, guid_offset, type_name in anchor_records:
            position = guid_offset + relative
            if not 0 <= position <= len(data) - 4:
                continue
            value = struct.unpack_from("<i", data, position)[0]
            values_by_name[type_name].add(value)
            names_by_value[value].add(type_name)
            readable += 1
        if readable < MIN_ANCHORS_FOR_REGISTRY:
            continue
        consistent = sum(1 for values in values_by_name.values() if len(values) == 1)
        distinct = sum(1 for names in names_by_value.values() if len(names) == 1)
        score = min(consistent, distinct)
        if score > best_score:
            best_offset, best_score = relative, score
    return best_offset, best_score


def extract_per_object_raw(objects, anchors):
    """objects: {object_id: raw bytes}. Learn the ID position and type field from anchor objects, apply to their siblings."""
    anchor_objects = []
    for object_id, data in objects.items():
        for guid_text, form, offset in find_anchor_offsets(data, anchors):
            anchor_objects.append((object_id, guid_text, form, offset))
    if len(anchor_objects) < MIN_ANCHORS_FOR_REGISTRY:
        return {}, {}, "fewer than three definition objects contain an anchor"

    form = Counter(form for _, _, form, _ in anchor_objects).most_common(1)[0][0]
    scripts = Counter(read_script_pointer(objects[object_id]) for object_id, _, hit_form, _ in anchor_objects if hit_form == form)
    script, _ = scripts.most_common(1)[0]

    # Where does the ID sit, measured from the end of m_Name (names vary in length)?
    relative_votes = Counter()
    for object_id, guid_text, hit_form, offset in anchor_objects:
        if hit_form != form:
            continue
        _, fields_start = read_object_name(objects[object_id])
        relative_votes[offset - fields_start if fields_start is not None else ("absolute", offset)] += 1
    relative, relative_count = relative_votes.most_common(1)[0]

    siblings = {object_id: data for object_id, data in objects.items() if read_script_pointer(data) == script}
    anchor_records = []
    for object_id, guid_text, hit_form, offset in anchor_objects:
        if hit_form == form and object_id in siblings:
            anchor_records.append((objects[object_id], offset, anchors[guid_text]))
    type_offset, type_score = learn_type_field(anchor_records)
    enum_names = {}
    if type_offset is not None:
        for data, guid_offset, type_name in anchor_records:
            enum_names[struct.unpack_from("<i", data, guid_offset + type_offset)[0]] = type_name

    rows, asset_names = {}, {}
    for object_id, data in siblings.items():
        name, fields_start = read_object_name(data)
        if isinstance(relative, tuple):
            guid_offset = relative[1]
        elif fields_start is None:
            continue
        else:
            guid_offset = fields_start + relative
        entry_bytes = data[guid_offset:guid_offset + 16]
        if not is_v4_guid(entry_bytes, form):
            continue
        guid = guid_text_from(entry_bytes, form)
        type_name = ""
        if type_offset is not None and 0 <= guid_offset + type_offset <= len(data) - 4:
            number = struct.unpack_from("<i", data, guid_offset + type_offset)[0]
            type_name = enum_names.get(number, f"UNKNOWN_TYPE_{number}")
        if guid in anchors:
            type_name = anchors[guid]
        elif type_name.startswith("UNKNOWN_TYPE_") and name:
            type_name = f"{name} ({type_name})"
        rows[guid] = type_name or name or ""
        asset_names[guid] = name or ""

    note = (f"one definition object per prefab ({len(siblings)} objects share the anchors' script), "
            f"ID at {relative} bytes after m_Name ({relative_count} anchors agree)"
            + (f", type number at {type_offset:+} bytes from the ID ({type_score} anchor types line up)" if type_offset is not None else ", no type number found, using object names"))
    return rows, asset_names, note


def extract_per_object_bundle(bundle_path, anchors):
    import UnityPy

    environment = UnityPy.load(str(bundle_path))
    raw_objects = {}
    trees = {}
    for unity_object in environment.objects:
        if unity_object.type.name != "MonoBehaviour":
            continue
        try:
            raw_objects[unity_object.path_id] = bytes(unity_object.get_raw_data())
        except Exception:
            continue

    gameobject_names = {}
    for unity_object in environment.objects:
        if unity_object.type.name != "GameObject":
            continue
        try:
            name = unity_object.read_typetree().get("m_Name", "")
        except Exception:
            try:
                name = unity_object.peek_name() or ""
            except Exception:
                name = ""
        gameobject_names[unity_object.path_id] = name

    anchor_ids = {object_id for object_id, data in raw_objects.items() if find_anchor_offsets(data, anchors)}
    for unity_object in environment.objects:
        if unity_object.path_id in anchor_ids:
            try:
                trees[unity_object.path_id] = unity_object.read_typetree()
            except Exception:
                pass

    if len(trees) >= MIN_ANCHORS_FOR_REGISTRY:
        script_ids = {read_script_pointer(raw_objects[object_id]) for object_id in anchor_ids}
        for unity_object in environment.objects:
            if unity_object.path_id in raw_objects and unity_object.path_id not in trees and read_script_pointer(raw_objects[unity_object.path_id]) in script_ids:
                try:
                    trees[unity_object.path_id] = unity_object.read_typetree()
                except Exception:
                    pass
        for tree in trees.values():
            game_object = tree.get("m_GameObject") if isinstance(tree, dict) else None
            if isinstance(game_object, dict) and game_object.get("m_FileID") == 0:
                tree["__prefab_name"] = gameobject_names.get(game_object.get("m_PathID"), "")
        print(f"    {len(gameobject_names)} GameObjects in the bundle, {sum(1 for tree in trees.values() if tree.get('__prefab_name'))} definitions linked to a named one")
        combined = {"objects": list(trees.values())}
        rows, note, names, sample_fields = extract_from_typetree(combined, anchors)
        if rows:
            if sample_fields is not None and (not validate(rows, anchors)[1] or "type number" in note):
                print("\n    Fields of one anchor definition object (paste these if the names still don't match):")
                for key, value in list(sample_fields.items())[:60]:
                    print(f"      {field_label(key)} = {value!r}"[:160])
            return rows, names, f"readable fields of {len(trees)} definition objects: {note}", combined

    rows, names, note = extract_per_object_raw(raw_objects, anchors)
    return rows, names, note, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate(rows, anchors):
    checked = [guid for guid in anchors if guid in rows]
    matching = [guid for guid in checked if names_match(anchors[guid], rows[guid])]
    return len(checked), len(matching)


def hex_context(data, offset, radius=64):
    start = max(0, offset - radius)
    return {"start": start, "hex": data[start:offset + 16 + radius].hex(" ")}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--anchors", required=True, help="PrefabIds.csv from an export (more rows = better)")
    parser.add_argument("--install", required=True, help="Rec Room install folder")
    parser.add_argument("--out-dir", default="prefab_registry")
    parser.add_argument("--no-bundles", action="store_true", help="Skip decompressing Unity bundles (much faster)")
    parser.add_argument("--unity-version", default=DEFAULT_UNITY_VERSION, help=f"Unity version to assume for bundles (default: {DEFAULT_UNITY_VERSION}, the final Steam client)")
    arguments = parser.parse_args()

    anchors = load_anchors(arguments.anchors)
    print(f"{len(anchors)} anchor prefabs from {arguments.anchors}")
    out_dir = Path(arguments.out_dir)
    (out_dir / "containers").mkdir(parents=True, exist_ok=True)

    unpack_bundles = not arguments.no_bundles
    if unpack_bundles:
        import UnityPy

        UnityPy.config.FALLBACK_UNITY_VERSION = arguments.unity_version
        print(f"Unity version: {arguments.unity_version}")

    automaton, longest = build_automaton(anchors)
    containers = locate_containers(arguments.install, automaton, longest, unpack_bundles)
    if not containers:
        sys.exit("None of the anchor IDs or names appear anywhere in the game files.")

    print("\nBest candidates:")
    for container in containers[:10]:
        print(f"  {len(container.guid_hits):3} IDs, {len(container.name_hits):3} names  {container.label}")

    report = {"anchors": len(anchors), "candidates": [], "extraction": None}
    best_rows, best_note, best_container, best_matches = {}, "", None, -1
    for rank, container in enumerate(containers[:CONTAINERS_TO_KEEP]):
        data = container.raw_data if container.raw_data is not None else Path(container.file_path).read_bytes()
        stem = f"{rank:02d}_{Path(container.file_path).name}{f'_{container.path_id}' if container.path_id else ''}"
        if container.raw_data is not None:
            (out_dir / "containers" / f"{stem}.bin").write_bytes(data)
        if container.typetree is not None:
            (out_dir / "containers" / f"{stem}.json").write_text(json.dumps(container.typetree, indent=1, default=str), encoding="utf-8")

        first_hits = [hits[0] for hits in container.guid_hits.values()]
        report["candidates"].append({
            "container": container.label,
            "ids_found": len(container.guid_hits),
            "names_found": len(container.name_hits),
            "forms": dict(Counter(form for hits in container.guid_hits.values() for _, form in hits)),
            "context": [hex_context(data, offset) for offset, _ in first_hits[:3]],
        })
        if len(container.guid_hits) < MIN_ANCHORS_FOR_REGISTRY:
            continue

        attempts = []
        if container.typetree is not None:
            rows, note, _, _ = extract_from_typetree(container.typetree, anchors)
            if rows:
                attempts.append((rows, f"readable fields: {note}"))
        for form in ("raw bytes", "big-endian bytes"):
            if any(hit_form == form for hits in container.guid_hits.values() for _, hit_form in hits):
                for extractor in (extract_prefixed_names, extract_raw_table):
                    rows, note = extractor(data, container, anchors, form)
                    if rows:
                        attempts.append((rows, f"raw {form}: {note}"))

        for rows, note in attempts:
            checked, matching = validate(rows, anchors)
            print(f"\n  {container.label}\n    {note}\n    {len(rows)} entries, {matching}/{checked} anchor names match")
            if matching > best_matches or (matching == best_matches and len(rows) > len(best_rows)):
                best_rows, best_note, best_container, best_matches = rows, note, container, matching

    asset_names = {}
    checked_best, matching_best = validate(best_rows, anchors) if best_rows else (0, 0)
    if unpack_bundles and matching_best < MIN_ANCHORS_FOR_REGISTRY:
        anchors_per_bundle = defaultdict(set)
        for container in containers:
            if container.path_id is not None:
                anchors_per_bundle[container.file_path].update(container.guid_hits)
        if anchors_per_bundle:
            bundle_path, bundle_anchors = max(anchors_per_bundle.items(), key=lambda item: len(item[1]))
            if len(bundle_anchors) >= MIN_ANCHORS_FOR_REGISTRY:
                print(f"\nAnchors are spread over many objects in {bundle_path}; reading them as one definition per prefab")
                rows, names, note, combined = extract_per_object_bundle(bundle_path, anchors)
                if combined is not None:
                    (out_dir / "containers" / "definition_objects.json").write_text(json.dumps(combined, indent=1, default=str), encoding="utf-8")
                checked, matching = validate(rows, anchors)
                print(f"    {note}\n    {len(rows)} entries, {matching}/{checked} anchor names match")
                if rows and matching >= matching_best:
                    best_rows, best_note, asset_names, best_matches = rows, note, names, matching
                    best_container = next(container for container in containers if container.file_path == bundle_path)

    registry_path = out_dir / "prefab_registry.csv"
    if best_rows:
        merged = {guid: anchors.get(guid) or name for guid, name in best_rows.items()}
        for guid, type_name in anchors.items():
            merged.setdefault(guid, type_name)
        with open(registry_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
            writer.writerow(["Guid", "Base64 Guid", "Type", "Asset Name"])
            for guid, type_name in sorted(merged.items(), key=lambda item: (item[1], item[0])):
                writer.writerow([guid, base64.b64encode(uuid.UUID(guid).bytes_le).decode("ascii"), type_name, asset_names.get(guid, "")])
        checked, matching = validate(best_rows, anchors)
        report["extraction"] = {"container": best_container.file_path if asset_names else best_container.label, "method": best_note, "entries": len(best_rows), "anchors_checked": checked, "anchors_matching": matching}
        print(f"\nWrote {len(merged)} prefabs to {registry_path} ({matching}/{checked} anchor names matched)")
        if checked and matching < checked:
            print("Some anchor names didn't match. Check prefab_registry.csv against the anchors before relying on it.")
    else:
        print("\nFound where the anchors are stored, but couldn't extract a table automatically.")
        print(f"The candidate objects and raw context around the anchors are in {out_dir}.")

    (out_dir / "registry_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {out_dir / 'registry_report.json'}")


if __name__ == "__main__":
    main()
