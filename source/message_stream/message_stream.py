from typing import BinaryIO
from typing import *
from abc import abstractmethod, ABC
import io
import dataclasses
import decimal


__all__ = ['Schema', 'ParseError', 'UnexpectedEof', 'UnknownControlCode', 'default_schema']


class ParseError(Exception):
    pass


class UnexpectedEof(ParseError):
    pass


class UnknownControlCode(ParseError):
    def __init__(self, control_code: int):
        super(f"Unknown control_code {control_code}")


class _SkipType:
    pass


_SKIP = _SkipType()

_STRUCT_DEF_CONTROL_CODE = 0
_BACK_REF_CONTROL_CODE = 1


ENDIAN = 'big'


class EncoderDecoder(Protocol):
    variants: List[Any]

    @abstractmethod
    def select_variant(self, value) -> Tuple[Callable[[Any, "EncoderContext"], None], Any, bool]:
        pass

    @abstractmethod
    def decode(self, variant: Any, source: "DecoderContext") -> Any:
        pass


class SingleVariantEncoder(EncoderDecoder, ABC):
    variants = [None]
    _supports_back_ref = True

    def select_variant(self, value) -> Tuple[Callable[[Any, "EncoderContext"], None], Any, bool]:
        return self._encode, None, self._supports_back_ref

    @abstractmethod
    def _encode(self, value, target: "EncoderContext"):
        pass


class _StructFieldMap(NamedTuple):
    encode_source: str
    decode_target: str
    name: str


class _StructDef(NamedTuple):
    encode_type: Type
    decode_type: Type
    struct_name: str
    fields: Collection[_StructFieldMap]


class Schema:
    _encoders: Dict[Type, Tuple[EncoderDecoder, Dict[Any, int]]]
    _decoders: Dict[int, Tuple[EncoderDecoder, Any]]
    _structures_by_type: Dict[Type, _StructDef]
    _structures_by_name: Dict[str, _StructDef]

    def __init__(self):
        try:
            self._encoders = default_schema._encoders.copy()
            self._decoders = default_schema._decoders.copy()
            self._structures_by_name = default_schema._structures_by_name.copy()
            self._structures_by_type = default_schema._structures_by_type.copy()
        except NameError:
            self._encoders = {}
            self._decoders = {}
            self._structures_by_name = {}
            self._structures_by_type = {}

    def decoder(self, source: BinaryIO) -> Union[Iterable[Any], Iterator[Any]]:
        return DecoderContext(self._decoders.copy(), self._structures_by_name, source)

    def encoder(self, target: BinaryIO) -> Callable[[Any], None]:
        return EncoderContext(self._encoders, self._structures_by_type, target)

    def dump_bytes(self, value: Any) -> bytes:
        buffer = io.BytesIO()
        encode = self.encoder(buffer)
        encode(value)
        return buffer.getvalue()

    def load_bytes(self, buffer: bytes):
        buffer = io.BytesIO(buffer)
        decoder = self.decoder(buffer)
        return next(decoder)

    def add_type(self, object_type: Type, encoder: EncoderDecoder,
                 control_codes: Union[int, Iterable[int], None] = None):
        if control_codes is None:
            try:
                max_control_code = max(self._decoders) + 1
            except ValueError:
                max_control_code = 9
            control_codes = range(max_control_code, max_control_code + len(encoder.variants))
        elif isinstance(control_codes, int):
            control_codes = (control_codes,)
        variant_map = dict(zip(encoder.variants, control_codes))
        if len(variant_map) != len(encoder.variants):
            raise ValueError(f"{str(encoder)} has {len(encoder.variants)} but only {len(variant_map)} "
                             f"control-codes were given")
        if len(set(variant_map.values())) != len(variant_map.values()):
            raise ValueError("Duplicate control_codes were given")
        for control_code in variant_map.values():
            if control_code in self._decoders:
                raise ValueError(f"Control code {control_code} already defined")
        for variant, control_code in variant_map.items():
            self._decoders[control_code] = encoder, variant
        self._encoders[object_type] = encoder, variant_map

    def auto_struct(self, _type_def: Type = ..., name: Union[str, Callable[[Type], str]] = None):
        def wrapper(type_def_2):
            return self.auto_struct(type_def_2, name)

        def eval_name(t):
            if hasattr(name, '__call__'):
                # A naming function has been provided.  It can decline to give a name returning None
                result = name(t)
                if result is not None:
                    return result
            if t is _type_def and isinstance(name, str):
                # A single string name was provided for this structure (only this one, not it's children)
                return name
            elif t in new_structures_by_type:
                # Don't auto-name something we already have a name for
                return new_structures_by_type[t][0]
            else:
                # All other options have failed so auto-name the structure
                return t.__name__

        if _type_def is ...:
            # This function has been called to generate a decorator
            return wrapper

        new_structures_by_name = self._structures_by_name.copy()
        new_structures_by_type = self._structures_by_type.copy()
        for new_type, fields in self._evaluate_struct_schema(_type_def).items():
            new_name = eval_name(new_type)
            if new_type in new_structures_by_type and new_name != new_structures_by_type[new_type][0]:
                del new_structures_by_name[new_structures_by_type[new_type][0]]
            if new_name in new_structures_by_name:
                raise ValueError(f"Duplicate struct name {name} for types {new_type} and "
                                 f"{self._structures_by_name[name][0]}")
            struct_def = _StructDef(new_type, new_type, new_name, tuple(_StructFieldMap(f, f, f) for f in fields))
            new_structures_by_type[new_type] = struct_def
            new_structures_by_name[new_name] = struct_def
        self._structures_by_type = new_structures_by_type
        self._structures_by_name = new_structures_by_name

    def _evaluate_struct_schema(self, structure: Type) -> Dict[Type, List[str]]:
        structure_results: Dict[Type, List[str]] = {}
        to_evaluate = {structure}
        while to_evaluate:
            current_struct = to_evaluate.pop()
            field_names = []
            structure_results[current_struct] = field_names
            if dataclasses.is_dataclass(current_struct):
                for field in dataclasses.fields(current_struct):
                    field_names.append(field.name)
                    if field.type not in self._encoders and field.type not in structure_results:
                        to_evaluate.add(field.type)
            elif issubclass(current_struct, tuple):
                for field in current_struct._fields:
                    field_names.append(field)
                    if hasattr(current_struct, '_field_types'):
                        field_type = current_struct._field_types.get(field)
                        if field_type not in self._encoders and field_type not in structure_results:
                            to_evaluate.add(field_type)
            else:
                raise TypeError(f"Cannot evaluate structure for type {current_struct}, must be dataclass or namedtuple")
        return structure_results


