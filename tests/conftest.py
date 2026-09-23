"""Loaded by pytest before any test module in this directory.

Some test modules (test_admin_iap_auth.py) install bare stand-in stubs for
googleapiclient.http/errors via sys.modules.setdefault(...) at *import* time,
so they can import app.py without the real google API client libraries.
setdefault(...) is a no-op if the real module is already cached, so importing
the genuine modules here -- before pytest imports any test file -- guarantees
drive_io.py and tests/test_drive_io.py always bind the real HttpError /
MediaFileUpload / MediaIoBaseDownload, regardless of which order pytest
collects test files in.
"""

import googleapiclient.errors  # noqa: F401
import googleapiclient.http  # noqa: F401

import drive_io  # noqa: F401
