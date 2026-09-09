import base64
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


class CompressedPayloadDecoder:

    def __init__(self, max_payload_bytes: int = 10 * 1024 * 1024):
        self.max_payload_bytes = max_payload_bytes

    def decompress_and_decode(
        self, raw_body: str, is_base64_encoded: bool
    ) -> Dict[str, Any]:
        logger.info(
            f"Decompressing payload (isBase64Encoded={is_base64_encoded}, length={len(raw_body)})"
        )

        # API Gateway proxy integrations deliver binary (zlib/gzip compressed) bodies
        # as a base64-encoded ASCII string when isBase64Encoded=True. We must first
        # recover the original compressed binary buffer via base64 decoding before
        # handing the bytes to zlib.decompress(). Passing the base64 text straight
        # into zlib (encoded as utf-8) produces an invalid zlib header and raises
        # zlib.error: Error -3 while decompressing data: incorrect header check.
        try:
            if is_base64_encoded:
                raw_bytes = base64.b64decode(raw_body)
            else:
                raw_bytes = raw_body.encode("utf-8")
        except (ValueError, TypeError) as decode_err:
            logger.error(f"Failed to base64-decode request body: {decode_err}")
            raise ValueError("Invalid base64-encoded request body") from decode_err

        try:
            decompressed_stream = zlib.decompress(raw_bytes)
        except zlib.error as zlib_err:
            logger.error(f"zlib decompression failed: {zlib_err}")
            raise ValueError(
                "Unable to decompress request body: incorrect zlib/gzip header"
            ) from zlib_err

        if len(decompressed_stream) > self.max_payload_bytes:
            raise ValueError("Decompressed payload exceeds maximum size quota")

        try:
            return json.loads(decompressed_stream.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as parse_err:
            logger.error(f"Failed to parse decompressed payload as JSON: {parse_err}")
            raise ValueError("Decompressed payload is not valid UTF-8 JSON") from parse_err


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Received compressed request payload...")

    if not event:
        # Real payload: compressed JSON with zlib, then base64-encoded.
        # Used for local/manual invocation testing when no event is supplied.
        payload_data = json.dumps(
            {"transaction_id": "TXN_774921", "batch_items": [101, 102, 103]}
        )
        compressed_binary = zlib.compress(payload_data.encode("utf-8"))
        b64_encoded_body = base64.b64encode(compressed_binary).decode("ascii")

        # API Gateway proxy event representation
        event = {
            "headers": {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            "isBase64Encoded": True,
            "body": b64_encoded_body,
        }

    raw_body = event.get("body")
    if not raw_body:
        logger.error("Request event is missing a 'body' payload")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Missing request body"}),
        }

    decoder = CompressedPayloadDecoder()

    try:
        parsed_json = decoder.decompress_and_decode(
            raw_body=raw_body,
            is_base64_encoded=event.get("isBase64Encoded", False),
        )
    except ValueError as validation_err:
        logger.error(f"Failed to decode/decompress request payload: {validation_err}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": str(validation_err)}),
        }
    except Exception as unexpected_err:
        logger.error(f"Unexpected error while processing payload: {unexpected_err}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }

    logger.info(f"Successfully unpacked payload: {parsed_json}")
    return {"statusCode": 200, "data": parsed_json}


if __name__ == "__main__":
    lambda_handler({}, None)