class EncoderContext:
    _encoders: Dict[Type, Tuple[EncoderDecoder, Dict[Any, int]]]
    _structures_in_schema: Dict[Type, _StructDef]
    _target: BinaryIO
    _max_control_code: int
    _write_position: int
    _back_references: Dict

    def __init__(self, encoders: Dict[Type, Tuple[EncoderDecoder, Dict[Any, int]]],
                 structures: Dict[Type, _StructDef], target: BinaryIO):
        self._encoders = encoders.copy()
        self._structures_in_schema = structures.copy()
        self._target = target
        self._max_control_code = max(c for s in self._encoders.values() for c in s[1].values())
        self._back_references = {}

    def write(self, value: bytes):
        self._target.write(value)

    def encode_object(self, value: Any, simple_form: bool = False):
        try:
            encoder, variant_map = self._encoders[type(value)]
        except KeyError as ex:
            if simple_form or type(value) not in self._structures_in_schema:
                raise ValueError(f"Cannot encode unknown type {type(value)}") from ex
            self._declare_structure(type(value))
            encoder, variant_map = self._encoders[type(value)]

        encode_method, variant, allow_backref = encoder.select_variant(value)
        position = self._target.tell()
        if allow_backref and id(value) in self._back_references:
            self._encode_back_reference(value)
        else:
            control_code = variant_map[variant]
            self.encode_variable_int(control_code)
            encode_method(value, self)
            self._back_references[position] = value

    def encode_variable_int(self, val: int):
        if val < 0x80:
            self.write(val.to_bytes(1, ENDIAN))
        elif val < 0x4000:
            self.write((val | 0x8000).to_bytes(2, ENDIAN))
        elif val < 0x20000000:
            self.write((val | 0xC0000000).to_bytes(4, ENDIAN))
        elif val < 0x1000000000000000:
            self.write((val | 0xE000000000000000).to_bytes(8, ENDIAN))
        else:
            raise ValueError(f"Out of range {val}")

    def encode_string(self, value: str):
        v = value.encode("utf-8")
        self.encode_variable_int(len(v))
        self.write(v)

    def _encode_back_reference(self, value):
        offset = self._target.tell() - self._back_references[id(value)]
        self.encode_variable_int(_BACK_REF_CONTROL_CODE)
        self.encode_variable_int(offset)

    def _add_encoder(self, obj_type: Type, encoder: EncoderDecoder):
        variant_map = {}
        for variant in encoder.variants:
            self._max_control_code += 1
            variant_map[variant] = self._max_control_code
        self._encoders[obj_type] = encoder, variant_map

    def _declare_structure(self, struct_type: Type):
        struct_def = self._structures_in_schema[struct_type]
        encoder = StructEncoderDecoder(struct_def)
        self._add_encoder(struct_type, encoder)

        self.encode_variable_int(_STRUCT_DEF_CONTROL_CODE)
        self.encode_string(struct_def.struct_name)
        # The encoder may have several variants.  Make sure we send a control code for each one ...
        self.encode_variable_int(len(self._encoders[struct_type][1]))
        for variant, control_code in self._encoders[struct_type][1].items():
            self.encode_variable_int(control_code)
            self.encode_object(variant, primitive_only=True)
        # Send the fields in the struct.  That way we never send the field names for every object.
        self.encode_variable_int(len(struct_def.fields))
        for field in struct_def.fields:
            self.encode_string(field.name)

    def __call__(self, value: Any):
        self.encode_object(value)


