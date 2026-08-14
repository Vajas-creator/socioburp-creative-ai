"""
Embeds the IPTC "Digital Source Type" property into every AI-generated
image before it's posted, per Meta's enforced 2026 AI-content-labeling
requirement -- see the Aug 2026 consolidated fix list, Priority 7.
Skipping this risks Meta's systems mislabeling (or account-level
strikes against) the client's Instagram.

Hand-builds the XMP packet directly and injects it into the PNG's iTXt
chunk (keyword "XML:com.adobe.xmp", the standard location XMP-aware
tools read) using only the stdlib (struct/zlib) -- deliberately NOT
using a native-dependency library like pyexiv2, given Render's free tier
has no shell to debug a failed native build if the wheel doesn't install
cleanly. piexif was considered and rejected: it only writes EXIF, and
this property is an XMP/IPTC field, not EXIF.

IMPORTANT — sourcing caveat: the exact tag/value pairing here (IPTC
Digital Source Type, XMP field Iptc4xmpExt:DigitalSourceType, value URI
http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia)
is sourced from IPTC.org's own published guidance plus secondary 2026
reporting on Meta's implementation, NOT fetched directly from Meta's own
Business Help Center -- facebook.com is blocked by this deployment
sandbox's network egress policy, so it could not be verified firsthand.
Cross-check this against Meta's own current documentation before
treating it as guaranteed-correct; update DIGITAL_SOURCE_TYPE_URI here
if Meta's actual requirement differs.

Only apply this to images Sakshi actually generated/edited with AI --
NEVER to a client's own uploaded photo delivered as-is (see
image_intent.py's _use_as_is()), which would be a false label in the
other direction.
"""
import logging
import struct
import zlib

logger = logging.getLogger("socioburp.engine.ai_metadata")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Sourcing caveat above applies to this URI specifically.
DIGITAL_SOURCE_TYPE_URI = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"

# The xpacket begin/end wrapper is the standard XMP packet envelope --
# "begin" conventionally carries a literal BOM character (built via
# chr(0xFEFF) below, deliberately NOT pasted as a literal invisible
# character into this source file, where it would be easy to lose or
# corrupt silently) so XMP-unaware byte scanners can still detect the
# packet's text encoding.
_BOM = chr(0xFEFF)
XMP_TEMPLATE = (
    '<?xpacket begin="' + _BOM + '" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""\n'
    '    xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">\n'
    "   <Iptc4xmpExt:DigitalSourceType>{uri}</Iptc4xmpExt:DigitalSourceType>\n"
    "  </rdf:Description>\n"
    " </rdf:RDF>\n"
    "</x:xmpmeta>\n"
    '<?xpacket end="w"?>'
)


def _build_itxt_chunk(keyword: str, text: str) -> bytes:
    """One PNG iTXt chunk: length + type + data + CRC, per the PNG spec."""
    data = (
        keyword.encode("latin-1") + b"\x00"
        + b"\x00"  # compression flag: 0 = uncompressed
        + b"\x00"  # compression method: 0 (only value defined)
        + b"\x00"  # language tag (empty) + its null terminator
        + b"\x00"  # translated keyword (empty) + its null terminator
        + text.encode("utf-8")
    )
    chunk_type = b"iTXt"
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return length + chunk_type + data + crc


def embed_ai_source_metadata(png_bytes: bytes) -> bytes:
    """
    Returns new PNG bytes with an IPTC Digital Source Type XMP chunk
    inserted right after IHDR (the conventional placement for metadata
    chunks). Best-effort: returns the original bytes unmodified if the
    input doesn't parse as a well-formed PNG -- every caller already
    generates real PNGs (see app/engine/image_gen.py), so this should
    never actually trigger in production, but delivering an unlabeled
    image beats blocking delivery entirely over a metadata failure.
    """
    if png_bytes[:8] != PNG_SIGNATURE:
        logger.warning("embed_ai_source_metadata: input doesn't look like a PNG, returning unmodified")
        return png_bytes

    pos = 8
    ihdr_end = None
    while pos + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[pos:pos + 4])[0]
        chunk_type = png_bytes[pos + 4:pos + 8]
        chunk_total_len = 8 + length + 4  # length + type + data + CRC
        if chunk_type == b"IHDR":
            ihdr_end = pos + chunk_total_len
            break
        pos += chunk_total_len

    if ihdr_end is None or ihdr_end > len(png_bytes):
        logger.warning("embed_ai_source_metadata: no valid IHDR chunk found, returning unmodified")
        return png_bytes

    itxt_chunk = _build_itxt_chunk("XML:com.adobe.xmp", XMP_TEMPLATE.format(uri=DIGITAL_SOURCE_TYPE_URI))
    return png_bytes[:ihdr_end] + itxt_chunk + png_bytes[ihdr_end:]
