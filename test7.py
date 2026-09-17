import base64
import binascii
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


class CompressedPayloadDecodeError(Exception):
    """Raised when a compressed API Gateway payload cannot be base64-decoded,
    zlib-decompressed, or JSON-parsed."""


class CompressedPayloadDecoder:

    def __init__(self, max_payload_bytes: int = 10 * 1024 * 1024):
        self.max_payload_bytes = max_payload_bytes

    def decompress_and_decode(
        self, raw_body: str, is_base64_encoded: bool
    ) -> Dict[str, Any]:
        logger.info(
            f"Decompressing payload (isBase64Encoded={is_base64_encoded}, length={len(raw_body)})"
        )

        if not raw_body:
            raise CompressedPayloadDecodeError("Empty payload body received")

        # API Gateway proxy integrations base64-encode the body whenever the
        # underlying content (e.g. gzip/zlib-compressed binary data) is not
        # valid UTF-8 text. We must strip that base64 layer BEFORE handing
        # the bytes to zlib, otherwise zlib.decompress() receives base64
        # ASCII text instead of raw compressed bytes and fails immediately
        # with "Error -3 while decompressing data: incorrect header check".
        if is_base64_encoded:
            try:
                raw_bytes = base64.b64decode(raw_body, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CompressedPayloadDecodeError(
                    f"Failed to base64-decode payload (isBase64Encoded={is_base64_encoded}, "
                    f"length={len(raw_body)}): {exc}"
                ) from exc
        else:
            raw_bytes = raw_body.encode("utf-8")

        try:
            decompressed_stream = zlib.decompress(raw_bytes)
        except zlib.error as exc:
            raise CompressedPayloadDecodeError(
                f"zlib decompression failed (isBase64Encoded={is_base64_encoded}, "
                f"length={len(raw_body)}): {exc}"
            ) from exc

        if len(decompressed_stream) > self.max_payload_bytes:
            raise ValueError("Decompressed payload exceeds maximum size quota")

        try:
            return json.loads(decompressed_stream.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CompressedPayloadDecodeError(
                f"Failed to parse decompressed payload as JSON: {exc}"
            ) from exc


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Received compressed request payload...")

    if event:
        # Real API Gateway proxy integration event: pull body and the
        # isBase64Encoded flag directly from the event, matching the
        # AWS Lambda proxy integration contract.
        raw_body = event.get("body", "")
        is_base64_encoded = event.get("isBase64Encoded", False)
    else:
        # Local/manual invocation fallback: simulate a compressed JSON body,
        # then base64-encode it exactly as API Gateway would before
        # delivering it to this handler.
        payload_data = json.dumps(
            {"transaction_id": "TXN_774921", "batch_items": [101, 102, 103]}
        )
        compressed_binary = zlib.compress(payload_data.encode("utf-8"))
        b64_encoded_body = base64.b64encode(compressed_binary).decode("ascii")

        simulated_event = {
            "headers": {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            "isBase64Encoded": True,
            "body": b64_encoded_body,
        }

        raw_body = simulated_event["body"]
        is_base64_encoded = simulated_event.get("isBase64Encoded", False)

    decoder = CompressedPayloadDecoder()

    try:
        parsed_json = decoder.decompress_and_decode(
            raw_body=raw_body,
            is_base64_encoded=is_base64_encoded,
        )
    except CompressedPayloadDecodeError as exc:
        logger.error(f"Compressed payload decode error: {exc}")
        return {
            "statusCode": 400,
            "error_code": "COMPRESSED_PAYLOAD_DECODE_ERROR",
            "message": str(exc),
        }
    except ValueError as exc:
        logger.error(f"Payload validation error: {exc}")
        return {
            "statusCode": 413,
            "error_code": "PAYLOAD_TOO_LARGE",
            "message": str(exc),
        }

    logger.info(f"Successfully unpacked payload: {parsed_json}")
    return {"statusCode": 200, "data": parsed_json}


if __name__ == "__main__":
    lambda_handler({}, None)