class DecoderContext:
    _decoders:  Dict[int, Tuple[EncoderDecoder, Any]]
    _source: BinaryIO
    _structures_in_schema: Dict[str, _StructDef]
    _back_references = Dict[int, Any]

    def __init__(self, decoders: Dict[int, Tuple[EncoderDecoder, Any]],
                 structures: Dict[str, _StructDef], source: BinaryIO):
        self._decoders = decoders.copy()
        self._structures_in_schema = structures.copy()
        self._source = source
        self._back_references = {}

    def read(self, byte_count: int) -> bytes:
        result = self._source.read(byte_count)
        if len(result) < byte_count:
            raise UnexpectedEof()
        return result

    def decode_object(self, eof_okay=False, type_def_okay=True):
        while True:
            position = self._source.tell()
            try:
                control_code = self.decode_variable_int()
            except UnexpectedEof:
                if eof_okay and self._source.tell() == position:
                    raise StopIteration()
                raise
            if control_code == _STRUCT_DEF_CONTROL_CODE:
                if not type_def_okay:
                    raise ParseError("Attempt to define a new type at invalid location")
                self._declare_structure()
                continue
            elif control_code == _BACK_REF_CONTROL_CODE:
                return self._decode_back_reference(position)
            try:
                decoder, variant = self._decoders[control_code]
            except KeyError:
                raise UnknownControlCode(control_code)
            result = decoder.decode(variant, self)
            self._back_references[position] = result
            return result

    def decode_variable_int(self) -> int:
        first_byte = self.read(1)
        if first_byte[0] & 0x80 == 0:
            return first_byte[0]
        if first_byte[0] & 0xC0 == 0x80:
            return int.from_bytes(first_byte + self.read(1), ENDIAN)
        if first_byte[0] & 0xE0 == 0xC0:
            return int.from_bytes(first_byte + self.read(3), ENDIAN)
        if first_byte[0] & 0xF0 == 0xE0:
            return int.from_bytes(first_byte + self.read(7), ENDIAN)
        else:
            raise ParseError(f"Invalid first byt for variable int {first_byte.hex()}")

    def decode_string(self) -> str:
        length = self.decode_variable_int()
        value = self.read(length)
        return value.decode("utf-8")

    def _decode_back_reference(self, current_position) -> Any:
        try:
            offset = self.decode_variable_int()
            return self._back_references[current_position - offset]
        except KeyError:
            raise ParseError("Invalid back reference")

    def _add_decoder(self, decoder: EncoderDecoder, variants: Collection[Tuple[int, Any]]):
        for control_code, variant in variants:
            self._decoders[control_code] = decoder, variant

    def _declare_structure(self):
        # Decode the message
        struct_name = self.decode_string()
        variants = []
        for _ in range(self.decode_variable_int()):
            variants.append((self.decode_variable_int(), self.decode_object(type_def_okay=False)))
        fields = []
        for _ in range(self.decode_variable_int()):
            fields.append(self.decode_string())

        # Match the given fields to what we have in the schema
        # TODO implement strict mode
        try:
            struct_def = self._structures_in_schema[struct_name]
        except KeyError:
            # We didn't know about this one, let's just decode it to a dict
            struct_def = _StructDef(type(None), dict, struct_name, tuple(_StructFieldMap(f, f, f) for f in fields))
        else:
            expected_fields: Dict[str, _StructFieldMap] = {f.name: f for f in struct_def.fields}
            # Fields
            struct_def = _StructDef(struct_def.encode_type, struct_def.decode_type, struct_def.struct_name, tuple(
                _StructFieldMap(expected_fields[f].encode_source, expected_fields[f].decode_target, f) for f in fields))

        # Allocate a decoder
        decoder = StructEncoderDecoder(struct_def)

        # Register the decoder
        self._add_decoder(decoder, variants)

    def __next__(self):
        return self.decode_object()

    def __iter__(self):
        return self


