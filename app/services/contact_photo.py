"""
Contact photo processing service.
Handles face detection and cropping using OpenCV.
"""
import base64
import io
import logging
from typing import Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Haar cascade for face detection (ships with OpenCV)
_face_cascade = None


def _get_face_cascade():
    """Lazy-load the face detection cascade."""
    global _face_cascade
    if _face_cascade is None:
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        _face_cascade = cv2.CascadeClassifier(cascade_path)
    return _face_cascade


def process_contact_photo(full_b64: str) -> Tuple[str, str]:
    """Process a pasted photo: detect face, crop thumbnail.

    Args:
        full_b64: Base64-encoded full image (PNG or JPEG).

    Returns:
        Tuple of (cropped_b64, full_b64) where cropped_b64 is a 120x120
        face-centered crop (or center crop if no face found).
    """
    # Decode image
    img_bytes = base64.b64decode(full_b64)
    img_array = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

    if img is None:
        raise ValueError("Could not decode image")

    # Scale down full image if huge (keep reasonable size for storage)
    max_full = 600
    h, w = img.shape[:2]
    if max(h, w) > max_full:
        scale = max_full / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    # Re-encode the (possibly scaled) full image
    _, full_buf = cv2.imencode('.png', img)
    scaled_full_b64 = base64.b64encode(full_buf).decode('ascii')

    # Detect faces
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = _get_face_cascade()
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(20, 20),
    )

    # Determine crop region
    crop_size = 120
    if len(faces) > 0:
        # Use the largest face
        face = max(faces, key=lambda f: f[2] * f[3])
        fx, fy, fw, fh = face
        # Center on face with 1.5x padding
        cx = fx + fw // 2
        cy = fy + fh // 2
        side = int(max(fw, fh) * 1.5)
        logger.info(f"Face detected at ({fx},{fy},{fw},{fh}), crop side={side}")
    else:
        # Center crop
        cx = w // 2
        cy = h // 2
        side = min(w, h)
        logger.info("No face detected, using center crop")

    # Clamp to image bounds
    side = min(side, min(w, h))
    x1 = max(0, min(cx - side // 2, w - side))
    y1 = max(0, min(cy - side // 2, h - side))

    # Crop and resize
    cropped = img[y1:y1 + side, x1:x1 + side]
    cropped = cv2.resize(cropped, (crop_size, crop_size), interpolation=cv2.INTER_AREA)

    # Encode
    _, crop_buf = cv2.imencode('.png', cropped)
    cropped_b64 = base64.b64encode(crop_buf).decode('ascii')

    return cropped_b64, scaled_full_b64
