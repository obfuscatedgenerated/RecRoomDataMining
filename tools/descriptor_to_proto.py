"""
Turn a protobuf descriptor set (such as Rec Room's descriptor_set.binpb) back into readable .proto files.

Usage:
    python descriptor_to_proto.py descriptor_set.binpb proto
"""

import sys
from pathlib import Path

from google.protobuf import descriptor_pb2

FieldProto = descriptor_pb2.FieldDescriptorProto

SCALAR_TYPE_NAMES = {
    FieldProto.TYPE_DOUBLE: "double",
    FieldProto.TYPE_FLOAT: "float",
    FieldProto.TYPE_INT64: "int64",
    FieldProto.TYPE_UINT64: "uint64",
    FieldProto.TYPE_INT32: "int32",
    FieldProto.TYPE_FIXED64: "fixed64",
    FieldProto.TYPE_FIXED32: "fixed32",
    FieldProto.TYPE_BOOL: "bool",
    FieldProto.TYPE_STRING: "string",
    FieldProto.TYPE_BYTES: "bytes",
    FieldProto.TYPE_UINT32: "uint32",
    FieldProto.TYPE_SFIXED32: "sfixed32",
    FieldProto.TYPE_SFIXED64: "sfixed64",
    FieldProto.TYPE_SINT32: "sint32",
    FieldProto.TYPE_SINT64: "sint64",
}


def field_type_name(field):
    if field.type in SCALAR_TYPE_NAMES:
        return SCALAR_TYPE_NAMES[field.type]
    return field.type_name  # Fully qualified with a leading dot, which is valid proto syntax.


def field_label(field, syntax):
    if field.label == FieldProto.LABEL_REPEATED:
        return "repeated "
    if syntax == "proto3":
        return "optional " if field.proto3_optional else ""
    if field.label == FieldProto.LABEL_REQUIRED:
        return "required "
    return "optional "


def render_enum(enum, indent):
    pad = "  " * indent
    lines = [f"{pad}enum {enum.name} {{"]
    if enum.options.allow_alias:
        lines.append(f"{pad}  option allow_alias = true;")
    for value in enum.value:
        lines.append(f"{pad}  {value.name} = {value.number};")
    lines.append(f"{pad}}}")
    return lines


def render_message(message, syntax, indent):
    pad = "  " * indent
    lines = [f"{pad}message {message.name} {{"]

    map_entries = {}
    for nested in message.nested_type:
        if nested.options.map_entry:
            map_entries[nested.name] = nested
        else:
            lines.extend(render_message(nested, syntax, indent + 1))

    for enum in message.enum_type:
        lines.extend(render_enum(enum, indent + 1))

    def render_field(field, field_indent, inside_oneof):
        field_pad = "  " * field_indent
        entry_name = field.type_name.rsplit(".", 1)[-1]
        if field.label == FieldProto.LABEL_REPEATED and entry_name in map_entries:
            entry = map_entries[entry_name]
            key_type = field_type_name(entry.field[0])
            value_type = field_type_name(entry.field[1])
            return f"{field_pad}map<{key_type}, {value_type}> {field.name} = {field.number};"
        label = "" if inside_oneof else field_label(field, syntax)
        return f"{field_pad}{label}{field_type_name(field)} {field.name} = {field.number};"

    real_oneof_fields = {}
    for field in message.field:
        is_real_oneof = field.HasField("oneof_index") and not field.proto3_optional
        if is_real_oneof:
            real_oneof_fields.setdefault(field.oneof_index, []).append(field)
        else:
            lines.append(render_field(field, indent + 1, inside_oneof=False))

    for oneof_index, oneof_fields in real_oneof_fields.items():
        oneof_name = message.oneof_decl[oneof_index].name
        lines.append(f"{pad}  oneof {oneof_name} {{")
        for field in oneof_fields:
            lines.append(render_field(field, indent + 2, inside_oneof=True))
        lines.append(f"{pad}  }}")

    for reserved in message.reserved_range:
        end = reserved.end - 1
        lines.append(f"{pad}  reserved {reserved.start}{f' to {end}' if end != reserved.start else ''};")

    lines.append(f"{pad}}}")
    return lines


def render_service(service):
    lines = [f"service {service.name} {{"]
    for method in service.method:
        input_stream = "stream " if method.client_streaming else ""
        output_stream = "stream " if method.server_streaming else ""
        lines.append(
            f"  rpc {method.name}({input_stream}{method.input_type}) "
            f"returns ({output_stream}{method.output_type});"
        )
    lines.append("}")
    return lines


def render_file(file_proto):
    syntax = file_proto.syntax or "proto2"
    lines = [f'syntax = "{syntax}";', ""]
    if file_proto.package:
        lines += [f"package {file_proto.package};", ""]
    for dependency in file_proto.dependency:
        lines.append(f'import "{dependency}";')
    if file_proto.dependency:
        lines.append("")
    if file_proto.options.HasField("csharp_namespace"):
        lines += [f'option csharp_namespace = "{file_proto.options.csharp_namespace}";', ""]
    for enum in file_proto.enum_type:
        lines += render_enum(enum, 0) + [""]
    for message in file_proto.message_type:
        lines += render_message(message, syntax, 0) + [""]
    for service in file_proto.service:
        lines += render_service(service) + [""]
    return "\n".join(lines)


def main():
    descriptor_path = Path(sys.argv[1] if len(sys.argv) > 1 else "descriptor_set.binpb")
    output_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "proto")

    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.ParseFromString(descriptor_path.read_bytes())

    written = 0
    for file_proto in descriptor_set.file:
        if file_proto.name.startswith("google/protobuf/") or file_proto.name.startswith("include/google/protobuf/"):
            continue
        output_path = output_dir / file_proto.name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_file(file_proto), encoding="utf-8")
        written += 1
    print(f"wrote {written} .proto files to {output_dir}")


if __name__ == "__main__":
    main()
