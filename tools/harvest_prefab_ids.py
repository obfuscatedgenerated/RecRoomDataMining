"""
Build one prefab ID -> type table from as many Rec Room exports as you can collect.

It reads every PrefabIds.csv it finds, and also scans each export's room data for prefab IDs,
so prefabs that appear in rooms but have no name yet are listed for people to identify.

Exports can be folders or .zip files, in any layout:
    python harvest_prefab_ids.py path/to/exports another/export.zip --out prefab_ids_merged.csv

Outputs:
    prefab_ids_merged.csv   Same columns as the export's PrefabIds.csv, loadable in the inspector
    prefab_report.json      Sources, conflicts, and unnamed prefab IDs with clues about what they are
"""

import argparse
import base64
import csv
import io
import json
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

ROOM_TYPE = "rec_room.PersistedRoomData"
DESCRIPTOR_NAME = "descriptor_set.binpb"
GENERIC_COMPONENTS = {
    "id", "transform", "child_views", "encoded_entity_idx", "spawnable_tool_data", "tool_entity_data",
    "creation_object_data", "tagged_tool_data", "tool_cleanup_data", "synced_data",
}


class ExportSource:
    """One export: a folder, or a folder inside a zip, holding a descriptor and room data."""

    def __init__(self, label):
        self.label = label
        self.descriptor_bytes = None
        self.room_files = []  # (name, bytes)
        self.csv_files = []  # (name, text)


def iter_files(path):
    """Yield (group label, file name, loader) for every file in folders and zips."""
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.endswith("/"):
                    continue
                member_path = PurePosixPath(member)
                group = f"{path}!{member_path.parent}"
                yield group, member_path.name, (lambda archive_path=path, name=member: zipfile.ZipFile(archive_path).read(name))
    elif path.is_dir():
        for file_path in path.rglob("*"):
            if file_path.is_file():
                if file_path.suffix.lower() == ".zip":
                    yield from iter_files(file_path)
                else:
                    yield str(file_path.parent), file_path.name, file_path.read_bytes
    elif path.is_file():
        yield str(path.parent), path.name, path.read_bytes


def gather_sources(paths):
    sources = {}
    for path in paths:
        for group, name, load in iter_files(path):
            lower_name = name.lower()
            is_csv = lower_name.startswith("prefabids") and lower_name.endswith(".csv")
            is_descriptor = lower_name == DESCRIPTOR_NAME
            is_room = lower_name.startswith("persisted_room_data") and lower_name.endswith(".binpb")
            if not (is_csv or is_descriptor or is_room):
                continue
            source = sources.setdefault(group, ExportSource(group))
            if is_csv:
                source.csv_files.append((name, load().decode("utf-8-sig", errors="replace")))
            elif is_descriptor:
                source.descriptor_bytes = load()
            else:
                source.room_files.append((name, load()))
    return list(sources.values())


def parse_prefab_csv(text):
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        guid_text = (row.get("Guid") or "").strip().lower()
        type_name = (row.get("Type") or "").strip()
        try:
            guid = uuid.UUID(guid_text)
        except ValueError:
            continue
        base64_text = (row.get("Base64 Guid") or "").strip()
        if base64_text and base64.b64decode(base64_text) != guid.bytes_le:
            print(f"  warning: {guid_text} has a Base64 value that doesn't match its GUID, using the GUID")
        if type_name:
            rows.append((str(guid), type_name))
    return rows


def room_message_class(descriptor_bytes):
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.ParseFromString(descriptor_bytes)
    pool = descriptor_pool.DescriptorPool()
    for file_proto in descriptor_set.file:
        pool.Add(file_proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(ROOM_TYPE))


def iter_messages(message):
    yield message
    for field, value in message.ListFields():
        if field.type != field.TYPE_MESSAGE:
            continue
        for item in ([value] if hasattr(value, "ListFields") else value):
            if hasattr(item, "ListFields"):
                yield from iter_messages(item)


def guid_from_bytes(raw_bytes):
    return str(uuid.UUID(bytes_le=bytes(raw_bytes))) if len(raw_bytes) == 16 else None


