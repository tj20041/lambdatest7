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

        # FAILS HERE: The payload was compressed with zlib/gzip and base64-encoded by API Gateway.
        # Instead of decoding base64 to binary bytes first, the handler passes the raw ASCII string/bytes
        # directly into zlib.decompress().
        # Raises: zlib.error: Error -3 while decompressing data: incorrect header check
        decompressed_stream = zlib.decompress(raw_body.encode("utf-8"))

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
    parsed_json = decoder.decompress_and_decode(
        raw_body=simulated_event["body"],
        is_base64_encoded=simulated_event.get("isBase64Encoded", False),
    )

    logger.info(f"Successfully unpacked payload: {parsed_json}")
    return {"statusCode": 200, "data": parsed_json}


if __name__ == "__main__":
    lambda_handler({}, None)
