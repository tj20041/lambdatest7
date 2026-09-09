import base64
import gzip
import json
import logging
import sys
import zlib
from typing import Any, Dict

logger = logging.getLogger("gzip_payload_handler")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.handlers = [stream_handler]


class PayloadDecompressionError(Exception):
    """Raised when a compressed API Gateway payload cannot be decompressed
    via either zlib or gzip, after being correctly base64-decoded."""


class CompressedPayloadDecoder:

    # Standard magic headers used to sanity-check decoded binary payloads
    # before handing them to a specific decompression codec.
    GZIP_MAGIC = b"\x1f\x8b"
    ZLIB_MAGIC_PREFIXES = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")

    def __init__(self, max_payload_bytes: int = 10 * 1024 * 1024):
        self.max_payload_bytes = max_payload_bytes

    def _to_binary(self, raw_body: str, is_base64_encoded: bool) -> bytes:
        """Convert the raw event body into the actual binary payload bytes.

        API Gateway proxy integrations base64-encode binary bodies and set
        isBase64Encoded=True. That flag MUST be honoured: if it is set, the
        raw_body string is base64 text and must be decoded via
        base64.b64decode() to recover the original compressed bytes. If it
        is not set, the body is already the raw text/bytes and should just
        be utf-8 encoded.
        """
        if is_base64_encoded:
            try:
                return base64.b64decode(raw_body)
            except (base64.binascii.Error, ValueError) as exc:
                raise PayloadDecompressionError(
                    f"Failed to base64-decode payload body: {exc}"
                ) from exc
        return raw_body.encode("utf-8")

    def _decompress_binary(self, binary_payload: bytes) -> bytes:
        """Decompress binary_payload using zlib, falling back to gzip.

        Some API Gateway configurations advertise Content-Encoding: gzip
        while the actual compression algorithm used upstream is raw zlib,
        and vice versa. We peek at the magic header bytes to pick the most
        likely codec first, then fall back to the other codec before
        surfacing a classified error.
        """
        header_preview = binary_payload[:2]
        logger.info(f"Decoded binary payload header bytes: {header_preview!r}")

        looks_like_gzip = binary_payload.startswith(self.GZIP_MAGIC)
        looks_like_zlib = header_preview in self.ZLIB_MAGIC_PREFIXES

        codec_order = []
        if looks_like_gzip:
            codec_order = ["gzip", "zlib"]
        elif looks_like_zlib:
            codec_order = ["zlib", "gzip"]
        else:
            # Unknown header - still attempt both codecs before failing.
            codec_order = ["zlib", "gzip"]

        last_error: Exception = None
        for codec in codec_order:
            try:
                if codec == "zlib":
                    return zlib.decompress(binary_payload)
                return gzip.decompress(binary_payload)
            except (zlib.error, OSError) as exc:
                last_error = exc
                logger.warning(f"Decompression attempt via '{codec}' failed: {exc}")

        raise PayloadDecompressionError(
            "Unable to decompress payload with either zlib or gzip codecs "
            f"(header bytes={header_preview!r}): {last_error}"
        ) from last_error

    def decompress_and_decode(
        self, raw_body: str, is_base64_encoded: bool
    ) -> Dict[str, Any]:
        logger.info(
            f"Decompressing payload (isBase64Encoded={is_base64_encoded}, length={len(raw_body)})"
        )

        # Step 1: Recover the true binary payload. API Gateway base64-encodes
        # binary/compressed bodies, so we must base64-decode BEFORE handing
        # bytes to zlib/gzip - never just utf-8 encode the base64 text itself.
        binary_payload = self._to_binary(raw_body, is_base64_encoded)

        # Step 2: Decompress, trying the codec indicated by the magic header
        # first, with a fallback to the other supported codec.
        try:
            decompressed_stream = self._decompress_binary(binary_payload)
        except PayloadDecompressionError:
            raise
        except zlib.error as exc:
            raise PayloadDecompressionError(
                f"zlib decompression failed: {exc}"
            ) from exc

        if len(decompressed_stream) > self.max_payload_bytes:
            raise ValueError("Decompressed payload exceeds maximum size quota")

        return json.loads(decompressed_stream.decode("utf-8"))


def _build_local_test_event() -> Dict[str, Any]:
    """Builds a simulated API Gateway proxy event for local/manual testing.

    This is ONLY used when the Lambda is invoked without a real event body
    (e.g. running `python test7.py` locally), so that production traffic
    always flows through the real `event` argument instead of test data.
    """
    payload_data = json.dumps(
        {"transaction_id": "TXN_774921", "batch_items": [101, 102, 103]}
    )
    compressed_binary = zlib.compress(payload_data.encode("utf-8"))
    b64_encoded_body = base64.b64encode(compressed_binary).decode("ascii")

    return {
        "headers": {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        "isBase64Encoded": True,
        "body": b64_encoded_body,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Received compressed request payload...")

    # Always prefer the real event supplied by API Gateway/Lambda. Only fall
    # back to a simulated event when no body is present at all (local/manual
    # invocation), so production traffic never bypasses real event data.
    if isinstance(event, dict) and event.get("body") is not None:
        source_event = event
    else:
        logger.warning(
            "No 'body' found on incoming event; falling back to simulated "
            "local test payload. This path should never be hit in production."
        )
        source_event = _build_local_test_event()

    decoder = CompressedPayloadDecoder()

    try:
        parsed_json = decoder.decompress_and_decode(
            raw_body=source_event["body"],
            is_base64_encoded=source_event.get("isBase64Encoded", False),
        )
    except PayloadDecompressionError as exc:
        logger.error(f"PAYLOAD_DECOMPRESSION_ERROR: {exc}")
        return {
            "statusCode": 400,
            "error_code": "PAYLOAD_DECOMPRESSION_ERROR",
            "message": str(exc),
        }
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error(f"PAYLOAD_VALIDATION_ERROR: {exc}")
        return {
            "statusCode": 400,
            "error_code": "PAYLOAD_VALIDATION_ERROR",
            "message": str(exc),
        }

    logger.info(f"Successfully unpacked payload: {parsed_json}")
    return {"statusCode": 200, "data": parsed_json}


if __name__ == "__main__":
    lambda_handler({}, None)