def scan_room(room, record_prefab):
    """Record every prefab ID a room object was spawned from, with the components that object carries."""
    for message in iter_messages(room):
        type_name = message.DESCRIPTOR.full_name
        if type_name == "rec_room.PersistenceViewData" and message.HasField("spawnable_tool_data"):
            guid = guid_from_bytes(message.spawnable_tool_data.prefab_id)
            if guid:
                components = [field.name for field, _ in message.ListFields() if field.name not in GENERIC_COMPONENTS]
                record_prefab(guid, components)
        elif type_name == "rec_room.UGCSpawnerData" and message.HasField("tool_type"):
            guid = guid_from_bytes(message.tool_type.value)
            if guid:
                record_prefab(guid, ["(spawned by an object spawner)"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="Export folders, zips, or folders full of them")
    parser.add_argument("--out", default="prefab_ids_merged.csv")
    parser.add_argument("--report", default="prefab_report.json")
    arguments = parser.parse_args()

    sources = gather_sources(arguments.paths)
    if not sources:
        raise SystemExit("No PrefabIds.csv or room data found in those paths.")

    names_by_guid = defaultdict(Counter)
    name_sources = defaultdict(set)
    seen_in_rooms = defaultdict(set)
    use_counts = Counter()
    components_by_guid = defaultdict(Counter)
    source_summaries = []

    for source in sources:
        csv_rows = 0
        for csv_name, text in source.csv_files:
            for guid, type_name in parse_prefab_csv(text):
                names_by_guid[guid][type_name] += 1
                name_sources[guid].add(source.label)
                csv_rows += 1

        room_objects = 0
        if source.room_files and source.descriptor_bytes:
            room_class = room_message_class(source.descriptor_bytes)
            for room_name, room_bytes in source.room_files:
                room = room_class()
                try:
                    room.ParseFromString(room_bytes)
                except Exception as error:
                    print(f"  skipped {source.label}/{room_name}: {error}")
                    continue

                def record_prefab(guid, components, room_label=f"{source.label}/{room_name}"):
                    nonlocal room_objects
                    room_objects += 1
                    use_counts[guid] += 1
                    seen_in_rooms[guid].add(room_label)
                    components_by_guid[guid].update(components)

                scan_room(room, record_prefab)
        elif source.room_files:
            print(f"  {source.label}: room data without {DESCRIPTOR_NAME}, skipping its room scan")

        source_summaries.append({"source": source.label, "csv_rows": csv_rows, "room_objects": room_objects})
        print(f"{source.label}: {csv_rows} named prefabs, {room_objects} room objects")

    merged = {}
    conflicts = {}
    for guid, type_counts in names_by_guid.items():
        type_name, _ = type_counts.most_common(1)[0]
        merged[guid] = type_name
        if len(type_counts) > 1:
            conflicts[guid] = dict(type_counts)

    with open(arguments.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(["Guid", "Base64 Guid", "Type", "Exports"])
        for guid, type_name in sorted(merged.items(), key=lambda item: (item[1], item[0])):
            base64_text = base64.b64encode(uuid.UUID(guid).bytes_le).decode("ascii")
            writer.writerow([guid, base64_text, type_name, len(name_sources[guid])])

    all_seen = set(use_counts)
    unnamed = sorted(all_seen - set(merged), key=lambda guid: -use_counts[guid])
    report = {
        "named_prefabs": len(merged),
        "prefabs_seen_in_rooms": len(all_seen),
        "unnamed_prefabs": len(unnamed),
        "sources": source_summaries,
        "conflicts": conflicts,
        "unnamed": [
            {
                "guid": guid,
                "objects": use_counts[guid],
                "rooms": sorted(seen_in_rooms[guid]),
                "components": dict(components_by_guid[guid].most_common()),
            }
            for guid in unnamed
        ],
    }
    Path(arguments.report).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{len(merged)} named prefabs from {sum(1 for summary in source_summaries if summary['csv_rows'])} exports -> {arguments.out}")
    print(f"{len(all_seen)} prefab IDs used in rooms, {len(unnamed)} of them still unnamed -> {arguments.report}")
    if conflicts:
        print(f"{len(conflicts)} IDs had different names in different exports; the most common name was kept")


if __name__ == "__main__":
    main()
