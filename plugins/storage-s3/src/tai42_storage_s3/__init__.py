"""S3 ``Storage`` backend for the TAI ecosystem.

Importing this package registers ``S3Storage`` as the active storage provider
via an import side-effect.
"""

from tai42_storage_s3.storage import S3Storage

__all__ = ["S3Storage"]
