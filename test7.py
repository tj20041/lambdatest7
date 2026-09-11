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


class CompressedPayloadDecoder:

    def __init__(self, max_payload_bytes: int = 10 * 1024 * 1024):
        self.max_payload_bytes = max_payload_bytes

    def decompress_and_decode(
        self, raw_body: str, is_base64_encoded: bool
    ) -> Dict[str, Any]:
        logger.info(
            f"Decompressing payload (isBase64Encoded={is_base64_encoded}, length={len(raw_body)})"
        )

        # FIXED: API Gateway proxy integrations deliver binary/gzip payloads as base64-encoded
        # ASCII text when isBase64Encoded=True. We must base64-decode the string into raw binary
        # bytes BEFORE handing it to zlib.decompress(); otherwise zlib receives the UTF-8 bytes of
        # the base64 text itself (which has no valid zlib/gzip header), producing:
        # zlib.error: Error -3 while decompressing data: incorrect header check
        try:
            if is_base64_encoded:
                binary_data = base64.b64decode(raw_body)
            else:
                binary_data = raw_body.encode("utf-8")
        except binascii.Error as b64_err:
            logger.error(f"Failed to base64-decode payload: {b64_err}")
            raise ValueError(f"Invalid base64-encoded payload: {b64_err}") from b64_err

        try:
            decompressed_stream = zlib.decompress(binary_data)
        except zlib.error as zlib_err:
            logger.error(f"Failed to zlib-decompress payload: {zlib_err}")
            raise ValueError(f"Invalid compressed payload: {zlib_err}") from zlib_err

        if len(decompressed_stream) > self.max_payload_bytes:
            raise ValueError("Decompressed payload exceeds maximum size quota")

        return json.loads(decompressed_stream.decode("utf-8"))


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Received compressed request payload...")

    # Real payload: compressed JSON with zlib, then base64-encoded
    payload_data = json.dumps(
        {"transaction_id": "TXN_774921", "batch_items": [101, 102, 103]}
    )
    compressed_binary = zlib.compress(payload_data.encode("utf-8"))
    b64_encoded_body = base64.b64encode(compressed_binary).decode("ascii")

    # API Gateway proxy event representation
    simulated_event = {
        "headers": {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        "isBase64Encoded": True,
        "body": b64_encoded_body,
    }

    decoder = CompressedPayloadDecoder()

    # NOTE: simulated_event['body'] is a base64-encoded string of zlib-compressed bytes,
    # matching isBase64Encoded=True. decompress_and_decode now honors this flag by
    # base64-decoding before attempting zlib decompression, so this call's expectation
    # matches the corrected decoder behavior.
    try:
        parsed_json = decoder.decompress_and_decode(
            raw_body=simulated_event["body"],
            is_base64_encoded=simulated_event.get("isBase64Encoded", False),
        )
    except ValueError as decode_err:
        logger.error(f"Payload decode/decompress failure: {decode_err}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": str(decode_err)}),
        }
    except Exception as unexpected_err:
        logger.error(f"Unexpected error while processing payload: {unexpected_err}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal error processing payload"}),
        }

    logger.info(f"Successfully unpacked payload: {parsed_json}")
    return {"statusCode": 200, "data": parsed_json}


def _run_local_smoke_tests() -> None:
    """Exercises both is_base64_encoded branches locally (not invoked by Lambda runtime)."""
    decoder = CompressedPayloadDecoder()

    # Branch 1: base64-encoded compressed payload (standard API Gateway proxy case)
    payload_data = json.dumps({"transaction_id": "TXN_LOCAL_1", "batch_items": [1, 2, 3]})
    compressed_binary = zlib.compress(payload_data.encode("utf-8"))
    b64_body = base64.b64encode(compressed_binary).decode("ascii")
    result_b64 = decoder.decompress_and_decode(raw_body=b64_body, is_base64_encoded=True)
    logger.info(f"[smoke-test] is_base64_encoded=True result: {result_b64}")

    # Branch 2: raw compressed bytes represented as a latin-1 decoded string (non-base64 case)
    payload_data_2 = json.dumps({"transaction_id": "TXN_LOCAL_2", "batch_items": [4, 5, 6]})
    compressed_binary_2 = zlib.compress(payload_data_2.encode("utf-8"))
    raw_body_str = compressed_binary_2.decode("latin-1")
    result_raw = decoder.decompress_and_decode(raw_body=raw_body_str, is_base64_encoded=False)
    logger.info(f"[smoke-test] is_base64_encoded=False result: {result_raw}")


if __name__ == "__main__":
    lambda_handler({}, None)
    _run_local_smoke_tests()
