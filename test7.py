import base64
import binascii
import gzip
import json
import logging
import sys
import zlib
from typing import Any, Dict, Optional

logger = logging.getLogger("gzip_payload_handler")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.handlers = [stream_handler]


class PayloadDecompressionError(Exception):
    """Raised when a compressed API Gateway payload cannot be decoded/
    decompressed via base64 + zlib/gzip, or fails downstream JSON parsing."""


class CompressedPayloadDecoder:

    # Standard magic headers used to sanity-check decoded binary payloads
    # before handing them to a specific decompression codec.
    GZIP_MAGIC = b"\x1f\x8b"
    ZLIB_MAGIC_PREFIXES = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")

    def __init__(self, max_payload_bytes: int = 10 * 1024 * 1024):
        self.max_payload_bytes = max_payload_bytes

    def _to_binary(self, raw_body: str, is_base64_encoded: bool) -> bytes:
        """Recover the true binary payload from the incoming request body.

        API Gateway (and our own simulated event) delivers compressed
        payloads as base64-encoded ASCII text when isBase64Encoded=True.
        Previously this flag was accepted but never used, so zlib was
        fed raw base64 characters instead of real compressed bytes,
        causing a deterministic 'incorrect header check' failure.
        """
        if is_base64_encoded:
            try:
                return base64.b64decode(raw_body)
            except (binascii.Error, ValueError) as exc:
                raise PayloadDecompressionError(
                    "Failed to base64-decode incoming payload"
                ) from exc

        # Legacy/non-base64 clients may send the body as a plain string
        # (already UTF-8 text, not compressed binary).
        return raw_body.encode("utf-8")

    def _decompress(self, binary_data: bytes) -> bytes:
        """Decompress binary_data using zlib or gzip, inspecting the byte
        header to choose the correct codec first and falling back to the
        other codec if the first attempt fails."""
        looks_like_gzip = binary_data[:2] == self.GZIP_MAGIC
        looks_like_zlib = binary_data[:2] in self.ZLIB_MAGIC_PREFIXES

        codecs_to_try = []
        if looks_like_gzip:
            codecs_to_try = ["gzip", "zlib"]
        elif looks_like_zlib:
            codecs_to_try = ["zlib", "gzip"]
        else:
            # Unknown header — try both, zlib first as the more common case.
            codecs_to_try = ["zlib", "gzip"]

        last_error: Optional[Exception] = None
        for codec in codecs_to_try:
            try:
                if codec == "zlib":
                    return zlib.decompress(binary_data)
                return gzip.decompress(binary_data)
            except (zlib.error, OSError) as exc:
                last_error = exc
                continue

        raise PayloadDecompressionError(
            "Unable to decompress payload using zlib or gzip"
        ) from last_error

    def decompress_and_decode(
        self, raw_body: str, is_base64_encoded: bool
    ) -> Dict[str, Any]:
        logger.info(
            f"Decompressing payload (isBase64Encoded={is_base64_encoded}, length={len(raw_body)})"
        )

        try:
            binary_data = self._to_binary(raw_body, is_base64_encoded)
            decompressed_stream = self._decompress(binary_data)
        except PayloadDecompressionError:
            raise
        except Exception as exc:
            raise PayloadDecompressionError(
                "Unexpected error while preparing/decompressing payload"
            ) from exc

        if len(decompressed_stream) > self.max_payload_bytes:
            raise ValueError("Decompressed payload exceeds maximum size quota")

        try:
            return json.loads(decompressed_stream.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayloadDecompressionError(
                "Decompressed payload is not valid UTF-8 JSON"
            ) from exc


def _build_local_test_event() -> Dict[str, Any]:
    """Builds a simulated API Gateway proxy event for local/manual testing
    only. Production traffic should always come through the real `event`
    parameter passed to lambda_handler by the Lambda runtime."""
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

    # Use the real incoming event when it carries a body (production API
    # Gateway traffic). Only fall back to the simulated payload when no
    # body is present, e.g. local/manual invocation for testing purposes.
    if event and event.get("body") is not None:
        working_event = event
    else:
        working_event = _build_local_test_event()

    decoder = CompressedPayloadDecoder()

    try:
        parsed_json = decoder.decompress_and_decode(
            raw_body=working_event["body"],
            is_base64_encoded=working_event.get("isBase64Encoded", False),
        )
    except PayloadDecompressionError as exc:
        logger.error(f"error: {exc}")
        return {
            "statusCode": 400,
            "body": json.dumps(
                {
                    "error_code": "PAYLOAD_DECOMPRESSION_ERROR",
                    "message": str(exc),
                }
            ),
        }
    except ValueError as exc:
        logger.error(f"error: {exc}")
        return {
            "statusCode": 400,
            "body": json.dumps(
                {
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "message": str(exc),
                }
            ),
        }

    logger.info(f"Successfully unpacked payload: {parsed_json}")
    return {"statusCode": 200, "body": json.dumps({"data": parsed_json})}


if __name__ == "__main__":
    lambda_handler({}, None)