class SentinelEncoder(SingleVariantEncoder):
    _supports_back_ref = False

    def __init__(self, sentinel_value):
        self._sentinel_value = sentinel_value

    def _encode(self, value, target: EncoderContext):
        pass

    def decode(self, variant: Any, source: "DecoderContext") -> Any:
        return self._sentinel_value


class BoolEncoderDecoder(EncoderDecoder):
    variants = [False, True]

    def select_variant(self, value) -> Tuple[Callable[[Any, "EncoderContext"], None], Any, bool]:
        return self.encode, bool(value), False

    def encode(self, _, target: EncoderContext):
        pass

    def decode(self, variant: bool, source: DecoderContext) -> bool:
        return variant


class IntEncoderDecoder(EncoderDecoder):
    variants = [1, 2, 4, 8, ...]

    def select_variant(self, value: int) -> Tuple[Callable[[Any, EncoderContext], None], Any, bool]:
        length = int((7 + value.bit_length()) / 8)
        if length == 1:
            return self._encode_1_byte, 1, False
        elif length == 2:
            return self._encode_2_byte, 2, False
        elif length <= 4:
            return self._encode_4_byte, 4, False
        elif length <= 8:
            return self._encode_8_byte, 8, False
        else:
            return self._encode_big_byte, ..., True

    @staticmethod
    def _encode_1_byte(value: int, target: EncoderContext):
        target.write(value.to_bytes(1, ENDIAN))

    @staticmethod
    def _encode_2_byte(value: int, target: EncoderContext):
        target.write(value.to_bytes(2, ENDIAN))

    @staticmethod
    def _encode_4_byte(value: int, target: EncoderContext):
        target.write(value.to_bytes(4, ENDIAN))

    @staticmethod
    def _encode_8_byte(value: int, target: EncoderContext):
        target.write(value.to_bytes(8, ENDIAN))

    @staticmethod
    def _encode_big_byte(value: int, target: EncoderContext):
        byte_length = int((7+value.bit_length())/8)
        target.encode_variable_int(byte_length)
        target.write(value.to_bytes(byte_length, ENDIAN))

    def decode(self, variant: Any, source: DecoderContext) -> int:
        if variant is ...:
            variant = source.decode_variable_int()
        value = source.read(variant)
        return int.from_bytes(value, ENDIAN)


class BytesEncoderDecoder(SingleVariantEncoder):

    def _encode(self, value, target: EncoderContext):
        target.encode_variable_int(len(value))
        target.write(value)

    def decode(self, variant: Any, source: DecoderContext) -> Any:
        length = source.decode_variable_int()
        return source.read(length)


class StringEncoderDecoder(EncoderDecoder):
    variants = [1, 0, ...]
    ENCODING = "utf8"

    def select_variant(self, value) -> Tuple[Callable[[Any, "EncoderContext"], None], Any, bool]:
        if len(value) == 0:
            return self._encode_fixed, 0, False
        if len(value) == 1:
            return self._encode_fixed, 1, False
        return self._encode, ..., True

    def _encode_fixed(self, value, target: EncoderContext):
        content = value.encode("utf-8")
        target.write(content)

    def _encode(self, value, target: EncoderContext):
        content = value.encode(self.ENCODING)
        target.encode_variable_int(len(content))
        target.write(content)

    def decode(self, variant: Any, source: DecoderContext) -> Any:
        if variant == 0:
            return ""
        if variant == 1:
            b = source.read(1)
            while b[-1] >= 128 and len(b) < 4:
                b += source.read(1)
            return b.decode(self.ENCODING)
        content_length = source.decode_variable_int()
        content = source.read(content_length)
        return content.decode(self.ENCODING)


