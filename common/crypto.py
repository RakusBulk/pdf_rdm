"""AES-256-GCM envelope for .cpdf (encrypted PDF) files.

File layout:
    MAGIC (5 bytes)  b"CPDF1"
    doc_id (36 bytes, ascii UUID string)
    nonce (12 bytes)
    ciphertext (variable, includes the 16-byte GCM tag)
"""
from __future__ import annotations

import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"CPDF1"
DOC_ID_LEN = 36
NONCE_LEN = 12


def generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt_pdf(pdf_bytes: bytes, key: bytes, doc_id: str | None = None) -> tuple[str, bytes]:
    doc_id = doc_id or str(uuid.uuid4())
    if len(doc_id) != DOC_ID_LEN:
        raise ValueError("doc_id must be a standard 36-char UUID string")

    nonce = os.urandom(NONCE_LEN)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, pdf_bytes, doc_id.encode("ascii"))

    blob = MAGIC + doc_id.encode("ascii") + nonce + ciphertext
    return doc_id, blob


def read_doc_id(blob: bytes) -> str:
    if blob[:5] != MAGIC:
        raise ValueError("Not a valid .cpdf file (bad magic)")
    return blob[5:5 + DOC_ID_LEN].decode("ascii")


def decrypt_pdf(blob: bytes, key: bytes) -> bytes:
    if blob[:5] != MAGIC:
        raise ValueError("Not a valid .cpdf file (bad magic)")
    doc_id = blob[5:5 + DOC_ID_LEN].decode("ascii")
    offset = 5 + DOC_ID_LEN
    nonce = blob[offset:offset + NONCE_LEN]
    ciphertext = blob[offset + NONCE_LEN:]

    aesgcm = AESGCM(key)
    pdf_bytes = aesgcm.decrypt(nonce, ciphertext, doc_id.encode("ascii"))
    return pdf_bytes
