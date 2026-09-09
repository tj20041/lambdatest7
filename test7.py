import base64
import json
import logging
import sys
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("multipart_parser_lambda")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.handlers = [stream_handler]

class MultipartStreamDecoder:
    def __init__(self, boundary: str):
        self.boundary = boundary.encode("utf-8")

    def parse_payload(self, raw_bytes: bytes) -> List[Dict[str, Any]]:
        logger.info(f"Deconstructing multipart stream using boundary token: {self.boundary.decode('utf-8')}")
        parts = raw_bytes.split(b"--" + self.boundary)
        extracted_sections = []

        for idx, part in enumerate(parts):
            # Clean outer delimiters
            cleaned = part.strip()
            if not cleaned or cleaned == b"--":
                continue

            logger.info(f"Parsing multipart fragment #{idx} (length: {len(cleaned)} bytes)")

            # FAILS HERE: The closing boundary delimiter '--' or malformed end markers 
            # do not have '\r\n\r\n' separating headers from body.
            # split() returns a list with only 1 element. Index [1] raises IndexError: list index out of range
            header_section, body_section = cleaned.split(b"\r\n\r\n", 1)
            
            headers_str = header_section.decode("utf-8")
            extracted_sections.append({
                "headers": headers_str,
                "data_size": len(body_section)
            })

        return extracted_sections

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Received binary multipart submission...")

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    
    # Multipart raw body with trailing boundary indicator
    raw_multipart_payload = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"upload_file\"; filename=\"audit.csv\"\r\n"
        f"Content-Type: text/csv\r\n\r\n"
        f"user_id,action\r\n101,login\r\n102,logout\r\n"
        f"--{boundary}--\r\n"  # Final boundary tag
    ).encode("utf-8")

    decoder = MultipartStreamDecoder(boundary=boundary)
    results = decoder.parse_payload(raw_multipart_payload)

    logger.info(f"Extracted {len(results)} form sections")
    return {"statusCode": 200, "sections_parsed": len(results)}

if __name__ == "__main__":
    lambda_handler({}, None)