class DecimalEncoder(EncoderDecoder):
    variants = [1, -1]
    _ENCODE_MAP = {key: value for value, key in enumerate("0123456789.", 1)}
    _DECODE_MAP = {key: value for key, value in enumerate("0123456789.", 1)}

    def select_variant(self, value: decimal.Decimal) -> Tuple[Callable[[Any, "EncoderContext"], None], Any, bool]:
        if value < 0:
            return self._encode, -1, False
        return self._encode, 1, False

    def _encode(self, value: decimal.Decimal, target: EncoderContext):
        def _encode_iter():
            encoded_value = str(value)
            try:
                iterator = iter(encoded_value)
                while True:
                    i = next(iterator)
                    j = next(iterator)
                    yield self._ENCODE_MAP[i] << 4 | self._ENCODE_MAP[j]
            except StopIteration:
                if len(encoded_value) % 2:
                    yield self._ENCODE_MAP[encoded_value[-1]] << 4

        if value < 0:
            value = 0 - value
        to_write = bytes(_encode_iter())
        target.encode_variable_int(len(to_write))
        target.write(to_write)

    def decode(self, variant: Any, source: "DecoderContext") -> Any:
        def decode_iter():
            byte_val = 0
            try:
                for byte_val in bytes_read:
                    yield self._DECODE_MAP[(byte_val & 0xF0) >> 4]
                    yield self._DECODE_MAP[byte_val & 0x0F]
            except KeyError:
                if byte_val & 0x0F:
                    raise ParseError(f"Unexpected byte value in decimal {hex(byte_val)}")
        length = source.decode_variable_int()
        bytes_read = source.read(length)
        result = decimal.Decimal(''.join(decode_iter()))
        return result * variant


class SequenceElementEncoder(SingleVariantEncoder):
    def __init__(self, sequence_factory):
        self._sequence_factory = sequence_factory

    def _encode(self, value, target: EncoderContext):
        target.encode_variable_int(len(value))
        for item in value:
            target.encode_object(item)

    def decode(self, variant: Any, source: DecoderContext) -> Any:
        item_count = source.decode_variable_int()
        result = self._sequence_factory(source.decode_object() for _ in range(item_count))
        return result


class DictEncoderDecoder(SingleVariantEncoder):
    def __init__(self, dict_factory=dict):
        self._dict_factory = dict_factory

    def _encode(self, value, target: EncoderContext):
        target.encode_variable_int(len(value))
        for a, b in value.items():
            target.encode_object(a)
            target.encode_object(b)

    def decode(self, variant: Any, source: "DecoderContext") -> Any:
        item_count = source.decode_variable_int()
        values = ((source.decode_object(), source.decode_object()) for _ in range(item_count))
        return self._dict_factory(values)


class StructEncoderDecoder(SingleVariantEncoder):

    def __init__(self, struct_def: _StructDef):
        self._struct_def = struct_def

    def _encode(self, value, target: EncoderContext):
        for field in self._struct_def.fields:
            target.encode_object(getattr(value, field.encode_source, _SKIP))

    def decode(self, variant: Any, source: "DecoderContext") -> Any:
        values = {}
        for field in self._struct_def.fields:
            v = source.decode_object()
            if v is not _SKIP and field.decode_target is not _SKIP:
                values[field.decode_target] = v
        return self._struct_def.decode_type(**values)


default_schema = Schema()
default_schema.add_type(type(_SKIP), SentinelEncoder(_SKIP))
default_schema.add_type(type(None), SentinelEncoder(None))
default_schema.add_type(bool, BoolEncoderDecoder())
default_schema.add_type(int, IntEncoderDecoder())
default_schema.add_type(bytes, BytesEncoderDecoder())
default_schema.add_type(str, StringEncoderDecoder())
default_schema.add_type(decimal.Decimal, DecimalEncoder())
default_schema.add_type(tuple, SequenceElementEncoder(tuple))
default_schema.add_type(list, SequenceElementEncoder(list))
default_schema.add_type(set, SequenceElementEncoder(set))
default_schema.add_type(dict, DictEncoderDecoder())


def dump_bytes(value: Any) -> bytes:
    return default_schema.dump_bytes(value)


def load_bytes(buffer: bytes):
    return default_schema.load_bytes(buffer)