"""ISO base media file format reader for HEIF/HEIC images and MOV/MP4/3GP videos.

Only box headers and the few payloads we need are read (meta, moov and the Exif item).
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from framesift.engine.metadata.base import (
    FileReader,
    MediaInfo,
    clean_text,
    iso_local_from_offset_string,
)
from framesift.engine.metadata.exif import parse_exif
from framesift.engine.metadata.jpeg import apply_exif

HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"hevm",
    b"hevs",
    b"mif1",
    b"msf1",
    b"avif",
    b"avis",
}
CONTAINER_BOXES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"udta",
    b"iprp",
    b"ipco",
    b"dinf",
    b"edts",
}
MAC_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
CODEC_NAMES = {
    b"hvc1": "hevc",
    b"hev1": "hevc",
    b"avc1": "h264",
    b"avc3": "h264",
    b"mp4v": "mpeg4",
    b"av01": "av1",
    b"vp09": "vp9",
    b"jpeg": "mjpeg",
    b"mjpa": "mjpeg",
    b"apch": "prores",
    b"apcn": "prores",
    b"apcs": "prores",
    b"apco": "prores",
    b"ap4h": "prores",
    b"s263": "h263",
    b"dvhe": "hevc",
    b"dvh1": "hevc",
}

Read = Callable[[int, int], bytes]


@dataclass(frozen=True)
class Box:
    type: bytes
    start: int
    payload: int
    end: int


def iter_boxes(read: Read, start: int, end: int) -> Iterator[Box]:
    pos = start
    while pos + 8 <= end:
        hdr = read(pos, 16)
        if len(hdr) < 8:
            return
        size = struct.unpack(">I", hdr[:4])[0]
        btype = hdr[4:8]
        hdr_len = 8
        if size == 1:
            if len(hdr) < 16:
                return
            size = struct.unpack(">Q", hdr[8:16])[0]
            hdr_len = 16
        elif size == 0:
            size = end - pos
        if btype == b"uuid":
            hdr_len += 16
        if size < hdr_len:
            return
        yield Box(btype, pos, pos + hdr_len, min(pos + size, end))
        pos += size


def children(read: Read, box: Box, skip: int = 0) -> list[Box]:
    return list(iter_boxes(read, box.payload + skip, box.end))


def _fullbox(read: Read, box: Box) -> tuple[int, int, int]:
    data = read(box.payload, 4)
    if len(data) < 4:
        return 0, 0, box.payload + 4
    return data[0], int.from_bytes(data[1:4], "big"), box.payload + 4


# --------------------------------------------------------------------------- HEIF


@dataclass
class HeifItem:
    item_id: int
    item_type: bytes = b""
    extents: list[tuple[int, int]] = field(default_factory=list)  # (absolute offset, length)
    construction: int = 0
    properties: list[int] = field(default_factory=list)


@dataclass
class HeifMeta:
    brand: bytes = b""
    primary: int | None = None
    items: dict[int, HeifItem] = field(default_factory=dict)
    props: list[tuple[bytes, bytes]] = field(default_factory=list)  # (type, payload) 1-based
    refs: list[tuple[bytes, int, list[int]]] = field(default_factory=list)  # (type, from, to[])
    idat: tuple[int, int] | None = None

    def item_props(self, item_id: int) -> list[tuple[bytes, bytes]]:
        item = self.items.get(item_id)
        if not item:
            return []
        return [self.props[i - 1] for i in item.properties if 0 < i <= len(self.props)]

    def find_prop(self, item_id: int, ptype: bytes) -> bytes | None:
        for t, payload in self.item_props(item_id):
            if t == ptype:
                return payload
        return None

    def items_of_type(self, item_type: bytes) -> list[HeifItem]:
        return [i for i in self.items.values() if i.item_type == item_type]

    def item_data(self, read: Read, item_id: int, limit: int = 16 * 1024 * 1024) -> bytes:
        item = self.items.get(item_id)
        if not item:
            return b""
        out = bytearray()
        for off, length in item.extents:
            if item.construction == 1:
                if not self.idat:
                    return b""
                off = self.idat[0] + off
            if len(out) + length > limit:
                break
            out += read(off, length)
        return bytes(out)


def parse_heif_meta(read: Read, size: int) -> HeifMeta:
    meta = HeifMeta()
    for box in iter_boxes(read, 0, size):
        if box.type == b"ftyp":
            meta.brand = read(box.payload, 4)
        elif box.type == b"meta":
            _parse_meta_box(read, box, meta)
            break
        elif box.type == b"mdat":
            continue
    return meta


def _parse_meta_box(read: Read, box: Box, meta: HeifMeta) -> None:
    _version, _flags, start = _fullbox(read, box)
    for child in iter_boxes(read, start, box.end):
        t = child.type
        if t == b"pitm":
            v, _, p = _fullbox(read, child)
            meta.primary = _u(read(p, 4 if v else 2))
        elif t == b"iinf":
            v, _, p = _fullbox(read, child)
            count_len = 2 if v == 0 else 4
            for infe in iter_boxes(read, p + count_len, child.end):
                if infe.type != b"infe":
                    continue
                iv, _, ip = _fullbox(read, infe)
                data = read(ip, 12)
                if iv >= 2 and len(data) >= 8:
                    if iv == 2:
                        item_id = _u(data[:2])
                        item_type = data[4:8]
                    else:
                        item_id = _u(data[:4])
                        item_type = data[6:10]
                    meta.items.setdefault(item_id, HeifItem(item_id)).item_type = item_type
        elif t == b"iloc":
            _parse_iloc(read, child, meta)
        elif t == b"iref":
            v, _, p = _fullbox(read, child)
            for ref in iter_boxes(read, p, child.end):
                idlen = 4 if v else 2
                data = read(ref.payload, ref.end - ref.payload)
                if len(data) < idlen + 2:
                    continue
                from_id = _u(data[:idlen])
                count = _u(data[idlen : idlen + 2])
                tos = [
                    _u(data[idlen + 2 + i * idlen : idlen + 2 + (i + 1) * idlen])
                    for i in range(count)
                ]
                meta.refs.append((ref.type, from_id, [x for x in tos if x]))
        elif t == b"iprp":
            for sub in iter_boxes(read, child.payload, child.end):
                if sub.type == b"ipco":
                    for prop in iter_boxes(read, sub.payload, sub.end):
                        meta.props.append(
                            (prop.type, read(prop.payload, min(prop.end - prop.payload, 4096)))
                        )
                elif sub.type == b"ipma":
                    _parse_ipma(read, sub, meta)
        elif t == b"idat":
            meta.idat = (child.payload, child.end - child.payload)


def _parse_iloc(read: Read, box: Box, meta: HeifMeta) -> None:
    v, _, p = _fullbox(read, box)
    data = read(p, box.end - p)
    if len(data) < 4:
        return
    offset_size, length_size = data[0] >> 4, data[0] & 0xF
    base_offset_size = data[1] >> 4
    index_size = data[1] & 0xF if v in (1, 2) else 0
    pos = 2
    if v < 2:
        count = _u(data[pos : pos + 2])
        pos += 2
    else:
        count = _u(data[pos : pos + 4])
        pos += 4
    for _ in range(count):
        if v < 2:
            item_id = _u(data[pos : pos + 2])
            pos += 2
        else:
            item_id = _u(data[pos : pos + 4])
            pos += 4
        construction = 0
        if v in (1, 2):
            construction = _u(data[pos : pos + 2]) & 0xF
            pos += 2
        pos += 2  # data_reference_index
        base = _u(data[pos : pos + base_offset_size]) if base_offset_size else 0
        pos += base_offset_size
        extent_count = _u(data[pos : pos + 2])
        pos += 2
        item = meta.items.setdefault(item_id, HeifItem(item_id))
        item.construction = construction
        for _e in range(extent_count):
            if index_size:
                pos += index_size
            off = _u(data[pos : pos + offset_size]) if offset_size else 0
            pos += offset_size
            length = _u(data[pos : pos + length_size]) if length_size else 0
            pos += length_size
            item.extents.append((base + off, length))
        if pos > len(data):
            break


def _parse_ipma(read: Read, box: Box, meta: HeifMeta) -> None:
    v, flags, p = _fullbox(read, box)
    data = read(p, box.end - p)
    if len(data) < 4:
        return
    count = _u(data[:4])
    pos = 4
    for _ in range(count):
        idlen = 4 if v else 2
        item_id = _u(data[pos : pos + idlen])
        pos += idlen
        if pos >= len(data):
            break
        n = data[pos]
        pos += 1
        props = []
        for _a in range(n):
            if flags & 1:
                val = _u(data[pos : pos + 2]) & 0x7FFF
                pos += 2
            else:
                val = data[pos] & 0x7F if pos < len(data) else 0
                pos += 1
            props.append(val)
        meta.items.setdefault(item_id, HeifItem(item_id)).properties = props


def _u(data: bytes) -> int:
    return int.from_bytes(data, "big") if data else 0


def read_heif(reader: FileReader) -> MediaInfo:
    info = MediaInfo(format="heic", extractor="isobmff", camera_known=True)
    read = reader.read_at
    reader.head()
    meta = parse_heif_meta(read, reader.size)
    if meta.brand in (b"avif", b"avis"):
        info.format = "avif"
    primary = meta.primary
    if primary is None or primary not in meta.items:
        info.structure_ok = False
        return info
    item = meta.items[primary]
    info.structure_ok = bool(item.extents) or item.item_type in (b"grid", b"iden", b"iovl")
    ispe = meta.find_prop(primary, b"ispe")
    if ispe and len(ispe) >= 12:
        info.width, info.height = struct.unpack(">II", ispe[4:12])
    irot = meta.find_prop(primary, b"irot")
    if irot:
        info.orientation = {0: 1, 1: 8, 2: 3, 3: 6}[irot[0] & 3]
    if meta.find_prop(primary, b"hvcC") is not None:
        info.codec = "hevc"
    elif meta.find_prop(primary, b"av1C") is not None:
        info.codec = "av1"
    elif item.item_type == b"grid":
        tiles = [t for (rt, f, tos) in meta.refs if rt == b"dimg" and f == primary for t in tos]
        if tiles and meta.find_prop(tiles[0], b"hvcC") is not None:
            info.codec = "hevc"
        elif tiles and meta.find_prop(tiles[0], b"av1C") is not None:
            info.codec = "av1"
    for exif_item in meta.items_of_type(b"Exif"):
        blob = meta.item_data(read, exif_item.item_id, limit=4 * 1024 * 1024)
        if len(blob) < 8:
            continue
        hdr_off = struct.unpack(">I", blob[:4])[0]
        tiff = blob[4 + hdr_off :] if 4 + hdr_off < len(blob) else blob[4:]
        try:
            exif = parse_exif(tiff)
        except (ValueError, struct.error):
            continue
        keep_orientation = info.orientation
        apply_exif(info, exif)
        if keep_orientation:
            info.orientation = keep_orientation
        break
    thumbs = [f for (rt, f, tos) in meta.refs if rt == b"thmb" and primary in tos]
    for tid in thumbs:
        titem = meta.items.get(tid)
        if titem and titem.extents:
            info.has_preview = True
            info.preview_kind = "heif_thumb"
            info.preview_offset, info.preview_length = titem.extents[0]
            break
    return info


# --------------------------------------------------------------------------- MOV / MP4


@dataclass
class TrackInfo:
    handler: bytes = b""
    codec: bytes = b""
    width: int = 0
    height: int = 0
    timescale: int = 0
    duration: int = 0
    samples: int = 0
    rotation: int = 0


def read_mp4(reader: FileReader) -> MediaInfo:
    info = MediaInfo(format="mp4", extractor="isobmff", camera_known=True)
    read = reader.read_at
    reader.head()
    moov: Box | None = None
    brand = b""
    for box in iter_boxes(read, 0, reader.size):
        if box.type == b"ftyp":
            brand = read(box.payload, 4)
        elif box.type == b"moov":
            moov = box
            break
    if brand == b"qt  ":
        info.format = "mov"
    elif brand in (b"3gp4", b"3gp5", b"3gp6", b"3ge6", b"3gg6"):
        info.format = "3gp"
    if moov is None:
        info.structure_ok = False
        return info
    timescale = duration = 0
    creation = 0
    tracks: list[TrackInfo] = []
    keys: dict[str, str] = {}
    udta_text: dict[str, str] = {}
    for child in iter_boxes(read, moov.payload, moov.end):
        if child.type == b"mvhd":
            v, _, p = _fullbox(read, child)
            data = read(p, 32)
            if v == 1 and len(data) >= 28:
                creation = _u(data[:8])
                timescale = _u(data[16:20])
                duration = _u(data[20:28])
            elif len(data) >= 16:
                creation = _u(data[:4])
                timescale = _u(data[8:12])
                duration = _u(data[12:16])
        elif child.type == b"trak":
            tracks.append(_parse_trak(read, child))
        elif child.type == b"meta":
            keys.update(_parse_meta_keys(read, child))
        elif child.type == b"udta":
            for sub in iter_boxes(read, child.payload, child.end):
                if sub.type == b"meta":
                    keys.update(_parse_meta_keys(read, sub))
                elif sub.type[:1] == b"\xa9":
                    udta_text[sub.type[1:].decode("latin-1")] = _udta_text(
                        read(sub.payload, min(sub.end - sub.payload, 512))
                    )
    info.structure_ok = timescale > 0
    if timescale and duration:
        info.duration_ms = int(duration * 1000 / timescale)
        if info.duration_ms:
            info.bitrate = int(reader.size * 8 * 1000 / info.duration_ms)
    video = next((t for t in tracks if t.handler == b"vide"), None)
    if video:
        info.codec = CODEC_NAMES.get(
            video.codec, video.codec.decode("latin-1", "replace").strip() or None
        )
        if video.width and video.height:
            info.width, info.height = video.width, video.height
        if video.rotation:
            info.orientation = {90: 6, 180: 3, 270: 8}.get(video.rotation)
        if video.samples and video.timescale and video.duration:
            info.fps = round(video.samples * video.timescale / video.duration, 3)
    info.make = keys.get("com.apple.quicktime.make") or udta_text.get("mak")
    info.model = keys.get("com.apple.quicktime.model") or udta_text.get("mod")
    info.software = keys.get("com.apple.quicktime.software") or udta_text.get("swr")
    if "com.android.version" in keys and not info.software:
        info.software = "Android " + keys["com.android.version"]
    info.content_id = keys.get("com.apple.quicktime.content.identifier")
    cdate = keys.get("com.apple.quicktime.creationdate") or udta_text.get("day")
    iso = iso_local_from_offset_string(cdate) if cdate else None
    if iso:
        info.date_taken = iso
        info.date_source = "quicktime"
    elif creation > 0:
        try:
            dt = datetime.fromtimestamp(creation - 2082844800, tz=UTC)
            info.date_taken = dt.strftime("%Y-%m-%dT%H:%M:%S")
            info.date_source = "quicktime_utc"
        except (OverflowError, OSError, ValueError):
            pass
    apple_keys = {k: v for k, v in keys.items() if k.startswith("com.apple.")}
    if apple_keys:
        info.extra["quicktime_keys"] = apple_keys
    if "com.android.version" in keys:
        info.extra["android"] = True
    return info


def _parse_trak(read: Read, trak: Box) -> TrackInfo:
    t = TrackInfo()
    for child in iter_boxes(read, trak.payload, trak.end):
        if child.type == b"tkhd":
            v, _, p = _fullbox(read, child)
            data = read(p, 100)
            base = 32 if v == 1 else 20
            if len(data) >= base + 60:
                matrix = struct.unpack(">9i", data[base + 16 : base + 52])
                a, b, c, d = matrix[0], matrix[1], matrix[3], matrix[4]
                if a == 0 and b == 65536 and c == -65536 and d == 0:
                    t.rotation = 90
                elif a == -65536 and d == -65536:
                    t.rotation = 180
                elif a == 0 and b == -65536 and c == 65536 and d == 0:
                    t.rotation = 270
        elif child.type == b"mdia":
            for sub in iter_boxes(read, child.payload, child.end):
                if sub.type == b"mdhd":
                    v, _, p = _fullbox(read, sub)
                    data = read(p, 32)
                    if v == 1 and len(data) >= 28:
                        t.timescale, t.duration = _u(data[16:20]), _u(data[20:28])
                    elif len(data) >= 16:
                        t.timescale, t.duration = _u(data[8:12]), _u(data[12:16])
                elif sub.type == b"hdlr":
                    data = read(sub.payload, 12)
                    t.handler = data[8:12]
                elif sub.type == b"minf":
                    _parse_minf(read, sub, t)
    return t


def _parse_minf(read: Read, minf: Box, t: TrackInfo) -> None:
    for sub in iter_boxes(read, minf.payload, minf.end):
        if sub.type != b"stbl":
            continue
        for s in iter_boxes(read, sub.payload, sub.end):
            if s.type == b"stsd":
                _, _, p = _fullbox(read, s)
                data = read(p, 4 + 16 + 78)
                if len(data) >= 12:
                    t.codec = data[8:12]
                if t.handler == b"vide" and len(data) >= 4 + 8 + 28:
                    entry = data[4:]
                    t.width = _u(entry[32:34])
                    t.height = _u(entry[34:36])
            elif s.type == b"stts":
                _, _, p = _fullbox(read, s)
                data = read(p, 4)
                count = _u(data)
                if 0 < count <= 200000:
                    table = read(p + 4, count * 8)
                    total = 0
                    for i in range(0, min(len(table), count * 8), 8):
                        total += _u(table[i : i + 4])
                    t.samples = total


def _parse_meta_keys(read: Read, meta: Box) -> dict[str, str]:
    """QuickTime 'mdta' keys/ilst pairs. Handles both QuickTime (no version) and ISO (FullBox) metas."""
    probe = read(meta.payload, 12)
    start = meta.payload
    if probe[4:8] != b"hdlr":
        start += 4
    names: list[str] = []
    values: dict[int, str] = {}
    for child in iter_boxes(read, start, meta.end):
        if child.type == b"keys":
            _, _, p = _fullbox(read, child)
            data = read(p, min(child.end - p, 64 * 1024))
            count = _u(data[:4])
            pos = 4
            for _ in range(count):
                if pos + 8 > len(data):
                    break
                size = _u(data[pos : pos + 4])
                if size < 8:
                    break
                names.append(data[pos + 8 : pos + size].decode("utf-8", "replace"))
                pos += size
        elif child.type == b"ilst":
            for entry in iter_boxes(read, child.payload, child.end):
                index = _u(entry.type)
                for dbox in iter_boxes(read, entry.payload, entry.end):
                    if dbox.type == b"data":
                        data = read(dbox.payload, min(dbox.end - dbox.payload, 4096))
                        if len(data) >= 8:
                            dtype = _u(data[:4])
                            payload = data[8:]
                            if dtype in (1, 2):
                                values[index] = clean_text(payload, 500) or ""
                            elif dtype in (21, 22, 23) and payload:
                                values[index] = str(
                                    int.from_bytes(payload, "big", signed=dtype == 21)
                                )
                            elif (
                                dtype == 0
                                and index
                                and index <= len(names)
                                and names[index - 1].startswith("com.android")
                            ):
                                values[index] = clean_text(payload, 500) or ""
    out: dict[str, str] = {}
    for idx, val in values.items():
        if 1 <= idx <= len(names):
            out[names[idx - 1]] = val
    return out


def _udta_text(data: bytes) -> str:
    if len(data) < 4:
        return ""
    size = _u(data[:2])
    return clean_text(data[4 : 4 + size], 200) or ""
